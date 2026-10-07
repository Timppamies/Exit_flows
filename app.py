import streamlit as st
import requests
import pandas as pd
import eurostat
import plotly.express as px
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings("ignore")

st.set_page_config(
    page_title="FinBalt Regional Gas Demand",
    page_icon="📊",
    layout="wide"
)

# Määritetään kohdepisteet ilman liian tiukkaa operatorKey-suodatusta
TARGET_EXIT_POINTS = [
    'LV-TP-0001', # Inčukalns UGS
    'LT-TP-0002', # GIPL Santaka (LT -> PL)
    'LT-TP-0001'  # GIPL/Kiemenai vaihtoehtoinen piste varmistukseksi
]

# --- 1. EUROSTAT DATA: Alueellinen Kulutus ---
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_eurostat_consumption():
    try:
        df_raw = eurostat.get_data_df("nrg_cb_gasm")
        df_raw = df_raw.reset_index()
        df_raw.columns = [str(c).split('\\')[-1].split(',')[-1].strip().lower() for c in df_raw.columns]
        
        geo_col = 'time_period' if 'time_period' in df_raw.columns else 'geo'
        countries = {'FI': 'Finland', 'EE': 'Estonia', 'LV': 'Latvia', 'LT': 'Lithuania'}
        
        mask = (
            df_raw[geo_col].isin(countries.keys()) & 
            (df_raw['siec'] == 'G3000') & 
            (df_raw['nrg_bal'] == 'IC_OBS')
        )
        df_filtered = df_raw[mask].copy()
        
        is_tj = 'unit' in df_filtered.columns and 'TJ_GCV' in df_filtered['unit'].values
        if is_tj:
            df_filtered = df_filtered[df_filtered['unit'] == 'TJ_GCV']
        else:
            df_filtered = df_filtered[df_filtered['unit'] == 'MIO_M3']
            
        date_cols = [c for c in df_filtered.columns if pd.Series(c).astype(str).str.match(r'^\d{4}-\d{2}$').any()]
        
        df_melted = df_filtered.melt(id_vars=[geo_col], value_vars=date_cols, var_name='Month', value_name='Volume_Raw')
        df_melted['Volume_Num'] = pd.to_numeric(df_melted['Volume_Raw'], errors='coerce').fillna(0)
        
        if is_tj:
            df_melted['Value_TWh'] = df_melted['Volume_Num'] * 0.000277778
        else:
            df_melted['Value_TWh'] = (df_melted['Volume_Num'] * 10.55) / 1000
            
        df_melted['Month'] = df_melted['Month'].astype(str)
        df_melted = df_melted[df_melted['Month'].str.match(r'^\d{4}-\d{2}$')]
        
        regional_consumption = df_melted.groupby('Month')['Value_TWh'].sum().reset_index()
        regional_consumption.rename(columns={'Value_TWh': 'Combined Regional Consumption'}, inplace=True)
        return regional_consumption
    except Exception as e:
        st.error(f"Error fetching Eurostat data: {e}")
        return pd.DataFrame(columns=['Month', 'Combined Regional Consumption'])


# --- 2. ENTSOG DATA: Luotettava täsmähaku pointKey-pisteillä ---
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_entsog_demand_fast(start_date_str, end_date_str):
    """
    Hakee 24kk historiatiedot suoraan valituista exit-pisteistä.
    Aikaa kuluu noin 3-5 sekuntia ja tulokset löytyvät varmasti.
    """
    url = "https://transparency.entsog.eu/api/v1/operationalData.json"
    all_records = []
    
    for point_key in TARGET_EXIT_POINTS:
        offset = 0
        limit = 5000
        
        while True:
            params = {
                'indicator': 'Physical Flow',
                'from': start_date_str,
                'to': end_date_str,
                'limit': limit,
                'offset': offset,
                'directionKey': 'exit',
                'pointKey': point_key
            }
            
            try:
                response = requests.get(url, params=params, timeout=15)
                if response.status_code == 200:
                    data = response.json().get('operationalData', [])
                    if not data:
                        break
                    all_records.extend(data)
                    if len(data) < limit:
                        break
                    offset += limit
                else:
                    break
            except Exception:
                break
                
    return pd.DataFrame(all_records)


def classify_demand_flow(row):
    point_label = str(row.get('pointLabel', '')).lower()
    direction = str(row.get('directionKey', '')).lower()
    
    if direction == 'exit':
        if 'incukalns' in point_label or 'inčukalns' in point_label:
            return 'Inčukalns UGS (Injection)'
        elif 'gipl' in point_label or 'santaka' in point_label:
            return 'GIPL Export (LT -> PL)'
            
    return None


# --- 3. KÄYTTÖLIITTYMÄ (STREAMLIT UI) ---

st.title("📈
