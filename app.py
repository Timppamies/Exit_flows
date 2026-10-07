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


# --- 2. ENTSOG DATA: Pätkävälimuisti (Kuukauden jaksot) ---
@st.cache_data(ttl=86400, show_spinner=False)
# Määritetään tarvittavat ENTSOG-pisteet (Inčukalns UGS injection + GIPL Santaka exit)
TARGET_DEMAND_POINTS = [
    'LV-TP-0001', # Inčukalns UGS
    'LT-TP-0002'  # GIPL Santaka (LT -> PL)
]

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_entsog_demand_chunk(from_str, to_str):
    """
    Hakee vain FinBalt-kysynnän kannalta oleelliset exit-pisteet.
    Suodatus suoraan API-tasolla lyhentää latausajan minuuteista sekunteihin.
    """
    url = "https://transparency.entsog.eu/api/v1/operationalData.json"
    chunk_data = []
    
    # Tehdään täsmähaku vain valituille pisteille
    for point in TARGET_DEMAND_POINTS:
        offset = 0
        limit = 5000
        
        while True:
            params = {
                'indicator': 'Physical Flow',
                'from': from_str,
                'to': to_str,
                'limit': limit,
                'offset': offset,
                'directionKey': 'exit',
                'pointKey': point  # Tärkeä: hakee vain Inčukalnsin tai GIPL:n dataa
            }
            
            try:
                response = requests.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json().get('operationalData', [])
                    if not data:
                        break
                    chunk_data.extend(data)
                    if len(data) < limit:
                        break
                    offset += limit
                else:
                    break
            except Exception:
                break
                
    return chunk_data

@st.cache_data(ttl=86400, show_spinner="Fetching ENTSOG gas demand flows (24 months)...")
def fetch_full_entsog_history():
    """
    Kokoaa 24 kuukauden historiatiedot kuukauden (30 pv) erissä.
    Tämä suoritetaan vain kerran 24 tunnissa.
    """
    today = datetime.today()
    first_day_current_month = today.replace(day=1)
    start_dt = (first_day_current_month - timedelta(days=24 * 31)).replace(day=1)
    
    current_start = start_dt
    all_records = []
    
    # Haetaan 30 päivän blokkeina 14 päivän sijaan (puolittaa pyyntöjen määrän)
    while current_start < today:
        current_end = min(current_start + timedelta(days=30), today)
        from_str = current_start.strftime('%Y-%m-%d')
        to_str = current_end.strftime('%Y-%m-%d')
        
        chunk = fetch_entsog_demand_chunk(from_str, to_str)
        all_records.extend(chunk)
        
        current_start = current_end + timedelta(days=1)
        
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

st.title("📈 FinBalt Regional Gas Demand")
st.markdown("Total gas demand across Finland, Estonia, Latvia, and Lithuania. Combines **regional end-use consumption (Eurostat)** with **Inčukalns storage injection** and **GIPL export to Poland (ENTSOG)**.")

# Sivupalkki
st.sidebar.header("Settings")
months_to_show = st.sidebar.slider("Select time period (months):", min_value=3, max_value=24, value=12, step=1)

if st.sidebar.button("Clear Cache & Refresh 🔄"):
    st.cache_data.clear()
    st.rerun()

# Haetaan koko historia muistiin taustalla (tapahtuu vain kerran, ei kaadu sliderista)
df_consumption = fetch_eurostat_consumption()
df_entsog_raw = fetch_full_entsog_history()

if df_entsog_raw.empty:
    st.warning("No flow data retrieved from ENTSOG. Please try clicking 'Clear Cache & Refresh'.")
else:
    # Luokitellaan exit-virrat
    df_entsog_raw['Category'] = df_entsog_raw.apply(classify_demand_flow, axis=1)
    df_entsog_filtered = df_entsog_raw.dropna(subset=['Category']).copy()
    
    if df_entsog_filtered.empty:
        st.warning("No matching demand exit flows found.")
    else:
        date_candidates = ['periodFrom', 'gasDayStart', 'periodStart', 'gasDayStartedOn']
        date_col = next((c for c in date_candidates if c in df_entsog_filtered.columns), None)
        if not date_col:
            date_col = next((c for c in df_entsog_filtered.columns if 'period' in c.lower() or 'date' in c.lower()), None)

        df_entsog_filtered['value'] = pd.to_numeric(df_entsog_filtered['value'], errors='coerce').fillna(0)
        df_entsog_filtered['Date_Parsed'] = pd.to_datetime(df_entsog_filtered[date_col], utc=True)
        df_entsog_filtered['Month'] = df_entsog_filtered['Date_Parsed'].dt.strftime('%Y-%m')
        
        entsog_summary = df_entsog_filtered.groupby(['Month', 'Category'])['value'].sum().reset_index()
        entsog_summary['Value_TWh'] = entsog_summary['value'] / 1e9
        
        entsog_pivot = entsog_summary.pivot(index='Month', columns='Category', values='Value_TWh').fillna(0)
        
        # Yhdistetään Eurostatin kulutus ja ENTSOG exit-virrat
        combined_df = pd.merge(df_consumption, entsog_pivot, on='Month', how='inner')
        combined_df.set_index('Month', inplace=True)
        
        demand_cols = ['Combined Regional Consumption', 'Inčukalns UGS (Injection)', 'GIPL Export (LT -> PL)']
        for col in demand_cols:
            if col not in combined_df.columns:
                combined_df[col] = 0.0
                
        # Slider vain leikkaa valmiiksi muistissa olevaa taulukkoa (0 viivettä, ei kaadu)
        df_display = combined_df[demand_cols].tail(months_to_show)
        
        # --- KPI-KORTIT ---
        latest_month = df_display.index[-1]
        latest_total_demand = df_display.loc[latest_month].sum()
        
        st.subheader(f"Latest Month Demand Overview ({latest_month})")
        kpi_cols = st.columns(4)
        kpi_cols[0].metric(label="Total Market Demand", value=f"{latest_total_demand:.3f} TWh")
        kpi_cols[1].metric(label="Regional Consumption", value=f"{df_display.loc[latest_month, 'Combined Regional Consumption']:.3f} TWh")
        kpi_cols[2].metric(label="Inčukalns Injection", value=f"{df_display.loc[latest_month, 'Inčukalns UGS (Injection)']:.3f} TWh")
        kpi_cols[3].metric(label="GIPL Export", value=f"{df_display.loc[latest_month, 'GIPL Export (LT -> PL)']:.3f} TWh")
        
        st.markdown("---")
        
        # --- PLOTLY GRAAFI ---
        st.subheader("Monthly Market Demand Breakdown (TWh)")
        
        plot_df = df_display.reset_index().melt(id_vars='Month', var_name='Demand Component', value_name='TWh')
        
        fig = px.bar(
            plot_df, 
            x='Month', 
            y='TWh', 
            color='Demand Component',
            title=f"FinBalt Market Demand Breakdown (Last {months_to_show} Months)",
            labels={'TWh': 'Energy (TWh / month)', 'Month': 'Month'},
            template='plotly_white',
            color_discrete_map={
                'Combined Regional Consumption': '#1f77b4',
                'Inčukalns UGS (Injection)': '#ff7f0e',
                'GIPL Export (LT -> PL)': '#2ca02c'
            }
        )
        
        fig.update_layout(
            barmode='stack',
            xaxis_tickangle=-45,
            legend_title_text='Demand Component',
            height=520,
            hovermode="x unified"
        )
        
        st.plotly_chart(fig, use_container_width=True)
        
        # --- TAULUKKO JA LATAUS ---
        st.subheader("Demand Summary Table")
        
        display_df = df_display.copy()
        display_df['Total Demand (TWh)'] = display_df.sum(axis=1)
        
        st.dataframe(display_df.style.format("{:.3f}"), use_container_width=True)
        
        csv_data = display_df.to_csv().encode('utf-8')
        st.download_button(
            label="Download Demand Data as CSV 📥",
            data=csv_data,
            file_name=f"finbalt_gas_demand_{latest_month}.csv",
            mime="text/csv"
        )
