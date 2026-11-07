import gzip, json, os, time, datetime as dt, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BJ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

def now_bj():
    return dt.datetime.now(BJ)

def get(url, headers=None, timeout=20, tries=3):
    h = dict(UA)
    if headers:
        h.update(headers)
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
            if len(b) >= 2 and b[:2] == b"\x1f\x8b":
                b = gzip.decompress(b)
            return b
        except Exception as e:
            err = e
            time.sleep(0.6 + i)
    raise err

def load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)

def save(name, obj):
    path = os.path.join(ROOT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
