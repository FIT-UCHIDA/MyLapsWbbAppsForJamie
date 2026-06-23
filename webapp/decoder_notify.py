#!/usr/bin/env python3
"""
decoder_notify.py — デコーダ異常メール通知スクリプト

cron から定期実行し、デコーダの欠落を検知した際にメールを送信する。
状態変化（ok/idle → error, error → ok）のタイミングだけ送信し、スパムを防ぐ。

使い方:
    python3 decoder_notify.py

設定:
  settings.json に以下を追記（smtp_pass は書かない）:
    "notify": {
        "enabled": true,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "your-account@gmail.com",
        "from_addr": "your-account@gmail.com",
        "to_addrs":  ["user1@example.com"],
        "check_window_minutes": 30,
        "ssm_pass_param": "/mylaps/notify/smtp_pass"
    }

  SMTP パスワードは AWS SSM Parameter Store に SecureString で登録する:
    aws ssm put-parameter \\
      --name /mylaps/notify/smtp_pass \\
      --value "xxxx xxxx xxxx xxxx" \\
      --type SecureString \\
      --region ap-northeast-1

  EC2 に SSM 読み取り権限の IAM ロールが必要（下記手順を参照）。
"""

import json
import os
import sys
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

# utils.py と同じディレクトリにあるため、パスを追加
_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

from utils import get_decoder_status, _load_json, SETTINGS_PATH

# ---------------------------------------------------------------------------
# SSM からシークレットを取得
# ---------------------------------------------------------------------------

def _get_smtp_pass_from_ssm(param_name: str, region: str = "ap-northeast-1") -> str:
    """AWS SSM Parameter Store から SMTP パスワードを取得する。"""
    import boto3
    client = boto3.client("ssm", region_name=region)
    resp = client.get_parameter(Name=param_name, WithDecryption=True)
    return resp["Parameter"]["Value"]

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(_DIR, "decoder_notify_state.json")
LOG_FILE   = os.path.join(_DIR, "logs", "decoder_notify.log")
JST = ZoneInfo("Asia/Tokyo")

# ---------------------------------------------------------------------------
# ロギング
# ---------------------------------------------------------------------------

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状態ファイル
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"prev_state": "unknown", "last_alert_sent": None}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# メール送信
# ---------------------------------------------------------------------------

def send_email(cfg: dict, smtp_pass: str, subject: str, body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["from_addr"]
    msg["To"]      = ", ".join(cfg["to_addrs"])
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["smtp_user"], smtp_pass)
        smtp.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())

    log.info("Email sent: %s → %s", subject, cfg["to_addrs"])

# ---------------------------------------------------------------------------
# メール本文生成
# ---------------------------------------------------------------------------

def make_alert_body(result: dict) -> tuple[str, str]:
    """(subject, body) を返す"""
    missing = result["missing"]
    from_jst = result["from_jst"]
    to_jst   = result["to_jst"]
    total    = result["total_passings"]

    subject = f"[ALERT] Decoder Missing: {', '.join(missing)}"
    body = (
        f"Decoder anomaly detected.\n"
        f"\n"
        f"Missing decoders : {', '.join(missing)}\n"
        f"Present decoders : {', '.join(result['present'])}\n"
        f"\n"
        f"Check window : {from_jst}  →  {to_jst}\n"
        f"Total passings in window : {total}\n"
        f"\n"
        f"Check the decoder status page:\n"
        f"  https://ec2-43-206-155-252.ap-northeast-1.compute.amazonaws.com/check_decoder\n"
        f"\n"
        f"-- Lap Time Analyzer (automated alert)"
    )
    return subject, body


def make_clear_body(result: dict) -> tuple[str, str]:
    subject = "[OK] All Decoders Restored"
    body = (
        f"All decoders are now reporting normally.\n"
        f"\n"
        f"Present decoders : {', '.join(result['present'])}\n"
        f"\n"
        f"Check window : {result['from_jst']}  →  {result['to_jst']}\n"
        f"Total passings in window : {result['total_passings']}\n"
        f"\n"
        f"-- Lap Time Analyzer (automated alert)"
    )
    return subject, body

# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    settings = _load_json(SETTINGS_PATH)
    cfg = settings.get("notify", {})

    if not cfg.get("enabled", False):
        log.info("Notifications disabled (notify.enabled=false in settings.json). Exiting.")
        return

    # 必須設定チェック
    for key in ("smtp_host", "smtp_port", "smtp_user", "from_addr", "to_addrs"):
        if not cfg.get(key):
            log.error("settings.json の notify.%s が未設定です。終了します。", key)
            return

    # SSM から SMTP パスワードを取得
    ssm_param = cfg.get("ssm_pass_param", "/mylaps/notify/smtp_pass")
    try:
        smtp_pass = _get_smtp_pass_from_ssm(ssm_param)
    except Exception as e:
        log.error("SSM からパスワードを取得できませんでした (%s): %s", ssm_param, e)
        log.error("EC2 に IAM ロール（SSM 読み取り権限）が付与されているか確認してください。")
        return

    check_window = int(cfg.get("check_window_minutes", 30))

    # デコーダチェック
    now = datetime.now(JST)
    from_dt = now - timedelta(minutes=check_window)
    result = get_decoder_status(from_dt, now)

    # 現在の状態を分類
    if result["status"] == "db_error":
        current_state = "db_error"
        log.warning("DB error: %s", result.get("error"))
    elif result["total_passings"] == 0:
        current_state = "idle"
        log.info("Idle (no passings in last %d min). No alert.", check_window)
    elif result["status"] == "error":
        current_state = "error"
        log.warning("Decoder error: missing=%s", result["missing"])
    else:
        current_state = "ok"
        log.info("All decoders OK (passings=%d).", result["total_passings"])

    # 状態ファイルを読み込む
    state = load_state()
    prev_state = state.get("prev_state", "unknown")

    # 状態変化に応じてメール送信
    try:
        if current_state == "error" and prev_state != "error":
            # 正常/不明/idle → 異常: アラート送信
            subject, body = make_alert_body(result)
            send_email(cfg, smtp_pass, subject, body)
            state["last_alert_sent"] = now.isoformat()

        elif current_state != "error" and prev_state == "error":
            # 異常 → 正常復帰: 復旧メール送信
            # idle や db_error への遷移でも復旧メールを送る
            subject, body = make_clear_body(result)
            send_email(cfg, smtp_pass, subject, body)

        # db_error は初回のみログ（メールは送らない — 頻発防止）
        elif current_state == "db_error" and prev_state != "db_error":
            log.error("DB connection lost. No email sent (check RDS/network).")

    except Exception as e:
        log.error("Failed to send email: %s", e)

    # 状態を保存
    state["prev_state"] = current_state
    state["last_checked"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
