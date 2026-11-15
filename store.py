"""Load ENTSOG daily flows for the app: the stored file first, the live API only
when the file is missing or out of date, or when the user asks for it."""
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from entsog import fetch_series, series_keys

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "entsog_daily.csv"
META_FILE = DATA_DIR / "entsog_meta.json"
# ENTSOG publishes a gas day on the following day, so the stored file is
# normally 1-2 days behind. Older than this, the app tops it up from the API.
STALE_DAYS = 3
# A live top-up also re-fetches this many days before the stored end, because
# recent days are often revised.
TOPUP_OVERLAP_DAYS = 14
LIVE_TTL = 6 * 3600  # cache for live API calls


@st.cache_data(show_spinner=False)
def read_stored(_path, mtime):
    """{(operator, point, direction): {day: kWh}} and the latest day.
    The file's modification time is the cache key, so a new file from
    GitHub Actions is read again automatically."""
    df = pd.read_csv(_path, dtype={"day": str, "operator": str, "point": str,
                                   "direction": str})
    flows = {key: dict(zip(g["day"], g["kwh"]))
             for key, g in df.groupby(["operator", "point", "direction"])}
    return flows, df["day"].max()


def read_meta():
    try:
        return json.loads(META_FILE.read_text())
    except Exception:
        return {}


@st.cache_data(ttl=LIVE_TTL, show_spinner=False)
def fetch_live(operator, point, direction, start_str, end_str):
    """Live ENTSOG call, cached for 6 hours."""
    return fetch_series(operator, point, direction, start_str, end_str)


def load_flows(start, today, force_live=False):
    """Daily flows from `start` on.

    Returns (flows, failed, dupes, info): flows {(op, pt, d): {day: kWh}},
    failed {(op, pt, d) or None: error}, info {"stored_last", "live_from"}.
    """
    stored, stored_last, failed = {}, None, {}
    if DATA_FILE.exists():
        try:
            stored, last = read_stored(DATA_FILE, DATA_FILE.stat().st_mtime)
            stored_last = date.fromisoformat(last)
        except Exception as e:
            failed[None] = f"Stored file could not be read: {e}"
    stale = stored_last is None or (today - stored_last).days > STALE_DAYS
    live_from = None
    if stale or force_live:
        live_from = (start if stored_last is None
                     else max(start, stored_last - timedelta(days=TOPUP_OVERLAP_DAYS)))

    keys = series_keys()
    first = start.isoformat()
    flows, dupes = {}, int(read_meta().get("dupes", 0)) if not live_from else 0
    bar = st.progress(0.0, text="Loading latest data from ENTSOG...") if live_from else None
    for i, key in enumerate(keys):
        days = {d: v for d, v in stored.get(key, {}).items() if d >= first}
        if live_from:
            bar.progress(i / len(keys), text=f"Loading latest data from ENTSOG "
                                             f"({i + 1}/{len(keys)})")
            try:
                r = fetch_live(*key, live_from.isoformat(), today.isoformat())
                days.update(r["days"])  # live values replace stored ones
                dupes += r["dupes"]
            except Exception as e:
                failed[key] = str(e)
        flows[key] = days
    if bar:
        bar.empty()
    return flows, failed, dupes, {"stored_last": stored_last, "live_from": live_from}


def source_text(info):
    """One line for the Data quality section."""
    meta = read_meta()
    if info["stored_last"]:
        text = (f"Stored file (updated every evening"
                + (f", last run {meta['fetched_at']}" if meta.get("fetched_at") else "")
                + f"): through {info['stored_last']}.")
    else:
        text = "No stored file."
    if info["live_from"]:
        text += f" Fetched live from ENTSOG: {info['live_from']} onwards."
    return text
