# 模拟盘。虚拟资金，只根据仓库里的正式调仓记录记账。
# 收盘确认目标权重，下一交易日开盘价成交。涨停开盘不买，跌停开盘不卖，顺延到之后的交易日。
# 成本：佣金万 3（双边）、滑点 0.1%（买在开盘价上、卖在开盘价下，价格四舍五入到分）、卖出印花税 0.05%。
# 不连接券商，不读取真实账户或持仓，不下单。
import common

INITIAL = 1000000.0
COMMISSION = 0.0003
SLIP = 0.001
STAMP = 0.0005
FILL = "信号在信号日收盘后计算，按下一交易日开盘价成交（T+1 open）。涨停买不进，跌停卖不出。"


def round_px(value):
    return int(float(value) * 100 + 0.5) / 100.0


def round_cash(value):
    return int(float(value) * 100 + 0.5) / 100.0


def lot_of(code):
    return 200 if str(code).startswith(("688", "689")) else 100


def limit_pct(code, name):
    """与 scripts/build.py 的涨跌幅口径一致，只用于判断开盘是否封死。"""
    name = name or ""
    if "ST" in name.upper() or "退" in name:
        return 0.05
    if str(code).startswith(("300", "301", "688", "689")):
        return 0.20
    if str(code).startswith(("83", "87", "43", "82", "88", "92")):
        return 0.30
    return 0.10


def limit_prices(prev, code, name):
    pct = limit_pct(code, name)
    return round_px(prev * (1 + pct)), round_px(prev * (1 - pct))


def _quote(prices, code, date):
    return ((prices.get(code) or {}).get(date)) or {}


def _desired_shares(equity, weight, open_px, code):
    lot = lot_of(code)
    fill = round_px(open_px * (1 + SLIP))
    if fill <= 0 or weight <= 0:
        return 0, fill
    raw = equity * (weight / 100.0) / fill
    return int(raw // lot) * lot, fill


def _buy(cash, code, name, shares, open_px, date, since, trades):
    fill = round_px(open_px * (1 + SLIP))
    notional = round_cash(fill * shares)
    commission = round_cash(notional * COMMISSION)
    slip_cash = round_cash((fill - open_px) * shares)
    cash = round_cash(cash - notional - commission)
    note = "按目标权重买入"
    if since and since != date:
        note = "涨停开盘买不进，自 %s 顺延" % since
    trades.append({
        "date": date, "code": code, "name": name, "side": "buy", "shares": shares,
        "open": open_px, "price": fill, "notional": notional, "commission": commission,
        "stamp": 0.0, "slippage": slip_cash, "note": note,
    })
    return cash


def _sell(cash, code, name, shares, open_px, date, since, trades):
    fill = round_px(open_px * (1 - SLIP))
    notional = round_cash(fill * shares)
    commission = round_cash(notional * COMMISSION)
    stamp = round_cash(notional * STAMP)
    slip_cash = round_cash((open_px - fill) * shares)
    cash = round_cash(cash + notional - commission - stamp)
    note = "调出或降权卖出"
    if since and since != date:
        note = "跌停开盘卖不出，自 %s 顺延" % since
    trades.append({
        "date": date, "code": code, "name": name, "side": "sell", "shares": shares,
        "open": open_px, "price": fill, "notional": notional, "commission": commission,
        "stamp": stamp, "slippage": slip_cash, "note": note,
    })
    return cash


def _fifo_close(lots, code, shares, proceeds):
    """按买入成本结清本次卖出，返回已实现盈亏。"""
    book = lots.setdefault(code, [])
    left, cost = shares, 0.0
    while left > 0 and book:
        lot_shares, lot_cost = book[0]
        take = min(left, lot_shares)
        cost += lot_cost * take
        lot_shares -= take
        left -= take
        if lot_shares <= 0:
            book.pop(0)
        else:
            book[0] = [lot_shares, lot_cost]
    return round_cash(proceeds - cost)


def rebalance(date, target, cash, positions, names, prices, lots, blocked_since, trades):
    """把持仓调向 target（代码 -> 权重百分数）。封板或缺少开盘价的一边留下，返回 (cash, still_blocked, blocks)。"""
    equity = cash
    held_codes = list(positions.keys())
    for code in held_codes:
        quote = _quote(prices, code, date)
        mark = quote.get("open")
        if mark is None:
            return cash, True, {code: "开盘价数据缺失"}
        equity += positions[code] * mark
    equity = round_cash(equity)
    plan = {}
    for code, weight in (target or {}).items():
        quote = _quote(prices, code, date)
        open_px = quote.get("open")
        if open_px is None:
            plan[code] = {"shares": None, "open": None, "weight": weight}
        else:
            shares, _fill = _desired_shares(equity, weight, open_px, code)
            plan[code] = {"shares": shares, "open": open_px, "weight": weight}
    blocks = {}
    # 先卖，把现金腾出来。目标里没有的仓位全部卖掉，含不足一手的零股。
    for code in sorted(set(positions) | set(plan)):
        have = positions.get(code, 0)
        want = 0 if code not in plan or plan[code]["shares"] is None else plan[code]["shares"]
        if code in plan and plan[code]["shares"] is None:
            if have:
                blocks[code] = "开盘价数据缺失"
            continue
        if have <= want:
            continue
        quote = _quote(prices, code, date)
        open_px, prev = quote.get("open"), quote.get("prev")
        name = names.get(code) or ""
        if open_px is None or prev is None:
            blocks[code] = "开盘价数据缺失"
            continue
        _up, down = limit_prices(prev, code, name)
        if open_px <= down + 1e-9:
            blocks[code] = "跌停开盘卖不出"
            blocked_since.setdefault(code, date)
            continue
        sell_shares = have - want
        if want == 0:
            sell_shares = have
        elif sell_shares >= lot_of(code):
            sell_shares = int(sell_shares // lot_of(code)) * lot_of(code)
        else:
            continue
        if sell_shares <= 0:
            continue
        cash = _sell(cash, code, name, sell_shares, open_px, date, blocked_since.get(code), trades)
        positions[code] = have - sell_shares
        if positions[code] <= 0:
            positions.pop(code, None)
            blocked_since.pop(code, None)
        trade = trades[-1]
        trade["pnl"] = _fifo_close(lots, code, sell_shares, round_cash(trade["notional"] - trade["commission"] - trade["stamp"]))
    for code in sorted(plan):
        item = plan[code]
        if item["shares"] is None:
            blocks[code] = "开盘价数据缺失"
            continue
        have = positions.get(code, 0)
        need = item["shares"] - have
        lot = lot_of(code)
        need = int(need // lot) * lot
        if need <= 0:
            continue
        quote = _quote(prices, code, date)
        open_px, prev = quote.get("open"), quote.get("prev")
        name = names.get(code) or ""
        if open_px is None or prev is None:
            blocks[code] = "开盘价数据缺失"
            continue
        up, _down = limit_prices(prev, code, name)
        if open_px + 1e-9 >= up:
            blocks[code] = "涨停开盘买不进"
            blocked_since.setdefault(code, date)
            continue
        fill = round_px(open_px * (1 + SLIP))
        unit = round_cash(fill * (1 + COMMISSION))
        if unit <= 0:
            continue
        affordable = int((cash / unit) // lot) * lot
        take = min(need, affordable)
        if take <= 0:
            blocks[code] = "现金不足"
            continue
        cash = _buy(cash, code, name, take, open_px, date, blocked_since.get(code), trades)
        positions[code] = have + take
        cost = round_cash(trades[-1]["notional"] + trades[-1]["commission"])
        per = cost / take
        lots.setdefault(code, []).append([take, per])
        if positions[code] >= item["shares"]:
            blocked_since.pop(code, None)
    still = any(reason != "现金不足" for reason in blocks.values())
    return cash, still, blocks


def _metrics(nav, initial, trades):
    equities = [row["equity"] for row in nav if row.get("equity") is not None]
    cum = None if not equities else equities[-1] / initial - 1.0
    peak, max_dd = None, 0.0
    for equity in equities:
        peak = equity if peak is None else max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak - equity) / peak)
    closed = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] < 0]
    win_rate = None if not closed else len(wins) / len(closed)
    payoff = None
    if wins and losses:
        payoff = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
    max_loss = min(losses) if losses else None
    return {
        "cum_return": None if cum is None else round(cum, 6),
        "max_drawdown": round(max_dd, 6),
        "win_rate": None if win_rate is None else round(win_rate, 4),
        "payoff": None if payoff is None else round(payoff, 4),
        "max_loss": max_loss,
        "closed": len(closed),
        "hs300_return": _index_return(nav, "hs300_nav"),
        "cyb_return": _index_return(nav, "cyb_nav"),
    }


def _index_return(nav, key):
    vals = [row[key] for row in nav if row.get(key) not in (None, 0)]
    if len(vals) < 1:
        return None
    return round(vals[-1] / vals[0] - 1.0, 6) if vals[0] else None


def _weight_key(target):
    return tuple(sorted((code, round(float(weight), 4)) for code, weight in (target or {}).items()))


def _pending(target, positions, names, blocks, signal_date, executed):
    rows = []
    codes = sorted(set(target) | set(positions) | set(blocks))
    for code in codes:
        weight = target.get(code) or 0
        have = positions.get(code, 0)
        reason = blocks.get(code) or ""
        if not executed:
            note = "信号已在收盘确认，待下一交易日开盘"
            side = "buy" if weight and have == 0 else ("sell" if have and not weight else "adjust")
        elif reason and reason != "现金不足":
            note = reason + "，顺延"
            side = "sell" if have and not weight else "buy"
        elif reason == "现金不足":
            note = "现金不足，本次不再加仓"
            side = "buy"
        else:
            continue
        if side == "adjust" and not reason:
            if executed:
                continue
        rows.append({
            "code": code, "name": names.get(code) or "", "side": side,
            "target_weight": weight, "shares": have, "since": signal_date, "note": note,
        })
    return rows


def simulate(events, days, prices, indexes, initial=INITIAL):
    events = sorted(events or [], key=lambda item: item.get("date") or "")
    by_date = {}
    for event in events:
        if event.get("date"):
            by_date[event["date"]] = event
    start = events[0]["date"] if events else (days[-1] if days else None)
    span = [day for day in days if start and day >= start]
    cash = float(initial)
    positions, names, lots = {}, {}, {}
    target, dirty = {}, False
    signal_date, blocked_since, last_blocks = None, {}, {}
    trades, nav = [], []
    hs_first = cy_first = None
    for date in span:
        executed = False
        if dirty:
            cash, still, last_blocks = rebalance(
                date, target, cash, positions, names, prices, lots, blocked_since, trades)
            dirty = still
            executed = True
            if not still:
                blocked_since.clear()
        equity = cash
        gap = False
        for code, shares in positions.items():
            close = _quote(prices, code, date).get("close")
            if close is None:
                gap = True
                break
            equity += shares * close
        if gap:
            equity = nav[-1]["equity"] if nav and nav[-1].get("equity") is not None else None
        hs = (indexes.get("hs300") or {}).get(date)
        cy = (indexes.get("cyb") or {}).get(date)
        if hs is not None and hs_first is None:
            hs_first = hs
        if cy is not None and cy_first is None:
            cy_first = cy
        nav.append({
            "date": date,
            "equity": None if equity is None else round_cash(equity),
            "nav": None if equity is None else round(equity / initial, 6),
            "cash": round_cash(cash),
            "hs300": hs,
            "cyb": cy,
            "hs300_nav": None if hs in (None, 0) or not hs_first else round(hs / hs_first, 6),
            "cyb_nav": None if cy in (None, 0) or not cy_first else round(cy / cy_first, 6),
            "price_gap": gap,
        })
        if date in by_date:
            event = by_date[date]
            new_target = {}
            for leg in event.get("weights") or []:
                code = str(leg.get("code") or "")
                weight = leg.get("weight")
                if not code or not weight:
                    continue
                new_target[code] = float(weight)
                if leg.get("name"):
                    names[code] = leg["name"]
            # 同一份权重每天重复写入 history 时不再交易，只在名单或权重变化时调仓。未完成的封板单继续顺延。
            if _weight_key(new_target) != _weight_key(target):
                target = new_target
                dirty = True
                signal_date = date
                last_blocks = {}
            else:
                target = new_target
    if signal_date and span and signal_date == span[-1]:
        pending = _pending(target, positions, names, {}, signal_date, False)
    elif dirty:
        pending = _pending(target, positions, names, last_blocks, signal_date, True)
    else:
        pending = []
    metrics = _metrics(nav, initial, trades)
    gaps = []
    if metrics["hs300_return"] is None:
        gaps.append("沪深300数据缺失")
    if metrics["cyb_return"] is None:
        gaps.append("创业板指数据缺失")
    return {
        "status": "ok",
        "currency": "CNY",
        "initial_cash": initial,
        "cash": round_cash(cash),
        "equity": None if not nav or nav[-1].get("equity") is None else nav[-1]["equity"],
        "positions": _positions(positions, names, target, prices, span[-1] if span else None),
        "pending": pending,
        "trades": trades,
        "nav": nav,
        "metrics": metrics,
        "benchmarks": {"hs300": "000300", "cyb": "399006"},
        "rules": {
            "signal": "收盘计算信号",
            "fill": "次日开盘价",
            "commission": COMMISSION,
            "slippage": SLIP,
            "stamp_sell": STAMP,
            "lot": "100，科创板200",
            "initial_cash": initial,
        },
        "gaps": gaps,
        "note": "虚拟资金。只按正式四王调仓记录建仓，策略标签不触发买卖。没有平仓时胜率、盈亏比、最大单笔亏损保持空值，不编造。",
    }


def _positions(positions, names, target, prices, date):
    rows = []
    equity = None
    for code, shares in sorted(positions.items()):
        close = _quote(prices, code, date).get("close") if date else None
        value = None if close is None else round_cash(shares * close)
        rows.append({
            "code": code, "name": names.get(code) or "", "shares": shares,
            "close": close, "market_value": value, "target_weight": target.get(code) or 0,
        })
    return rows


def normalize_events(history):
    events = []
    for event in (history or {}).get("events") or []:
        weights = []
        for leg in event.get("weights") or []:
            if not leg.get("code"):
                continue
            weights.append({"code": str(leg["code"]), "name": leg.get("name") or "", "weight": leg.get("weight")})
        events.append({"date": event.get("date"), "weights": weights})
    return events


def build(history, calendar, asof):
    events = normalize_events(history)
    codes = sorted({leg["code"] for event in events for leg in event["weights"] if leg.get("weight")})
    prices, used = {}, []
    missing = []
    if codes:
        import marketdata
        got, sources, _note = marketdata.load_many(codes, asof)
        used.extend(sources or [])
        for code in codes:
            bars = got.get(code)
            if not bars:
                missing.append(code)
                continue
            book, prev = {}, None
            for bar in bars:
                book[bar["date"]] = {"open": bar["open"], "close": bar["close"], "prev": prev}
                prev = bar["close"]
            prices[code] = book
    indexes, index_sources = {}, []
    import marketdata
    for key, symbol in (("hs300", "sh000300"), ("cyb", "sz399006")):
        bars, src = marketdata.load_index(symbol, asof)
        if src:
            index_sources.append(src)
        indexes[key] = {bar["date"]: bar["close"] for bar in bars or [] if bar["date"] <= asof}
    days = {day for day in calendar or [] if day <= asof}
    start = events[0]["date"] if events and events[0].get("date") else None
    for series in indexes.values():
        for day in series:
            if day <= asof and (start is None or day >= start):
                days.add(day)
    for event in events:
        if event.get("date") and event["date"] <= asof:
            days.add(event["date"])
    days = sorted(days)
    doc = simulate(events, days, prices, indexes)
    doc["asof"] = asof
    doc["updated_at"] = common.now_bj().strftime("%Y-%m-%d %H:%M")
    doc["sources"] = sorted(set(used + index_sources))
    if missing:
        doc["gaps"].append("持仓标的日K数据缺失：" + "、".join(missing))
        doc["note"] += " 缺少开盘价的标的没有强行成交。"
    return doc


def summary(doc):
    metrics = doc.get("metrics") or {}
    return {
        "status": doc.get("status") or "数据缺失",
        "fill": FILL,
        "initial": doc.get("initial_cash"),
        "equity": doc.get("equity"),
        "cash": doc.get("cash"),
        "cum_return": metrics.get("cum_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "win_rate": metrics.get("win_rate"),
        "payoff": metrics.get("payoff"),
        "max_loss": metrics.get("max_loss"),
        "closed": metrics.get("closed"),
        "hs300_return": metrics.get("hs300_return"),
        "cyb_return": metrics.get("cyb_return"),
        "note": doc.get("note") or "",
        "source": "data/paper.json",
    }


def failed_doc(asof, message):
    return {
        "status": "数据缺失",
        "asof": asof,
        "updated_at": common.now_bj().strftime("%Y-%m-%d %H:%M"),
        "error": message,
        "currency": "CNY",
        "initial_cash": INITIAL,
        "cash": None,
        "equity": None,
        "positions": [],
        "pending": [],
        "trades": [],
        "nav": [],
        "metrics": {
            "cum_return": None, "max_drawdown": None, "win_rate": None, "payoff": None,
            "max_loss": None, "closed": 0, "hs300_return": None, "cyb_return": None,
        },
        "benchmarks": {"hs300": "000300", "cyb": "399006"},
        "rules": {"initial_cash": INITIAL, "commission": COMMISSION, "slippage": SLIP, "stamp_sell": STAMP},
        "gaps": [message],
        "note": "模拟盘计算失败，已标数据缺失，不编造净值。",
        "sources": [],
    }


def _book(code, rows):
    out = {}
    prev = None
    for date, open_, close in rows:
        out[date] = {"open": open_, "close": close, "prev": prev}
        prev = close
    return {code: out}


def self_test():
    assert lot_of("688001") == 200 and lot_of("000001") == 100
    assert limit_pct("300001", "甲") == 0.20
    assert limit_pct("000001", "*ST甲") == 0.05
    up, down = limit_prices(10, "000001", "平安银行")
    assert up == 11 and down == 9
    days = ["2026-11-06", "2026-11-09", "2026-11-10"]
    prices = _book("000001", [
        ("2026-11-06", 10.0, 10.0),
        ("2026-11-09", 10.0, 11.0),
        ("2026-11-10", 11.0, 11.2),
    ])
    indexes = {"hs300": {"2026-11-06": 100.0, "2026-11-09": 110.0, "2026-11-10": 105.0},
               "cyb": {"2026-11-06": 200.0, "2026-11-09": 200.0, "2026-11-10": 210.0}}
    doc = simulate(
        [{"date": "2026-11-06", "weights": [{"code": "000001", "name": "平安银行", "weight": 50}]}],
        days, prices, indexes)
    assert doc["trades"] and all(t["date"] != "2026-11-06" for t in doc["trades"])
    buy = doc["trades"][0]
    assert buy["date"] == "2026-11-09" and buy["side"] == "buy" and buy["stamp"] == 0
    assert buy["price"] == round_px(10 * (1 + SLIP))
    assert buy["shares"] % 100 == 0 and buy["shares"] == int((500000 / buy["price"]) // 100) * 100
    assert buy["commission"] == round_cash(buy["notional"] * COMMISSION)
    assert abs(doc["nav"][0]["equity"] - INITIAL) < 0.001
    assert abs(doc["nav"][1]["equity"] - (doc["nav"][1]["cash"] + buy["shares"] * 11)) < 0.02
    assert doc["nav"][0]["hs300_nav"] == 1 and abs(doc["metrics"]["hs300_return"] - 0.05) < 1e-9
    assert doc["metrics"]["win_rate"] is None and doc["metrics"]["closed"] == 0
    assert doc["positions"][0]["shares"] == buy["shares"]

    # 涨停开盘买不进，下一交易日再买。
    blocked_px = _book("000001", [
        ("2026-11-06", 10.0, 10.0),
        ("2026-11-09", 11.0, 11.0),
        ("2026-11-10", 10.5, 10.6),
    ])
    blocked = simulate(
        [{"date": "2026-11-06", "weights": [{"code": "000001", "name": "平安银行", "weight": 10}]}],
        days, blocked_px, indexes)
    assert not any(t["date"] == "2026-11-09" for t in blocked["trades"])
    assert any(t["date"] == "2026-11-10" and "顺延" in t["note"] for t in blocked["trades"])

    # 跌停开盘卖不出。
    sell_days = days + ["2026-11-11", "2026-11-12"]
    sell_px = _book("000001", [
        ("2026-11-06", 10.0, 10.0),
        ("2026-11-09", 10.0, 11.0),
        ("2026-11-10", 11.0, 11.0),
        ("2026-11-11", 9.9, 9.9),
        ("2026-11-12", 10.0, 10.2),
    ])
    # 11日开盘 9.9，前收 11，跌停价是 9.90，应卖不出。12日卖出并计印花税。
    sold = simulate([
        {"date": "2026-11-06", "weights": [{"code": "000001", "name": "平安银行", "weight": 50}]},
        {"date": "2026-11-10", "weights": []},
    ], sell_days, sell_px, {"hs300": {}, "cyb": {}})
    assert not any(t["date"] == "2026-11-11" and t["side"] == "sell" for t in sold["trades"])
    sells = [t for t in sold["trades"] if t["side"] == "sell"]
    assert sells and sells[0]["date"] == "2026-11-12" and sells[0]["stamp"] > 0 and "跌停" in sells[0]["note"]
    assert sold["metrics"]["closed"] == 1 and sold["metrics"]["max_drawdown"] >= 0
    assert sold["positions"] == []

    empty = simulate(
        [{"date": "2026-11-06", "weights": []}, {"date": "2026-11-09", "weights": []}],
        days, {}, indexes)
    assert empty["trades"] == [] and empty["positions"] == []
    assert all(abs(row["equity"] - INITIAL) < 0.001 for row in empty["nav"])
    assert empty["metrics"]["cum_return"] == 0 and empty["metrics"]["max_drawdown"] == 0
    assert "000002" not in {t["code"] for t in empty["trades"]}
    print("paper self-test ok")


if __name__ == "__main__":
    self_test()
