import requests, pandas as pd

ops = ['FI-TSO-0001', 'EE-TSO-0001', 'LV-TSO-0001', 'LT-TSO-0001']
r = requests.get(
    "https://transparency.entsog.eu/api/v1/operatorpointdirections",
    params={'limit': -1}, timeout=120
)
r.raise_for_status()
opd = pd.DataFrame(r.json()['operatorpointdirections'])
print(opd.columns.tolist())

sel = opd[opd['operatorKey'].isin(ops) & (opd['directionKey'] == 'exit')]
extra = [c for c in opd.columns if 'ountry' in c or 'ype' in c]
print(sel[['operatorKey', 'pointKey', 'pointLabel'] + extra].to_string())
