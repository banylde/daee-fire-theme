# 模拟盘。虚拟资金，只根据仓库里的正式调仓记录记账。
# 收盘确认目标权重，下一交易日开盘价成交。涨停开盘不买，跌停开盘不卖，顺延到之后的交易日。
# 成本：佣金万 3（双边）、滑点 0.1%（买在开盘价上、卖在开盘价下，价格四舍五入到分）、卖出印花税 0.05%。
# A 股 T+1 可卖和资金交收只借鉴 PanWatch 的规则说明（https://github.com/TNT-Likely/PanWatch ，MIT），
# 没有复制它的代码，也不部署。买入日按上交所交易日：当日买入不可卖，下一交易日可卖，不等满 24 小时。
# 卖出款当日可以再买；交收另计，到交收日不把现金或盈亏再记一笔。没有港股、美股仓位，不做 T+2。
# 不连接券商，不读取真实账户或持仓，不下单，不模拟融资、换汇或券商垫资。
import common

INITIAL = 1000000.0
COMMISSION = 0.0003
SLIP = 0.001
STAMP = 0.0005
FILL = "信号在信号日收盘后计算，按下一交易日开盘价成交（T+1 open）。涨停买不进，跌停卖不出。"
# 上面的 T+1 open 是成交时点。下面这条才是股份何时可卖、卖出款何时能再买。
T_PLUS = "A股 T+1：买入当日不可卖（调仓卖出同样不行），下一上交所交易日可卖，不等满 24 小时。锁定期间仍按收盘价计浮盈亏。"
FUNDS = "可卖与交收分开。卖出款当日可再买，买入立即占用现金。交收日只结束待交收，不重复增加现金。不模拟融资。"
CALENDAR_GAP = "上交所交易日历缺失，T+1 可卖日和交收日无法确认。锁定股份不成交，待交收卖出款保持待交收，不把普通工作日猜成交易日。"


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


def trading_sessions(calendar):
    """只保留传入的上交所交易日。周末补班若出现在这份日历里照样算交易日；不另把周一到周五猜成开市。"""
    seen = set()
    out = []
    for raw in calendar or []:
        day = str(raw or "")[:10]
        if len(day) != 10 or day in seen:
            continue
        seen.add(day)
        out.append(day)
    out.sort()
    return out


def next_trading_day(calendar, day):
    """day 之后的第一个上交所交易日。日历没覆盖就返回空，调用方保持锁定或待交收。"""
    current = str(day or "")[:10]
    for session in trading_sessions(calendar):
        if session > current:
            return session
    return None


def lot_sellable(buy_date, session, calendar):
    """按交易日而不是时钟。买入当日不可卖；session 不在日历里也不可卖。"""
    buy = str(buy_date or "")[:10]
    day = str(session or "")[:10]
    if not buy or not day or day <= buy:
        return False
    sessions = trading_sessions(calendar)
    if day not in sessions:
        return False
    nxt = next_trading_day(sessions, buy)
    return bool(nxt and day >= nxt)


def _lot_parts(lot):
    shares = int(lot[0])
    cost = float(lot[1]) if len(lot) > 1 else 0.0
    buy_date = str(lot[2])[:10] if len(lot) > 2 and lot[2] else ""
    return shares, cost, buy_date


def _sellable_shares(lots, code, date, calendar):
    total = 0
    for lot in lots.get(code) or []:
        shares, _cost, buy_date = _lot_parts(lot)
        if lot_sellable(buy_date, date, calendar):
            total += shares
    return total


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


def _fifo_close(lots, code, shares, proceeds, date, calendar):
    """按买入成本结清本次卖出。只动已经可卖的批次，避免把当日买入的成本结掉。"""
    book = lots.setdefault(code, [])
    left, cost = shares, 0.0
    while left > 0 and book:
        lot_shares, lot_cost, buy_date = _lot_parts(book[0])
        if not lot_sellable(buy_date, date, calendar):
            break
        take = min(left, lot_shares)
        cost += lot_cost * take
        lot_shares -= take
        left -= take
        if lot_shares <= 0:
            book.pop(0)
        else:
            book[0] = [lot_shares, lot_cost, buy_date]
    return round_cash(proceeds - cost)


def _record_sale(settlements, trade, calendar):
    """卖出净额已经进了现金。这里只登记何时交收，交收时不再加一笔。"""
    amount = round_cash(trade["notional"] - trade["commission"] - trade["stamp"])
    due = next_trading_day(calendar, trade["date"])
    settlements.append({
        "date": trade["date"], "code": trade["code"], "amount": amount, "remaining": amount,
        "settlement_date": due, "status": "pending" if due else "unknown",
    })


def _consume_proceeds(settlements, outlay):
    """买入先占用尚未交收的卖出款，已交收现金能不动就不动。"""
    left = round_cash(outlay)
    for row in settlements:
        if left <= 0:
            break
        if row.get("status") not in ("pending", "unknown") or row.get("remaining", 0) <= 0:
            continue
        used = min(left, row["remaining"])
        row["remaining"] = round_cash(row["remaining"] - used)
        left = round_cash(left - used)
        if row["remaining"] <= 0:
            row["remaining"] = 0.0
            row["status"] = "settled"
            row["note"] = "买入已占用，交收不再加现金"


def _release_due(settlements, date):
    """交收日开始时结束待交收。现金在卖出当天已经加上，这里不加第二遍。"""
    day = str(date or "")[:10]
    for row in settlements:
        if row.get("status") not in ("pending", "unknown"):
            continue
        due = row.get("settlement_date")
        if due and day >= due:
            row["status"] = "settled"
            row["remaining"] = 0.0
            row["note"] = "交收日到达，不重复增加现金"


def _unsettled_amount(settlements):
    return round_cash(sum(row.get("remaining") or 0 for row in settlements if row.get("status") in ("pending", "unknown")))


def _cash_view(cash, settlements):
    cash = round_cash(cash)
    unsettled = _unsettled_amount(settlements)
    # A 股购买力等于现金：卖出款已经在现金里，并且当日就能再买。不把待交收从购买力里扣掉，也不另加融资。
    return {
        "cash": cash,
        "buying_power": round_cash(max(0.0, cash)),
        "settled_cash": round_cash(max(0.0, cash - unsettled)),
        "unsettled_proceeds": unsettled,
    }


def _locked_total(positions, lots, date, calendar):
    total = 0
    for code, shares in positions.items():
        total += shares - _sellable_shares(lots, code, date, calendar)
    return total


def rebalance(date, target, cash, positions, names, prices, lots, blocked_since, trades, calendar, settlements):
    """把持仓调向 target（代码 -> 权重百分数）。封板、缺开盘价或 T+1 锁定的一边留下，返回 (cash, still_blocked, blocks)。"""
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
    # 没有止盈、止损单。调仓想卖时，当日买入的部分不成交，到下一交易日按当时目标权重和开盘价再决定。
    for code in sorted(set(positions) | set(plan)):
        have = positions.get(code, 0)
        want = 0 if code not in plan or plan[code]["shares"] is None else plan[code]["shares"]
        if code in plan and plan[code]["shares"] is None:
            if have:
                blocks[code] = "开盘价数据缺失"
            continue
        if have <= want:
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
        sellable = _sellable_shares(lots, code, date, calendar)
        tradable = min(sell_shares, sellable)
        if want != 0:
            tradable = int(tradable // lot_of(code)) * lot_of(code)
        if tradable <= 0:
            blocks[code] = "T+1锁定不可卖"
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
        cash = _sell(cash, code, name, tradable, open_px, date, blocked_since.get(code), trades)
        positions[code] = have - tradable
        if positions[code] <= 0:
            positions.pop(code, None)
            blocked_since.pop(code, None)
        trade = trades[-1]
        trade["pnl"] = _fifo_close(
            lots, code, tradable, round_cash(trade["notional"] - trade["commission"] - trade["stamp"]), date, calendar)
        _record_sale(settlements, trade, calendar)
        if sell_shares > tradable:
            blocks[code] = "T+1锁定不可卖"
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
        # 购买力用现金。A 股卖出款已经计入现金，当日可再买；不另加融资额度。
        power = round_cash(max(0.0, cash))
        affordable = int((power / unit) // lot) * lot
        take = min(need, affordable)
        if take <= 0:
            blocks[code] = "现金不足"
            continue
        cash = _buy(cash, code, name, take, open_px, date, blocked_since.get(code), trades)
        positions[code] = have + take
        cost = round_cash(trades[-1]["notional"] + trades[-1]["commission"])
        per = cost / take
        lots.setdefault(code, []).append([take, per, date])
        _consume_proceeds(settlements, cost)
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


def simulate(events, days, prices, indexes, initial=INITIAL, calendar=None):
    events = sorted(events or [], key=lambda item: item.get("date") or "")
    by_date = {}
    for event in events:
        if event.get("date"):
            by_date[event["date"]] = event
    # calendar 为 None 时，沿用调用方给出的交易日。显式传入空列表则降级，不把工作日猜成开市。
    if calendar is None:
        sessions = trading_sessions(days)
        calendar_status = "ok" if sessions else "缺失"
    else:
        sessions = trading_sessions(calendar)
        calendar_status = "ok" if sessions else "缺失"
    session_set = set(sessions)
    start = events[0]["date"] if events else (days[-1] if days else None)
    span = [day for day in days if start and day >= start]
    cash = float(initial)
    positions, names, lots = {}, {}, {}
    settlements = []
    target, dirty = {}, False
    signal_date, blocked_since, last_blocks = None, {}, {}
    trades, nav = [], []
    calendar_gaps = []
    if calendar_status == "缺失":
        calendar_gaps.append(CALENDAR_GAP)
    hs_first = cy_first = None
    for date in span:
        _release_due(settlements, date)
        executed = False
        if dirty:
            cash, still, last_blocks = rebalance(
                date, target, cash, positions, names, prices, lots, blocked_since, trades, sessions, settlements)
            dirty = still
            executed = True
            if not still:
                blocked_since.clear()
        if sessions and date not in session_set and any(
            _lot_parts(lot)[2] < date for book in lots.values() for lot in book
        ):
            text = "交易日历未覆盖 %s，该日不解锁 T+1，也不作为交收日。" % date
            if text not in calendar_gaps:
                calendar_gaps.append(text)
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
        view = _cash_view(cash, settlements)
        nav.append({
            "date": date,
            "equity": None if equity is None else round_cash(equity),
            "nav": None if equity is None else round(equity / initial, 6),
            "hs300": hs,
            "cyb": cy,
            "hs300_nav": None if hs in (None, 0) or not hs_first else round(hs / hs_first, 6),
            "cyb_nav": None if cy in (None, 0) or not cy_first else round(cy / cy_first, 6),
            "price_gap": gap,
            "locked_shares": _locked_total(positions, lots, date, sessions),
            "cash": view["cash"],
            "buying_power": view["buying_power"],
            "settled_cash": view["settled_cash"],
            "unsettled_proceeds": view["unsettled_proceeds"],
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
    gaps = list(calendar_gaps)
    if metrics["hs300_return"] is None:
        gaps.append("沪深300数据缺失")
    if metrics["cyb_return"] is None:
        gaps.append("创业板指数据缺失")
    last_session = sessions[-1] if sessions else None
    if any(row.get("status") == "unknown" and row.get("date") != last_session for row in settlements):
        gaps.append("部分卖出款的交收日超出上交所日历，保持待交收，不重复增加现金。")
    asof = span[-1] if span else None
    view = _cash_view(cash, settlements)
    locked_shares = _locked_total(positions, lots, asof, sessions)
    return {
        "status": "ok",
        "currency": "CNY",
        "initial_cash": initial,
        "cash": view["cash"],
        "buying_power": view["buying_power"],
        "settled_cash": view["settled_cash"],
        "unsettled_proceeds": view["unsettled_proceeds"],
        "locked_shares": locked_shares,
        "equity": None if not nav or nav[-1].get("equity") is None else nav[-1]["equity"],
        "positions": _positions(positions, names, target, prices, asof, lots, sessions),
        "pending": pending,
        "trades": trades,
        "settlements": settlements,
        "nav": nav,
        "metrics": metrics,
        "benchmarks": {"hs300": "000300", "cyb": "399006"},
        "calendar": {
            "market": "SSE",
            "status": calendar_status,
            "count": len(sessions),
            "first": sessions[0] if sessions else None,
            "last": last_session,
            "source": "与行情同一份上交所交易日：东财上证指数日K，失败则腾讯。不另做港股日历。",
        },
        "rules": {
            "signal": "收盘计算信号",
            "fill": "次日开盘价",
            "t_plus_sell": T_PLUS,
            "funds": FUNDS,
            "commission": COMMISSION,
            "slippage": SLIP,
            "stamp_sell": STAMP,
            "lot": "100，科创板200",
            "initial_cash": initial,
            "calendar": "上交所指数日K（东财 sh000001，失败则腾讯）" if calendar_status == "ok" else "缺失",
        },
        "gaps": gaps,
        "note": "虚拟资金。只按正式四王调仓记录建仓，策略标签不触发买卖。没有平仓时胜率、盈亏比、最大单笔亏损保持空值，不编造。A股买入当日不可卖，下一交易日可卖；卖出款当日可再买，交收不重复入账。",
    }


def _positions(positions, names, target, prices, date, lots, calendar):
    rows = []
    for code, shares in sorted(positions.items()):
        close = _quote(prices, code, date).get("close") if date else None
        value = None if close is None else round_cash(shares * close)
        sellable = _sellable_shares(lots, code, date, calendar)
        locked = shares - sellable
        basis, newest, lot_rows = 0.0, "", []
        for lot in lots.get(code) or []:
            sh, cost, buy_date = _lot_parts(lot)
            basis += sh * cost
            if buy_date > newest:
                newest = buy_date
            lot_rows.append({
                "shares": sh,
                "buy_date": buy_date,
                "sellable_date": next_trading_day(calendar, buy_date),
                "sellable": bool(date) and lot_sellable(buy_date, date, calendar),
            })
        rows.append({
            "code": code, "name": names.get(code) or "", "shares": shares,
            "sellable_shares": sellable, "locked_shares": locked,
            "sellable_date": next_trading_day(calendar, newest) if locked and newest else None,
            "close": close, "market_value": value,
            "unrealized_pnl": None if close is None else round_cash(shares * close - basis),
            "target_weight": target.get(code) or 0,
            "lots": lot_rows,
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
    # 可卖和交收只认这份上交所日历。行情里多出来的日期仍可用于净值，但不能把缺失日历补成工作日。
    sessions = [day for day in trading_sessions(calendar) if not asof or day <= asof]
    doc = simulate(events, days, prices, indexes, calendar=sessions)
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
        "buying_power": doc.get("buying_power"),
        "settled_cash": doc.get("settled_cash"),
        "unsettled_proceeds": doc.get("unsettled_proceeds"),
        "locked_shares": doc.get("locked_shares") or 0,
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
        "buying_power": None,
        "settled_cash": None,
        "unsettled_proceeds": None,
        "locked_shares": 0,
        "equity": None,
        "positions": [],
        "pending": [],
        "trades": [],
        "settlements": [],
        "nav": [],
        "metrics": {
            "cum_return": None, "max_drawdown": None, "win_rate": None, "payoff": None,
            "max_loss": None, "closed": 0, "hs300_return": None, "cyb_return": None,
        },
        "benchmarks": {"hs300": "000300", "cyb": "399006"},
        "calendar": {"market": "SSE", "status": "缺失", "count": 0, "first": None, "last": None,
                     "source": "与行情同一份上交所交易日：东财上证指数日K，失败则腾讯。不另做港股日历。"},
        "rules": {"initial_cash": INITIAL, "commission": COMMISSION, "slippage": SLIP, "stamp_sell": STAMP,
                  "t_plus_sell": T_PLUS, "funds": FUNDS, "calendar": "缺失"},
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
    assert empty["buying_power"] == empty["cash"] == INITIAL
    assert empty["locked_shares"] == 0 and empty["unsettled_proceeds"] == 0
    assert empty["calendar"]["market"] == "SSE"

    # 买入当日立刻把目标打成空仓：当日卖不出，下一交易日按当天开盘价卖。不等满 24 小时，也不另设止盈止损。
    same_days = ["2026-11-06", "2026-11-09", "2026-11-10"]
    same_px = _book("000001", [
        ("2026-11-06", 10.0, 10.0),
        ("2026-11-09", 10.0, 10.8),
        ("2026-11-10", 10.6, 10.4),
    ])
    same = simulate([
        {"date": "2026-11-06", "weights": [{"code": "000001", "name": "平安银行", "weight": 40}]},
        {"date": "2026-11-09", "weights": []},
    ], same_days, same_px, {"hs300": {}, "cyb": {}}, calendar=same_days)
    assert [t["date"] for t in same["trades"] if t["side"] == "buy"] == ["2026-11-09"]
    assert not any(t["side"] == "sell" and t["date"] == "2026-11-09" for t in same["trades"])
    held = next(row for row in same["nav"] if row["date"] == "2026-11-09")
    bought = next(t for t in same["trades"] if t["side"] == "buy")
    assert held["locked_shares"] == bought["shares"] > 0
    assert abs(held["equity"] - (held["cash"] + bought["shares"] * 10.8)) < 0.02
    assert held["equity"] != held["cash"]
    sold_next = [t for t in same["trades"] if t["side"] == "sell"]
    assert sold_next and sold_next[0]["date"] == "2026-11-10"
    assert sold_next[0]["price"] == round_px(10.6 * (1 - SLIP))
    assert same["positions"] == []
    # 绕开「次日才调仓」的时点，直接在买入当日再调一次，仍然卖不出。
    cash, pos, book = float(INITIAL), {}, {}
    blocked_since, tickets, ledger = {}, [], []
    names = {"000001": "平安银行"}
    cash, _still, _blocks = rebalance(
        "2026-11-09", {"000001": 40}, cash, pos, names, same_px, book, blocked_since, tickets, same_days, ledger)
    assert tickets and tickets[-1]["side"] == "buy" and pos["000001"] > 0
    before = len(tickets)
    cash, still, blocks = rebalance(
        "2026-11-09", {}, cash, pos, names, same_px, book, blocked_since, tickets, same_days, ledger)
    assert len(tickets) == before and still and blocks.get("000001") == "T+1锁定不可卖"
    assert _sellable_shares(book, "000001", "2026-11-09", same_days) == 0
    cash, still, blocks = rebalance(
        "2026-11-10", {}, cash, pos, names, same_px, book, blocked_since, tickets, same_days, ledger)
    assert any(t["side"] == "sell" and t["date"] == "2026-11-10" for t in tickets)
    assert pos == {} and not still

    # 跨周末：周五买入，周六即使已过 24 小时也不可卖，下周一按周一开盘价可卖。
    week_cal = ["2026-11-05", "2026-11-06", "2026-11-09"]
    assert not lot_sellable("2026-11-06", "2026-11-06", week_cal)
    assert not lot_sellable("2026-11-06", "2026-11-07", week_cal)
    assert not lot_sellable("2026-11-06", "2026-11-08", week_cal)
    assert lot_sellable("2026-11-06", "2026-11-09", week_cal)
    assert next_trading_day(week_cal, "2026-11-06") == "2026-11-09"
    assert lot_sellable("2026-11-09", "2026-11-10", ["2026-11-09", "2026-11-10"])
    week_px = _book("000001", [
        ("2026-11-05", 10.0, 10.0),
        ("2026-11-06", 10.0, 10.5),
        ("2026-11-07", 10.5, 10.5),
        ("2026-11-09", 10.2, 10.3),
    ])
    week = simulate([
        {"date": "2026-11-05", "weights": [{"code": "000001", "name": "平安银行", "weight": 30}]},
        {"date": "2026-11-06", "weights": []},
    ], ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-09"], week_px, {"hs300": {}, "cyb": {}}, calendar=week_cal)
    assert not any(t["date"] in ("2026-11-06", "2026-11-07") and t["side"] == "sell" for t in week["trades"])
    friday = next(row for row in week["nav"] if row["date"] == "2026-11-06")
    assert friday["locked_shares"] > 0
    assert abs(friday["equity"] - (friday["cash"] + friday["locked_shares"] * 10.5)) < 0.02
    week_sell = [t for t in week["trades"] if t["side"] == "sell"]
    assert week_sell and week_sell[0]["date"] == "2026-11-09"
    assert week_sell[0]["price"] == round_px(10.2 * (1 - SLIP))
    assert any("2026-11-07" in g for g in week["gaps"])

    # 跨国庆：2026-09-30 收盘后想卖，10-01 至 10-07 休市，10-08 才可卖。依据上交所公告〔2025〕45 号。
    holiday_cal = ["2026-09-29", "2026-09-30", "2026-10-08"]
    assert next_trading_day(holiday_cal, "2026-09-30") == "2026-10-08"
    for closed in ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07"):
        assert not lot_sellable("2026-09-30", closed, holiday_cal)
    assert lot_sellable("2026-09-30", "2026-10-08", holiday_cal)
    holiday_px = _book("000001", [
        ("2026-09-29", 10.0, 10.0),
        ("2026-09-30", 10.0, 10.4),
        ("2026-10-01", 10.4, 10.4),
        ("2026-10-08", 10.1, 10.2),
    ])
    holiday = simulate([
        {"date": "2026-09-29", "weights": [{"code": "000001", "name": "平安银行", "weight": 20}]},
        {"date": "2026-09-30", "weights": []},
    ], ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-08"], holiday_px, {"hs300": {}, "cyb": {}}, calendar=holiday_cal)
    assert not any(t["date"] == "2026-10-01" for t in holiday["trades"])
    holiday_sell = [t for t in holiday["trades"] if t["side"] == "sell"]
    assert holiday_sell and holiday_sell[0]["date"] == "2026-10-08"
    assert holiday_sell[0]["price"] == round_px(10.1 * (1 - SLIP))
    assert any("2026-10-01" in g for g in holiday["gaps"])

    # 卖出款当日可再买。买入占用现金后剩余现金不够一手，靠当日卖出净额才能买进另一只。
    fund_days = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    fund_px = {}
    fund_px.update(_book("000001", [
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 10.5),
        ("2026-11-11", 10.5, 10.5),
        ("2026-11-12", 10.5, 10.5),
    ]))
    fund_px.update(_book("000002", [
        ("2026-11-10", 200.0, 200.0),
        ("2026-11-11", 200.0, 201.0),
        ("2026-11-12", 201.0, 202.0),
    ]))
    funded = simulate([
        {"date": "2026-11-09", "weights": [{"code": "000001", "name": "平安银行", "weight": 99}]},
        {"date": "2026-11-10", "weights": [{"code": "000002", "name": "万科A", "weight": 99}]},
    ], fund_days, fund_px, {"hs300": {}, "cyb": {}}, calendar=fund_days)
    buy_a = next(t for t in funded["trades"] if t["code"] == "000001" and t["side"] == "buy")
    sell_a = next(t for t in funded["trades"] if t["code"] == "000001" and t["side"] == "sell")
    buy_b = next(t for t in funded["trades"] if t["code"] == "000002" and t["side"] == "buy")
    assert buy_a["date"] == "2026-11-10" and sell_a["date"] == buy_b["date"] == "2026-11-11"
    locked_day = next(row for row in funded["nav"] if row["date"] == "2026-11-10")
    assert locked_day["locked_shares"] == buy_a["shares"]
    assert abs(locked_day["equity"] - (locked_day["cash"] + buy_a["shares"] * 10.5)) < 0.02
    assert buy_b["notional"] + buy_b["commission"] > locked_day["cash"] + 1
    sold_day = next(row for row in funded["nav"] if row["date"] == "2026-11-11")
    assert sold_day["buying_power"] == sold_day["cash"]
    assert sold_day["buying_power"] <= sold_day["cash"]
    assert sold_day["unsettled_proceeds"] > 0
    assert abs(sold_day["settled_cash"] - locked_day["cash"]) < 0.05
    settled_day = next(row for row in funded["nav"] if row["date"] == "2026-11-12")
    assert settled_day["cash"] == sold_day["cash"]
    assert settled_day["unsettled_proceeds"] == 0
    assert settled_day["settled_cash"] == settled_day["cash"] == settled_day["buying_power"]
    assert funded["positions"] and funded["positions"][0]["code"] == "000002"
    assert funded["positions"][0]["locked_shares"] == 0

    # 日历缺失时不猜工作日：卖不出，并写明降级。卖出日没被日历覆盖到下一交易日时，款保持待交收。
    blind = simulate([
        {"date": "2026-11-06", "weights": [{"code": "000001", "name": "平安银行", "weight": 20}]},
        {"date": "2026-11-09", "weights": []},
    ], ["2026-11-06", "2026-11-09", "2026-11-10"], same_px, {"hs300": {}, "cyb": {}}, calendar=[])
    assert any(t["side"] == "buy" for t in blind["trades"])
    assert not any(t["side"] == "sell" for t in blind["trades"])
    assert blind["positions"][0]["locked_shares"] == blind["positions"][0]["shares"]
    assert blind["positions"][0]["sellable_shares"] == 0
    assert blind["calendar"]["status"] == "缺失"
    assert any("交易日历缺失" in g for g in blind["gaps"])
    short_cal = ["2026-11-06", "2026-11-09", "2026-11-10"]
    short = simulate([
        {"date": "2026-11-06", "weights": [{"code": "000001", "name": "平安银行", "weight": 20}]},
        {"date": "2026-11-09", "weights": []},
    ], short_cal + ["2026-11-11"], _book("000001", [
        ("2026-11-06", 10.0, 10.0),
        ("2026-11-09", 10.0, 10.2),
        ("2026-11-10", 10.2, 10.2),
        ("2026-11-11", 10.2, 10.2),
    ]), {"hs300": {}, "cyb": {}}, calendar=short_cal)
    short_sell = [t for t in short["trades"] if t["side"] == "sell"]
    assert short_sell and short_sell[0]["date"] == "2026-11-10"
    assert short["settlements"][0]["status"] == "unknown" and short["settlements"][0]["settlement_date"] is None
    after = next(row for row in short["nav"] if row["date"] == "2026-11-11")
    sold_nav = next(row for row in short["nav"] if row["date"] == "2026-11-10")
    assert after["cash"] == sold_nav["cash"]
    assert after["unsettled_proceeds"] == sold_nav["unsettled_proceeds"] > 0
    assert after["buying_power"] == after["cash"]
    print("paper self-test ok")


if __name__ == "__main__":
    self_test()
