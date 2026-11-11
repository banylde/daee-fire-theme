#!/usr/bin/env python3
"""四王盗火：抓取最近一个交易日的涨停结构，写 data/latest.json、data/official.json 和 data/paper.json。

正式四王的门槛、只数和权重不在这里改。策略标签、龙虎榜、抢筹、概念资金和模拟盘都是附加结果。
缺数据保持缺数据。每次正式调仓追加 data/history.json。主源失败时改用已有备用源；仍失败则把上一份行情标成过期，不编造报价，也不删除股票。
"""
import os, re, sys, json, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import enrich
import strategy_tags
import paper

UT = "7eea3edcaed734bea9cbfc24409ed989"
EM = "https://push2ex.eastmoney.com/%s?ut=%s&dpt=wz.ztzt&Pageindex=%d&pagesize=%d&sort=%s&date=%s"

def ymd(iso):
    return iso.replace("-", "")

def limit_pct(code, name):
    n = name.upper()
    if "ST" in n or "退" in name:
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("83", "87", "43", "82", "88", "92")):
        return 0.30
    return 0.10

def hms_tuple(t):
    if t is None:
        return None
    s = str(int(t)).zfill(6)
    return int(s[0:2]), int(s[2:4]), int(s[4:6])

def hms_text(t):
    p = hms_tuple(t)
    return "%02d:%02d:%02d" % p if p else ""

def ge_1430(t):
    p = hms_tuple(t)
    return bool(p and (p[0], p[1], p[2]) >= (14, 30, 0))

def le_925(t):
    p = hms_tuple(t)
    return bool(p and (p[0], p[1], p[2]) <= (9, 25, 59))

def pool(path, date, sort, pagesize=400):
    rows, seen, tc = [], set(), None
    for page in range(6):
        raw = common.get(EM % (path, UT, page, pagesize, sort, date))
        j = __import__("json").loads(raw)
        data = j.get("data") or {}
        tc = data.get("tc", tc)
        chunk = data.get("pool") or []
        fresh = 0
        for x in chunk:
            if x.get("c") in seen:
                continue
            seen.add(x.get("c"))
            rows.append(x)
            fresh += 1
        if not chunk or fresh == 0 or (tc is not None and len(rows) >= tc):
            break
    return tc, rows

def index_dates(end):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
           "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&end=%s&lmt=30"
           "&ut=fa5fd1943c7b386f172d6893dbfba10b") % end
    j = __import__("json").loads(common.get(url))
    return [x.split(",")[0] for x in j["data"]["klines"]]

def board_amount(bk, end):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.%s"
           "&fields1=f1,f2,f3&fields2=f51,f57&klt=101&fqt=0&end=%s&lmt=8"
           "&ut=fa5fd1943c7b386f172d6893dbfba10b") % (bk, end)
    j = __import__("json").loads(common.get(url))
    data = j.get("data") or {}
    lines = data.get("klines") or []
    out = []
    for line in lines:
        d, amt = line.split(",")[:2]
        out.append((d, float(amt)))
    return data.get("name") or bk, out

def ths_pool(date):
    url = ("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?page=1&limit=200&date=%s"
           "&field=199112,10,9001,9002,330323,330325&order_field=330324&order_type=0") % date
    j = __import__("json").loads(common.get(url, {"Referer": "https://data.10jqka.com.cn/"}))
    if j.get("status_code") not in (0, "0", None) and not (j.get("data") or {}).get("info"):
        raise RuntimeError("同花顺涨停池 status=%s" % j.get("status_code"))
    info = (j.get("data") or {}).get("info") or []
    page = (j.get("data") or {}).get("page") or {}
    return page.get("total"), {x["code"]: x for x in info}

def monitor(asof):
    raw = common.get("https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json",
                     {"Referer": "https://vipmoney.eastmoney.com/"})
    rows = __import__("json").loads(raw)
    active = []
    for m in rows:
        a, b = m.get("VALIDATESTARTDATE") or "", m.get("VALIDATEENDDATE") or ""
        if a and b and a <= asof <= b:
            active.append({"code": m.get("STKCODE"), "name": m.get("STKNAME"),
                           "start": a, "end": b, "market": m.get("MARKET")})
    return active

def is_zibei(seg):
    s = seg.strip()
    return "字辈" in s or s.startswith("“") or s.startswith('"') or s.startswith("「")

def zibei_name(seg):
    m = re.search(r"[“\"「](.+?)[”\"」]", seg)
    key = m.group(1) if m else "字面"
    return "zibei:" + key, "字辈·" + key

def classify(reason, cfg):
    """返回首个有效题材 (id, name, denied)。字辈单独成组且不可做。"""
    rules = cfg["themes"]
    generic = cfg["generic_skip"]
    for seg in re.split(r"[+＋]", reason or ""):
        s = seg.strip()
        if not s:
            continue
        if is_zibei(s):
            return (*zibei_name(s), True)
        if any(g in s for g in generic):
            continue
        hit = None
        for rule in rules:
            for kw in rule["keywords"]:
                if kw and kw in s and (hit is None or len(kw) > len(hit[0])):
                    hit = (kw, rule)
        if hit:
            return hit[1]["id"], hit[1]["name"], False
    return None, None, False

def check(state, text):
    return {"state": state, "text": text}

def build_themes(stocks, cfg, asof):
    groups = {}
    for s in stocks:
        tid, name, denied = s["theme_id"], s["theme"], s["theme_denied"]
        if not tid:
            continue
        g = groups.setdefault(tid, {"id": tid, "name": name, "denied": denied, "members": []})
        g["members"].append(s)
    rule_board = {t["id"]: t.get("board") or "" for t in cfg["themes"]}
    rows = []
    for g in groups.values():
        mem = sorted(g["members"], key=lambda x: (-x["lbc"], x["fbt"] or 0))
        # 备用源可能没有连板数。天数和板数不同的「N天M板」不当成连板，这时整项缺数据，不把 0 写成未过。
        unknown_lbc = any(not x.get("lbc_known", True) for x in mem)
        n = len(mem)
        if unknown_lbc:
            height, has2 = None, False
        else:
            height = max(x["lbc"] for x in mem)
            has2 = any(x["lbc"] >= cfg["lbc_min"] for x in mem)
        if g["denied"]:
            cat = check("fail", "字面连名，不算事件催化")
        elif cfg["manual_catalyst"].get(g["name"]):
            cat = check("pass", cfg["manual_catalyst"][g["name"]])
        else:
            cat = check("manual", "待人工确认")
        if unknown_lbc:
            ech = check("missing", "缺数据：连板数未取到")
        else:
            ech_ok = n >= cfg["echelon_min"] and has2
            ech = check("pass" if ech_ok else "fail",
                        "涨停%d只，最高%d板，%s" % (n, height, "有%d板以上" % cfg["lbc_min"] if has2 else "没有%d板以上" % cfg["lbc_min"]))
        cap = check("missing", "缺数据")
        bk = rule_board.get(g["id"]) or ""
        if not bk:
            cap = check("missing", "缺数据：未绑定板块，成交额和中军未核")
        else:
            try:
                bname, series = board_amount(bk, ymd(asof))
                today = [a for d, a in series if d == asof]
                prev = [a for d, a in series if d < asof][-5:]
                if not today or len(prev) < 3:
                    cap = check("missing", "缺数据：板块成交额序列不足（%s）" % bname)
                else:
                    med = sorted(prev)[len(prev) // 2]
                    ratio = today[0] / med if med else None
                    if not ratio:
                        amp = "成交额缺数据"
                    else:
                        amp = "成交额为前5日中位数的 %.2f 倍（门槛 %.2f，%s）" % (
                            ratio, cfg["amp_ratio"], "已放大" if ratio >= cfg["amp_ratio"] else "未放大")
                    # 中军趋势本轮不核，容量整项保持缺数据
                    cap = check("missing", "%s；中军：缺数据（本轮未核市值与均线）" % amp)
            except Exception as e:
                print("板块行情失败", g["name"], e, file=sys.stderr)
                cap = check("missing", "缺数据：板块成交额未取到")
        day = check("missing", "缺数据：还没有逐日题材档案，不能推定第几天")
        states = [cat["state"], ech["state"], cap["state"], day["state"]]
        qualified = states == ["pass", "pass", "pass", "pass"]
        rows.append({
            "id": g["id"], "name": g["name"], "n": n, "height": height, "has_lbc2": has2,
            "denied": g["denied"], "qualified": qualified,
            "catalyst": cat, "echelon": ech, "capacity": cap, "day": day,
            "members": [{"code": x["code"], "name": x["name"], "lbc": x["lbc"],
                         "reason": x["reason"], "limit_type": x["limit_type"],
                         "hs": x["hs"], "float_yi": x["float_yi"], "tail": x["tail"],
                         "fbt": x["fbt_text"], "hy": x["hybk"], "close": x["close"]} for x in mem],
        })
    rows.sort(key=lambda r: (-r["n"], -(r["height"] if isinstance(r["height"], int) else -1), r["name"]))
    return rows

def sentiment(zt, dt, zb, yzt_avg, prev):
    """prev: {dt, height, broken, date}。对比项缺失则暂停入选。"""
    zt_n, dt_n, zb_n = zt, dt, zb
    missing = []
    if zt_n is None: missing.append("涨停数")
    if dt_n is None: missing.append("跌停数")
    if zb_n is None: missing.append("炸板数")
    height = prev.get("today_height")
    if height is None: missing.append("连板高度")
    if yzt_avg is None: missing.append("昨日涨停今日平均收益")
    zb_rate = round(100.0 * zb_n / (zt_n + zb_n), 1) if zt_n is not None and zb_n is not None and zt_n + zb_n else None
    if zb_rate is None: missing.append("炸板率")
    ebb, ebb_missing = [], []
    if prev.get("broken") is None: ebb_missing.append("高度标断板")
    elif prev["broken"]: ebb.append("高度标断板")
    if prev.get("dt") is None: ebb_missing.append("跌停数增加")
    elif dt_n is not None and dt_n > prev["dt"]: ebb.append("跌停数增加（%d→%d）" % (prev["dt"], dt_n))
    if yzt_avg is None: ebb_missing.append("昨日涨停今日平均收益")
    elif yzt_avg < 0: ebb.append("昨日涨停今日平均收益为负（%.2f%%）" % yzt_avg)
    if missing or ebb_missing:
        status, why = "暂停入选", "情绪对比缺数据：" + "、".join(missing + ebb_missing)
    elif ebb:
        status, why = "退潮", "；".join(ebb)
    elif zt_n >= 60 and zb_rate is not None and zb_rate <= 25 and yzt_avg >= 1 and not prev.get("broken") and height >= (prev.get("height") or 0):
        status, why = "进攻", "涨停≥60、炸板率≤25%、昨日涨停今日收益≥1%、高度未断"
    else:
        status, why = "观望", "未到进攻，也没有已核实的退潮信号"
    return {
        "status": status, "why": why, "zt": zt_n, "dt": dt_n, "zb": zb_n,
        "height": height, "zb_rate": zb_rate,
        "yzt_avg": None if yzt_avg is None else round(yzt_avg, 2),
        "prev_date": prev.get("date"), "dt_prev": prev.get("dt"),
        "height_prev": prev.get("height"), "height_broken": prev.get("broken"),
        "ebb": ebb, "missing": missing + ebb_missing,
    }

def stock_rows(themes, monitor_codes):
    rows = []
    for t in themes:
        if t["n"] < 3 and not t["qualified"]:
            continue
        best = None
        for m in t["members"]:
            if m["limit_type"] == "一字板":
                continue
            if best is None or m["lbc"] > best["lbc"]:
                best = m
        for m0 in t["members"]:
            # 角色只按当日连板结构标注，流动性与公告未核
            role = "回避"
            if m0["limit_type"] == "一字板":
                role = "回避·一字"
            elif m0.get("tail"):
                role = "回避·尾盘"
            elif "ST" in m0["name"].upper() or "退" in m0["name"]:
                role = "回避·ST"
            elif best and m0["code"] == best["code"] and m0["lbc"] >= 2:
                role = "龙头"
            elif m0["lbc"] <= 2:
                role = "补涨"
            else:
                role = "连板"
            blocks = []
            if role.startswith("回避"):
                blocks.append(role)
            fy = m0.get("float_yi")
            if fy is None:
                blocks.append("流通市值缺数据")
            elif fy < 15 or fy > 150:
                blocks.append("流通市值%.1f亿，不在15–150亿" % fy)
            blocks.append("20日均成交额：缺数据")
            blocks.append("公告排雷：缺数据")
            if m0["code"] in monitor_codes:
                blocks.append("重点监控在册")
            elif monitor_codes is None:
                blocks.append("重点监控：缺数据")
            rows.append({
                "code": m0["code"], "name": m0["name"], "theme": t["name"], "role": role,
                "lbc": m0["lbc"], "hs": m0.get("hs"), "float_yi": fy,
                "limit_type": m0.get("limit_type") or "", "reason": m0.get("reason") or "",
                "status": "未正式入池", "blocks": blocks,
            })
    rows.sort(key=lambda r: (r["theme"], -r["lbc"], r["code"]))
    return rows

def next_weekday(iso):
    d = dt.date.fromisoformat(iso) + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat()

HISTORY_NOTE = "只收录 official.json 里已经写下的调仓。调入、调出和权重为空表示四王留空，没有补造买卖。"
THS_BACKUP_FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
PAPER_FILL = "信号在信号日收盘后计算，纸面成交记在下一交易日开盘（T+1 open）。涨停买不进，跌停卖不出。"

class SourceFailed(RuntimeError):
    def __init__(self, message, expected=None):
        super().__init__(message)
        self.expected = expected

def completed_session(dates, now):
    """最近一个已收盘的 A 股交易日。15:10 前当天 K 线不算数，与原先数据日规则相同。"""
    if not dates:
        return None
    last = dates[-1]
    if last == now.date().isoformat() and now.hour * 60 + now.minute < 15 * 60 + 10 and len(dates) > 1:
        return dates[-2]
    return last

def lags(data_date, expected):
    return bool(data_date and expected and data_date < expected)

def ths_lbc(high_days):
    """只认「首板」、「N连板」和「N天N板」。天数和板数不同的「N天M板」不是连板。"""
    if not isinstance(high_days, str):
        return None
    s = high_days.strip()
    if s == "首板":
        return 1
    m = re.fullmatch(r"(\d+)连板", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d+)天(\d+)板", s)
    if m and m.group(1) == m.group(2):
        return int(m.group(1))
    return None

def ths_hms(ts):
    if not ts:
        return None
    return int(dt.datetime.fromtimestamp(int(ts), common.BJ).strftime("%H%M%S"))

def ths_em_row(x):
    latest = x.get("latest")
    open_num = x.get("open_num")
    return {
        "c": x.get("code"),
        "n": x.get("name") or "",
        "lbc": ths_lbc(x.get("high_days")),
        "fbt": ths_hms(x.get("first_limit_up_time")),
        "zbc": open_num if isinstance(open_num, int) else None,
        "hs": x.get("turnover_rate"),
        "ltsz": x.get("currency_value"),
        "amount": None,
        "hybk": "",
        "p": None if latest is None else int(round(float(latest) * 1000)),
    }

def tencent_index_dates():
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,40,qfq"
    j = json.loads(common.get(url))
    node = (j.get("data") or {}).get("sh000001") or {}
    rows = node.get("qfqday") or node.get("day") or []
    dates = [r[0] for r in rows if isinstance(r, list) and r and isinstance(r[0], str)]
    if len(dates) < 2:
        raise RuntimeError("腾讯交易日不足")
    return dates

def load_trading_dates(end):
    try:
        return index_dates(end), None
    except Exception as e:
        try:
            return tencent_index_dates(), "东财交易日历：" + str(e)
        except Exception as e2:
            raise SourceFailed("交易日历主源与备用都失败：%s；腾讯：%s。沿用上一份数据，不编造行情。" % (e, e2))

def ths_backup_pool(date):
    rows, total = [], None
    for page in range(1, 6):
        url = ("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?page=%d&limit=200&date=%s"
               "&field=%s&order_field=330324&order_type=0") % (page, date, THS_BACKUP_FIELDS)
        j = json.loads(common.get(url, {"Referer": "https://data.10jqka.com.cn/"}))
        data = j.get("data") or {}
        if j.get("status_code") not in (0, "0", None) and not data.get("info"):
            raise RuntimeError("同花顺涨停池 status=%s" % j.get("status_code"))
        info = data.get("info") or []
        total = (data.get("page") or {}).get("total", total)
        rows.extend(info)
        if not info or (total is not None and len(rows) >= int(total)):
            break
    return total, {x["code"]: x for x in rows if x.get("code")}

def ths_pool_total(path, date):
    url = "https://data.10jqka.com.cn/dataapi/limit_up/%s?page=1&limit=1&date=%s" % (path, date)
    j = json.loads(common.get(url, {"Referer": "https://data.10jqka.com.cn/"}))
    if j.get("status_code") not in (0, "0", None):
        raise RuntimeError("status=%s" % j.get("status_code"))
    total = ((j.get("data") or {}).get("page") or {}).get("total")
    if total is None:
        raise RuntimeError("无家数")
    return int(total)

def load_side_count(em_path, date, sort, pagesize, backup_path):
    try:
        tc, _ = pool(em_path, date, sort, pagesize)
        if tc is None:
            raise RuntimeError("无家数")
        return tc, None, None
    except Exception as e:
        try:
            return ths_pool_total(backup_path, date), "ths-" + backup_path, "东财%s：%s；已改用同花顺家数" % (em_path, e)
        except Exception as e2:
            return None, None, "东财%s：%s；同花顺备用：%s" % (em_path, e, e2)

def load_zt(date):
    primary_err = None
    try:
        tc, rows = pool("getTopicZTPool", date, "fbt:asc")
        if rows:
            return tc, rows, None, "eastmoney-zt", None
        primary_err = "东财涨停池为空"
    except Exception as e:
        primary_err = "东财涨停池：" + str(e)
    try:
        total, info = ths_backup_pool(date)
        rows = [ths_em_row(x) for x in info.values()]
        if not rows:
            raise RuntimeError("同花顺涨停池为空")
        return total, rows, info, "ths-limit-up-backup", primary_err
    except Exception as e:
        raise RuntimeError(primary_err + "；备用同花顺涨停池：" + str(e))

def apply_stale(doc, reason, expected=None):
    out = dict(doc)
    out["stale"] = True
    out["stale_reason"] = reason
    if expected:
        out["expected_date"] = expected
    failed = list(out.get("failed") or [])
    if reason not in failed:
        failed.append(reason)
    out["failed"] = failed
    return out

def write_stale_latest(reason, expected=None):
    """主源和备用都失败时保留上一份数字，只打过期标记，不改行情字段。"""
    path = os.path.join(common.ROOT, "data", "latest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    else:
        doc = {
            "updated_at": common.now_bj().strftime("%Y-%m-%d %H:%M"),
            "data_date": None,
            "sources": [],
            "failed": [],
            "themes": [],
            "pool": [],
            "kings": [],
            "sentiment": {"status": "暂停入选", "why": "没有行情", "zt": None, "dt": None, "zb": None,
                          "height": None, "zb_rate": None, "yzt_avg": None, "dt_prev": None,
                          "height_prev": None, "height_broken": None, "missing": ["数据"], "ebb": []},
            "official": {"changes": [], "codes": [], "items": []},
            "paper": {"status": "缺数据", "fill": PAPER_FILL, "note": "胜率、盈亏比、最大单笔亏损没有成交记录，保持缺数据，不编造。"},
            "config": {},
            "monitor": [],
            "monitor_ok": False,
            "untagged": 0,
            "untagged_names": [],
            "scope_note": "",
        }
    if lags(doc.get("data_date"), expected):
        extra = "数据日 %s 落后于最近一个A股交易日 %s。" % (doc["data_date"], expected)
        if extra not in reason:
            reason = reason + extra
    doc = apply_stale(doc, reason, expected)
    try:
        asof = doc.get("data_date") or expected or doc.get("expected_date")
        cfg = common.load("config.json")
        failed = list(doc.get("failed") or [])
        sources = list(doc.get("sources") or [])
        try:
            dates, note = load_trading_dates("20500101")
            if note and note not in failed:
                failed.append(note)
        except Exception as e:
            dates = []
            failed.append("附加数据交易日历：" + str(e))
        if asof and asof not in dates:
            dates.append(asof)
            dates.sort()
        extra = "策略标签和模拟盘是附加信息，不改变已经留下的四王数字。"
        note = doc.get("scope_note") or ""
        if extra not in note:
            doc["scope_note"] = (note + " " + extra).strip()
        finish_outputs(doc, stocks_from_doc(doc), cfg, asof, dates, failed, sources, None, None, False)
    except Exception as e:
        print("附加数据失败，行情过期标记仍写回：", e, file=sys.stderr)
        common.save("data/latest.json", doc)

def index_items(items):
    out = {}
    for it in items or []:
        code = it.get("code")
        if code:
            out[code] = it
    return out

def leg(code, items_by_code, weight):
    it = items_by_code.get(code) or {}
    w = it.get("weight")
    if w is None:
        w = weight
    return {"code": code, "name": it.get("name") or "", "weight": w}

def prior_book(old, asof):
    """同一天重跑时沿用已经记下的上一日持仓，避免把今天的名单当成调出。"""
    if not old:
        return [], [], None
    if old.get("rebalance_date") == asof:
        return list(old.get("prev_codes") or []), list(old.get("prev_items") or []), old.get("prev_rebalance_date")
    return list(old.get("codes") or []), list(old.get("items") or []), old.get("rebalance_date")

def make_rebalance_event(official, old, weight_each):
    asof = official["rebalance_date"]
    if old and old.get("rebalance_date") == asof:
        prev_codes = list(old.get("prev_codes") or [])
        prev_items = list(old.get("prev_items") or [])
    elif old:
        prev_codes = list(old.get("codes") or [])
        prev_items = list(old.get("items") or [])
    else:
        prev_codes, prev_items = [], []
    new_codes = list(official.get("codes") or [])
    prev_map = index_items(prev_items)
    new_map = index_items(official.get("items"))
    ins = [c for c in new_codes if c not in set(prev_codes)]
    outs = [c for c in prev_codes if c not in set(new_codes)]
    changes = list(official.get("changes") or [])
    return {
        "date": asof,
        "in": [leg(c, new_map, weight_each) for c in ins],
        "out": [leg(c, prev_map, weight_each) for c in outs],
        "weights": [leg(c, new_map, weight_each) for c in new_codes],
        "changes": changes,
        "reason": "；".join(changes),
        "computed_at": official.get("computed_at") or "",
    }

def upsert_events(events, event):
    events = [e for e in events if e.get("date") != event["date"]]
    events.append(event)
    events.sort(key=lambda e: e["date"])
    return events

def save_history_event(event):
    path = os.path.join(common.ROOT, "data", "history.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            hist = json.load(f)
    else:
        hist = {"note": HISTORY_NOTE, "events": []}
    hist.setdefault("note", HISTORY_NOTE)
    hist["events"] = upsert_events(hist.get("events") or [], event)
    common.save("data/history.json", hist)


def stocks_from_doc(doc):
    stocks, seen = [], set()
    for theme in doc.get("themes") or []:
        for member in theme.get("members") or []:
            code = member.get("code")
            if code and code not in seen:
                seen.add(code)
                stocks.append(member)
    return stocks


def finish_outputs(out, stocks, cfg, asof, dates, failed, sources, official, old, write_official):
    """附加数据、策略标签和模拟盘。失败只打标，不改四王，不删除池内股票。"""
    pool_before = [row.get("code") for row in (out.get("pool") or [])]
    try:
        out.update(enrich.gather(out.get("pool") or [], out.get("themes") or [], stocks or [], cfg, asof, failed, sources))
    except Exception as e:
        failed.append("数据增强：" + str(e))
        out.setdefault("limitup_reason", {"status": "数据缺失", "date": asof, "count": 0, "detail_missing": 0})
        for key in ("lhb", "chip_open", "chip_end"):
            out.setdefault(key, {"status": "数据缺失", "date": asof, "rows": [], "count": 0, "error": str(e)})
        out.setdefault("concept_flow", {"status": "数据缺失", "date": asof, "matched": [], "top": [], "count": 0, "error": str(e)})
    try:
        strategy_tags.apply(out.get("pool") or [], asof, failed, sources)
    except Exception as e:
        failed.append("策略标签：" + str(e))
        for row in out.get("pool") or []:
            row["strategy_tags"] = []
            row["strategy_status"] = "数据缺失"
    if [row.get("code") for row in (out.get("pool") or [])] != pool_before:
        raise RuntimeError("附加数据改变了股票池")
    out["strategy_rules"] = strategy_tags.RULES
    out["failed"] = failed
    out["sources"] = sources
    if write_official and official is not None:
        common.save("data/official.json", official)
        save_history_event(make_rebalance_event(official, old, cfg["weight_each"]))
    try:
        hist_path = os.path.join(common.ROOT, "data", "history.json")
        if os.path.exists(hist_path):
            with open(hist_path, encoding="utf-8") as f:
                hist = json.load(f)
        else:
            hist = {"events": []}
        paper_doc = paper.build(hist, dates or [], asof)
    except Exception as e:
        failed.append("模拟盘：" + str(e))
        out["failed"] = failed
        paper_doc = paper.failed_doc(asof, str(e))
    common.save("data/paper.json", paper_doc)
    out["paper"] = paper.summary(paper_doc)
    common.save("data/latest.json", out)

def run():
    cfg = common.load("config.json")
    dates, cal_note = load_trading_dates("20500101")
    expected = completed_session(dates, common.now_bj())
    asof = os.environ.get("ASOF")
    if asof:
        asof = asof[:10]
        if asof not in dates:
            dates.append(asof)
            dates.sort()
    else:
        asof = expected
    prev_date = dates[dates.index(asof) - 1] if asof in dates and dates.index(asof) > 0 else None
    print("数据日", asof, "前一交易日", prev_date, "最近交易日", expected)
    try:
        zt_tc, zt, backup_ths, zt_source, zt_err = load_zt(ymd(asof))
    except RuntimeError as e:
        raise SourceFailed(str(e) + "。沿用上一份数据，不编造行情。", expected)
    failed, sources = [], [zt_source]
    if zt_err:
        failed.append(zt_err)
    if cal_note:
        failed.append(cal_note)
        sources.append("tencent-calendar-backup")
    dt_tc, dt_src, dt_note = load_side_count("getTopicDTPool", ymd(asof), "fund:asc", 20, "lower_limit_pool")
    zb_tc, zb_src, zb_note = load_side_count("getTopicZBPool", ymd(asof), "fbt:asc", 20, "open_limit_pool")
    for note, src in ((dt_note, dt_src), (zb_note, zb_src)):
        if note:
            failed.append(note)
        if src:
            sources.append(src)
    yzt_avg = None
    try:
        y_tc, yrows = pool("getYesterdayZTPool", ymd(asof), "zs:desc")
        yzt_avg = sum(x["zdp"] for x in yrows) / len(yrows) if yrows else None
        if y_tc and len(yrows) < y_tc:
            failed.append("昨日涨停池未取全 %d/%s" % (len(yrows), y_tc))
    except Exception as e:
        failed.append("昨日涨停池：" + str(e))
    if backup_ths is not None:
        ths_total, ths = zt_tc, backup_ths
    else:
        ths_total, ths = None, {}
        try:
            ths_total, ths = ths_pool(ymd(asof))
            sources.append("ths-limit-up")
        except Exception as e:
            failed.append("同花顺涨停原因：" + str(e))
    mon = None
    try:
        mon = monitor(asof)
        sources.append("eastmoney-monitor")
    except Exception as e:
        failed.append("重点监控：" + str(e))
    mon_codes = None if mon is None else {m["code"] for m in mon}
    prev = {"date": prev_date, "dt": None, "height": None, "broken": None, "today_height": None}
    if prev_date:
        try:
            ptc, pz = pool("getTopicZTPool", ymd(prev_date), "fbt:asc")
            pdt, _ = pool("getTopicDTPool", ymd(prev_date), "fund:asc", 5)
            prev["dt"] = pdt
            if pz:
                prev["height"] = max(x["lbc"] for x in pz)
                today_codes = {x["c"] for x in zt}
                if prev["height"] >= 2:
                    leaders = [x["c"] for x in pz if x["lbc"] == prev["height"]]
                    prev["broken"] = all(c not in today_codes for c in leaders)
                    prev["leaders"] = leaders
                else:
                    prev["broken"] = False
            sources.append("eastmoney-prev")
        except Exception as e:
            failed.append("前一交易日涨跌停：" + str(e))
    stocks = []
    for x in zt:
        code, name = x["c"], x["n"]
        info = ths.get(code) or {}
        reason = info.get("reason_type") or ""
        limit_type = info.get("limit_up_type") or ""
        if not limit_type and le_925(x.get("fbt")) and not x.get("zbc"):
            limit_type = "一字板"
        ts = info.get("first_limit_up_time")
        ths_hms = None
        if ts:
            ths_hms = int(dt.datetime.fromtimestamp(int(ts), common.BJ).strftime("%H%M%S"))
        tail = ge_1430(x.get("fbt")) or ge_1430(ths_hms)
        tid, tname, denied = classify(reason, cfg) if reason else (None, None, False)
        if not reason:
            tid, tname = None, None
        lbc_raw = x.get("lbc")
        stocks.append({
            "code": code, "name": name, "lbc": 0 if lbc_raw is None else lbc_raw,
            "lbc_known": lbc_raw is not None, "fbt": x.get("fbt"),
            "fbt_text": hms_text(x.get("fbt")), "zbc": x.get("zbc"),
            "hs": round(x["hs"], 2) if x.get("hs") is not None else None,
            "float_yi": round(x["ltsz"] / 1e8, 2) if x.get("ltsz") else None,
            "amount_yi": round(x["amount"] / 1e8, 2) if x.get("amount") else None,
            "hybk": x.get("hybk") or "", "close": round(x["p"] / 1000, 2) if x.get("p") else None,
            "reason": reason, "limit_type": limit_type, "tail": tail,
            "theme_id": tid, "theme": tname, "theme_denied": denied,
            "limit_pct": limit_pct(code, name),
        })
    if stocks and all(s["lbc_known"] for s in stocks):
        prev["today_height"] = max(s["lbc"] for s in stocks)
    else:
        prev["today_height"] = None
    themes = build_themes(stocks, cfg, asof)
    shown = [t for t in themes if t["n"] >= 3 or t["qualified"]] or themes[:8]
    sent = sentiment(zt_tc, dt_tc, zb_tc, yzt_avg, prev)
    # 正式四王：题材四条全过，且情绪不是暂停入选。本轮天数与中军未核，通常为空。
    kings = []
    if sent["status"] != "暂停入选":
        cap_n = 1 if sent["status"] == "退潮" else cfg["kings"]
        for t in themes:
            if len(kings) >= cap_n:
                break
            if not t["qualified"]:
                continue
            kings.append(t["name"])
    reasons = []
    if not any(t["qualified"] for t in themes):
        reasons.append("没有题材同时通过催化、梯队、容量、天数")
    if sent["status"] == "暂停入选":
        reasons.append("情绪数据不完整，暂停入选")
    elif sent["status"] == "退潮":
        reasons.append("情绪退潮，新开仓压到半仓以下（最多1只、7.5%）")
    if not kings:
        reasons.append("四王留空，不凑数")
    opath = os.path.join(common.ROOT, "data", "official.json")
    old = {}
    if os.path.exists(opath):
        old = __import__("json").load(open(opath, encoding="utf-8"))
    prev_codes, prev_items, prev_rebalance_date = prior_book(old, asof)
    official = {
        "rebalance_date": asof,
        "next_rebalance": next_weekday(asof),
        "freq": "daily",
        "codes": [],
        "prev_codes": prev_codes,
        "prev_items": prev_items,
        "prev_rebalance_date": prev_rebalance_date,
        "changes": reasons,
        "items": [],
        "computed_at": common.now_bj().strftime("%Y-%m-%d %H:%M"),
    }
    untagged = sum(1 for s in stocks if not s["theme_id"])
    out = {
        "updated_at": common.now_bj().strftime("%Y-%m-%d %H:%M"),
        "data_date": asof,
        "stale": lags(asof, expected),
        "stale_reason": ("数据日 %s 落后于最近一个A股交易日 %s" % (asof, expected)) if lags(asof, expected) else "",
        "expected_date": expected,
        "prev_date": prev_date,
        "next_date": next_weekday(asof),
        "sources": sources,
        "failed": failed,
        "ths_total": ths_total,
        "zt_count": zt_tc,
        "untagged": untagged,
        "untagged_names": ["%s %s" % (s["name"], s["code"]) for s in stocks if not s["theme_id"]],
        "sentiment": sent,
        "themes": shown,
        "pool": stock_rows(shown, mon_codes),
        "kings": [],
        "official": official,
        "monitor": mon or [],
        "monitor_ok": mon is not None,
        "paper": {"status": "数据缺失", "fill": PAPER_FILL, "note": ""},
        "config": {
            "kings": cfg["kings"], "weight_each": cfg["weight_each"], "weight_cap": cfg["weight_cap"],
            "stop_pct": cfg["stop_pct"], "no_chase_gap": cfg["no_chase_gap"],
            "echelon_min": cfg["echelon_min"], "lbc_min": cfg["lbc_min"],
            "float_min_yi": cfg["float_min_yi"], "float_max_yi": cfg["float_max_yi"],
            "float_mid_max_yi": cfg["float_mid_max_yi"], "amount_avg_yi": cfg["amount_avg_yi"],
        },
        "scope_note": "正式四王仍只看催化、梯队、容量、天数和情绪，这些门槛没有改。策略标签、龙虎榜、抢筹、概念资金和模拟盘都是附加信息，不参与入选。题材第几天、中军趋势、20日成交额、公告排雷未核的，仍是缺数据。",
    }
    # 观察买点只写在未入选说明里，正式四王为空就不编造价位。附加数据失败也不能改这四只的产生逻辑。
    finish_outputs(out, stocks, cfg, asof, dates, failed, sources, official, old, True)
    print("情绪", sent["status"], sent["why"])
    print("题材", [(t["name"], t["n"], t["qualified"], t["day"]["state"], t["capacity"]["text"][:40]) for t in shown[:8]])
    print("四王", kings or "空", "未归类", untagged, "失败", failed)

def main():
    try:
        run()
    except SourceFailed as e:
        write_stale_latest(str(e), e.expected)
        print("数据源失败，已标记数据过期：", e, file=sys.stderr)

def _self_test():
    cfg = {
        "generic_skip": ["三季报", "回购", "国资", "重组"],
        "themes": [
            {"id": "pv", "name": "光伏", "keywords": ["光伏", "风光"]},
            {"id": "storage", "name": "储能", "keywords": ["储能"]},
        ],
    }
    assert classify("光伏边框+机器人+三季报扭亏", cfg)[:2] == ("pv", "光伏")
    assert classify("“东方”+光伏+重组", cfg)[2] is True
    assert classify("三季报增长+光伏玻璃", cfg)[:2] == ("pv", "光伏")
    assert classify("储能并网+风光储一体化", cfg)[0] == "storage"
    before = dt.datetime(2026, 11, 9, 15, 9, tzinfo=common.BJ)
    after = dt.datetime(2026, 11, 9, 15, 10, tzinfo=common.BJ)
    dates = ["2026-11-06", "2026-11-09"]
    assert completed_session(dates, before) == "2026-11-06"
    assert completed_session(dates, after) == "2026-11-09"
    assert completed_session(["2026-11-06"], dt.datetime(2026, 11, 8, 18, tzinfo=common.BJ)) == "2026-11-06"
    assert lags("2026-11-06", "2026-11-09") is True
    assert lags("2026-11-09", "2026-11-09") is False
    assert ths_lbc("首板") == 1
    assert ths_lbc("3连板") == 3
    assert ths_lbc("6天6板") == 6
    assert ths_lbc("2天2板") == 2
    assert ths_lbc("7天3板") is None
    assert ths_lbc(None) is None
    row = ths_em_row({"code": "002088", "name": "鲁阳节能", "latest": 11.09, "high_days": "首板",
                      "turnover_rate": 12.4, "currency_value": 5611147800, "open_num": 0})
    assert row["lbc"] == 1 and row["p"] == 11090 and row["n"] == "鲁阳节能"
    assert ths_em_row({"code": "601188", "name": "龙江交通", "high_days": "7天3板", "latest": 3.81})["lbc"] is None
    old = {"rebalance_date": "2026-11-06", "codes": ["000001"], "items": [{"code": "000001", "name": "平安银行", "weight": 7.5}]}
    new = {"rebalance_date": "2026-11-09", "codes": ["000002"], "items": [{"code": "000002", "name": "万科A"}],
           "changes": ["替换"], "computed_at": "2026-11-09 16:00"}
    ev = make_rebalance_event(new, old, 7.5)
    assert ev["in"] == [{"code": "000002", "name": "万科A", "weight": 7.5}]
    assert ev["out"] == [{"code": "000001", "name": "平安银行", "weight": 7.5}]
    assert ev["weights"][0]["code"] == "000002" and ev["reason"] == "替换"
    same = {"rebalance_date": "2026-11-09", "codes": ["000002"], "items": [{"code": "000002", "name": "万科A"}],
            "prev_codes": ["000001"], "prev_items": [{"code": "000001", "name": "平安银行", "weight": 7.5}]}
    ev2 = make_rebalance_event(new, same, 7.5)
    assert [x["code"] for x in ev2["out"]] == ["000001"]
    assert [x["code"] for x in ev2["in"]] == ["000002"]
    assert prior_book(same, "2026-11-09")[0] == ["000001"]
    empty = make_rebalance_event(
        {"rebalance_date": "2026-11-09", "codes": [], "items": [], "changes": ["四王留空，不凑数"], "computed_at": "2026-11-09 20:04"},
        {"rebalance_date": "2026-11-09", "codes": [], "prev_codes": [], "items": [], "prev_items": []}, 7.5)
    assert empty["in"] == [] and empty["out"] == [] and empty["weights"] == []
    kept = upsert_events([{"date": "2026-11-06", "in": []}, empty], dict(empty, changes=["四王留空，不凑数"]))
    assert [e["date"] for e in kept] == ["2026-11-06", "2026-11-09"]
    doc = {"zt_count": 54, "data_date": "2026-11-06", "themes": [{"n": 14}], "failed": [], "sentiment": {"zt": 54}}
    flagged = apply_stale(doc, "主源与备用都失败。", "2026-11-09")
    assert flagged["stale"] is True and flagged["zt_count"] == 54 and flagged["themes"][0]["n"] == 14
    assert flagged["sentiment"]["zt"] == 54 and "stale" not in doc
    page = open(os.path.join(common.ROOT, "index.html"), encoding="utf-8").read()
    assert "数据过期" in page and "T+1 open" in page and "涨停买不进" in page and "跌停卖不出" in page
    readme = open(os.path.join(common.ROOT, "README.md"), encoding="utf-8").read()
    assert "T+1 open" in readme and "涨停买不进" in readme and "数据过期" in readme
    assert "InStock" in readme and "Apache-2.0" in readme
    assert "模拟盘" in page and "策略标签" in page
    workflow = open(os.path.join(common.ROOT, ".github", "workflows", "update.yml"), encoding="utf-8").read()
    assert "data/paper.json" in workflow
    import strategy_tags, enrich, paper
    strategy_tags.self_test()
    enrich.self_test()
    paper.self_test()
    hist = json.load(open(os.path.join(common.ROOT, "data", "history.json"), encoding="utf-8"))
    by = {e["date"]: e for e in hist["events"]}
    for day in ("2026-11-06", "2026-11-09"):
        assert by[day]["in"] == [] and by[day]["out"] == [] and by[day]["weights"] == []
        assert by[day]["changes"]
    print("self-test ok")

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        main()
