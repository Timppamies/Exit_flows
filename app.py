"""FinBalt Regional Gas Demand: Finland, Estonia, Latvia, Lithuania (ENTSOG).

Run with:  streamlit run finbalt_gas_demand.py
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="FinBalt Gas Demand", page_icon="📊", layout="wide")

URL = "https://transparency.entsog.eu/api/v1/operationalData.json"
CHUNK_DAYS = 180  # one request covers at most this many days

# (group, operatorKey, pointKey, direction, label)
SERIES = [
    # Direct consumption points (aggregated "Final consumers")
    ("EE_cons", "EE-TSO-0001", "FNC-00037", "exit", "Estonia final consumers"),
    ("LV_cons", "LV-TSO-0001", "FNC-00205", "exit", "Latvia domestic consumption"),
    # Inčukalns storage injection
    ("LV_inj", "LV-TSO-0001", "UGS-00029", "exit", "Inčukalns injection"),
    # Finland: balance (no consumption point reported)
    ("FI_in", "FI-TSO-0003", "ITP-00024", "entry", "Imatra (RU→FI)"),
    ("FI_in", "FI-TSO-0003", "ITP-00550", "entry", "Balticconnector (EE→FI)"),
    ("FI_in", "FI-TSO-0003", "LNG-00011", "entry", "Hamina LNG"),
    ("FI_in", "FI-TSO-0003", "LNG-00072", "entry", "Inkoo LNG"),
    ("FI_out", "FI-TSO-0003", "ITP-00550", "exit", "Balticconnector (FI→EE)"),
    # Lithuania: balance (no consumption point reported)
    ("LT_in", "LT-TSO-0001", "LNG-00030", "entry", "Klaipėda LNG"),
    ("LT_in", "LT-TSO-0001", "ITP-00054", "entry", "Kiemenai (LV→LT)"),
    ("LT_in", "LT-TSO-0001", "ITP-00556", "entry", "Santaka (PL→LT)"),
    ("LT_in", "LT-TSO-0001", "ITP-00085", "entry", "Kotlovka (BY→LT)"),
    ("LT_out_lv", "LT-TSO-0001", "ITP-00054", "exit", "Kiemenai (LT→LV)"),
    ("LT_gipl", "LT-TSO-0001", "ITP-00556", "exit", "Santaka (LT→PL)"),
    ("LT_kal", "LT-TSO-0001", "ITP-00050", "exit", "Sakiai (LT→RU)"),
]
GROUPS = ["EE_cons", "LV_cons", "LV_inj", "FI_in", "FI_out",
          "LT_in", "LT_out_lv", "LT_gipl", "LT_kal"]

COUNTRY_COLS = [
    "Consumption: Finland",
    "Consumption: Estonia",
    "Consumption: Latvia",
    "Consumption: Lithuania",
]
OTHER_COLS = [
    "Inčukalns UGS (Injection)",
    "GIPL Export (LT → PL)",
]


# ---------------------------------------------------------------- data fetch
def _request(params):
    """GET with retries. Raises RuntimeError if all attempts fail."""
    err = "unknown error"
    for attempt in range(4):
        try:
            r = requests.get(URL, params=params, timeout=120)
            if r.status_code == 200:
                return r.json().get("operationalData", [])
            err = f"HTTP {r.status_code}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break  # retrying will not help
        except (requests.RequestException, ValueError) as e:
            err = type(e).__name__
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(err)


def _chunks(start, end):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_series(operator, point, direction, start_str, end_str):
    """Daily Physical Flow (kWh/d) for one operator/point/direction.

    Raises if any chunk fails, so partial results are never cached.
    """
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)

    def one(rng):
        a, b = rng
        return _request({
            "indicator": "Physical Flow",
            "periodType": "day",
            "from": a.isoformat(),
            "to": b.isoformat(),
            "pointKey": point,
            "directionKey": direction,
            "limit": -1,
        })

    with ThreadPoolExecutor(max_workers=4) as ex:
        parts = list(ex.map(one, list(_chunks(start, end))))

    days, dupes = {}, 0
    for recs in parts:
        for x in recs:
            if (x.get("operatorKey") != operator
                    or x.get("pointKey") != point
                    or x.get("directionKey") != direction):
                continue
            day = str(x.get("periodFrom", ""))[:10]
            try:
                value = float(x.get("value") or 0)
            except (TypeError, ValueError):
                value = 0.0
            if day in days:
                dupes += 1
            days[day] = value
    return {"days": days, "dupes": dupes}


def load_all(start_str, end_str):
    frames, failed, dupes = [], [], 0
    bar = st.progress(0.0, text="Loading ENTSOG data...")
    for i, (grp, op, pt, d, label) in enumerate(SERIES):
        bar.progress(i / len(SERIES), text=f"Loading {label} ({i + 1}/{len(SERIES)})")
        try:
            res = fetch_series(op, pt, d, start_str, end_str)
        except Exception as e:
            failed.append(f"{label} [{op} {pt} {d}]: {e}")
            continue
        dupes += res["dupes"]
        if res["days"]:
            frames.append(pd.DataFrame({
                "day": list(res["days"].keys()),
                "TWh": [v / 1e9 for v in res["days"].values()],  # kWh/d -> TWh
                "group": grp,
                "series": f"{grp}: {label}",
            }))
    bar.empty()
    if frames:
        raw = pd.concat(frames, ignore_index=True)
    else:
        raw = pd.DataFrame(columns=["day", "TWh", "group", "series"])
    return raw, failed, dupes


def build_monthly(raw):
    raw = raw.copy()
    raw["Month"] = raw["day"].str[:7]
    g = raw.pivot_table(index="Month", columns="group", values="TWh", aggfunc="sum")
    g = g.reindex(columns=GROUPS).fillna(0.0)

    out = pd.DataFrame(index=g.index)
    out["Consumption: Finland"] = g["FI_in"] - g["FI_out"]
    out["Consumption: Estonia"] = g["EE_cons"]
    out["Consumption: Latvia"] = g["LV_cons"]
    out["Consumption: Lithuania"] = (g["LT_in"] - g["LT_out_lv"]
                                     - g["LT_gipl"] - g["LT_kal"])
    out["Inčukalns UGS (Injection)"] = g["LV_inj"]
    out["GIPL Export (LT → PL)"] = g["LT_gipl"]
    return out.sort_index()


# ---------------------------------------------------------------------- UI
st.title("📊 FinBalt Regional Gas Demand")
st.markdown(
    "Monthly gas demand in **Finland, Estonia, Latvia and Lithuania** from the "
    "ENTSOG Transparency Platform (daily Physical Flow), plus Inčukalns storage "
    "injection and exports."
)

st.sidebar.header("Settings")
months_to_show = st.sidebar.slider("Time period (months):", 3, 24, 12, 1)
if st.sidebar.button("Clear cache & refresh 🔄"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(
    "Data is cached for 6 hours. Series that fail to load are retried on the "
    "next refresh; successful ones stay cached."
)

today = date.today()
start_date = (pd.Timestamp(today.replace(day=1)) - pd.DateOffset(months=24)).date()
raw, failed, dupes = load_all(start_date.isoformat(), today.isoformat())

if failed:
    st.warning(
        "Some ENTSOG series could not be loaded, so the figures below may be "
        "incomplete. Press the refresh button to retry (the rest is cached)."
    )
    with st.expander("Failed series"):
        for f in failed:
            st.write(f)

if raw.empty:
    st.error("No flow data retrieved from ENTSOG.")
    st.stop()

monthly = build_monthly(raw)
df_display = monthly.tail(months_to_show)[COUNTRY_COLS + OTHER_COLS]

last_day = date.fromisoformat(raw["day"].max())
latest = df_display.index[-1]
month_end = pd.Period(latest, freq="M").end_time.date()
partial = last_day < month_end

# --- KPI cards
label = f"{latest} (partial, data through {last_day})" if partial else latest
st.subheader(f"Latest month overview: {label}")
row = df_display.loc[latest]
k = st.columns(6)
k[0].metric("Total consumption (4 countries)", f"{row[COUNTRY_COLS].sum():.1f} TWh")
k[1].metric("Finland", f"{row['Consumption: Finland']:.1f} TWh")
k[2].metric("Estonia", f"{row['Consumption: Estonia']:.1f} TWh")
k[3].metric("Latvia", f"{row['Consumption: Latvia']:.1f} TWh")
k[4].metric("Lithuania", f"{row['Consumption: Lithuania']:.1f} TWh")
k[5].metric("Storage + exports", f"{row[OTHER_COLS].sum():.1f} TWh")

st.markdown("---")

# --- Chart
st.subheader("Monthly demand breakdown (TWh)")
plot_df = (df_display.reset_index()
           .rename(columns={"index": "Month"})
           .melt(id_vars="Month", var_name="Component", value_name="TWh"))
fig = px.bar(
    plot_df, x="Month", y="TWh", color="Component",
    title=f"FinBalt gas demand by component (last {months_to_show} months)",
    labels={"TWh": "Energy (TWh / month)"},
    template="plotly_white",
)
fig.update_layout(barmode="stack", xaxis_tickangle=-45, height=520,
                  legend_title_text="Component", hovermode="x unified")
fig.update_yaxes(tickformat=".1f")
st.plotly_chart(fig, use_container_width=True)

if (df_display["Consumption: Lithuania"] < 0).any() or \
        (df_display["Consumption: Finland"] < 0).any():
    st.caption(
        "Note: Finland and Lithuania are calculated as balances, so a single "
        "month can come out low or even negative because of linepack changes."
    )

# --- Table and download
st.subheader("Summary table (TWh)")
table = df_display.copy()
table["Total consumption (4 countries)"] = table[COUNTRY_COLS].sum(axis=1)
table["Total incl. storage & exports"] = table[COUNTRY_COLS + OTHER_COLS].sum(axis=1)
st.dataframe(table.style.format("{:.1f}"), use_container_width=True)

st.download_button(
    "Download CSV 📥",
    table.round(1).to_csv().encode("utf-8"),
    file_name=f"finbalt_gas_demand_{latest}.csv",
    mime="text/csv",
)

# --- Method and data quality
with st.expander("Method and caveats"):
    st.markdown(
        """
**Source:** ENTSOG Transparency Platform, indicator *Physical Flow*, daily values
(kWh/d), summed per month and converted to TWh.

- **Estonia, Latvia:** ENTSOG aggregated *Final consumers* exit points
  (`FNC-00037`, `FNC-00205`). Read directly, no calculation.
- **Finland:** no consumption point is reported, so it is a balance:
  Imatra + Balticconnector (EE→FI) + Hamina LNG + Inkoo LNG − Balticconnector (FI→EE).
- **Lithuania:** no consumption point is reported, so it is a balance:
  Klaipėda LNG + Kiemenai (LV→LT) + Santaka (PL→LT) + Kotlovka − Kiemenai (LT→LV)
  − Santaka (LT→PL) − Sakiai (LT→RU). Sakiai is Kaliningrad transit: it is subtracted from the balance but not shown.
- **Inčukalns:** storage *injection* only (exit direction). Withdrawal is not demand.
- Balances ignore linepack changes, losses and domestic biogas, so individual months
  are approximate.
- Estonia and Latvia share a balancing zone and no flow between them is reported,
  so they can only be cross-checked together (the sum of the two consumption points
  matched the combined balance closely when checked).
- The latest month is incomplete until the month ends.
        """
    )

with st.expander("Data quality"):
    st.write(f"Duplicate daily records dropped: {dupes}")
    tail_months = sorted(raw["day"].str[:7].unique())[-months_to_show:]
    sub = raw[raw["day"].str[:7].isin(tail_months)].copy()
    sub["Month"] = sub["day"].str[:7]
    st.markdown("**Days of data per month and series**")
    st.dataframe(sub.groupby(["Month", "series"]).size().unstack("series").fillna(0).astype(int),
                 use_container_width=True)
    st.markdown("**Monthly TWh per series**")
    st.dataframe(sub.pivot_table(index="Month", columns="series", values="TWh",
                                 aggfunc="sum").fillna(0).style.format("{:.3f}"),
                 use_container_width=True)
