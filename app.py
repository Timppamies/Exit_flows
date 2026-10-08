import streamlit as st
import requests
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings("ignore")

st.set_page_config(
    page_title="FinBalt Regional Gas Demand - Country Breakdown",
    page_icon="📊",
    layout="wide"
)

# --- 1. ENTSOG DATA: Haetaan kaikki alueen exit-virrat kerralla ilman operaattorirajoitteita ---
@st.cache_data(ttl=86400, show_spinner="Loading ENTSOG regional gas flows...")
def fetch_full_entsog_history():
    url = "https://transparency.entsog.eu/api/v1/operationalData.json"
    all_records = []
    
    today = datetime.today()
    first_day_current_month = today.replace(day=1)
    start_dt = (first_day_current_month - timedelta(days=24 * 31)).replace(day=1)
    
    start_date_str = start_dt.strftime('%Y-%m-%d')
    end_date_str = today.strftime('%Y-%m-%d')
    
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
            # Rajataan maantieteellisesti FinBalt-alueen operaattoreihin tai haetaan suoraan alueen pisteet
            'areaLabel': 'Baltic' # tai haetaan ilman areajakoa, jos halutaan varmistaa kaikki
        }
        
        # Koska haluamme varmasti Suomen (Gasgrid), Viron (Elering), Latvian (Conexus) ja Liettuan (Amber Grid), 
        # haetaan suoraan ilman maarajauksia tai käytetään tunnettuja operaattoreita. Tehdään haku ilman operaattorifiltteriä 
        # ja poimitaan oikeat maamerkinnät.
        del params['areaLabel']
        
        with requests.Session() as s:
            offset = 0
            while True:
                params['offset'] = offset
                try:
                    response = s.get(url, params=params, timeout=20)
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
        
    return pd.DataFrame(all_records)


def classify_demand_flow(row):
    point_key = str(row.get('pointKey', '')).lower()
    point_label = str(row.get('pointLabel', '')).lower()
    operator_key = str(row.get('operatorKey', '')).upper()
    direction = str(row.get('directionKey', '')).lower()
    
    if direction != 'exit':
        return None
        
    # 1. Varaston täyttö (Inčukalns injection)
    if 'incukalns' in point_label or 'inčukalns' in point_label or 'ugs-00029' in point_key:
        return 'Inčukalns UGS (Injection)'
    
    # 2. GIPL vienti Puolaan (Santaka)
    elif 'santaka' in point_label or 'itp-00556' in point_key:
        return 'GIPL Export (LT -> PL)'
        
    # 3. Poistetaan selkeät suuret siirtopisteet, jotka eivät ole loppukulutusta (Sakiai, Kiemenai)
    elif any(x in point_label or x in point_key for x in ['sakiai', 'kiemenai', 'itp-00050', 'itp-00054']):
        return None
        
    # 4. Tunnistetaan maa operaattoriavaimen tai pisteen perusteella
    if 'FI' in operator_key or 'finland' in point_label or 'gasgrid' in str(row.get('operatorLabel', '')).lower() or 'inkoo' in point_label or 'imatra' in point_label:
        return 'Consumption: Finland'
    elif 'EE' in operator_key or 'estonia' in point_label or 'elering' in str(row.get('operatorLabel', '')).lower():
        return 'Consumption: Estonia'
    elif 'LV' in operator_key or 'latvia' in point_label or 'conexus' in str(row.get('operatorLabel', '')).lower():
        return 'Consumption: Latvia'
    elif 'LT' in operator_key or 'lithuania' in point_label or 'amber' in str(row.get('operatorLabel', '')).lower() or 'jaunaičiai' in point_label:
        return 'Consumption: Lithuania'
        
    return None


# --- 2. KÄYTTÖLIITTYMÄ (STREAMLIT UI) ---

st.title("📊 FinBalt Regional Gas Demand - Country Breakdown")
st.markdown("Total gas demand broken down by **Country**, plus Storage (Inčukalns) and Export (GIPL).")

st.sidebar.header("Settings")
months_to_show = st.sidebar.slider("Select time period (months):", min_value=3, max_value=24, value=12, step=1)

if st.sidebar.button("Clear Cache & Refresh 🔄"):
    st.cache_data.clear()
    st.rerun()

df_entsog_raw = fetch_full_entsog_history()

if df_entsog_raw.empty:
    st.warning("No flow data retrieved from ENTSOG.")
else:
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
        
        df_daily = df_entsog_filtered.groupby(['Month', 'Category', 'pointKey', df_entsog_filtered['Date_Parsed'].dt.date], as_index=False)['value'].max()
        
        monthly_summary = df_daily.groupby(['Month', 'Category'])['value'].sum().reset_index()
        monthly_summary['Value_TWh'] = monthly_summary['value'] / 1e9
        
        pivot_df = monthly_summary.pivot(index='Month', columns='Category', values='Value_TWh').fillna(0)
        
        expected_cols = [
            'Consumption: Finland', 
            'Consumption: Estonia', 
            'Consumption: Latvia', 
            'Consumption: Lithuania', 
            'Inčukalns UGS (Injection)', 
            'GIPL Export (LT -> PL)'
        ]
        
        for col in expected_cols:
            if col not in pivot_df.columns:
                pivot_df[col] = 0.0
                
        df_display = pivot_df[expected_cols].tail(months_to_show)
        
        # --- KPI-KORTIT ---
        latest_month = df_display.index[-1]
        latest_total_demand = df_display.loc[latest_month].sum()
        
        st.subheader(f"Latest Month Demand Overview ({latest_month})")
        kpi_cols = st.columns(6)
        kpi_cols[0].metric(label="Total Demand", value=f"{latest_total_demand:.1f} TWh")
        kpi_cols[1].metric(label="Finland", value=f"{df_display.loc[latest_month, 'Consumption: Finland']:.1f} TWh")
        kpi_cols[2].metric(label="Estonia", value=f"{df_display.loc[latest_month, 'Consumption: Estonia']:.1f} TWh")
        kpi_cols[3].metric(label="Latvia", value=f"{df_display.loc[latest_month, 'Consumption: Latvia']:.1f} TWh")
        kpi_cols[4].metric(label="Lithuania", value=f"{df_display.loc[latest_month, 'Consumption: Lithuania']:.1f} TWh")
        kpi_cols[5].metric(label="Inčukalns/GIPL", value=f"{(df_display.loc[latest_month, 'Inčukalns UGS (Injection)'] + df_display.loc[latest_month, 'GIPL Export (LT -> PL)']):.1f} TWh")
        
        st.markdown("---")
        
        # --- PLOTLY GRAAFI ---
        st.subheader("Monthly Market Demand Breakdown by Country (TWh)")
        
        plot_df = df_display.reset_index().melt(id_vars='Month', var_name='Demand Component', value_name='TWh')
        
        fig = px.bar(
            plot_df, 
            x='Month', 
            y='TWh', 
            color='Demand Component',
            title=f"FinBalt Market Demand by Country (Last {months_to_show} Months)",
            labels={'TWh': 'Energy (TWh / month)', 'Month': 'Month'},
            template='plotly_white'
        )
        
        fig.update_traces(texttemplate='%{y:.1f}', textposition='none')
        fig.update_layout(
            barmode='stack',
            xaxis_tickangle=-45,
            legend_title_text='Component',
            height=520,
            hovermode="x unified"
        )
        fig.update_yaxes(tickformat=".1f")
        
        st.plotly_chart(fig, use_container_width=True)
        
        # --- TAULUKKO JA LATAUS ---
        st.subheader("Demand Summary Table by Country")
        
        display_df = df_display.copy()
        display_df['Total Demand (TWh)'] = display_df.sum(axis=1)
        
        st.dataframe(display_df.style.format("{:.1f}"), use_container_width=True)
        
        csv_data = display_df.to_csv().encode('utf-8')
        st.download_button(
            label="Download Country Breakdown CSV 📥",
            data=csv_data,
            file_name=f"finbalt_gas_demand_by_country_{latest_month}.csv",
            mime="text/csv"
        )
