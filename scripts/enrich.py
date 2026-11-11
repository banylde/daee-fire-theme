# 盘后数据增强。抓取逻辑对照 InStock（Apache-2.0），用标准库直连公开接口，不部署原项目，不用数据库。
# 来源：https://github.com/myhhub/stock
#   instock/core/crawling/stock_limitup_reason.py  同花顺涨停原因 / 详因
#   instock/core/crawling/stock_lhb_em.py         东财龙虎榜详情 stock_lhb_detail_em
#   instock/core/crawling/stock_chip_race.py      通达信早盘抢筹 / 尾盘抢筹
#   instock/core/crawling/stock_fund_em.py        东财板块资金流，sector_type=概念资金流，indicator=今日
# 任一接口失败只把对应字段标成「数据缺失」，不中断构建，不删除股票。
# 题材归类仍用同花顺涨停池的 reason_type，这里的揭密原因只展示，避免另一路接口改写四王。
import html as html_lib, json, re, time
import common

MISSING = "数据缺失"
# 通达信竞价接口要求 body 里带一个非空 Token。空串会返回 {}。
# 这里用字面量 public，不是账户、券商或本仓库的密钥。失败则整表标数据缺失。
CHIP_TOKEN = "public"
CHIP_URL = "http://excalc.icfqs.com:7616/TQLEX?Entry=HQServ.hq_nlp"


def norm_code(code):
    s = str(code or "").strip()
    return s.zfill(6) if s.isdigit() else s


def _num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_reason_payload(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    out = {}
    for row in data or []:
        if isinstance(row, dict):
            code = norm_code(row.get("code") or row.get("代码"))
            if not code:
                continue
            out[code] = {
                "id": row.get("id") or row.get("ID"),
                "name": row.get("name") or row.get("名称") or "",
                "reason": row.get("reason") or row.get("原因") or "",
            }
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            code = norm_code(row[2])
            if code:
                out[code] = {"id": row[0], "name": row[1] or "", "reason": row[3] or ""}
    return out


def parse_detail_html(text):
    if not text:
        return ""
    match = re.search(r"var data = '(.*?)';", text, re.S)
    if not match:
        return ""
    raw = html_lib.unescape(match.group(1))
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = raw.replace("\r", "").replace("\n", "")
    return re.sub(r"\s+", " ", raw).strip()[:240]


def parse_lhb_page(payload):
    result = (payload or {}).get("result") or {}
    rows = []
    for row in result.get("data") or []:
        code = norm_code(row.get("SECURITY_CODE"))
        if not code:
            continue
        rows.append({
            "code": code,
            "name": row.get("SECURITY_NAME_ABBR") or "",
            "reason": row.get("EXPLANATION") or "",
            "close": _num(row.get("CLOSE_PRICE")),
            "pct": _num(row.get("CHANGE_RATE")),
            "net": _num(row.get("BILLBOARD_NET_AMT")),
            "buy": _num(row.get("BILLBOARD_BUY_AMT")),
            "sell": _num(row.get("BILLBOARD_SELL_AMT")),
            "deal": _num(row.get("BILLBOARD_DEAL_AMT")),
        })
    pages = result.get("pages") or (1 if rows else 0)
    return rows, int(pages or 0)


def merge_lhb(rows):
    """同一只股票多条上榜原因时合并原因。净买额相同视为重复，取一条；不同则保留绝对值较大的一条。"""
    grouped = {}
    for row in rows:
        cur = grouped.get(row["code"])
        if cur is None:
            grouped[row["code"]] = dict(row, reasons=[row["reason"]] if row["reason"] else [])
            continue
        if row["reason"] and row["reason"] not in cur["reasons"]:
            cur["reasons"].append(row["reason"])
        if row["net"] is not None and (cur["net"] is None or abs(row["net"]) > abs(cur["net"])):
            reasons = cur["reasons"]
            grouped[row["code"]] = dict(row, reasons=reasons)
    out = []
    for row in grouped.values():
        row["reason"] = "；".join(row.pop("reasons"))
        out.append(row)
    out.sort(key=lambda r: abs(r["net"] or 0), reverse=True)
    return out


def parse_chip_datas(datas):
    rows = []
    for item in datas or []:
        if not isinstance(item, (list, tuple)) or len(item) < 12:
            continue
        pre = _num(item[2])
        opn = _num(item[3])
        deal = _num(item[4])
        rate = _num(item[5])
        trust = _num(item[6])
        filled = _num(item[7])
        last = _num(item[8])
        pre = None if pre is None else pre / 10000.0
        opn = None if opn is None else opn / 10000.0
        pct = None
        if last not in (None, 0) and pre not in (None, 0):
            pct = round((last / pre - 1.0) * 100.0, 2)
        ratio = None
        if deal not in (None, 0) and filled is not None:
            ratio = round(filled / deal * 100.0, 2)
        rows.append({
            "code": norm_code(item[0]),
            "name": str(item[1] or "").replace(" ", ""),
            "pct": pct,
            "pre_close": None if pre is None else round(pre, 4),
            "open": None if opn is None else round(opn, 4),
            "deal_amount": None if deal is None else round(deal, 2),
            "bid_rate": None if rate is None else round(rate * 100.0, 2),
            "bid_trust": None if trust is None else round(trust, 2),
            "bid_deal": None if filled is None else round(filled, 2),
            "bid_ratio": ratio,
            "days": item[10],
            "boards": item[11],
        })
    return rows


def parse_concept_diff(diff):
    rows = []
    for row in diff or []:
        if not isinstance(row, dict):
            continue
        name = row.get("f14")
        if name in (None, "", "-"):
            continue
        rows.append({
            "code": row.get("f12") or "",
            "name": name,
            "pct": _num(row.get("f3")),
            "main_net": _num(row.get("f62")),
            "main_pct": _num(row.get("f184")),
            "top_name": row.get("f204") or "",
            "top_code": norm_code(row.get("f205") or ""),
        })
    return rows


def match_concept(theme_name, keywords, concepts):
    best, score = None, 0
    for concept in concepts:
        cname = concept.get("name") or ""
        cur = 0
        if theme_name and cname == theme_name:
            cur = 1000
        elif theme_name and theme_name in cname:
            cur = 800 - min(len(cname), 200)
        else:
            for kw in keywords or []:
                if kw and kw in cname:
                    cur = max(cur, 100 + len(kw))
        if cur > score:
            best, score = concept, cur
    return best


def _fail_block(date, message):
    return {"status": MISSING, "date": date, "error": message, "rows": [], "count": 0}


def fetch_reasons(asof, pool_codes):
    url = ("http://zx.10jqka.com.cn/event/api/getharden/date/%s/orderby/date/orderway/desc/charset/GBK/"
           % asof)
    payload = json.loads(common.get(url, {"Referer": "http://zx.10jqka.com.cn/"}, timeout=15, tries=2))
    found = parse_reason_payload(payload)
    if not isinstance(payload, dict) or "data" not in payload:
        raise RuntimeError("涨停原因返回无法解析")
    want = [c for c in pool_codes if c in found and found[c].get("id")]
    deadline = time.time() + 45
    missing_detail = 0

    def one(code):
        if time.time() > deadline:
            return code, ""
        rid = found[code]["id"]
        page = "http://zx.10jqka.com.cn/event/harden/stockreason/id/%s" % rid
        try:
            raw = common.get(page, {"Referer": "http://zx.10jqka.com.cn/"}, timeout=10, tries=1)
            text = raw.decode("utf-8", "replace")
            return code, parse_detail_html(text)
        except Exception:
            return code, ""

    from concurrent.futures import ThreadPoolExecutor, as_completed
    if want:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(one, code) for code in want]
            for fut in as_completed(futs):
                code, detail = fut.result()
                found[code]["detail"] = detail
                if not detail:
                    missing_detail += 1
    return found, missing_detail


def fetch_lhb_url(asof, page):
    return ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW"
            "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLANATION,CLOSE_PRICE,CHANGE_RATE,"
            "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_DEAL_AMT"
            "&filter=(TRADE_DATE%3E%3D%27{d}%27)(TRADE_DATE%3C%3D%27{d}%27)"
            "&pageNumber={p}&pageSize=500&sortColumns=BILLBOARD_NET_AMT&sortTypes=-1&source=WEB&client=WEB"
            ).format(d=asof, p=page)


def fetch_lhb(asof):
    rows, page, pages = [], 1, 1
    while page <= pages and page <= 6:
        payload = json.loads(common.get(
            fetch_lhb_url(asof, page),
            {"Referer": "https://data.eastmoney.com/stock/tradedetail.html"}, timeout=15, tries=2))
        chunk, pages = parse_lhb_page(payload)
        rows.extend(chunk)
        if not chunk:
            break
        page += 1
    return merge_lhb(rows)


def fetch_chip(asof, period):
    body = [{"funcId": 20, "offset": 0, "count": 100, "sort": 1, "period": period,
             "Token": CHIP_TOKEN, "modname": "JJQC", "date": asof}]
    payload = json.loads(common.post_json(
        CHIP_URL, body,
        {"User-Agent": "Mozilla/5.0", "Referer": "http://excalc.icfqs.com/"}, timeout=15, tries=2))
    if not isinstance(payload, dict) or "datas" not in payload:
        raise RuntimeError("抢筹返回无法解析")
    return parse_chip_datas(payload.get("datas") or [])


def _concept_page(page):
    """东财概念板块资金。带 ut 的地址会间歇 502，备用地址不带 ut。"""
    urls = [
        ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
         "&fltt=2&invt=2&fid=f62&fs=m:90+t:3&fields=f12,f14,f3,f62,f184,f204,f205") % page,
        ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
         "&ut=b2884a393a59ad64002292a3e90d46a5&fltt=2&invt=2&fid=f62&fs=m:90+t:3"
         "&fields=f12,f14,f3,f62,f184,f204,f205") % page,
    ]
    err = None
    for url in urls:
        try:
            return json.loads(common.get(url, {"Referer": "https://data.eastmoney.com/bkzj/gn.html"}, timeout=8, tries=1))
        except Exception as e:
            err = e
    raise err


def fetch_concepts():
    rows, page, total = [], 1, None
    while page <= 8:
        try:
            payload = _concept_page(page)
        except Exception:
            if rows:
                return rows, "概念资金流向未取全 %d/%s" % (len(rows), total or "?")
            raise
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        total = data.get("total") if data.get("total") is not None else total
        rows.extend(parse_concept_diff(diff))
        if not diff or (total is not None and page * 100 >= int(total)):
            break
        page += 1
        time.sleep(0.15)
    if not rows:
        raise RuntimeError("概念资金流向为空")
    if total and len(rows) + 5 < int(total):
        return rows, "概念资金流向未取全 %d/%s" % (len(rows), total)
    return rows, None


def _yi_text(net, pct, name):
    if net is None:
        return name or ""
    signed = "%+.2f亿" % (net / 1e8)
    if pct is None:
        return "%s %s" % (signed, name or "")
    return "%s（%+.2f%%）%s" % (signed, pct, name or "")


def _chip_text(row):
    if not row:
        return "未上榜"
    rate = row.get("bid_rate")
    ratio = row.get("bid_ratio")
    parts = []
    if rate is not None:
        parts.append("幅度%+.2f%%" % rate)
    if ratio is not None:
        parts.append("占比%.1f%%" % ratio)
    return " ".join(parts) or "上榜"


def annotate(pool, stocks, reason_map, lhb_map, open_map, end_map, reason_ok, lhb_ok, open_ok, end_ok):
    ths = {}
    for row in stocks or []:
        code = norm_code(row.get("code"))
        if code and row.get("reason"):
            ths[code] = row.get("reason")
    for row in pool:
        code = norm_code(row.get("code"))
        info = (reason_map or {}).get(code) or {}
        reason = info.get("reason") or ths.get(code) or ""
        if reason:
            row["limitup_reason"] = reason
            row["limitup_reason_source"] = "ths-getharden" if info.get("reason") else "ths-limit-up-pool"
        elif reason_ok:
            row["limitup_reason"] = "数据缺失"
            row["limitup_reason_source"] = ""
        else:
            row["limitup_reason"] = MISSING
            row["limitup_reason_source"] = ""
        detail = info.get("detail") or ""
        row["limitup_detail"] = detail or MISSING
        if lhb_ok:
            hit = (lhb_map or {}).get(code)
            row["lhb_text"] = ("净买 %+.2f亿 %s" % ((hit["net"] or 0) / 1e8, hit["reason"])) if hit else "未上榜"
            row["lhb_net"] = None if not hit else hit.get("net")
        else:
            row["lhb_text"] = MISSING
            row["lhb_net"] = None
        if open_ok:
            row["chip_open_text"] = _chip_text((open_map or {}).get(code))
        else:
            row["chip_open_text"] = MISSING
        if end_ok:
            row["chip_end_text"] = _chip_text((end_map or {}).get(code))
        else:
            row["chip_end_text"] = MISSING


def attach_concept(themes, concepts, keywords_by_id, ok, error):
    matched = []
    for theme in themes:
        if not ok:
            theme["concept_flow"] = {"status": MISSING, "text": MISSING}
            continue
        hit = match_concept(theme.get("name"), keywords_by_id.get(theme.get("id")) or [], concepts)
        if not hit:
            theme["concept_flow"] = {"status": "未匹配", "text": "未匹配到概念板块"}
            continue
        text = _yi_text(hit.get("main_net"), hit.get("main_pct"), hit.get("name"))
        flow = {
            "status": "ok", "text": text, "concept": hit.get("name"),
            "concept_code": hit.get("code"), "main_net": hit.get("main_net"),
            "main_pct": hit.get("main_pct"), "pct": hit.get("pct"),
            "top_name": hit.get("top_name") or "", "top_code": hit.get("top_code") or "",
        }
        theme["concept_flow"] = flow
        matched.append({"id": theme.get("id"), "name": theme.get("name"), **{k: flow[k] for k in
                        ("concept", "concept_code", "main_net", "main_pct", "pct", "top_name", "top_code")}})
    top = sorted([c for c in concepts if c.get("main_net") is not None], key=lambda c: c["main_net"], reverse=True)[:8]
    return {
        "status": "ok" if ok else MISSING,
        "error": error or "",
        "matched": matched,
        "top": [{k: c.get(k) for k in ("code", "name", "pct", "main_net", "main_pct", "top_name", "top_code")} for c in top],
        "count": len(concepts),
    }


def gather(pool, themes, stocks, cfg, asof, failed, sources):
    """写入池和题材的附加字段，并返回要放进 latest.json 的板块。股票一行都不删。"""
    before = [row.get("code") for row in pool]
    codes = [norm_code(row.get("code")) for row in pool if row.get("code")]
    reason_map, reason_ok, detail_missing = {}, False, 0
    try:
        reason_map, detail_missing = fetch_reasons(asof, codes)
        reason_ok = True
        sources.append("ths-limitup-reason")
        if detail_missing:
            failed.append("涨停详因数据缺失 %d 只，短原因仍保留" % detail_missing)
    except Exception as e:
        failed.append("涨停原因：" + str(e))
    lhb_rows, lhb_ok = [], False
    try:
        lhb_rows = fetch_lhb(asof)
        lhb_ok = True
        sources.append("eastmoney-lhb")
    except Exception as e:
        failed.append("龙虎榜：" + str(e))
    chip_open, chip_end = [], []
    open_ok = end_ok = False
    try:
        chip_open = fetch_chip(asof, 0)
        open_ok = True
        sources.append("tdx-chip-open")
    except Exception as e:
        failed.append("早盘抢筹：" + str(e))
    try:
        chip_end = fetch_chip(asof, 1)
        end_ok = True
        sources.append("tdx-chip-end")
    except Exception as e:
        failed.append("尾盘抢筹：" + str(e))
    concepts, flow_note, flow_ok = [], None, False
    try:
        concepts, flow_note = fetch_concepts()
        flow_ok = True
        sources.append("eastmoney-concept-flow")
        if flow_note:
            failed.append(flow_note)
    except Exception as e:
        failed.append("概念资金流向：" + str(e))
    keywords = {t.get("id"): t.get("keywords") or [] for t in (cfg.get("themes") or [])}
    lhb_map = {row["code"]: row for row in lhb_rows}
    annotate(pool, stocks, reason_map, lhb_map,
             {row["code"]: row for row in chip_open},
             {row["code"]: row for row in chip_end},
             reason_ok, lhb_ok, open_ok, end_ok)
    concept_block = attach_concept(themes, concepts, keywords, flow_ok, "" if flow_ok else (failed[-1] if failed else MISSING))
    if [row.get("code") for row in pool] != before:
        raise RuntimeError("数据增强改变了股票池")
    return {
        "limitup_reason": {
            "status": "ok" if reason_ok else MISSING,
            "date": asof,
            "count": len(reason_map),
            "detail_missing": detail_missing,
        },
        "lhb": {
            "status": "ok" if lhb_ok else MISSING,
            "date": asof,
            "count": len(lhb_rows),
            "rows": lhb_rows[:80],
            "error": "" if lhb_ok else "数据缺失",
        },
        "chip_open": {
            "status": "ok" if open_ok else MISSING,
            "date": asof,
            "count": len(chip_open),
            "rows": chip_open[:30],
            "error": "" if open_ok else "数据缺失",
        },
        "chip_end": {
            "status": "ok" if end_ok else MISSING,
            "date": asof,
            "count": len(chip_end),
            "rows": chip_end[:30],
            "error": "" if end_ok else "数据缺失",
        },
        "concept_flow": dict(concept_block, date=asof),
    }


def self_test():
    payload = {"errocode": 0, "data": [
        {"id": 1, "name": "甲", "code": "2886", "reason": "机器人"},
        ["2", "乙", "600776", "通信", "2026-11-11"],
    ]}
    found = parse_reason_payload(payload)
    assert found["002886"]["reason"] == "机器人" and found["600776"]["id"] == "2"
    detail = parse_detail_html("var title = '机器人'; var data = '公司原因：&lt;span class=&quot;hl&quot;&gt;订单&lt;/span&gt;';")
    assert "订单" in detail and "<" not in detail
    rows, pages = parse_lhb_page({"result": {"pages": 2, "data": [
        {"SECURITY_CODE": "26", "SECURITY_NAME_ABBR": "甲", "EXPLANATION": "涨幅偏离", "BILLBOARD_NET_AMT": 100.0,
         "CLOSE_PRICE": 10, "CHANGE_RATE": 10, "BILLBOARD_BUY_AMT": 200, "BILLBOARD_SELL_AMT": 100, "BILLBOARD_DEAL_AMT": 300},
        {"SECURITY_CODE": "000026", "SECURITY_NAME_ABBR": "甲", "EXPLANATION": "换手率", "BILLBOARD_NET_AMT": 100.0},
        {"SECURITY_CODE": "000026", "SECURITY_NAME_ABBR": "甲", "EXPLANATION": "振幅", "BILLBOARD_NET_AMT": 50.0},
    ]}})
    assert pages == 2
    merged = merge_lhb(rows)
    assert merged[0]["code"] == "000026" and "涨幅偏离" in merged[0]["reason"] and "换手率" in merged[0]["reason"]
    assert merged[0]["net"] == 100.0
    chip = parse_chip_datas([["002209", "达 意 隆", 126899, 139600, 9664996.6, 0.10008, 1000, 500, 13.96, 1, 4, 2]])
    assert chip[0]["code"] == "002209" and chip[0]["name"] == "达意隆"
    assert abs(chip[0]["bid_rate"] - 10.008) < 0.01
    concepts = parse_concept_diff([
        {"f12": "BK1", "f14": "机器人概念", "f3": 3.2, "f62": 1.2e8, "f184": 4.5, "f204": "锋龙", "f205": "2931"},
        {"f12": "BK2", "f14": "-", "f3": 1, "f62": 1},
    ])
    assert len(concepts) == 1 and concepts[0]["top_code"] == "002931"
    hit = match_concept("机器人", ["具身", "机器人"], concepts)
    assert hit["code"] == "BK1"
    assert match_concept("光伏", ["光伏"], concepts) is None
    pool = [{"code": "002209", "name": "达意隆"}, {"code": "000001", "name": "平安银行"}]
    annotate(pool, [{"code": "002209", "reason": "池内原因"}],
             {"002209": {"reason": "揭密原因", "detail": "详因"}},
             {}, {}, {}, True, False, True, False)
    assert [r["code"] for r in pool] == ["002209", "000001"]
    assert pool[0]["limitup_reason"] == "揭密原因" and pool[0]["limitup_detail"] == "详因"
    assert pool[0]["lhb_text"] == MISSING and pool[0]["chip_open_text"] == "未上榜"
    assert pool[1]["chip_end_text"] == MISSING and pool[1]["limitup_reason"] == "数据缺失"
    themes = [{"id": "robot", "name": "机器人", "qualified": False}]
    block = attach_concept(themes, concepts, {"robot": ["机器人"]}, True, "")
    assert themes[0]["qualified"] is False and themes[0]["concept_flow"]["status"] == "ok"
    assert block["top"][0]["name"] == "机器人概念"
    themes[0]["qualified"] = False
    attach_concept(themes, [], {"robot": []}, False, "down")
    assert themes[0]["concept_flow"]["status"] == MISSING and themes[0]["qualified"] is False
    print("enrich self-test ok")


if __name__ == "__main__":
    self_test()
