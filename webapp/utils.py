"""
utils.py — データ処理コア（Flask Web版）
PyQt5依存を除去し、Flask アプリから利用可能にしたもの。
"""

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Callable, Optional
import unicodedata
import re

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

translate_dict = {
    '00042056': '100m',
    '00042616': '150m',
    '000427a3': '50m',
    '000427bc': '0m',
    '000427be': 'AP2',
    '0004282a': 'AP1',
    '0004283c': 'FP',
    '00042899': '60m',
    '000428a2': '200m',
    '000428a3': 'BP',
    '10044ec3': 'SB1',
    '20044001': 'SW1',
    '20044002': 'SW2',
    '200475ca': 'SW3',
    '00042b8f': 'PL1',
    '00042b55': 'PL2',
    '00042b4e': 'PL3',
    '00042b2e': 'PL4',
}

POINT_ORDER = ["FP", "0m", "60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m", "FP_END"]

SEGMENTS = [
    ("FP_start", "0m_start", 17.97),
    ("0m_start", "60m",      42.03),
    ("60m",      "AP1",       2.50),
    ("AP1",      "50m",       5.47),
    ("50m",      "100m",     50.00),
    ("100m",     "BP",        7.03),
    ("BP",       "150m",     42.97),
    ("150m",     "AP2",      19.53),
    ("AP2",      "200m",     30.47),
    ("200m",     "FP_END",   32.03),
]

TRACK_ORDER = [
    "FP_start", "SB1", "0m_start", "60m", "AP1", "50m",
    "100m", "BP", "150m", "AP2", "200m", "FP_2nd", "0m_2nd",
]

NAME_COLUMNS = ["first_name", "last_name", "Date", "FP_start"]

# アプリルートを基準にパスを解決
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(_APP_DIR, "settings.json")
KPI_INTERVALS_PATH = os.path.join(_APP_DIR, "kpi.json")

CUM_DIST = {"FP": 0.0}
_total = 0.0
for a, b, d in SEGMENTS:
    _total += d
    CUM_DIST[b] = _total
LAP_LENGTH = CUM_DIST["FP_END"]

# ---------------------------------------------------------------------------
# JSON ヘルパ
# ---------------------------------------------------------------------------

def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _load_kpi_intervals(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# DB接続
# ---------------------------------------------------------------------------

def _get_db_url_from_settings():
    s = _load_json(SETTINGS_PATH)
    db = s.get("db", {})

    # 環境変数オーバーライド
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    if "url" in db and db["url"]:
        return db["url"]
    driver = db.get("driver", "mysql+pymysql")
    host   = db.get("host", "127.0.0.1")
    port   = int(db.get("port", 3306))
    name   = db.get("name")
    user   = db.get("user")
    passwd = db.get("pass")
    if not all([name, user, passwd]):
        raise RuntimeError("settings.json の db.name / db.user / db.pass を設定してください。")
    return f"{driver}://{user}:{passwd}@{host}:{port}/{name}"


def get_df_from_db(query):
    engine = create_engine(_get_db_url_from_settings(), pool_pre_ping=True)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return df


# ---------------------------------------------------------------------------
# タイムゾーン変換
# ---------------------------------------------------------------------------

def jst_str_to_utc_sql(ts_jst_str: str) -> str:
    if not ts_jst_str:
        return ts_jst_str
    s = ts_jst_str.strip()
    # 複数フォーマットに対応
    formats = [
        "%Y-%m-%d %H:%M:%S",       # 標準: 2026-06-10 15:00:00
        "%Y-%m-%dT%H:%M:%S",       # ISO:  2026-06-10T15:00:00
        "%Y-%m-%dT%H:%M",          # ISO短: 2026-06-10T15:00
        "%Y-%m-%d %H:%M",          # 短:   2026-06-10 15:00
        "%m/%d/%Y, %I:%M:%S %p",   # US:   06/10/2026, 03:00:00 PM
        "%m/%d/%Y %I:%M:%S %p",    # US2:  06/10/2026 03:00:00 PM
        "%m/%d/%Y %H:%M:%S",       # US24: 06/10/2026 15:00:00
        "%m/%d/%Y",                # US日付のみ: 06/10/2026
        "%Y-%m-%d",                # 日付のみ: 2026-06-10
    ]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(f"日付フォーマットを認識できません: '{ts_jst_str}'")
    jst = dt.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    utc = jst.astimezone(ZoneInfo("UTC"))
    return utc.strftime("%Y-%m-%d %H:%M:%S")


def to_jst_naive(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series, errors="coerce", utc=True)
    s = s.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    return s


# ---------------------------------------------------------------------------
# 距離マップ
# ---------------------------------------------------------------------------

def _build_distance_map():
    order = ["FP_start"]
    dist = {"FP_start": 0.0}
    for a, b, d in SEGMENTS:
        if a not in dist:
            dist[a] = dist[order[-1]]
        dist[b] = dist[a] + d
        if b not in order:
            order.append(b)
    return order, dist


# ---------------------------------------------------------------------------
# 補間
# ---------------------------------------------------------------------------

def impute_times_by_distance(df_laps: pd.DataFrame) -> pd.DataFrame:
    cols_order, dist_map = _build_distance_map()
    df = df_laps.copy()
    df["FP_END"] = df.get("FP_2nd", pd.NaT)

    flag_cols = [c for c in cols_order if c != "FP_END"]
    for c in flag_cols:
        df[f"imputed__{c}"] = False

    for i in df.index:
        times = df.loc[i, cols_order].copy()
        known_idx = [k for k, c in enumerate(cols_order) if pd.notna(times[c])]
        if len(known_idx) < 2:
            continue

        for a, b in zip(known_idx[:-1], known_idx[1:]):
            colL, colR = cols_order[a], cols_order[b]
            tL, tR = times[colL], times[colR]
            if not (pd.notna(tL) and pd.notna(tR) and tR > tL):
                continue
            dL, dR = dist_map[colL], dist_map[colR]
            span = dR - dL
            if span <= 0:
                continue
            for k in range(a + 1, b):
                ck = cols_order[k]
                if ck == "FP_END":
                    continue
                if pd.isna(times[ck]):
                    frac = (dist_map[ck] - dL) / span
                    times[ck] = tL + (tR - tL) * frac
                    df.at[i, f"imputed__{ck}"] = True

        df.loc[i, cols_order] = times

    df = df.drop(columns=["FP_END"])
    return df


# ---------------------------------------------------------------------------
# ラップ分割
# ---------------------------------------------------------------------------

def split_laps(df, all_data=None):
    """
    ラップ分割（0m基準 — legacy-v1互換）
    - 0mを検出したら新しいラップを開始
    - SB1は all_data から user_id が空のレコードを 0m の5秒前以内で検索
    - FPは df から 0m の5秒前以内で検索
    """
    expected_order = [
        "FP", "0m", "60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m",
    ]

    def _canon_pos(x):
        if x is None:
            return ""
        s = unicodedata.normalize("NFKC", str(x)).strip()
        u = s.upper()
        if u in {"FP", "\uff26\uff30"}:
            return "FP"
        if u in {"0M", "OM", "0\uff2d"}:
            return "0m"
        if re.fullmatch(r"SB[\s\-]*1", u) or u == "SB1":
            return "SB1"
        return s

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    df["position"] = df["position"].map(_canon_pos)

    # SB1候補の準備（all_data から user_id が空の SB1 レコード）
    sb1_candidates = None
    if all_data is not None:
        adc = all_data.copy()
        adc["timestamp"] = pd.to_datetime(adc["timestamp"], errors="coerce")
        adc["position"] = adc["position"].map(_canon_pos)
        if "user_id" in adc.columns:
            sb1_candidates = adc[
                (adc["position"] == "SB1")
                & (adc["user_id"].isna()
                   | (adc["user_id"].astype(str).str.strip() == ""))
            ].copy()

    rows = []
    current_lap = {}
    lap_id = 0

    def complete_lap(lap_dict, lid):
        """ラップを完成させて rows に追加"""
        sb1_value = None
        fp_value = None

        if "0m" in lap_dict and lap_dict["0m"] is not None:
            zero_m_time = pd.to_datetime(lap_dict["0m"])
            tw_start = zero_m_time - timedelta(seconds=5)
            tw_end = zero_m_time

            # SB1 検索
            if sb1_candidates is not None and len(sb1_candidates) > 0:
                ts_col = pd.to_datetime(sb1_candidates["timestamp"])
                sb1_in = sb1_candidates[
                    (ts_col >= tw_start) & (ts_col <= tw_end)
                ]
                if len(sb1_in) > 0:
                    sb1_in = sb1_in.sort_values("timestamp", ascending=False)
                    sb1_value = sb1_in.iloc[0]["timestamp"]

            # FP 検索
            fp_cands = df[df["position"] == "FP"].copy()
            if len(fp_cands) > 0:
                ts_col = pd.to_datetime(fp_cands["timestamp"])
                fp_in = fp_cands[
                    (ts_col >= tw_start) & (ts_col <= tw_end)
                ]
                if len(fp_in) > 0:
                    fp_in = fp_in.sort_values("timestamp", ascending=False)
                    fp_value = fp_in.iloc[0]["timestamp"]

        result = {}
        for pos, ts in lap_dict.items():
            if pos != "FP":
                result[pos] = ts
        result["SB1"] = sb1_value
        result["FP"] = fp_value
        rows.append(result)

    # --- 0m 検出でラップ分割 ---
    for _, row in df.iterrows():
        pos = row["position"]
        ts = row["timestamp"]

        if pos == "0m":
            if current_lap:
                lap_id += 1
                complete_lap(current_lap, lap_id)
                current_lap = {}
            current_lap[pos] = ts
        else:
            if current_lap:
                current_lap[pos] = ts

    # 最後のラップ
    if current_lap:
        lap_id += 1
        complete_lap(current_lap, lap_id)

    # --- 結果構築 ---
    empty_cols = [
        "Date", "FP_start", "SB1", "0m_start",
        "60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m",
        "FP_2nd", "0m_2nd",
    ]
    if not rows:
        return pd.DataFrame(columns=empty_cols)

    # 全列を収集して順序付け（期待列は常に含める）
    all_columns = set()
    for d in rows:
        all_columns.update(d.keys())

    ordered = ["SB1", "FP"]
    for col in expected_order:
        if col != "FP":
            ordered.append(col)
    for col in sorted(all_columns - set(ordered)):
        ordered.append(col)

    result_df = pd.DataFrame(rows, columns=ordered)

    # Date 列
    def get_date(r):
        for c in ["SB1", "FP"] + [c for c in expected_order if c != "FP"]:
            if c in r.index and pd.notna(r.get(c)):
                return pd.to_datetime(r[c]).date()
        return None

    result_df.insert(0, "Date", result_df.apply(get_date, axis=1))

    # Web app 互換の列名にリネーム
    result_df = result_df.rename(columns={"FP": "FP_start", "0m": "0m_start"})

    # FP_2nd / 0m_2nd（次ラップの値）
    result_df["FP_2nd"] = (
        result_df["FP_start"].shift(-1) if "FP_start" in result_df.columns else pd.NaT
    )
    result_df["0m_2nd"] = (
        result_df["0m_start"].shift(-1) if "0m_start" in result_df.columns else pd.NaT
    )

    return result_df


# ---------------------------------------------------------------------------
# KPI計算関数
# ---------------------------------------------------------------------------

def calculate_entry_speed(row):
    return ''

def calculate_jump_speed(row):
    return ''

def calculate_time_000_to_100(row):
    t1, t2 = row.get("150m"), row.get("50m")
    if pd.isna(t1) or pd.isna(t2):
        return np.nan
    td = t1 - t2
    if pd.isna(td) or td.total_seconds() == 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_100_to_200(row):
    t1, t2 = row.get("0m_2nd"), row.get("150m")
    if pd.isna(t1) or pd.isna(t2):
        return np.nan
    td = t1 - t2
    if pd.isna(td) or td.total_seconds() == 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_000_to_200(row):
    t1, t2 = row.get("0m_2nd"), row.get("50m")
    if pd.isna(t1) or pd.isna(t2):
        return np.nan
    td = t1 - t2
    if pd.isna(td) or td.total_seconds() == 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_000_to_625(row):
    t1, t2 = row.get("AP1"), row.get("FP_start")
    if pd.isna(t1) or pd.isna(t2):
        return np.nan
    td = t1 - t2
    if pd.isna(td) or td.total_seconds() == 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_625_to_125(row):
    t1, t2 = row.get("BP"), row.get("AP1")
    if pd.isna(t1) or pd.isna(t2):
        return np.nan
    td = t1 - t2
    if pd.isna(td) or td.total_seconds() == 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_000_to_125(row):
    t1, t2 = row.get("BP"), row.get("FP_start")
    if pd.isna(t1) or pd.isna(t2):
        return np.nan
    td = t1 - t2
    if pd.isna(td) or td.total_seconds() == 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_125_to_250(row):
    t0, t1 = row.get("BP"), row.get("FP_2nd")
    if pd.isna(t0) or pd.isna(t1):
        return np.nan
    dt = t1 - t0
    return round(dt.total_seconds(), 3) if pd.notna(dt) and dt.total_seconds() > 0 else np.nan

def calculate_time_000_to_625_from_sb(row):
    t0, t1 = row.get("SB1"), row.get("AP1")
    if pd.isna(t0) or pd.isna(t1):
        return np.nan
    td = t1 - t0
    if pd.isna(td) or td.total_seconds() <= 0:
        return np.nan
    return round(td.total_seconds(), 3)

def calculate_time_000_to_125_from_sb(row):
    t0, t1 = row.get("SB1"), row.get("BP")
    if pd.isna(t0) or pd.isna(t1):
        return np.nan
    td = t1 - t0
    if pd.isna(td) or td.total_seconds() <= 0:
        return np.nan
    return round(td.total_seconds(), 3)


# ---------------------------------------------------------------------------
# 補完フラグ伝搬
# ---------------------------------------------------------------------------

def _propagate_imputed_flags_to_kpi(df: pd.DataFrame) -> pd.DataFrame:
    deps_map = {
        "entry_speed":   ["FP_start", "0m_start"],
        "jump_speed":    ["50m", "AP1"],
        "Time000to100":  ["150m", "50m"],
        "Time100to200":  ["0m_2nd", "150m"],
        "Time000to200":  ["0m_2nd", "50m"],
    }
    for kpi, deps in deps_map.items():
        if kpi not in df.columns:
            continue
        m = pd.Series(False, index=df.index)
        for d in deps:
            fcol = f"imputed__{d}"
            if fcol in df.columns:
                m = m | df[fcol].fillna(False)
        df[f"imputed__{kpi}"] = m
    return df


# ---------------------------------------------------------------------------
# メインパイプライン: DB→前処理→split→補間→結合
# ---------------------------------------------------------------------------

def fetch_df_from_db(
    query: str,
    progress: Optional[Callable[[str], None]] = None,
):
    """
    DB → 前処理 → split → 補間 → 結合（legacy-v1 互換パイプライン）
    """
    def _p(msg: str):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    _p("DB問い合わせ中...")
    df = get_df_from_db(query)

    users = df["first_name"].unique() if "first_name" in df.columns else []
    if df.empty:
        _p("データ0件")
        return df.copy(), users

    _p("タイムスタンプ変換中...")
    if "timestamp" in df.columns:
        df["timestamp"] = to_jst_naive(df["timestamp"])

    _p("位置ラベル生成中...")
    if "decoder_id" in df.columns:
        df["position"] = df["decoder_id"].map(translate_dict).fillna("Unknown")
    else:
        df["position"] = "Unknown"

    _p("ユーザーごとに集計中...")
    all_dfs = []
    group_key = "user_id" if "user_id" in df.columns else None
    groups = df.groupby(group_key) if group_key else [(None, df)]

    for uid, group in groups:
        _p(f"ユーザー {uid if uid is not None else '-'}: split中...")

        group = group.sort_values(by=["timestamp"]).reset_index(drop=True)

        # legacy-v1 互換: 全データを渡して SB1 検索用に使用
        temp = split_laps(group, all_data=df)

        temp = temp.reset_index(drop=True)

        # 氏名・user_id 付与
        if "first_name" in group.columns:
            temp["first_name"] = group["first_name"].iloc[0]
        if "last_name" in group.columns:
            temp["last_name"] = group["last_name"].iloc[0]
        if "user_id" in group.columns:
            temp["user_id"] = uid

        all_dfs.append(temp.copy())

    if not all_dfs:
        _p("集計結果0件")
        return df.iloc[0:0].copy(), users

    _p("結合中...")
    result_df = pd.concat(all_dfs, ignore_index=True)
    _p("完了")
    return result_df, users


# ---------------------------------------------------------------------------
# KPIページ用ヘルパ（kpi_page.pyから移植）
# ---------------------------------------------------------------------------

import math

def ensure_interval_columns(df_all: pd.DataFrame, interval_config: dict) -> pd.DataFrame:
    """
    kpi.json の start/end 定義に基づき、区間タイム列を df_all に追加する。
    """
    if df_all is None or df_all.empty:
        return df_all

    cfg = interval_config or {}
    pos_index = {name: i for i, name in enumerate(TRACK_ORDER)}

    for mode, entries in cfg.items():
        if mode in ("settings", "mainKPI"):
            continue

        if isinstance(entries, dict):
            interval_list = entries.get("intervals", [])
        elif isinstance(entries, list):
            interval_list = entries
        else:
            continue

        for ent in interval_list:
            if not isinstance(ent, dict):
                continue
            start = ent.get("start")
            end = ent.get("end")
            if not start or not end:
                continue

            col_name = ent.get("name") or f"{start}-{end}"
            if col_name in df_all.columns:
                continue
            if start not in df_all.columns or end not in df_all.columns:
                continue

            s = df_all[start]
            e = df_all[end]

            idx_s = pos_index.get(start)
            idx_e = pos_index.get(end)

            if idx_s is not None and idx_e is not None and idx_s > idx_e:
                e_series = e.shift(-1)
            else:
                e_series = e

            s = pd.to_datetime(s, errors='coerce')
            e_series = pd.to_datetime(e_series, errors='coerce')

            mask_valid = pd.notna(s) & pd.notna(e_series)
            diff = pd.Series(index=s.index, dtype='timedelta64[ns]')
            diff[mask_valid] = e_series[mask_valid] - s[mask_valid]
            diff[~mask_valid] = pd.NaT

            result = pd.Series(index=diff.index, dtype=float)
            valid_mask = pd.notna(diff)
            result[valid_mask] = diff[valid_mask].dt.total_seconds().round(3)
            result[~valid_mask] = math.nan
            df_all[col_name] = result

    return df_all


def display_kpi_columns(time_mode: str, interval_config: dict, df_all: pd.DataFrame) -> list:
    """現在のモードに対応するKPI列名を返す。"""
    mode = (time_mode or "").lower()
    cfg = interval_config or {}
    mode_data = cfg.get(mode, [])

    if isinstance(mode_data, dict):
        entries = mode_data.get("intervals", [])
    elif isinstance(mode_data, list):
        entries = mode_data
    else:
        entries = []

    cols = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        start = ent.get("start")
        end = ent.get("end")
        if not start or not end:
            continue
        name = ent.get("name") or f"{start}-{end}"
        cols.append(name)

    return [c for c in cols if c in df_all.columns]


def filter_by_main_kpi(df: pd.DataFrame, time_mode: str, interval_config: dict) -> pd.DataFrame:
    """mainKPIに基づいてフィルタリング（選手ごとに上位N行）。"""
    if df is None or df.empty:
        return df

    mode = (time_mode or "").lower()
    cfg = interval_config or {}
    mode_data = cfg.get(mode, [])

    if isinstance(mode_data, dict):
        main_kpi = mode_data.get("mainKPI")
    else:
        main_kpi = cfg.get("mainKPI")

    if not main_kpi or main_kpi not in df.columns:
        return df

    player_id_cols = []
    if "user_id" in df.columns:
        player_id_cols = ["user_id"]
    elif "first_name" in df.columns and "last_name" in df.columns:
        player_id_cols = ["first_name", "last_name"]
    else:
        return df

    settings = interval_config.get("settings", {})
    max_rows = settings.get("maxRows", 5)

    filtered_rows = []
    for _, group in df.groupby(player_id_cols, dropna=False):
        group_sorted = group.sort_values(by=main_kpi, ascending=True, na_position='last')
        valid_group = group_sorted[group_sorted[main_kpi].notna()]
        if len(valid_group) > 0:
            filtered_rows.append(valid_group.head(max_rows) if max_rows > 0 else valid_group)
        else:
            if max_rows > 0:
                top_rows = group_sorted.head(max_rows)
            else:
                top_rows = group_sorted
            if len(top_rows) > 0:
                filtered_rows.append(top_rows)

    if filtered_rows:
        return pd.concat(filtered_rows, ignore_index=True)
    return df


# ===========================================================================
# Legacy-v1 参照実装（比較チェック用）
# ===========================================================================

def _legacy_v1_split_laps(df, all_data=None):
    """
    legacy-v1 オリジナルの split_laps（GitHub legacy-v1 branch から移植）。
    比較チェック専用 — 本番処理には使わない。
    """
    expected_order = [
        "FP", "0m", "60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m",
    ]

    def _canon(x):
        if x is None:
            return ""
        s = unicodedata.normalize("NFKC", str(x)).strip()
        u = s.upper()
        if u in {"FP", "\uff26\uff30"}:
            return "FP"
        if u in {"0M", "OM", "0\uff2d"}:
            return "0m"
        if re.fullmatch(r"SB[\s\-]*1", u) or u == "SB1":
            return "SB1"
        return s

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    df["position"] = df["position"].map(_canon)

    sb1_candidates = None
    if all_data is not None:
        adc = all_data.copy()
        adc["timestamp"] = pd.to_datetime(adc["timestamp"], errors="coerce")
        adc["position"] = adc["position"].map(_canon)
        if "user_id" in adc.columns:
            sb1_candidates = adc[
                (adc["position"] == "SB1")
                & (adc["user_id"].isna()
                   | (adc["user_id"].astype(str).str.strip() == ""))
            ].copy()

    rows = []
    current_lap = {}
    lap_id = 0

    def complete_lap(lap_dict, lid):
        sb1_value = None
        fp_value = None
        if "0m" in lap_dict and lap_dict["0m"] is not None:
            zero_m = pd.to_datetime(lap_dict["0m"])
            tw_s = zero_m - timedelta(seconds=5)
            tw_e = zero_m
            if sb1_candidates is not None and len(sb1_candidates) > 0:
                ts = pd.to_datetime(sb1_candidates["timestamp"])
                hit = sb1_candidates[(ts >= tw_s) & (ts <= tw_e)]
                if len(hit) > 0:
                    sb1_value = hit.sort_values("timestamp", ascending=False).iloc[0]["timestamp"]
            fp_c = df[df["position"] == "FP"].copy()
            if len(fp_c) > 0:
                ts = pd.to_datetime(fp_c["timestamp"])
                hit = fp_c[(ts >= tw_s) & (ts <= tw_e)]
                if len(hit) > 0:
                    fp_value = hit.sort_values("timestamp", ascending=False).iloc[0]["timestamp"]
        result = {}
        for pos, ts in lap_dict.items():
            if pos != "FP":
                result[pos] = ts
        result["SB1"] = sb1_value
        result["FP"] = fp_value
        rows.append(result)

    for _, row in df.iterrows():
        pos, ts = row["position"], row["timestamp"]
        if pos == "0m":
            if current_lap:
                lap_id += 1
                complete_lap(current_lap, lap_id)
                current_lap = {}
            current_lap[pos] = ts
        else:
            if current_lap:
                current_lap[pos] = ts

    if current_lap:
        lap_id += 1
        complete_lap(current_lap, lap_id)

    if not rows:
        return pd.DataFrame(columns=[
            "Date", "FP_start", "SB1", "0m_start",
            "60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m",
        ])

    all_cols = set()
    for d in rows:
        all_cols.update(d.keys())
    ordered = ["SB1", "FP"]
    for c in expected_order:
        if c in all_cols and c != "FP":
            ordered.append(c)
    for c in sorted(all_cols - set(ordered)):
        ordered.append(c)

    result_df = pd.DataFrame(rows, columns=ordered)

    def get_date(r):
        for c in ["SB1", "FP"] + [c for c in expected_order if c != "FP"]:
            if c in r.index and pd.notna(r.get(c)):
                return pd.to_datetime(r[c]).date()
        return None

    result_df.insert(0, "Date", result_df.apply(get_date, axis=1))
    result_df = result_df.rename(columns={"FP": "FP_start", "0m": "0m_start"})
    return result_df


def legacy_v1_pipeline(df_raw):
    """
    legacy-v1 互換パイプライン（補間なし）。
    df_raw は get_df_from_db() の生データ（UTC タイムスタンプ）。
    """
    df = df_raw.copy()
    if df.empty:
        return df

    if "timestamp" in df.columns:
        df["timestamp"] = to_jst_naive(df["timestamp"])
    if "decoder_id" in df.columns:
        df["position"] = df["decoder_id"].map(translate_dict).fillna("Unknown")
    else:
        df["position"] = "Unknown"

    all_dfs = []
    group_key = "user_id" if "user_id" in df.columns else None
    groups = df.groupby(group_key) if group_key else [(None, df)]

    for uid, group in groups:
        group = group.sort_values("timestamp").reset_index(drop=True)
        temp = _legacy_v1_split_laps(group, all_data=df)
        if "first_name" in group.columns:
            temp["first_name"] = group["first_name"].iloc[0]
        if "last_name" in group.columns:
            temp["last_name"] = group["last_name"].iloc[0]
        if "user_id" in group.columns:
            temp["user_id"] = uid
        all_dfs.append(temp.copy())

    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True)


def compare_pipelines(webapp_df, legacy_df, params=None):
    """
    webapp パイプラインと legacy-v1 パイプラインの結果を比較。
    JSON シリアライズ可能な dict を返す。
    """
    compare_cols = [
        "FP_start", "SB1", "0m_start",
        "60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m",
    ]

    def _ts_str(v):
        if pd.isna(v):
            return None
        if isinstance(v, (pd.Timestamp, datetime)):
            return v.strftime("%H:%M:%S.%f")[:-3]
        return str(v)

    def _make_key(row):
        u = ""
        if "last_name" in row.index and pd.notna(row.get("last_name")):
            u = f"{row['last_name']} {row.get('first_name', '')}"
        z = _ts_str(row.get("0m_start"))
        return (u.strip(), str(row.get("Date", "")), z or "")

    # キー→行 のマップを構築
    wa_map = {}
    for i, row in webapp_df.iterrows():
        k = _make_key(row)
        wa_map.setdefault(k, []).append(row)

    lg_map = {}
    for i, row in legacy_df.iterrows():
        k = _make_key(row)
        lg_map.setdefault(k, []).append(row)

    all_keys = sorted(set(wa_map.keys()) | set(lg_map.keys()))

    differences = []
    matched = 0
    mismatched = 0
    webapp_only = 0
    legacy_only = 0

    for key in all_keys:
        wa_rows = wa_map.get(key, [])
        lg_rows = lg_map.get(key, [])

        if not lg_rows:
            webapp_only += len(wa_rows)
            for r in wa_rows:
                differences.append({
                    "type": "webapp_only",
                    "user": key[0], "date": key[1], "0m_start": key[2],
                })
            continue

        if not wa_rows:
            legacy_only += len(lg_rows)
            for r in lg_rows:
                differences.append({
                    "type": "legacy_only",
                    "user": key[0], "date": key[1], "0m_start": key[2],
                })
            continue

        # 1対1で比較（同キーに複数ある場合は順序で対応）
        for idx in range(max(len(wa_rows), len(lg_rows))):
            if idx >= len(wa_rows):
                legacy_only += 1
                differences.append({
                    "type": "legacy_only",
                    "user": key[0], "date": key[1], "0m_start": key[2],
                })
                continue
            if idx >= len(lg_rows):
                webapp_only += 1
                differences.append({
                    "type": "webapp_only",
                    "user": key[0], "date": key[1], "0m_start": key[2],
                })
                continue

            wa_r = wa_rows[idx]
            lg_r = lg_rows[idx]
            row_diffs = []

            for col in compare_cols:
                wa_v = wa_r.get(col) if col in wa_r.index else None
                lg_v = lg_r.get(col) if col in lg_r.index else None
                wa_na = pd.isna(wa_v) if wa_v is not None else True
                lg_na = pd.isna(lg_v) if lg_v is not None else True

                if wa_na and lg_na:
                    continue
                if wa_na != lg_na:
                    row_diffs.append({
                        "column": col,
                        "webapp": _ts_str(wa_v),
                        "legacy": _ts_str(lg_v),
                    })
                    continue
                # 両方値あり — 1秒以内なら一致とみなす
                try:
                    diff_sec = abs((pd.Timestamp(wa_v) - pd.Timestamp(lg_v)).total_seconds())
                    if diff_sec > 1.0:
                        row_diffs.append({
                            "column": col,
                            "webapp": _ts_str(wa_v),
                            "legacy": _ts_str(lg_v),
                            "diff_sec": round(diff_sec, 3),
                        })
                except Exception:
                    if str(wa_v) != str(lg_v):
                        row_diffs.append({
                            "column": col,
                            "webapp": _ts_str(wa_v),
                            "legacy": _ts_str(lg_v),
                        })

            if row_diffs:
                mismatched += 1
                differences.append({
                    "type": "value_mismatch",
                    "user": key[0], "date": key[1], "0m_start": key[2],
                    "columns": row_diffs,
                })
            else:
                matched += 1

    result = {
        "params": params or {},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "webapp_rows": len(webapp_df),
            "legacy_rows": len(legacy_df),
            "matched": matched,
            "mismatched": mismatched,
            "webapp_only": webapp_only,
            "legacy_only": legacy_only,
        },
        "differences": differences,
    }
    return result
