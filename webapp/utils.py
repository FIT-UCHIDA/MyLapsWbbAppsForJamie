"""
utils.py — データ処理コア（Flask Web版）
PyQt5依存を除去し、Flask アプリから利用可能にしたもの。
"""

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
import os
import json
from datetime import datetime
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
    dt = datetime.strptime(ts_jst_str, "%Y-%m-%d %H:%M:%S")
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

def split_laps(df):
    def _canon_pos(x: str) -> str:
        if x is None:
            return ""
        s = unicodedata.normalize("NFKC", str(x)).strip()
        u = s.upper()
        if u in {"FP", "FP"}:
            return "FP"
        if u in {"0M", "OM", "0M"}:
            return "0m"
        if re.fullmatch(r"SB[\s\-]*1", u) or u == "SB1":
            return "SB1"
        return s

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    df["position"] = df["position"].map(_canon_pos)

    anchors   = {"FP", "0m"}
    inner_pts = ["60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m"]

    tmp_cols = ["FP_first", "SB1", "0m_start"] + inner_pts
    rows = []
    cur = {k: None for k in tmp_cols}
    anchor = None

    def flush():
        nonlocal cur, rows, anchor
        if any(v is not None for v in cur.values()):
            rows.append(cur)
        cur = {k: None for k in tmp_cols}
        anchor = None

    for _, r in df.iterrows():
        pos = r["position"]
        ts  = r["timestamp"]

        if (pos not in anchors) and (pos not in inner_pts) and (pos != "SB1"):
            continue

        if pos in anchors:
            if anchor is None:
                anchor = pos
                if pos == "FP":
                    if cur["FP_first"] is None:
                        cur["FP_first"] = ts
                else:
                    if cur["0m_start"] is None:
                        cur["0m_start"] = ts
            else:
                if pos == anchor:
                    flush()
                    anchor = pos
                    if pos == "FP":
                        cur["FP_first"] = ts
                    else:
                        cur["0m_start"] = ts
                else:
                    if pos == "FP" and cur["FP_first"] is None:
                        cur["FP_first"] = ts
                    if pos == "0m" and cur["0m_start"] is None:
                        cur["0m_start"] = ts
            continue

        if pos == "SB1":
            fp_seen     = (cur.get("FP_first") is not None) and (not pd.isna(cur.get("FP_first")))
            z0_not_seen = (cur.get("0m_start") is None) or pd.isna(cur.get("0m_start"))
            sb1_not_set = (cur.get("SB1") is None) or pd.isna(cur.get("SB1"))
            if (anchor is not None) and fp_seen and z0_not_seen and sb1_not_set:
                cur["SB1"] = ts
            continue

        if anchor is not None and pos in inner_pts and cur[pos] is None:
            cur[pos] = ts

    flush()

    if not rows:
        return pd.DataFrame(columns=["FP_start", "SB1", "0m_start", *inner_pts, "FP_2nd", "0m_2nd"])

    out_tmp = pd.DataFrame(rows, columns=tmp_cols)

    def compute_fp_start(row):
        fp = row["FP_first"]
        z0 = row["0m_start"]
        if pd.isna(fp):
            return pd.NaT
        if pd.isna(z0):
            return fp
        return fp if fp <= z0 else pd.NaT

    out = pd.DataFrame()
    out["FP_start"] = out_tmp.apply(compute_fp_start, axis=1)
    out["SB1"]      = out_tmp["SB1"]
    out["0m_start"] = out_tmp["0m_start"]
    for c in inner_pts:
        out[c] = out_tmp[c]

    out["FP_2nd"] = out_tmp["FP_first"].shift(-1)
    out["0m_2nd"] = out["0m_start"].shift(-1)

    cols_for_date = ["FP_first", "0m_start"] + inner_pts
    first_ts = out_tmp[cols_for_date].apply(
        lambda r: r.dropna().min() if r.notna().any() else pd.NaT, axis=1
    )
    out.insert(0, "Date", pd.to_datetime(first_ts).dt.date)

    return out


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

        sb1_all = df[df["position"] == "SB1"].copy()
        if not group.empty:
            tmin, tmax = group["timestamp"].min(), group["timestamp"].max()
            sb1_all = sb1_all[
                (sb1_all["timestamp"] >= tmin) & (sb1_all["timestamp"] <= tmax)
            ]

        is_fp_user = group["position"].eq("FP")
        is_0m_user = group["position"].eq("0m")
        fp_cum_user = is_fp_user.cumsum()

        interval_id_user = fp_cum_user
        first0_ts_by_int = (
            group.assign(_is0m=is_0m_user)
            .groupby(interval_id_user, dropna=False)
            .apply(
                lambda s: s.loc[s["_is0m"], "timestamp"].iloc[0]
                if s["_is0m"].any()
                else pd.NaT
            )
        )

        work = pd.concat([group, sb1_all], ignore_index=True).sort_values(
            "timestamp"
        ).reset_index(drop=True)

        is_fp_in_work = (work["position"].eq("FP")) & (work.get("user_id") == uid)
        is_0m_in_work = (work["position"].eq("0m")) & (work.get("user_id") == uid)
        fp_cum_work = is_fp_in_work.cumsum()
        z0_cum_work = is_0m_in_work.cumsum()
        between_work = fp_cum_work.gt(z0_cum_work)
        interval_id_work = fp_cum_work
        work["_first0"] = interval_id_work.map(first0_ts_by_int)

        sb1_between = (
            work["position"].eq("SB1")
            & between_work
            & work["timestamp"].lt(work["_first0"])
        )
        sb1_cnt = sb1_between.groupby(interval_id_work, dropna=False).transform("sum")
        sb1_keep = sb1_between & sb1_cnt.eq(1)

        keep_mask = (work.get("user_id") == uid) | sb1_keep
        group_plus_sb1 = (
            work[keep_mask].copy().sort_values("timestamp").reset_index(drop=True)
        )

        temp = split_laps(group_plus_sb1)

        _p(f"ユーザー {uid if uid is not None else '-'}: 補間中...")
        temp = impute_times_by_distance(temp)
        temp = temp.reset_index(drop=True)

        temp["0m_2nd"] = temp["0m_start"].shift(-1)
        temp["FP_2nd"] = (
            temp["FP_first"].shift(-1)
            if "FP_first" in temp.columns
            else temp["FP_start"].shift(-1)
        )

        if "first_name" in group.columns:
            temp["first_name"] = group["first_name"].iloc[0]
        if "last_name" in group.columns:
            temp["last_name"] = group["last_name"].iloc[0]

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
    for _, group in df.groupby(player_id_cols):
        group_sorted = group.sort_values(by=main_kpi, ascending=True, na_position='last')
        valid_group = group_sorted[group_sorted[main_kpi].notna()]
        if len(valid_group) > 0:
            filtered_rows.append(valid_group.head(max_rows))
        else:
            top_rows = group_sorted.head(max_rows)
            if len(top_rows) > 0:
                filtered_rows.append(top_rows)

    if filtered_rows:
        return pd.concat(filtered_rows, ignore_index=True)
    return df
