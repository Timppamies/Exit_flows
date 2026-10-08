import streamlit as st
import requests
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings("ignore")

st.set_page_config(
    page_title="FinBalt Regional Gas Demand - Jan/Feb Debug",
    page_icon="📊",
    layout="wide"
)

# --- 1. ENTSOG DATA: Exit-haku ---
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_entsog_operator_data(operator_key, start_date_str, end_date_str):
    url = "https://transparency.entsog.eu/api/v1/operationalData.json"
    all_records = []
    
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
    
    current_start = start_dt
    while current_start < end_dt:
        current_end = min(current_start + timedelta(days=365), end_dt)
        
        params = {
            'indicator': 'Physical Flow',
            'from': current_start.strftime('%Y-%m-%d'),
            'to': current_end.strftime('%Y-%m-%d'),
            'limit': 5000,
            'offset': 0,
            'directionKey': 'exit',
            'operatorKey': operator_key
        }
        
        with requests.Session() as s:
            offset = 0
            while True:
                params['offset'] = offset
                try:
                    response = s.get(url, params=params, timeout=15)
                    if response.status_code == 200:
                        data = response.json().get('operationalData', [])
                        if not data:
                            break
                        all_records.extend(data)
                        if len(data) < 5000:
                            break
                        offset += 5000
                    else:
                        break
                except Exception:
                    break
                    
        current_start = current_end + timedelta(days=1)
        
    return all_records


@st.cache_data(ttl=86400, show_spinner="Loading ENTSOG regional gas flows...")
def fetch_full_entsog_history():
    today = datetime.today()
    first_day_current_month = today.replace(day=1)
    start_dt = (first_day_current_month - timedelta(days=24 * 31)).replace(day=1)
    
    start_date_str = start_dt.strftime('%Y-%m-%d')
    end_date_str = today.strftime('%Y-%m-%d')
    
    operators = ['FI-TSO-0001', 'LV-TSO-0001', 'LT-TSO-0001', 'EE-TSO-0001']
    all_data = []
    
    for op in operators:
        records = fetch_entsog_operator_data(op, start_date_str, end_date_str)
        all_data.extend(records)
        
    return pd.DataFrame(all_data)


def classify_demand_flow(row):
    point_key = str(row.get('pointKey', '')).lower()
    point_label = str(row.get('pointLabel', '')).lower()
    direction = str(row.get('directionKey', '')).lower()
    
    if direction == 'exit':
        if 'incukalns' in point_label or 'inčukalns' in point_label or 'ugs-00029' in point_key:
            return 'Inčukalns UGS (Injection)'
        elif 'santaka' in point_label or 'itp-00556' in point_key:
            return 'GIPL Export (LT -> PL)'
        elif any(x in point_label or x in point_key for x in ['sakiai', 'kiemenai', 'balticconnector', 'itp-00050', 'itp-00054', 'itp-00550']):
            return None
        else:
            return 'Combined Regional Consumption'
    return None


# --- 2. KÄYTTÖLIITTYMÄ ---

st.title("📊 FinBalt Gas Demand - Tammikuu & Helmikuu 2026 Pisteselvitys")
st.markdown("Tämä näkymä listaa erikseen **tammikuun 2026** ja **helmikuun 2026** kaikkien exit-pisteiden voly，ytsit, jotta näemme mikä piste aiheuttaa ylimääräisen piikin.")

if st.button("Clear Cache & Refresh 🔄"):
    st.cache_data.clear()
    st.rerun()

df_entsog_raw = fetch_full_entsog_history()

if df_entsog_raw.empty:
    st.warning("Ei dataa ENTSOGista.")
else:
    date_candidates = ['periodFrom', 'gasDayStart', 'periodStart', 'gasDayStartedOn']
    date_col = next((c for c in date_candidates if c in df_entsog_raw.columns), None)
    
    df_entsog_raw['value'] = pd.to_numeric(df_entsog_raw['value'], errors='coerce').fillna(0)
    df_entsog_raw['Date_Parsed'] = pd.to_datetime(df_entsog_raw[date_col], utc=True)
    df_entsog_raw['Month'] = df_entsog_raw['Date_Parsed'].dt.strftime('%Y-%m')
    
    # Suodatetaan pelkät tammi- ja helmikuu 2026
    df_jan_feb = df_entsog_raw[df_entsog_raw['Month'].isin(['2026-01', '2026-02'])].copy()
    
    if df_jan_feb.empty:
        st.warning("Ei dataa tammi-helmikuulle 2026.")
    else:
        st.subheader("Tammi- ja helmikuun 2026 Exit-pisteet (GWh / TWh)")
        
        # Ryhmitellään kuukauden, operaattorin, pisteen ja suunnan mukaan
        group_cols = ['Month', 'operatorKey', 'pointKey', 'pointLabel', 'directionKey']
        # Otetaan päiväarvojen summa tai maksimi kuukausitasolla
        df_monthly_points = df_jan_feb.groupby(group_cols, as_index=False)['value'].sum()
        df_monthly_points['Value_TWh'] = df_monthly_points['value'] / 1e9
        df_monthly_points = df_monthly_points.sort_values(by=['Month', 'Value_TWh'], ascending=[True, False])
        
        st.dataframe(df_monthly_points[['Month', 'operatorKey', 'pointKey', 'pointLabel', 'directionKey', 'Value_TWh']], use_container_width=True)
        
        # CSV-latauspainike tälle listaukselle
        csv_bytes = df_monthly_points.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="Lataa Tammi-Helmikuun pisteet CSV-tiedostona 📥",
            data=csv_bytes,
            file_name="finbalt_jan_feb_2026_debug.csv",
            mime="text/csv"
        )
