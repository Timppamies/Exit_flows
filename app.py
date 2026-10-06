import streamlit as st
import requests
import pandas as pd
import eurostat
import plotly.express as px
from datetime import datetime, timedelta
import warnings
import time

warnings.filterwarnings("ignore")

# Sovelluksen konfiguraatio
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
        
        # Yksikkömuunnos -> TWh
        if is_tj:
            df_melted['Value_TWh'] = df_melted['Volume_Num'] * 0.000277778
        else:
            df_melted['Value_TWh'] = (df_melted['Volume_Num'] * 10.55) / 1000
            
        df_melted['Month'] = df_melted['Month'].astype(str)
        df_melted = df_melted[df_melted['Month'].str.match(r'^\d{4}-\d{2}$')]
        
        # Summataan koko alueen kulutus kuukausittain
        regional_consumption = df_melted.groupby('Month')['Value_TWh'].sum().reset_index()
        regional_consumption.rename(columns={'Value_TWh': 'Combined Regional Consumption'}, inplace=True)
        return regional_consumption
    except Exception as e:
        st.error(f"Error fetching Eurostat data: {e}")
        return pd.DataFrame(columns=['Month', 'Combined Regional Consumption'])


# --- 2. ENTSOG DATA: UGS Injection ja GIPL Export ---
@st.cache_data(ttl=14400, show_spinner=False)
def fetch_entsog_demand_flows(start_date_str, end_date_str):
    url = "https://transparency.entsog.eu/api/v1/operationalData.json"
    
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
    
    all_data = []
    current_start = start_dt
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total_days = (end_dt - start_dt).days or 1
    
    while current_start < end_dt:
        current_end = min(current_start + timedelta(days=14), end_dt)
        from_str = current_start.strftime('%Y-%m-%d')
        to_str = current_end.strftime('%Y-%m-%d')
        
        elapsed_days = (current_start - start_dt).days
        progress_bar.progress(min(elapsed_days / total_days, 1.0))
        status_text.text(f"Fetching ENTSOG demand flows: {from_str} to {to_str}...")
        
        offset = 0
        limit = 5000
        
        while True:
            params = {
                'indicator': 'Physical Flow',
                'from': from_str,
                'to': to_str,
                'limit': limit,
                'offset': offset
            }
            
            max_retries = 3
            success = False
            
            for attempt in range(1, max_retries + 1):
                try:
                    response = requests.get(url, params=params, timeout=25)
                    if response.status_code == 200:
                        data = response.json().get('operationalData', [])
                        if data:
                            all_data.extend(data)
                            if len(data) >= limit:
                                offset += limit
                            else:
                                success = True
                                break
                        else:
                            success = True
                            break
                    elif response.status_code == 404:
                        success = True
                        break
                    else:
                        time.sleep(attempt)
                except (requests.exceptions.Timeout, requests.exceptions.RequestException):
                    time.sleep(attempt)
            
            if not success or (response.status_code == 200 and len(data) < limit) or response.status_code == 404:
                break
                
        current_start = current_end + timedelta(days=1)
        
    progress_bar.empty()
    status_text.empty()
    return pd.DataFrame(all_data)


def classify_demand_flow(row):
    point_label = str(row.get('pointLabel', '')).lower()
    direction = str(row.get('directionKey', '')).lower()
    
    # 1. Inčukalns UGS täyttö / syöttö varastoon (Exit verkosta varastoon)
    if 'incukalns' in point_label or 'inčukalns' in point_label:
        if direction == 'exit':
            return 'Inčukalns UGS (Injection)'
            
    # 2. GIPL vienti (Liettuasta Puolaan / Exit verkosta)
    elif 'gipl' in point_label or 'santaka' in point_label:
        if direction == 'exit':
            return 'GIPL Export (LT -> PL)'
            
    return None


# --- 3. KÄYTTÖLIITTYMÄ (STREAMLIT UI) ---

st.title("📈 FinBalt Regional Gas Demand")
st.markdown("Total gas demand across Finland, Estonia, Latvia, and Lithuania. Combines **regional end-use consumption (Eurostat)** with **Inčukalns storage injection** and **GIPL export to Poland (ENTSOG)**.")

# Sivupalkki
st.sidebar.header("Settings")
months_to_show = st.sidebar.slider("Select time period (months):", min_value=3, max_value=24, value=12, step=1)

if st.sidebar.button("Refresh Data 🔄"):
    st.cache_data.clear()

today = datetime.today()
first_day_current_month = today.replace(day=1)
start_dt = (first_day_current_month - timedelta(days=months_to_show * 31)).replace(day=1)

start_date = start_dt.strftime('%Y-%m-%d')
end_date = today.strftime('%Y-%m-%d')

# Datan haku
df_consumption = fetch_eurostat_consumption()
df_entsog_raw = fetch_entsog_demand_flows(start_date, end_date)

if df_entsog_raw.empty:
    st.warning("No flow data retrieved from ENTSOG. Please try refreshing.")
else:
    # Luokitellaan ENTSOG exit-virrat
    df_entsog_raw['Category'] = df_entsog_raw.apply(classify_demand_flow, axis=1)
    df_entsog_filtered = df_entsog_raw.dropna(subset=['Category']).copy()
    
    date_candidates = ['periodFrom', 'gasDayStart', 'periodStart', 'gasDayStartedOn']
    date_col = next((c for c in date_candidates if c in df_entsog_filtered.columns), None)
    if not date_col:
        date_col = next((c for c in df_entsog_filtered.columns if 'period' in c.lower() or 'date' in c.lower()), None)

    df_entsog_filtered['value'] = pd.to_numeric(df_entsog_filtered['value'], errors='coerce').fillna(0)
    df_entsog_filtered['Date_Parsed'] = pd.to_datetime(df_entsog_filtered[date_col], utc=True)
    df_entsog_filtered['Month'] = df_entsog_filtered['Date_Parsed'].dt.strftime('%Y-%m')
    
    # ENTSOG ryhmittely kuukausittain
    entsog_summary = df_entsog_filtered.groupby(['Month', 'Category'])['value'].sum().reset_index()
    entsog_summary['Value_TWh'] = entsog_summary['value'] / 1e9
    
    entsog_pivot = entsog_summary.pivot(index='Month', columns='Category', values='Value_TWh').fillna(0)
    
    # Yhdistetään Eurostatin kulutus ja ENTSOG exit-virrat
    combined_df = pd.merge(df_consumption, entsog_pivot, on='Month', how='inner')
    combined_df.set_index('Month', inplace=True)
    
    # Varmistetaan sarakkeet
    demand_cols = ['Combined Regional Consumption', 'Inčukalns UGS (Injection)', 'GIPL Export (LT -> PL)']
    for col in demand_cols:
        if col not in combined_df.columns:
            combined_df[col] = 0.0
            
    combined_df = combined_df[demand_cols].tail(months_to_show)
    
    # --- KPI-KORTIT ---
    latest_month = combined_df.index[-1]
    latest_total_demand = combined_df.loc[latest_month].sum()
    
    st.subheader(f"Latest Month Demand Overview ({latest_month})")
    kpi_cols = st.columns(4)
    kpi_cols[0].metric(label="Total Market Demand", value=f"{latest_total_demand:.3f} TWh")
    kpi_cols[1].metric(label="Regional Consumption", value=f"{combined_df.loc[latest_month, 'Combined Regional Consumption']:.3f} TWh")
    kpi_cols[2].metric(label="Inčukalns Injection", value=f"{combined_df.loc[latest_month, 'Inčukalns UGS (Injection)']:.3f} TWh")
    kpi_cols[3].metric(label="GIPL Export", value=f"{combined_df.loc[latest_month, 'GIPL Export (LT -> PL)']:.3f} TWh")
    
    st.markdown("---")
    
    # --- PLOTLY GRAAFI ---
    st.subheader("Monthly Market Demand Breakdown (TWh)")
    
    plot_df = combined_df.reset_index().melt(id_vars='Month', var_name='Demand Component', value_name='TWh')
    
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
    
    display_df = combined_df.copy()
    display_df['Total Demand (TWh)'] = display_df.sum(axis=1)
    
    st.dataframe(display_df.style.format("{:.3f}"), use_container_width=True)
    
    csv_data = display_df.to_csv().encode('utf-8')
    st.download_button(
        label="Download Demand Data as CSV 📥",
        data=csv_data,
        file_name=f"finbalt_gas_demand_{latest_month}.csv",
        mime="text/csv"
    )
