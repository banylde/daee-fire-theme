# 日 K 读取，供策略标签和模拟盘使用。
# 东方财富 push2his 在海外经常被断开，所以探测一次，失败就改用腾讯财经，再失败用新浪。
# 成交量统一成股：东财、腾讯的手乘 100，与 InStock stockfetch.fetch_stock_hist 一致。
# 不保存账户、密钥或真实持仓。
import json, os
from concurrent.futures import ThreadPoolExecutor, as_completed
import common

CACHE = os.path.join(common.ROOT, "data", ".cache")
_EM_OK = None


def norm_code(code):
    s = str(code or "").strip()
    return s.zfill(6) if s.isdigit() else s


def qq_symbol(code):
    code = norm_code(code)
    # 92 是北交所，要排在「9 开头上证」之前；900 仍是上证 B 股。
    if code.startswith(("43", "82", "83", "87", "88", "92", "4", "8")):
        return "bj" + code
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    return "sz" + code


def em_secid(code):
    code = norm_code(code)
    if code.startswith(("43", "82", "83", "87", "88", "92", "4", "8", "0", "1", "2", "3")):
        return "0." + code
    return "1." + code


def _bars_from_ohlcv(rows, volume_in_lots):
    """rows: date, open, close, high, low, volume。涨跌幅按前收计算，第一根为 0。"""
    out, prev = [], None
    for row in rows:
        if not row or len(row) < 6:
            continue
        date = str(row[0])[:10]
        try:
            o, c, h, l, v = (float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
        except (TypeError, ValueError):
            continue
        vol = v * 100.0 if volume_in_lots else v
        pct = 0.0 if not prev else (c / prev - 1.0) * 100.0
        out.append({
            "date": date, "open": o, "close": c, "high": h, "low": l,
            "volume": vol, "p_change": pct,
        })
        prev = c
    return out


def clip_asof(bars, asof):
    if not asof:
        return bars
    return [b for b in bars if b["date"] <= asof]


def em_alive():
    global _EM_OK
    if _EM_OK is not None:
        return _EM_OK
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000300"
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101&lmt=2"
           "&ut=fa5fd1943c7b386f172d6893dbfba10b")
    try:
        j = json.loads(common.get(url, timeout=8, tries=1))
        _EM_OK = bool(((j.get("data") or {}).get("klines")))
    except Exception:
        _EM_OK = False
    return _EM_OK


def _fetch_em(code, asof, lmt):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           "&klt=101&fqt=1&end=%s&lmt=%d&ut=fa5fd1943c7b386f172d6893dbfba10b"
           ) % (em_secid(code), asof.replace("-", ""), lmt)
    j = json.loads(common.get(url, timeout=12, tries=2))
    lines = ((j.get("data") or {}).get("klines")) or []
    rows = []
    for line in lines:
        p = line.split(",")
        if len(p) >= 6:
            rows.append([p[0], p[1], p[2], p[3], p[4], p[5]])
    return clip_asof(_bars_from_ohlcv(rows, True), asof), "eastmoney-kline"


def _fetch_qq(symbol, asof, lmt):
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (symbol, lmt)
    j = json.loads(common.get(url, {"Referer": "https://gu.qq.com/"}, timeout=12, tries=2))
    node = ((j.get("data") or {}).get(symbol)) or {}
    rows = node.get("qfqday") or node.get("day") or []
    return clip_asof(_bars_from_ohlcv(rows, True), asof), "tencent-kline"


def _fetch_sina(symbol, asof, lmt):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d") % (symbol, lmt)
    raw = common.get(url, {"Referer": "https://finance.sina.com.cn/"}, timeout=12, tries=2)
    data = json.loads(raw)
    rows = [[r.get("day"), r.get("open"), r.get("close"), r.get("high"), r.get("low"), r.get("volume")] for r in data or []]
    return clip_asof(_bars_from_ohlcv(rows, False), asof), "sina-kline"


def _cache_path(key, asof):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, "%s-%s.json" % (key, asof))


def load_symbol(symbol, asof, lmt=320, cache_key=None):
    """symbol 为 6 位代码或 sh000300 这类腾讯代码。返回 (bars, source)。"""
    key = cache_key or symbol
    path = _cache_path(key, asof)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("bars"):
                return doc["bars"], doc.get("source") or "cache"
        except Exception:
            pass
    qq = symbol if symbol[:2] in ("sh", "sz", "bj") else qq_symbol(symbol)
    errors = []
    loaders = []
    if em_alive() and qq[2:].isdigit():
        loaders.append(lambda: _fetch_em(qq[2:], asof, lmt))
    loaders.append(lambda: _fetch_qq(qq, asof, lmt))
    loaders.append(lambda: _fetch_sina(qq, asof, lmt))
    for loader in loaders:
        try:
            bars, src = loader()
            if len(bars) >= 2:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump({"source": src, "bars": bars}, f, ensure_ascii=False)
                except Exception:
                    pass
                return bars, src
        except Exception as e:
            errors.append(str(e))
    return None, None


def load_many(codes, asof, lmt=320):
    """返回 (code->bars, sources, error_note)。缺失的代码不在字典里，调用方不得因此删股票。"""
    want = []
    seen = set()
    for code in codes or []:
        code = norm_code(code)
        if code and code not in seen:
            seen.add(code)
            want.append(code)
    out, sources, missing = {}, set(), []

    def job(code):
        bars, src = load_symbol(code, asof, lmt=lmt, cache_key=code)
        return code, bars, src

    if want:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(job, code) for code in want]
            for fut in as_completed(futs):
                code, bars, src = fut.result()
                if bars:
                    out[code] = bars
                    if src and src != "cache":
                        sources.add(src)
                else:
                    missing.append(code)
    note = None
    if missing:
        note = "个股日K数据缺失 %d 只，策略标签记数据缺失，股票仍保留" % len(missing)
    return out, sorted(sources), note


def load_index(qq, asof):
    bars, src = load_symbol(qq, asof, lmt=400, cache_key=qq)
    return bars, src


def self_test():
    rows = _bars_from_ohlcv([
        ["2026-11-10", "10", "10", "10.2", "9.8", "100"],
        ["2026-11-11", "10", "11", "11", "10", "200"],
    ], True)
    assert rows[0]["volume"] == 10000 and rows[0]["p_change"] == 0
    assert abs(rows[1]["p_change"] - 10) < 1e-9
    assert abs(_bars_from_ohlcv([["2026-11-11", "1", "1", "1", "1", "100"]], False)[0]["volume"] - 100) < 1e-9
    assert qq_symbol("600000") == "sh600000"
    assert qq_symbol("002931") == "sz002931"
    assert qq_symbol("920157") == "bj920157"
    assert em_secid("600000") == "1.600000"
    assert clip_asof(rows, "2026-11-10")[0]["date"] == "2026-11-10" and len(clip_asof(rows, "2026-11-10")) == 1
    print("marketdata self-test ok")


if __name__ == "__main__":
    self_test()
