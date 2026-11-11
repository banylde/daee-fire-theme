import gzip, json, os, time, datetime as dt, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BJ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

def now_bj():
    return dt.datetime.now(BJ)

def _read(url, headers=None, data=None, timeout=20, tries=3):
    h = dict(UA)
    if headers:
        h.update(headers)
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
            if len(b) >= 2 and b[:2] == b"\x1f\x8b":
                b = gzip.decompress(b)
            return b
        except Exception as e:
            err = e
            if i + 1 < tries:
                time.sleep(0.6 + i)
    raise err

def get(url, headers=None, timeout=20, tries=3):
    return _read(url, headers=headers, timeout=timeout, tries=tries)

def post_json(url, payload, headers=None, timeout=20, tries=3):
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=UTF-8"}
    if headers:
        h.update(headers)
    return _read(url, headers=h, data=body, timeout=timeout, tries=tries)

def load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)

def save(name, obj):
    path = os.path.join(ROOT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
