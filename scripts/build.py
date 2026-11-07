#!/usr/bin/env python3
"""四王盗火：抓取最近一个交易日的涨停结构，写 data/latest.json 与 data/official.json。

本轮不做题材天数回填，也不做纸面成交。缺数据保持缺数据，四王不凑数。
"""
import os, re, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

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
        n, height = len(mem), max(x["lbc"] for x in mem)
        has2 = any(x["lbc"] >= cfg["lbc_min"] for x in mem)
        if g["denied"]:
            cat = check("fail", "字面连名，不算事件催化")
        elif cfg["manual_catalyst"].get(g["name"]):
            cat = check("pass", cfg["manual_catalyst"][g["name"]])
        else:
            cat = check("manual", "待人工确认")
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
        day = check("missing", "缺数据：尚未落档 data/history，不能推定第几天")
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
    rows.sort(key=lambda r: (-r["n"], -r["height"], r["name"]))
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

def main():
    cfg = common.load("config.json")
    end = os.environ.get("ASOF", "20500101").replace("-", "")
    dates = index_dates(end if end != "20500101" else "20500101")
    asof = os.environ.get("ASOF")
    if asof:
        asof = asof[:10]
        if asof not in dates:
            dates.append(asof)
            dates.sort()
    else:
        asof = dates[-1]
        now = common.now_bj()
        if asof == now.date().isoformat() and now.hour * 60 + now.minute < 15 * 60 + 10 and len(dates) > 1:
            asof = dates[-2]
    prev_date = dates[dates.index(asof) - 1] if asof in dates and dates.index(asof) > 0 else None
    print("数据日", asof, "前一交易日", prev_date)
    failed, sources = [], ["eastmoney-zt"]
    zt_tc, zt = pool("getTopicZTPool", ymd(asof), "fbt:asc")
    if not zt:
        sys.exit("涨停池为空，停止写入")
    dt_tc, _ = pool("getTopicDTPool", ymd(asof), "fund:asc", 20)
    zb_tc, _ = pool("getTopicZBPool", ymd(asof), "fbt:asc", 20)
    y_tc, yrows = pool("getYesterdayZTPool", ymd(asof), "zs:desc")
    yzt_avg = sum(x["zdp"] for x in yrows) / len(yrows) if yrows else None
    if y_tc and len(yrows) < y_tc:
        failed.append("昨日涨停池未取全 %d/%s" % (len(yrows), y_tc))
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
        stocks.append({
            "code": code, "name": name, "lbc": x.get("lbc") or 0, "fbt": x.get("fbt"),
            "fbt_text": hms_text(x.get("fbt")), "zbc": x.get("zbc"),
            "hs": round(x["hs"], 2) if x.get("hs") is not None else None,
            "float_yi": round(x["ltsz"] / 1e8, 2) if x.get("ltsz") else None,
            "amount_yi": round(x["amount"] / 1e8, 2) if x.get("amount") else None,
            "hybk": x.get("hybk") or "", "close": round(x["p"] / 1000, 2) if x.get("p") else None,
            "reason": reason, "limit_type": limit_type, "tail": tail,
            "theme_id": tid, "theme": tname, "theme_denied": denied,
            "limit_pct": limit_pct(code, name),
        })
    prev["today_height"] = max(s["lbc"] for s in stocks)
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
    official = {
        "rebalance_date": asof,
        "next_rebalance": next_weekday(asof),
        "freq": "daily",
        "codes": [],
        "prev_codes": old.get("codes") or [],
        "prev_rebalance_date": old.get("rebalance_date"),
        "changes": reasons,
        "items": [],
        "computed_at": common.now_bj().strftime("%Y-%m-%d %H:%M"),
    }
    untagged = sum(1 for s in stocks if not s["theme_id"])
    out = {
        "updated_at": common.now_bj().strftime("%Y-%m-%d %H:%M"),
        "data_date": asof,
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
        "paper": {"status": "缺数据", "note": "纸面胜率、盈亏比、最大单笔亏损与成交记录下一轮再做"},
        "config": {
            "kings": cfg["kings"], "weight_each": cfg["weight_each"], "weight_cap": cfg["weight_cap"],
            "stop_pct": cfg["stop_pct"], "no_chase_gap": cfg["no_chase_gap"],
            "echelon_min": cfg["echelon_min"], "lbc_min": cfg["lbc_min"],
            "float_min_yi": cfg["float_min_yi"], "float_max_yi": cfg["float_max_yi"],
            "float_mid_max_yi": cfg["float_mid_max_yi"], "amount_avg_yi": cfg["amount_avg_yi"],
        },
        "scope_note": "本轮只核当日涨停结构、情绪和板块成交额放大的比例。题材第几天、中军趋势、20日成交额、公告排雷、纸面成交都还是缺数据。",
    }
    # 观察买点只写在未入选说明里，正式四王为空就不编造价位
    common.save("data/latest.json", out)
    common.save("data/official.json", official)
    print("情绪", sent["status"], sent["why"])
    print("题材", [(t["name"], t["n"], t["qualified"], t["day"]["state"], t["capacity"]["text"][:40]) for t in shown[:8]])
    print("四王", kings or "空", "未归类", untagged, "失败", failed)

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
    print("self-test ok")

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        main()
