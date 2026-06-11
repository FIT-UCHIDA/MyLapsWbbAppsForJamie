"""
app.py — Flask Webアプリ本体
競輪ラップタイム分析 KPI表示システム
"""

import os
import io
import csv
import math
import json
import copy
import glob
import shutil
import subprocess
from datetime import datetime
from functools import wraps

import pandas as pd
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, Response, flash, send_from_directory,
)

from utils import (
    jst_str_to_utc_sql, get_df_from_db, fetch_df_from_db,
    _load_json, _save_json, _load_kpi_intervals,
    ensure_interval_columns, display_kpi_columns, filter_by_main_kpi,
    _propagate_imputed_flags_to_kpi,
    legacy_v1_pipeline, compare_pipelines,
    SETTINGS_PATH, KPI_INTERVALS_PATH, TRACK_ORDER, NAME_COLUMNS,
)

# ---------------------------------------------------------------------------
# Flask アプリ初期化
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

# アプリルート
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(APP_DIR)

# 最大バックアップ保持数
MAX_BACKUPS = 50


# ---------------------------------------------------------------------------
# バックアップ関数
# ---------------------------------------------------------------------------

def _create_backup(file_path):
    """ファイル保存前にタイムスタンプ付きバックアップを作成する。
    保存先: <同じディレクトリ>/backups/<basename>_YYYYMMDD_HHMMSS.<ext>
    最大 MAX_BACKUPS 件保持し、古いものから自動削除。
    """
    try:
        if not os.path.exists(file_path):
            return
        parent = os.path.dirname(file_path)
        backup_dir = os.path.join(parent, "backups")
        os.makedirs(backup_dir, exist_ok=True)

        basename = os.path.basename(file_path)
        name, ext = os.path.splitext(basename)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{name}_{ts}{ext}"
        backup_path = os.path.join(backup_dir, backup_name)

        shutil.copy2(file_path, backup_path)

        # 古いバックアップの整理
        pattern = os.path.join(backup_dir, f"{name}_*{ext}")
        existing = sorted(glob.glob(pattern))
        if len(existing) > MAX_BACKUPS:
            for old in existing[:-MAX_BACKUPS]:
                os.remove(old)
    except Exception:
        pass  # バックアップ失敗は保存を妨げない


# ---------------------------------------------------------------------------
# バージョン管理
# ---------------------------------------------------------------------------

def _get_git_version():
    """gitからバージョン情報を取得。gitが無い場合はversion.jsonから読む。"""
    try:
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %ci"],
            cwd=PROJECT_DIR, stderr=subprocess.DEVNULL
        ).decode().strip()
        if commit:
            parts = commit.split(" ", 1)
            return {"commit": parts[0], "date": parts[1] if len(parts) > 1 else ""}
    except Exception:
        pass

    # フォールバック: version.json
    version_path = os.path.join(APP_DIR, "version.json")
    if os.path.exists(version_path):
        try:
            with open(version_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return {"commit": "unknown", "date": ""}


@app.context_processor
def inject_version():
    """全テンプレートに app_version を注入"""
    return {"app_version": _get_git_version()}


# ---------------------------------------------------------------------------
# パスワード認証
# ---------------------------------------------------------------------------

APP_PASSWORD = os.environ.get("APP_PASSWORD", "HPCJC")


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/login", methods=["GET", "POST"])
def login_page():
    users = _list_enabled_users()
    if request.method == "POST":
        password = request.form.get("password", "").strip()
        username = request.form.get("username", "").strip()

        if password == APP_PASSWORD:
            session["authenticated"] = True
            if username and username in users:
                session["current_user"] = username
            else:
                session.pop("current_user", None)
            return redirect(url_for("main_page"))
        else:
            flash("Incorrect password.", "error")
            return render_template("login.html", users=users)

    return render_template("login.html", users=users)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# ユーザープロファイル管理
# ---------------------------------------------------------------------------

USERS_DIR = os.path.join(APP_DIR, "users")
USERS_CONFIG_PATH = os.path.join(APP_DIR, "users_config.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")


def _ensure_users_dir():
    os.makedirs(USERS_DIR, exist_ok=True)


def _load_users_config():
    """ユーザー設定ファイルを読み込む（enable/disable等）"""
    if os.path.exists(USERS_CONFIG_PATH):
        try:
            with open(USERS_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_users_config(config):
    with open(USERS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _list_users():
    """全ユーザー名一覧を返す（users/ 配下のディレクトリ名）"""
    _ensure_users_dir()
    return sorted([
        d for d in os.listdir(USERS_DIR)
        if os.path.isdir(os.path.join(USERS_DIR, d))
    ])


def _list_enabled_users():
    """有効なユーザー名のみ返す"""
    config = _load_users_config()
    return [u for u in _list_users() if config.get(u, {}).get("enabled", True)]


def _user_settings_path(username):
    return os.path.join(USERS_DIR, username, "settings.json")


def _user_kpi_path(username):
    return os.path.join(USERS_DIR, username, "kpi.json")


def _get_user_settings_path():
    """セッション中のユーザーに対応する settings.json パスを返す。未選択ならグローバル。"""
    username = session.get("current_user")
    if username:
        path = _user_settings_path(username)
        if os.path.exists(path):
            return path
    return SETTINGS_PATH


def _get_user_kpi_path():
    """セッション中のユーザーに対応する kpi.json パスを返す。未選択ならグローバル。"""
    username = session.get("current_user")
    if username:
        path = _user_kpi_path(username)
        if os.path.exists(path):
            return path
    return KPI_INTERVALS_PATH


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function


# --- 管理画面ログイン ---
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "").strip()
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_page"))
        else:
            flash("Incorrect admin password.", "error")
    return render_template("admin_login.html")


# --- 管理画面トップ ---
@app.route("/admin")
@admin_required
def admin_page():
    users = _list_users()
    config = _load_users_config()
    # 各ユーザーの有効/無効状態
    users_status = {u: config.get(u, {}).get("enabled", True) for u in users}
    return render_template("admin.html", users=users, users_status=users_status)


# --- ユーザー追加 ---
@app.route("/admin/add_user", methods=["POST"])
@admin_required
def admin_add_user():
    username = request.form.get("username", "").strip()
    if not username:
        flash("ユーザー名を入力してください。", "error")
        return redirect(url_for("admin_page"))

    # 安全なディレクトリ名チェック
    if "/" in username or "\\" in username or ".." in username:
        flash("無効なユーザー名です。", "error")
        return redirect(url_for("admin_page"))

    user_dir = os.path.join(USERS_DIR, username)
    if os.path.exists(user_dir):
        flash(f"ユーザー '{username}' は既に存在します。", "error")
        return redirect(url_for("admin_page"))

    os.makedirs(user_dir, exist_ok=True)

    # グローバルの settings.json / kpi.json をコピーして初期化
    if os.path.exists(SETTINGS_PATH):
        shutil.copy2(SETTINGS_PATH, _user_settings_path(username))
    if os.path.exists(KPI_INTERVALS_PATH):
        shutil.copy2(KPI_INTERVALS_PATH, _user_kpi_path(username))

    # users_config に追加（デフォルト有効）
    config = _load_users_config()
    config[username] = {"enabled": True}
    _save_users_config(config)

    flash(f"ユーザー '{username}' を作成しました。", "info")
    return redirect(url_for("admin_page"))


# --- ユーザー削除 ---
@app.route("/admin/delete_user/<username>", methods=["POST"])
@admin_required
def admin_delete_user(username):
    user_dir = os.path.join(USERS_DIR, username)
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir)
        # configからも削除
        config = _load_users_config()
        config.pop(username, None)
        _save_users_config(config)
        flash(f"ユーザー '{username}' を削除しました。", "info")
    else:
        flash(f"ユーザー '{username}' が見つかりません。", "error")
    return redirect(url_for("admin_page"))


# --- ユーザー有効/無効切替 ---
@app.route("/admin/toggle_user/<username>", methods=["POST"])
@admin_required
def admin_toggle_user(username):
    config = _load_users_config()
    current = config.get(username, {}).get("enabled", True)
    config.setdefault(username, {})["enabled"] = not current
    _save_users_config(config)
    status = "有効" if not current else "無効"
    flash(f"ユーザー '{username}' を{status}にしました。", "info")
    return redirect(url_for("admin_page"))


# --- ユーザー編集画面（settings.json / kpi.json のテキスト編集） ---
@app.route("/admin/user/<username>", methods=["GET"])
@admin_required
def admin_user_edit(username):
    user_dir = os.path.join(USERS_DIR, username)
    if not os.path.exists(user_dir):
        flash(f"ユーザー '{username}' が見つかりません。", "error")
        return redirect(url_for("admin_page"))

    # settings.json 読み込み
    settings_path = _user_settings_path(username)
    settings_content = ""
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            settings_content = f.read()

    # kpi.json 読み込み
    kpi_path = _user_kpi_path(username)
    kpi_content = ""
    if os.path.exists(kpi_path):
        with open(kpi_path, "r", encoding="utf-8") as f:
            kpi_content = f.read()

    return render_template("admin_user_edit.html",
        username=username,
        settings_content=settings_content,
        kpi_content=kpi_content)


# --- JSON保存API（バリデーション付き） ---
@app.route("/admin/user/<username>/save", methods=["POST"])
@admin_required
def admin_user_save(username):
    user_dir = os.path.join(USERS_DIR, username)
    if not os.path.exists(user_dir):
        return jsonify({"error": "ユーザーが見つかりません"}), 404

    data = request.get_json(silent=True) or {}
    file_type = data.get("type", "")  # "settings" or "kpi"
    content = data.get("content", "")

    # JSONバリデーション
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        return jsonify({
            "error": f"JSON構文エラー: {str(e)}",
            "line": e.lineno,
            "col": e.colno,
            "msg": e.msg,
        }), 400

    if file_type == "settings":
        path = _user_settings_path(username)
    elif file_type == "kpi":
        path = _user_kpi_path(username)
    else:
        return jsonify({"error": "不正なfile type"}), 400

    # バックアップ作成後に保存
    _create_backup(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True})


# --- JSONファイルアップロード ---
@app.route("/admin/user/<username>/upload", methods=["POST"])
@admin_required
def admin_user_upload(username):
    user_dir = os.path.join(USERS_DIR, username)
    if not os.path.exists(user_dir):
        flash("ユーザーが見つかりません。", "error")
        return redirect(url_for("admin_page"))

    file_type = request.form.get("type", "")
    uploaded = request.files.get("file")

    if not uploaded or uploaded.filename == "":
        flash("ファイルが選択されていません。", "error")
        return redirect(url_for("admin_user_edit", username=username))

    content = uploaded.read().decode("utf-8")

    # JSONバリデーション
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        flash(f"アップロードファイルのJSON構文エラー: {e}", "error")
        return redirect(url_for("admin_user_edit", username=username))

    if file_type == "settings":
        path = _user_settings_path(username)
    elif file_type == "kpi":
        path = _user_kpi_path(username)
    else:
        flash("不正なfile type", "error")
        return redirect(url_for("admin_user_edit", username=username))

    _create_backup(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    flash(f"{file_type}.json をアップロードしました。", "info")
    return redirect(url_for("admin_user_edit", username=username))


# --- ユーザー切替（メインアプリ用） ---
@app.route("/select_user", methods=["GET", "POST"])
@login_required
def select_user():
    users = _list_enabled_users()
    if request.method == "POST":
        username = request.form.get("username", "")
        if username and username in users:
            session["current_user"] = username
            flash(f"ユーザー '{username}' を選択しました。", "info")
        else:
            session.pop("current_user", None)
            flash("グローバル設定を使用します。", "info")
        return redirect(url_for("main_page"))
    return render_template("select_user.html", users=users,
                           current_user=session.get("current_user"))


# ---------------------------------------------------------------------------
# メインページ（日付選択 + 選手一覧）
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
@login_required
def main_page():
    settings = _load_json(_get_user_settings_path())
    ui = settings.get("ui", {})
    start_dt = ui.get("start_datetime", "2025-01-09 00:00:00")
    end_dt   = ui.get("end_datetime", "2025-01-10 23:59:59")
    users = _list_enabled_users()
    current_user = session.get("current_user")
    return render_template("main.html", start_datetime=start_dt, end_datetime=end_dt,
                           users=users, current_user=current_user)


# ---------------------------------------------------------------------------
# API: 選手一覧
# ---------------------------------------------------------------------------

@app.route("/api/players", methods=["POST"])
@login_required
def api_players():
    data = request.get_json(silent=True) or {}
    start_jst = data.get("start", "")
    end_jst   = data.get("end", "")

    if not start_jst or not end_jst:
        return jsonify({"error": "日付を指定してください"}), 400

    # 日付を settings.json に保存
    user_settings_path = _get_user_settings_path()
    settings = _load_json(user_settings_path)
    settings.setdefault("ui", {})
    settings["ui"]["start_datetime"] = start_jst
    settings["ui"]["end_datetime"] = end_jst
    try:
        _save_json(user_settings_path, settings)
    except Exception:
        pass

    try:
        start_utc = jst_str_to_utc_sql(start_jst)
        end_utc   = jst_str_to_utc_sql(end_jst)
    except Exception as e:
        return jsonify({"error": f"日付フォーマットエラー: {str(e)}"}), 400

    query = f"""
    SELECT DISTINCT u.first_name, u.last_name, u.id
    FROM passing p
    JOIN transponder_user tu ON p.transponder_id = tu.transponder_id
    JOIN `user` u ON tu.user_id = u.id
    WHERE p.timestamp BETWEEN '{start_utc}' AND '{end_utc}'
    AND tu.since <= p.timestamp
    AND (tu.until IS NULL OR tu.until >= p.timestamp)
    ORDER BY u.last_name, u.first_name;
    """

    try:
        df = get_df_from_db(query)
    except Exception as e:
        return jsonify({"error": f"DB接続エラー: {str(e)}"}), 500

    if df.empty:
        return jsonify({"players": []})

    players = []
    for _, row in df.iterrows():
        players.append({
            "id": int(row["id"]),
            "first_name": str(row["first_name"]),
            "last_name": str(row["last_name"]),
        })

    return jsonify({"players": players})


# ---------------------------------------------------------------------------
# KPI ページ
# ---------------------------------------------------------------------------

def _build_kpi_query(start_jst, end_jst, user_ids):
    """KPI表示用のSQLクエリを構築"""
    start_utc = jst_str_to_utc_sql(start_jst)
    end_utc   = jst_str_to_utc_sql(end_jst)
    ids_str = ",".join(map(str, user_ids))

    return f"""
    SELECT
        p.timestamp,
        p.decoder_id,
        u.first_name,
        u.last_name,
        p.transponder_id,
        tu.user_id,
        tu.id AS transponder_user_id
    FROM passing p
    LEFT JOIN (
        SELECT id, transponder_id, user_id, since, until
        FROM transponder_user
        WHERE user_id IN ({ids_str})
    ) tu
    ON tu.transponder_id = p.transponder_id
    AND tu.since <= p.timestamp
    AND (tu.until IS NULL OR tu.until >= p.timestamp)
    LEFT JOIN `user` u
    ON u.id = tu.user_id
    WHERE p.timestamp >= '{start_utc}'
    AND p.timestamp <  '{end_utc}'
    AND (
            tu.user_id IS NOT NULL
            OR p.transponder_id IS NULL
            OR p.transponder_id = ''
        )
    ORDER BY p.timestamp
    LIMIT 50000;
    """


def _sort_key(val):
    """ソート用キーを生成。空白は常に最下部になるよう ZZZ を返す。"""
    if pd.isna(val):
        return "ZZZ"
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(val, float):
        if math.isnan(val):
            return "ZZZ"
        # 数値は0埋め20桁文字列にして文字列ソートでも正しく並ぶようにする
        return f"{val:020.6f}"
    return str(val)


def _format_timestamp(val):
    """Timestamp値を表示用文字列に変換"""
    if pd.isna(val):
        return ""
    if isinstance(val, (pd.Timestamp, datetime)):
        ms = val.microsecond // 1000
        fmt = "%H:%M:%S" if ms == 0 else "%H:%M:%S.%f"
        text = val.strftime(fmt)
        return text if ms == 0 else text[:-3]
    return str(val)


def _format_value(val):
    """任意の値を表示用文字列に変換"""
    if pd.isna(val):
        return ""
    if isinstance(val, (pd.Timestamp, datetime)):
        return _format_timestamp(val)
    if isinstance(val, pd.Timedelta):
        total_ms = int(val / pd.Timedelta(milliseconds=1))
        sign = "-" if total_ms < 0 else ""
        total_ms = abs(total_ms)
        minutes, rem = divmod(total_ms, 60_000)
        seconds, ms = divmod(rem, 1000)
        return f"{sign}{minutes:02d}:{seconds:02d}.{ms:03d}"
    if isinstance(val, float):
        if math.isnan(val):
            return ""
        return str(round(val, 3))
    return str(val)


@app.route("/kpi")
@login_required
def kpi_page():
    settings = _load_json(_get_user_settings_path())
    ui = settings.get("ui", {})

    start_jst = request.args.get("start", ui.get("start_datetime", ""))
    end_jst   = request.args.get("end", ui.get("end_datetime", ""))
    ids_param = request.args.get("ids", "")
    mode      = request.args.get("mode", "")

    if not start_jst or not end_jst or not ids_param:
        flash("パラメータが不足しています。メインページから操作してください。", "error")
        return redirect(url_for("main_page"))

    user_ids = [int(x) for x in ids_param.split(",") if x.strip()]
    if not user_ids:
        flash("選手が選択されていません。", "error")
        return redirect(url_for("main_page"))

    # KPI設定読み込み
    interval_config = _load_kpi_intervals(_get_user_kpi_path())
    available_modes = [k for k in interval_config.keys() if k != "settings"]

    if not mode:
        mode = ui.get("time_mode", "")
    if mode not in available_modes and available_modes:
        mode = available_modes[0]

    # モード保存
    settings.setdefault("ui", {})["time_mode"] = mode
    try:
        _save_json(_get_user_settings_path(), settings)
    except Exception:
        pass

    # 設定値
    kpi_settings = interval_config.get("settings", {})
    show_all_cols = kpi_settings.get("showAllColumns", True)
    show_all_data = kpi_settings.get("showAllData", False)
    max_rows = kpi_settings.get("maxRows", 5)

    # クエリパラメータでオーバーライド
    if "show_all_cols" in request.args:
        show_all_cols = request.args.get("show_all_cols", "true").lower() == "true"
    if "show_all_data" in request.args:
        show_all_data = request.args.get("show_all_data", "false").lower() == "true"
    if "max_rows" in request.args:
        try:
            max_rows = int(request.args.get("max_rows", 5))
        except ValueError:
            max_rows = 5

    # DB問い合わせ + KPI計算
    try:
        query = _build_kpi_query(start_jst, end_jst, user_ids)
        df_all, _users = fetch_df_from_db(query, progress=lambda m: print(f"[KPI] {m}"))
    except Exception as e:
        flash(f"データ取得エラー: {str(e)}", "error")
        return redirect(url_for("main_page"))

    if df_all is None or df_all.empty:
        return render_template("kpi.html",
            columns=[], rows=[], modes=available_modes, current_mode=mode,
            show_all_cols=show_all_cols, show_all_data=show_all_data,
            max_rows=max_rows, start=start_jst, end=end_jst,
            ids=ids_param, empty=True)

    # 行ID付与
    df_all["__row_id"] = range(len(df_all))

    # 区間タイム列を追加
    df_all = ensure_interval_columns(df_all, interval_config)

    # mainKPIフィルタリング
    # maxRowsを一時的にオーバーライド
    config_for_filter = copy.deepcopy(interval_config)
    config_for_filter.setdefault("settings", {})["maxRows"] = max_rows

    if show_all_data:
        # mainKPIソートは無効だが、max_rows > 0 なら選手ごとの行数制限を適用
        if max_rows > 0:
            player_id_cols = []
            if "user_id" in df_all.columns:
                player_id_cols = ["user_id"]
            elif "first_name" in df_all.columns and "last_name" in df_all.columns:
                player_id_cols = ["first_name", "last_name"]

            if player_id_cols:
                parts = []
                for _, group in df_all.groupby(player_id_cols):
                    parts.append(group.head(max_rows))
                df_filtered = pd.concat(parts, ignore_index=True) if parts else df_all.copy()
            else:
                df_filtered = df_all.copy()
        else:
            df_filtered = df_all.copy()
    else:
        df_filtered = filter_by_main_kpi(df_all.copy(), mode, config_for_filter)

    # 表示列の決定
    kpi_cols = display_kpi_columns(mode, interval_config, df_all)

    # Simple表示用の名前列（SB1がある場合はFP_startの代わりにSB1を使用）
    simple_name_cols = list(NAME_COLUMNS)
    if "SB1" in df_filtered.columns and df_filtered["SB1"].dropna().astype(str).str.strip().ne("").any():
        simple_name_cols = [("SB1" if c == "FP_start" else c) for c in simple_name_cols]

    if show_all_cols:
        all_cols = [
            c for c in df_filtered.columns
            if not (c.startswith("imputed__") or c == "__row_id")
        ]
        priority = [c for c in NAME_COLUMNS if c in all_cols]
        priority += kpi_cols
        extras = [c for c in all_cols if c not in priority]
        seen = set()
        columns = []
        for c in priority + extras:
            if c not in seen:
                columns.append(c)
                seen.add(c)
    else:
        columns = simple_name_cols + kpi_cols
        columns = [
            c for c in columns
            if c in df_filtered.columns and not (c.startswith("imputed__") or c == "__row_id")
        ]
        if not columns:
            columns = [
                c for c in df_filtered.columns
                if not (c.startswith("imputed__") or c == "__row_id")
            ]

    # 補完フラグマップ構築
    flag_map = {}
    for c in columns:
        fc = f"imputed__{c}"
        if fc in df_filtered.columns:
            flag_map[c] = fc

    # テーブルデータ構築
    rows = []
    for idx in df_filtered.index:
        row_data = []
        for c in columns:
            val = df_filtered.at[idx, c]
            is_imputed = False
            if c in flag_map:
                try:
                    is_imputed = bool(df_filtered.at[idx, flag_map[c]])
                except Exception:
                    pass
            row_data.append({
                "value": _format_value(val),
                "imputed": is_imputed,
                "raw": val if not pd.isna(val) else None,
                "sort_key": _sort_key(val),
            })
        rows.append(row_data)

    # セッションにKPIデータを保存（CSVエクスポート用）
    session["kpi_columns"] = columns
    session["kpi_start"] = start_jst
    session["kpi_end"] = end_jst
    session["kpi_ids"] = ids_param
    session["kpi_mode"] = mode

    return render_template("kpi.html",
        columns=columns, rows=rows, modes=available_modes, current_mode=mode,
        show_all_cols=show_all_cols, show_all_data=show_all_data,
        max_rows=max_rows, start=start_jst, end=end_jst,
        ids=ids_param, empty=False)


# ---------------------------------------------------------------------------
# API: CSVエクスポート
# ---------------------------------------------------------------------------

@app.route("/api/export_csv")
@login_required
def api_export_csv():
    start_jst = request.args.get("start", "")
    end_jst   = request.args.get("end", "")
    ids_param = request.args.get("ids", "")
    mode      = request.args.get("mode", "")

    if not start_jst or not end_jst or not ids_param:
        return "パラメータ不足", 400

    user_ids = [int(x) for x in ids_param.split(",") if x.strip()]
    interval_config = _load_kpi_intervals(_get_user_kpi_path())

    show_all_cols = request.args.get("show_all_cols", "true").lower() == "true"
    show_all_data = request.args.get("show_all_data", "false").lower() == "true"
    max_rows_str  = request.args.get("max_rows", "5")
    try:
        max_rows = int(max_rows_str)
    except ValueError:
        max_rows = 5

    try:
        query = _build_kpi_query(start_jst, end_jst, user_ids)
        df_all, _ = fetch_df_from_db(query)
    except Exception as e:
        return f"データ取得エラー: {e}", 500

    if df_all is None or df_all.empty:
        return "データなし", 404

    df_all = ensure_interval_columns(df_all, interval_config)

    config_for_filter = copy.deepcopy(interval_config)
    config_for_filter.setdefault("settings", {})["maxRows"] = max_rows

    if show_all_data:
        if max_rows > 0:
            player_id_cols = []
            if "user_id" in df_all.columns:
                player_id_cols = ["user_id"]
            elif "first_name" in df_all.columns and "last_name" in df_all.columns:
                player_id_cols = ["first_name", "last_name"]

            if player_id_cols:
                parts = []
                for _, group in df_all.groupby(player_id_cols):
                    parts.append(group.head(max_rows))
                df_filtered = pd.concat(parts, ignore_index=True) if parts else df_all
            else:
                df_filtered = df_all
        else:
            df_filtered = df_all
    else:
        df_filtered = filter_by_main_kpi(df_all, mode, config_for_filter)

    kpi_cols = display_kpi_columns(mode, interval_config, df_all)

    # Simple表示用の名前列（SB1がある場合はFP_startの代わりにSB1を使用）
    simple_name_cols = list(NAME_COLUMNS)
    if "SB1" in df_filtered.columns and df_filtered["SB1"].dropna().astype(str).str.strip().ne("").any():
        simple_name_cols = [("SB1" if c == "FP_start" else c) for c in simple_name_cols]

    if show_all_cols:
        all_cols = [c for c in df_filtered.columns if not (c.startswith("imputed__") or c == "__row_id")]
        priority = [c for c in NAME_COLUMNS if c in all_cols] + kpi_cols
        extras = [c for c in all_cols if c not in priority]
        seen = set()
        columns = []
        for c in priority + extras:
            if c not in seen:
                columns.append(c)
                seen.add(c)
    else:
        columns = simple_name_cols + kpi_cols
        columns = [c for c in columns if c in df_filtered.columns and not c.startswith("imputed__") and c != "__row_id"]

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(columns)

    for idx in df_filtered.index:
        row = []
        for c in columns:
            val = df_filtered.at[idx, c]
            row.append(_format_value(val))
        writer.writerow(row)

    output = si.getvalue()
    return Response(
        "\ufeff" + output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=kpi_export.csv"},
    )


# ---------------------------------------------------------------------------
# API: KPIデータ (fit_fetchData.py 相当の REST API)
# ---------------------------------------------------------------------------

@app.route("/api/kpis")
@login_required
def api_kpis():
    start_jst = request.args.get("start", "")
    end_jst   = request.args.get("end", "")

    if not start_jst or not end_jst:
        return jsonify({"error": "start, end パラメータが必要です"}), 400

    try:
        start_utc = jst_str_to_utc_sql(start_jst)
        end_utc   = jst_str_to_utc_sql(end_jst)
    except Exception as e:
        return jsonify({"error": f"日付フォーマットエラー: {str(e)}"}), 400

    query = f"""
    SELECT
        p.timestamp,
        p.decoder_id,
        p.transponder_id,
        u.first_name,
        u.last_name,
        tu.user_id,
        tu.id AS transponder_user_id
    FROM passing p
    LEFT JOIN transponder_user tu
        ON tu.transponder_id = p.transponder_id
        AND tu.since <= p.timestamp
        AND (tu.until IS NULL OR tu.until >= p.timestamp)
    LEFT JOIN `user` u
        ON u.id = tu.user_id
    WHERE p.timestamp >= '{start_utc}'
      AND p.timestamp <  '{end_utc}'
      AND (
            tu.user_id IS NOT NULL
            OR p.transponder_id IS NULL
            OR p.transponder_id = ''
          )
    ORDER BY p.timestamp
    LIMIT 50000;
    """

    try:
        result_df, users = fetch_df_from_db(query, progress=lambda msg: print(f"  -> {msg}"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if result_df.empty:
        return jsonify({})

    output_columns = [
        "Date", "Time000to625", "Time625to125", "Time125to250",
        "Time000to125_roll", "Time000to125_stand",
        "Time000to100", "Time100to200", "Time000to200",
        "entry_speed", "jump_speed",
        "FP_start", "SB1", "0m_start", "60m", "AP1", "50m",
        "100m", "BP", "150m", "AP2", "200m", "FP_2nd", "0m_2nd",
    ]

    output = {}
    for (first_name, last_name), group in result_df.groupby(['first_name', 'last_name'], dropna=False):
        if pd.isna(first_name) or pd.isna(last_name):
            player_name = "Unknown"
        else:
            player_name = f"{last_name} {first_name}"

        laps = []
        for _, row in group.iterrows():
            lap_data = []
            for col in output_columns:
                if col in row.index:
                    value = row[col]
                    if pd.isna(value):
                        lap_data.append(None)
                    elif isinstance(value, pd.Timestamp):
                        lap_data.append(value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
                    elif isinstance(value, pd.Timedelta):
                        lap_data.append(value.total_seconds())
                    else:
                        lap_data.append(value)
                else:
                    lap_data.append(None)
            laps.append(lap_data)

        output[player_name] = laps

    return jsonify(output)


# ---------------------------------------------------------------------------
# KPI設定エディタ
# ---------------------------------------------------------------------------

@app.route("/kpi/editor", methods=["GET"])
@login_required
def kpi_editor_page():
    interval_config = _load_kpi_intervals(_get_user_kpi_path())
    mode_keys = sorted([k for k in interval_config.keys() if k != "settings"])

    # 各モードの intervals を整理
    modes_data = {}
    for key in mode_keys:
        mode_data = interval_config.get(key, {})
        if isinstance(mode_data, dict):
            intervals = mode_data.get("intervals", [])
            main_kpi  = mode_data.get("mainKPI")
        else:
            intervals = mode_data if isinstance(mode_data, list) else []
            main_kpi  = None
        modes_data[key] = {"intervals": intervals, "mainKPI": main_kpi}

    settings = _load_json(_get_user_settings_path())
    track_image = settings.get("image_path", "")

    kpi_settings = interval_config.get("settings", {})

    return render_template("kpi_editor.html",
        modes=mode_keys,
        modes_data=modes_data,
        kpi_settings=kpi_settings,
        available_points=TRACK_ORDER,
        track_image=track_image)


@app.route("/api/kpi_editor/save", methods=["POST"])
@login_required
def api_kpi_editor_save():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "データがありません"}), 400

    try:
        _create_backup(_get_user_kpi_path())
        _save_json(_get_user_kpi_path(), data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# KPI設定の更新API（チェックボックスやMax Rows変更用）
# ---------------------------------------------------------------------------

@app.route("/api/kpi_settings", methods=["POST"])
@login_required
def api_kpi_settings():
    data = request.get_json(silent=True) or {}
    user_kpi_path = _get_user_kpi_path()
    interval_config = _load_kpi_intervals(user_kpi_path)

    if "settings" not in interval_config:
        interval_config["settings"] = {}

    for key in ("showAllColumns", "showAllData", "maxRows"):
        if key in data:
            interval_config["settings"][key] = data[key]

    try:
        _create_backup(user_kpi_path)
        _save_json(user_kpi_path, interval_config)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# ユーザー向け KPI JSON エディタ
# ---------------------------------------------------------------------------

@app.route("/my/kpi", methods=["GET"])
@login_required
def my_kpi_editor():
    """現在のユーザーが自分の kpi.json をテキスト編集できるページ"""
    kpi_path = _get_user_kpi_path()
    kpi_content = ""
    if os.path.exists(kpi_path):
        with open(kpi_path, "r", encoding="utf-8") as f:
            kpi_content = f.read()

    current_user = session.get("current_user")
    profile_label = current_user if current_user else "Global (default)"
    is_global = current_user is None

    return render_template("my_kpi_editor.html",
        kpi_content=kpi_content,
        profile_label=profile_label,
        is_global=is_global)


@app.route("/my/kpi/save", methods=["POST"])
@login_required
def my_kpi_save():
    """ユーザーが自分の kpi.json を保存"""
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        return jsonify({
            "error": f"JSON構文エラー: {str(e)}",
            "line": e.lineno,
            "col": e.colno,
            "msg": e.msg,
        }), 400

    kpi_path = _get_user_kpi_path()
    _create_backup(kpi_path)
    with open(kpi_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True})


@app.route("/my/kpi/upload", methods=["POST"])
@login_required
def my_kpi_upload():
    """ユーザーが自分の kpi.json をアップロード"""
    uploaded = request.files.get("file")
    if not uploaded or uploaded.filename == "":
        flash("ファイルが選択されていません。", "error")
        return redirect(url_for("my_kpi_editor"))

    content = uploaded.read().decode("utf-8")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        flash(f"JSON構文エラー: {e}", "error")
        return redirect(url_for("my_kpi_editor"))

    kpi_path = _get_user_kpi_path()
    _create_backup(kpi_path)
    with open(kpi_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    flash("kpi.json をアップロードしました。", "info")
    return redirect(url_for("my_kpi_editor"))


# ---------------------------------------------------------------------------
# Admin: 全ユーザー KPI 一覧
# ---------------------------------------------------------------------------

@app.route("/admin/kpi_overview")
@admin_required
def admin_kpi_overview():
    """全ユーザーの KPI 設定を一覧表示"""
    users = _list_users()
    config = _load_users_config()

    users_kpi = []
    for username in users:
        kpi_path = _user_kpi_path(username)
        kpi_data = None
        mode_count = 0
        if os.path.exists(kpi_path):
            try:
                with open(kpi_path, "r", encoding="utf-8") as f:
                    kpi_data = json.load(f)
                mode_count = len([k for k in kpi_data.keys() if k != "settings"])
            except (json.JSONDecodeError, ValueError):
                kpi_data = "INVALID JSON"

        users_kpi.append({
            "username": username,
            "enabled": config.get(username, {}).get("enabled", True),
            "mode_count": mode_count,
            "has_kpi": os.path.exists(kpi_path),
            "kpi_data": kpi_data,
        })

    # グローバル kpi.json
    global_kpi = None
    global_modes = 0
    if os.path.exists(KPI_INTERVALS_PATH):
        try:
            with open(KPI_INTERVALS_PATH, "r", encoding="utf-8") as f:
                global_kpi = json.load(f)
            global_modes = len([k for k in global_kpi.keys() if k != "settings"])
        except (json.JSONDecodeError, ValueError):
            pass

    return render_template("admin_kpi_overview.html",
        users_kpi=users_kpi,
        global_modes=global_modes)


# ---------------------------------------------------------------------------
# Legacy-v1 比較チェック（admin限定）
# ---------------------------------------------------------------------------

@app.route("/admin/check")
@admin_required
def admin_check():
    """webapp と legacy-v1 のパイプライン結果を比較し JSON で返す。"""
    start_jst = request.args.get("start", "")
    end_jst = request.args.get("end", "")
    ids_param = request.args.get("ids", "")

    if not start_jst or not end_jst or not ids_param:
        return jsonify({"error": "start, end, ids パラメータが必要です"}), 400

    try:
        user_ids = [int(x) for x in ids_param.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "ids は数値のカンマ区切りで指定してください"}), 400

    if not user_ids:
        return jsonify({"error": "ids が空です"}), 400

    # 同じクエリで生データを取得
    try:
        query = _build_kpi_query(start_jst, end_jst, user_ids)
        raw_df = get_df_from_db(query)
    except Exception as e:
        return jsonify({"error": f"DB接続エラー: {str(e)}"}), 500

    if raw_df.empty:
        return jsonify({
            "params": {"start": start_jst, "end": end_jst, "ids": user_ids},
            "summary": {"webapp_rows": 0, "legacy_rows": 0,
                        "matched": 0, "mismatched": 0,
                        "webapp_only": 0, "legacy_only": 0},
            "differences": [],
        })

    # webapp パイプライン
    webapp_df, _ = fetch_df_from_db(query, progress=lambda m: None)

    # legacy-v1 パイプライン
    legacy_df = legacy_v1_pipeline(raw_df)

    # 比較
    params = {"start": start_jst, "end": end_jst, "ids": user_ids}
    result = compare_pipelines(webapp_df, legacy_df, params=params)

    # format=json の場合は JSON を返す（ダウンロード用）
    fmt = request.args.get("format", "html")
    if fmt == "json":
        return jsonify(result)

    # デフォルトは HTML 確認報告書
    return render_template("check_report.html", result=result)


# ---------------------------------------------------------------------------
# ヘルスチェック（認証不要）
# ---------------------------------------------------------------------------

@app.route("/health")
def health_check():
    """ヘルスチェック用エンドポイント。外部監視やcronから使用。"""
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Helpページ
# ---------------------------------------------------------------------------

@app.route("/help")
@login_required
def help_page():
    return render_template("help.html")


# ---------------------------------------------------------------------------
# 静的ファイル（トラック画像）
# ---------------------------------------------------------------------------

@app.route("/track-image")
@login_required
def track_image():
    return send_from_directory(APP_DIR, "track-ref.jpg")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(debug=True, host="0.0.0.0", port=port)
