"""Mirror public Bondio market data unchanged. No account or broker credentials."""
import argparse, concurrent.futures, json, pathlib, re, urllib.request
p = argparse.ArgumentParser(); p.add_argument('--calendars', action='store_true'); args = p.parse_args()
root = pathlib.Path(__file__).resolve().parents[1]
base = 'https://bondio-data.tipuck.chatgpt.site'
def get(path):
    with urllib.request.urlopen(base + path, timeout=25) as response:
        raw = response.read(4 * 1024 * 1024)
    return json.loads(raw)
def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp'); temp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'); temp.replace(path)
data = get('/api/bonds')
assert isinstance(data.get('rows'), list) and len(data['rows']) >= 50 and data.get('asOf')
save(root / 'data/bonds.json', data)
if args.calendars:
    def calendar(row):
        isin = row.get('isin', '')
        if not re.fullmatch(r'(?:RU|SU)[A-Z0-9]{10}', isin): return
        try:
            value = get('/api/bond/' + isin + '/cashflows')
            if value.get('isin') != isin or not value.get('coupons') or not value.get('asOf'): return
            save(root / 'data/calendars' / (isin + '.json'), value)
        except Exception:
            print('Previous calendar retained:', isin)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(calendar, data['rows']))
print('Original source timestamp:', data['asOf'], 'rows:', len(data['rows']))
