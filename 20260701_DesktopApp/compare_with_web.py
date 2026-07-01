#!/usr/bin/env python3
"""
Web版とDesktop版のsplit_lapsパイプライン結果を比較するスクリプト。

使い方:
  # kpi_log.jsonl の最後のエントリのパラメータを使って比較
  python compare_with_web.py

  # パラメータを直接指定
  python compare_with_web.py --start "2026-06-01 08:00:00" --end "2026-06-01 18:00:00" --ids 42,57

  # ログファイルのパスを明示
  python compare_with_web.py --log path/to/kpi_log.jsonl
"""
import sys
import os
import json
import argparse
import importlib.util

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_WEBAPP = os.path.join(_HERE, "..", "webapp")

# Desktop utils（このフォルダのutils.py）
sys.path.insert(0, _HERE)
import utils as desktop_utils   # noqa: E402

# Web utils（../webapp/utils.py）を別名でロード（importlibで名前衝突回避）
_spec = importlib.util.spec_from_file_location("web_utils", os.path.join(_WEBAPP, "utils.py"))
web_utils = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(web_utils)
except Exception as e:
    print(f"[警告] webapp/utils.py のロード中にエラー: {e}")
    print("       split_laps() の比較はスキップします。")
    web_utils = None


# ---------------------------------------------------------------------------

def load_last_log(log_path=None):
    if log_path is None:
        log_path = os.path.join(_HERE, "kpi_log.jsonl")
    if not os.path.exists(log_path):
        print(f"[エラー] ログファイルが見つかりません: {log_path}")
        return None
    with open(log_path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        print("[エラー] ログファイルが空です")
        return None
    return json.loads(lines[-1])


def fetch_raw(start_jst, end_jst, ids):
    from utils import jst_str_to_utc_sql, get_df_from_db
    start_utc = jst_str_to_utc_sql(start_jst)
    end_utc   = jst_str_to_utc_sql(end_jst)
    ids_str   = ",".join(map(str, ids))
    query = f"""
    SELECT
        p.timestamp, p.decoder_id,
        u.first_name, u.last_name,
        p.transponder_id, tu.user_id,
        tu.id AS transponder_user_id
    FROM passing p
    LEFT JOIN (
        SELECT id, transponder_id, user_id, since, until
        FROM transponder_user
        WHERE user_id IN ({ids_str})
    ) tu ON tu.transponder_id = p.transponder_id
        AND tu.since <= p.timestamp
        AND (tu.until IS NULL OR tu.until > p.timestamp)
    LEFT JOIN `user` u ON u.id = tu.user_id
    WHERE p.timestamp >= '{start_utc}'
      AND p.timestamp <  '{end_utc}'
      AND (tu.user_id IS NOT NULL OR p.transponder_id IS NULL OR p.transponder_id = '')
    ORDER BY p.timestamp
    LIMIT 10000;
    """
    return get_df_from_db(query)


def compare_split_laps(raw_df):
    """両パイプラインのsplit_lapsを同じraw_dfで実行して比較する"""
    print("\n[Desktop] split_laps() 実行中...")
    desktop_laps = desktop_utils.split_laps(raw_df)
    print(f"[Desktop] {len(desktop_laps)} ラップ")

    if web_utils is None:
        print("\n[Web] webapp/utils.py が使えないためスキップ")
        return desktop_laps, None

    print("\n[Web] split_laps() 実行中...")
    web_laps = web_utils.split_laps(raw_df)
    print(f"[Web]     {len(web_laps)} ラップ")

    print("\n--- split_laps 比較 ---")
    if len(desktop_laps) != len(web_laps):
        print(f"[差異] ラップ数: Desktop={len(desktop_laps)}, Web={len(web_laps)}")
    else:
        print(f"[OK]  ラップ数一致: {len(desktop_laps)}")

    d_cols = set(desktop_laps.columns)
    w_cols = set(web_laps.columns)
    common = d_cols & w_cols

    if d_cols - w_cols:
        print(f"[Desktop専用列] {sorted(d_cols - w_cols)}")
    if w_cols - d_cols:
        print(f"[Web専用列]     {sorted(w_cols - d_cols)}")

    if len(desktop_laps) == len(web_laps):
        id_col   = "user_id"   if "user_id"   in common else None
        time_col = "FP_start"  if "FP_start"  in common else ("0m_start" if "0m_start" in common else None)
        sort_key = [k for k in [id_col, time_col] if k]

        d = desktop_laps.sort_values(sort_key).reset_index(drop=True) if sort_key else desktop_laps
        w = web_laps.sort_values(sort_key).reset_index(drop=True)     if sort_key else web_laps

        mismatches = []
        for col in sorted(common - set(sort_key)):
            try:
                if pd.api.types.is_datetime64_any_dtype(d[col]) or pd.api.types.is_datetime64_any_dtype(w[col]):
                    diff = (pd.to_datetime(d[col]) - pd.to_datetime(w[col])).abs().dropna()
                    big  = diff[diff > pd.Timedelta(milliseconds=10)]
                    if len(big) > 0:
                        mismatches.append(f"  {col}: {len(big)} 行で差あり (最大 {diff.max()})")
                else:
                    dn = pd.to_numeric(d[col], errors="coerce")
                    wn = pd.to_numeric(w[col], errors="coerce")
                    diff = (dn - wn).abs().dropna()
                    big  = diff[diff > 1e-3]
                    if len(big) > 0:
                        mismatches.append(f"  {col}: {len(big)} 行で差あり (最大 {big.max():.4f})")
            except Exception:
                pass

        if mismatches:
            print(f"[差異あり] {len(mismatches)} 列:")
            for m in mismatches:
                print(m)
        else:
            print("[OK]  共通列はすべて一致")

    return desktop_laps, web_laps


def show_log_summary(entry):
    print(f"\n--- Desktop 操作ログ (最終エントリ) ---")
    print(f"  セッション : {entry.get('session_ts')}")
    p = entry.get("params", {})
    print(f"  期間       : {p.get('start')} 〜 {p.get('end')}")
    print(f"  ユーザーID : {p.get('ids')}")
    print(f"  モード     : {entry.get('mode')}")
    print(f"  エフォート : {entry.get('effort_count')} 件")
    for e in entry.get("efforts", []):
        kpi_str = "  ".join(f"{k}={v:.3f}" for k, v in (e.get("kpi") or {}).items() if v is not None)
        print(f"    {e.get('player'):30s}  {e.get('date')}  {kpi_str or '(KPIなし)'}")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Web版とDesktop版のパイプライン比較")
    parser.add_argument("--start", help='開始日時 JST "YYYY-MM-DD HH:MM:SS"')
    parser.add_argument("--end",   help='終了日時 JST "YYYY-MM-DD HH:MM:SS"')
    parser.add_argument("--ids",   help="ユーザーIDのカンマ区切り (例: 42,57)")
    parser.add_argument("--log",   help="kpi_log.jsonlのパス", default=None)
    parser.add_argument("--skip-db", action="store_true",
                        help="DBクエリをスキップ（ログのサマリー表示のみ）")
    args = parser.parse_args()

    if args.start and args.end and args.ids:
        start_jst = args.start
        end_jst   = args.end
        ids       = [int(x) for x in args.ids.split(",")]
        log_entry = None
    else:
        log_entry = load_last_log(args.log)
        if log_entry is None:
            sys.exit(1)
        show_log_summary(log_entry)
        p         = log_entry.get("params", {})
        start_jst = p.get("start")
        end_jst   = p.get("end")
        ids       = p.get("ids", [])

    if not start_jst or not end_jst or not ids:
        print("[エラー] start / end / ids が取得できません")
        sys.exit(1)

    if args.skip_db:
        print("\n--skip-db 指定のためDBクエリをスキップ")
        return

    print(f"\n[DB] クエリ: {start_jst} 〜 {end_jst}, ids={ids}")
    raw_df = fetch_raw(start_jst, end_jst, ids)
    print(f"[DB] {len(raw_df)} 行取得")

    if raw_df.empty:
        print("[比較] データなし — 終了")
        return

    compare_split_laps(raw_df)


if __name__ == "__main__":
    main()
