# 策略标签。规则按 InStock 的实现移植（Apache-2.0），不是按注释里的简写。
# 来源：https://github.com/myhhub/stock
#   instock/core/strategy/enter.py                  放量上涨 check_volume
#   instock/core/strategy/keep_increasing.py        均线多头 check
#   instock/core/strategy/parking_apron.py          停机坪 check / check_internal
#   instock/core/strategy/turtle_trade.py           停机坪依赖的 check_enter（不单独展示）
#   instock/core/strategy/backtrace_ma250.py        回踩年线 check
#   instock/core/strategy/breakthrough_platform.py  突破平台 check
#   instock/core/strategy/low_backtrace_increase.py 无大幅回撤 check
# 均线用算术平均，对应原代码里的 talib.MA；成交量单位是股，成交额用收盘价×成交量。
# 无大幅回撤的原实现把前一日开盘价初值设为 -1000000，第一根 K 线的
# (收盘-前开)/前开 恒小于 -10%，规则会永远返回假。这里只在前开盘价为正时才做这两日比较，
# 与同函数里 previous_p_change=100 用来跳过首日的写法对齐。其余比较式未改。
# 只给候选池做辅助标签。命中与否不写回正式四王，也不删除股票。
import datetime as dt

RULES = [
    {"name": "放量上涨", "text": "当日涨跌幅≥2%且收盘≥开盘，收盘价×成交量≥2亿，当日成交量≥前一日5日均量的2倍"},
    {"name": "停机坪", "text": "近15日内有涨幅超过9.5%且收盘为当时近15日最高；其后3日高开、收盘高于该日，且相对开盘价偏离小于3%，后两日涨跌幅在±5%内"},
    {"name": "回踩年线", "text": "近60日最高收盘之前自年线下方上穿，之后收在年线上；最高价到回踩低点间隔10至50个自然日，高点量能大于低点2倍且低点不足高点的80%"},
    {"name": "突破平台", "text": "60日内某日开盘低于60日均线、收盘不低于该均线，且当日满足放量上涨；此前收盘相对60日均线的偏离在-5%到20%之间"},
    {"name": "无大幅回撤", "text": "60日涨幅不低于60%，且没有单日跌幅超7%、高开低走超7%、两日累计跌幅超10%或两日高开低走超10%"},
    {"name": "均线多头", "text": "最近30根K线的30日均线在第1、11、21、30根上严格抬高，且最新一根比窗口起点高出20%以上"},
]
_FUNCS = None


def _until(bars, end_date):
    if not end_date:
        return list(bars)
    return [b for b in bars if b["date"] <= end_date]


def _sma(values, n):
    out, total = [], 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else 0.0)
    return out


def check_volume(bars, end_date=None, threshold=60):
    data = _until(bars, end_date)
    if len(data) < threshold:
        return False
    last = data[-1]
    if last["p_change"] < 2 or last["close"] < last["open"]:
        return False
    ma = _sma([b["volume"] for b in data], 5)
    if len(data) < threshold + 1:
        return False
    last_close = data[-1]["close"]
    last_vol = data[-1]["volume"]
    if last_close * last_vol < 200000000:
        return False
    mean_vol = ma[-2]
    if mean_vol <= 0:
        return False
    return last_vol / mean_vol >= 2


def check_keep(bars, end_date=None, threshold=30):
    data = _until(bars, end_date)
    if len(data) < threshold:
        return False
    ma = _sma([b["close"] for b in data], 30)
    window = ma[-threshold:]
    step1 = round(threshold / 3)
    step2 = round(threshold * 2 / 3)
    a, b, c, d = window[0], window[step1], window[step2], window[-1]
    return a < b < c < d and d > 1.2 * a


def check_enter(bars, end_date, threshold=60):
    """海龟入场：截止日收盘不低于当时最近 threshold 根收盘价。停机坪用 threshold=15。"""
    data = _until(bars, end_date)
    if len(data) < threshold:
        return False
    data = data[-threshold:]
    max_price = 0.0
    for bar in data:
        if bar["close"] > max_price:
            max_price = bar["close"]
    return data[-1]["close"] >= max_price


def _parking_internal(data, limit_price, limit_date):
    nxt = [b for b in data if b["date"] > limit_date][:3]
    if len(nxt) < 3:
        return False
    day1 = nxt[0]
    if day1["open"] <= 0:
        return False
    if not (day1["close"] > limit_price and day1["open"] > limit_price and 0.97 < day1["close"] / day1["open"] < 1.03):
        return False
    for bar in nxt[1:]:
        if bar["open"] <= 0:
            return False
        if not (0.97 < bar["close"] / bar["open"] < 1.03 and -5 < bar["p_change"] < 5
                and bar["close"] > limit_price and bar["open"] > limit_price):
            return False
    return True


def check_parking(bars, end_date=None, threshold=15):
    origin = _until(bars, end_date)
    if len(origin) < threshold:
        return False
    data = origin[-threshold:]
    for bar in data:
        if bar["p_change"] > 9.5 and check_enter(origin, bar["date"], threshold):
            if _parking_internal(data, bar["close"], bar["date"]):
                return True
    return False


def check_backtrace(bars, end_date=None, threshold=60):
    data = _until(bars, end_date)
    if len(data) < 250:
        return False
    ma = _sma([b["close"] for b in data], 250)
    paired = list(zip(data, ma))[-threshold:]
    lowest = [1000000.0, 0.0, ""]
    highest = [0.0, 0.0, ""]
    recent = [1000000.0, 0.0, ""]
    for bar, _m in paired:
        if bar["close"] > highest[0]:
            highest = [bar["close"], bar["volume"], bar["date"]]
        elif bar["close"] < lowest[0]:
            lowest = [bar["close"], bar["volume"], bar["date"]]
    if lowest[1] == 0 or highest[1] == 0:
        return False
    front = [(bar, m) for bar, m in paired if bar["date"] < highest[2]]
    end = [(bar, m) for bar, m in paired if bar["date"] >= highest[2]]
    if not front:
        return False
    if not (front[0][0]["close"] < front[0][1] and front[-1][0]["close"] > front[-1][1]):
        return False
    for bar, m in end:
        if bar["close"] < m:
            return False
        if bar["close"] < recent[0]:
            recent = [bar["close"], bar["volume"], bar["date"]]
    if not recent[2] or not highest[2] or recent[1] == 0 or highest[0] == 0:
        return False
    diff = dt.date.fromisoformat(recent[2]) - dt.date.fromisoformat(highest[2])
    if not (dt.timedelta(days=10) <= diff <= dt.timedelta(days=50)):
        return False
    return highest[1] / recent[1] > 2 and recent[0] / highest[0] < 0.8


def check_breakthrough(bars, end_date=None, threshold=60):
    origin = _until(bars, end_date)
    if len(origin) < threshold:
        return False
    ma = _sma([b["close"] for b in origin], 60)
    paired = list(zip(origin, ma))[-threshold:]
    found = None
    for bar, m in paired:
        if bar["open"] < m <= bar["close"] and check_volume(origin, bar["date"], threshold):
            found = bar["date"]
            break
    if found is None:
        return False
    for bar, m in paired:
        if bar["date"] < found and m > 0:
            dev = (m - bar["close"]) / m
            if not (-0.05 < dev < 0.2):
                return False
    return True


def check_low_backtrace(bars, end_date=None, threshold=60):
    data = _until(bars, end_date)
    if len(data) < threshold:
        return False
    data = data[-threshold:]
    base = data[0]["close"]
    if base <= 0:
        return False
    if (data[-1]["close"] - base) / base < 0.6:
        return False
    prev_pc, prev_open = 100.0, -1000000.0
    for bar in data:
        if bar["open"] == 0:
            return False
        intraday = (bar["close"] - bar["open"]) / bar["open"] * 100.0
        two_open = 0.0 if prev_open <= 0 else (bar["close"] - prev_open) / prev_open * 100.0
        if bar["p_change"] < -7 or intraday < -7 or prev_pc + bar["p_change"] < -10 or two_open < -10:
            return False
        prev_pc = bar["p_change"]
        prev_open = bar["open"]
    return True


def _funcs():
    global _FUNCS
    if _FUNCS is None:
        _FUNCS = (
            ("放量上涨", check_volume),
            ("停机坪", check_parking),
            ("回踩年线", check_backtrace),
            ("突破平台", check_breakthrough),
            ("无大幅回撤", check_low_backtrace),
            ("均线多头", check_keep),
        )
    return _FUNCS


def hit_tags(bars, end_date):
    names = []
    for name, fn in _funcs():
        try:
            if fn(bars, end_date):
                names.append(name)
        except Exception:
            continue
    return names


def apply(pool, asof, failed, sources, loader=None):
    """给池内每一行写 strategy_tags / strategy_status。不增删行。"""
    before = [row.get("code") for row in pool]
    if loader is None:
        import marketdata
        loader = marketdata.load_many
    codes = [row.get("code") for row in pool if row.get("code")]
    try:
        got, srcs, note = loader(codes, asof)
    except Exception as e:
        got, srcs, note = {}, [], "策略标签：" + str(e)
    for src in srcs or []:
        if src not in sources:
            sources.append(src)
    if note:
        failed.append(note)
    for row in pool:
        bars = (got or {}).get(row.get("code"))
        if not bars:
            row["strategy_tags"] = []
            row["strategy_status"] = "数据缺失"
        else:
            row["strategy_tags"] = hit_tags(bars, asof)
            row["strategy_status"] = "ok"
    after = [row.get("code") for row in pool]
    if after != before:
        raise RuntimeError("策略标签改变了股票池顺序或成员")
    return pool


def _series(closes, volumes=None, start=dt.date(2024, 1, 2)):
    volumes = volumes or [1000000.0] * len(closes)
    out, prev = [], None
    for i, close in enumerate(closes):
        prev_c = prev if prev is not None else close
        pct = 0.0 if prev is None else (close / prev_c - 1.0) * 100.0
        out.append({
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "open": prev_c, "close": close, "high": max(prev_c, close), "low": min(prev_c, close),
            "volume": volumes[i], "p_change": pct,
        })
        prev = close
    return out


def self_test():
    flat = _series([10.0] * 70)
    assert check_volume(flat) is False
    closes = [10.0] * 69 + [10.3]
    vols = [5000000.0] * 69 + [25000000.0]
    surge = _series(closes, vols)
    surge[-1]["open"] = 10.0
    surge[-1]["p_change"] = 3.0
    assert check_volume(surge) is True
    weak = [dict(b) for b in surge]
    weak[-1] = dict(weak[-1], p_change=1.0)
    assert check_volume(weak) is False

    rise = _series([10.0 * (1.01 ** i) for i in range(90)])
    assert check_keep(rise) is True
    assert check_keep(_series([10.0] * 40)) is False

    assert check_backtrace(_series([10.0] * 200)) is False
    assert check_low_backtrace(_series([10.0] * 60)) is False
    slow = []
    price = 10.0
    for i in range(60):
        price *= 1.009
        slow.append(price)
    assert check_low_backtrace(_series(slow)) is True
    crashed = _series(slow)
    crashed[30] = dict(crashed[30], p_change=-8.0)
    assert check_low_backtrace(crashed) is False

    # 停机坪：第 17 根（下标 16）大涨且是当时 15 日新高，随后三天仍落在最近 15 根里。成交量故意很小。
    park_c = [10.0] * 16 + [11.2, 11.4, 11.5, 11.55]
    park = _series(park_c, [1000.0] * len(park_c))
    park[16]["open"] = 10.0
    park[16]["p_change"] = 12.0
    for bar, op, cl, pct in (
        (park[17], 11.30, 11.40, 1.8),
        (park[18], 11.42, 11.50, 0.9),
        (park[19], 11.48, 11.55, 0.4),
    ):
        bar["open"], bar["close"], bar["p_change"] = op, cl, pct
    assert check_enter(park, park[16]["date"], 15) is True
    assert check_parking(park) is True
    assert check_volume(park) is False

    base = [10.0] * 79
    vols = [8000000.0] * 79
    base.append(10.2)
    vols.append(30000000.0)
    br = _series(base, vols)
    br[-1]["open"] = 9.9
    br[-1]["p_change"] = 2.0
    assert check_breakthrough(br) is True

    pool = [{"code": "000001", "name": "甲"}, {"code": "000002", "name": "乙"}]

    def fake_loader(codes, asof):
        return {"000001": surge}, ["fixture"], "个股日K数据缺失 1 只，策略标签记数据缺失，股票仍保留"

    failed, sources = [], []
    apply(pool, "2024-03-11", failed, sources, loader=fake_loader)
    assert [r["code"] for r in pool] == ["000001", "000002"]
    assert pool[0]["strategy_status"] == "ok" and "放量上涨" in pool[0]["strategy_tags"]
    assert pool[1]["strategy_status"] == "数据缺失" and pool[1]["strategy_tags"] == []
    assert failed and sources == ["fixture"]
    print("strategy self-test ok")


if __name__ == "__main__":
    self_test()
