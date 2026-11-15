"""FinBalt Regional Gas Demand: Finland, Estonia, Latvia, Lithuania (ENTSOG).

Data is fetched every evening by GitHub Actions into data/entsog_daily.csv
(fetch_data.py), so the app normally reads it from the repository and calls
the ENTSOG API only if that file is missing or out of date.
Run with:  streamlit run app.py
"""
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from entsog import SERIES
from store import fetch_live, load_flows, source_text

st.set_page_config(page_title="FinBalt Gas Demand", page_icon="📊", layout="wide")

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


# ---------------------------------------------------------------- data load
def load_all(start, end, force_live=False):
    """Stored data (updated every evening), topped up live from ENTSOG if needed."""
    flows, failed_keys, dupes, info = load_flows(start, end, force_live)
    failed = [f"Stored file: {failed_keys[None]}"] if None in failed_keys else []
    frames = []
    for grp, op, pt, d, label in SERIES:
        if (op, pt, d) in failed_keys:
            failed.append(f"{label} [{op} {pt} {d}]: {failed_keys[(op, pt, d)]}")
        days = flows.get((op, pt, d), {})
        if days:
            frames.append(pd.DataFrame({
                "day": list(days.keys()),
                "TWh": [v / 1e9 for v in days.values()],  # kWh/d -> TWh
                "group": grp,
                "series": f"{grp}: {label}",
            }))
    if frames:
        raw = pd.concat(frames, ignore_index=True)
    else:
        raw = pd.DataFrame(columns=["day", "TWh", "group", "series"])
    return raw, failed, dupes, info


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
if st.sidebar.button("Fetch latest from ENTSOG 🔄"):
    fetch_live.clear()
    # Kept for the session, so the live data stays when other settings change.
    st.session_state["force_live"] = True
if st.sidebar.button("Clear cache"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(
    "Data is fetched from ENTSOG into the repository every evening, so the app "
    "opens without waiting for the API. ENTSOG publishes each gas day on the "
    "following day."
)
force_live = st.session_state.get("force_live", False)

today = date.today()
start_date = (pd.Timestamp(today.replace(day=1)) - pd.DateOffset(months=24)).date()
# Stored history starts on 1 January of the window's first year.
raw, failed, dupes, info = load_all(date(start_date.year, 1, 1), today, force_live)

if failed:
    st.warning(
        "Some ENTSOG series could not be loaded, so the figures below may be "
        "incomplete. Press \"Fetch latest from ENTSOG\" to retry."
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
  Balticconnector (EE→FI) + Hamina LNG + Inkoo LNG − Balticconnector (FI→EE).
  Imatra (Russia) is excluded because the border is closed.
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
- The whole period is re-fetched every evening, so ENTSOG's revisions to old
  days show up the next day.
        """
    )

with st.expander("Data quality"):
    st.write(f"Duplicate daily records dropped: {dupes}")
    st.write(source_text(info))
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
