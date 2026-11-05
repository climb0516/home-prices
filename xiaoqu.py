# Created by 青云 on 2026/11/05.
"""杭州小区成交均价：从杭州网「二手房价涨跌榜」配图识别出各小区每月成交均价，用高德地理编码补坐标，写入 xiaoqu.json。

数据：潮新闻美好生活研究院每月发布、经杭州网刊登，表格注明数据来源于中指研究院（当月 / 上月二手房成交均价、环比）。
表格是图片，用 macOS Vision 识别（ocr.swift），所以只能在 macOS 本机运行，不放进 GitHub Actions。

用法：
  python3 xiaoqu.py                 # 重新处理 xiaoqu.json 里已登记的所有文章（图片和识别结果有缓存）
  python3 xiaoqu.py <文章网址> ...  # 登记新的一期再处理，例如每月新榜单出来后
高德 key：环境变量 AMAP_KEY，或本目录 .amap_key，或 ../hz-weekend/.amap_key（都不提交到仓库）。
人工修正：xiaoqu_fix.json（识别错的小区名、要剔除的行、手动坐标），见文件里的说明。
"""
import gzip, json, re, statistics, subprocess, sys, time, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "xiaoqu.json"
GEO = ROOT / "xiaoqu_geo.json"
FIX = ROOT / "xiaoqu_fix.json"
CACHE = ROOT / ".cache" / "xiaoqu"
OCR_SRC, OCR_BIN = ROOT / "ocr.swift", ROOT / ".ocr_bin"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
ADCODE = {"上城区": "330102", "拱墅区": "330105", "西湖区": "330106", "滨江区": "330108", "萧山区": "330109",
          "余杭区": "330110", "富阳区": "330111", "临安区": "330112", "临平区": "330113", "钱塘区": "330114",
          "桐庐县": "330122", "淳安县": "330127", "建德市": "330182"}
GEO_LEVELS = {"住宅区", "兴趣点", "门牌号", "单元号", "村庄"}
SEARCH_BUDGET = 80                       # 地理编码查不准时改用 POI 搜索，每次运行最多调用次数（个人额度较少）


def fetch(url, binary=False, referer=None, timeout=40):
    h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw if binary else raw.decode("utf-8", "ignore")


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


# ── 文章：所有分页的配图 ───────────────────────────
def article_key(url):
    m = re.search(r"content/(\d{4})-(\d\d)/(\d\d)/content_(\d+)", url)
    if not m:
        raise ValueError(f"不认识的文章地址：{url}")
    y, mo, d, cid = m.groups()
    return cid, f"{y}-{mo}-{d}", url[:m.end()]           # base：去掉 _N.htm 分页后缀


def article_images(url):
    cid, pub, base = article_key(url)
    first = fetch(base + ".htm")
    title = re.search(r"<title>([^<_]+)", first)
    pages = sorted({int(n) for n in re.findall(rf"content_{cid}_(\d+)\.htm", first)} - {0, 1})
    imgs = []
    for i, n in enumerate([None] + pages):
        t = first if n is None else fetch(f"{base}_{n}.htm")
        imgs += [u for u in re.findall(r'src="(https?://[^"]+/content/\d{4}-\d\d/\d\d/[0-9a-f-]{36}\.(?:jpg|jpeg|png))"', t, re.I)
                 if u not in imgs]
        time.sleep(0.5)
    return {"id": cid, "url": base + ".htm", "published": pub, "title": title.group(1).strip() if title else "", "images": imgs}


def ocr(img_path):
    tsv = img_path.with_suffix(".tsv")
    if not tsv.exists():
        if not OCR_BIN.exists() or OCR_BIN.stat().st_mtime < OCR_SRC.stat().st_mtime:
            subprocess.run(["swiftc", "-O", str(OCR_SRC), "-o", str(OCR_BIN)], check=True)
        tsv.write_text(subprocess.run([str(OCR_BIN), str(img_path)], check=True, capture_output=True, text=True).stdout)
    lines = tsv.read_text().splitlines()
    W, H = map(int, lines[0].split("\t"))
    toks = []
    for ln in lines[1:]:
        y, x, h, t = ln.split("\t", 3)
        toks.append({"y": float(y), "x": float(x), "h": float(h), "t": t.strip()})
    return W, H, toks


# ── 表格解析：以「均价」数字为锚点分行，左侧文字是小区名，带 % 的是环比 ──
PRICE = re.compile(r"^\d{1,3}(?:,?\d{3})$")
PCT = re.compile(r"(-?\s*\d+(?:\.\d+)?)\s*%")
TITLE = re.compile(r"(\d{1,2})月(.{0,8}?(?:区|县|市))?.{0,12}(?:成交|均价|涨跌)")
HEADER_WORDS = ("成交均价", "环比", "元/", "涨跌情况")   # 包含即视为表头；「小区」「楼盘」只在整段相等时才算（很多小区名以「小区」结尾）


def parse_image(W, H, toks, district):
    """返回 (title_month, district, rows)。district 沿用上一张图（表格可能跨图片续接）。"""
    month = None
    for t in sorted(toks, key=lambda t: t["y"]):
        m = TITLE.search(t["t"])
        if m and "成交均价" not in t["t"].replace(" ", "")[:6]:
            if m.group(2):
                d = m.group(2).replace("杭州", "")
                if d in ADCODE:
                    district = d
            month = month or int(m.group(1))
    # 本月 / 上月两列：价格数字的横坐标按最大间隔分成两组（表头可能被识别成一整段，不能靠表头定位）
    xs = sorted(t["x"] for t in toks if PRICE.match(t["t"].replace(" ", "")) and t["x"] > W * 0.25)
    if not xs:
        return month, district, []
    gap, cut = max(((b - a, i) for i, (a, b) in enumerate(zip(xs, xs[1:]))), default=(0, 0))
    if gap > W * 0.08:
        left, right = xs[:cut + 1], xs[cut + 1:]
        cols = [statistics.median(left), statistics.median(right)]
    else:
        cols = [statistics.median(xs)]
    name_edge = cols[0] - W * 0.08                    # 小区名列的右边界
    anchors = sorted([t for t in toks if PRICE.match(t["t"].replace(" ", "")) and t["x"] > name_edge], key=lambda t: t["y"])
    if not anchors:
        return month, district, []
    gaps = [b["y"] - a["y"] for a, b in zip(anchors, anchors[1:]) if b["y"] - a["y"] > 20]
    rowh = statistics.median(gaps) if gaps else 90
    tol = rowh * 0.45
    groups = []
    for a in anchors:
        if groups and a["y"] - groups[-1][-1]["y"] < tol:
            groups[-1].append(a)
        else:
            groups.append([a])
    rows = []
    for g in groups:
        y = sum(a["y"] for a in g) / len(g)
        near = [t for t in toks if abs(t["y"] - y) < tol]
        cur = prev = None
        for a in g:
            v = int(a["t"].replace(",", "").replace(" ", ""))
            if len(cols) > 1 and abs(a["x"] - cols[1]) < abs(a["x"] - cols[0]):
                prev = v
            else:
                cur = v
        pct = None
        for t in near:
            m = PCT.search(t["t"])
            if m and t["x"] > (cols[-1] + W * 0.05):
                pct = float(m.group(1).replace(" ", ""))
        name = "".join(t["t"] for t in sorted(near, key=lambda t: t["x"])
                       if t["x"] < name_edge and not PRICE.match(t["t"].replace(" ", "")) and not PCT.search(t["t"])
                       and not any(w in t["t"] for w in HEADER_WORDS) and t["t"].strip() not in ("小区", "楼盘", "小区名称"))
        name = re.sub(r"[\s'‘’\"|]", "", name)
        name = re.sub(r"[：:]?(?:N?E?WS|NEWS|潮新闻)$", "", name)   # 水印「潮新闻 NEWS」压在名字上时会粘进来
        name = re.sub(r"^[\-—]+|[\-—：:]+$", "", name)
        if cur is None and prev is None:
            continue
        flags, ocr_pct = [], pct
        if not name or re.search(r"\d{4,}", name):
            flags.append("名称")
        if cur is None:
            flags.append("缺本月")
        if cur and prev:                              # 环比一律用两个均价算（识别出的 % 偶尔多 / 少一位），对不上的留给人工核对
            calc = round((cur / prev - 1) * 100, 1)
            if pct is not None and abs(calc - pct) > 0.15:
                flags.append(f"环比识别{pct}≠计算{calc}")
            pct = calc
        rows.append({"name": name, "district": district, "cur": cur, "prev": prev, "pct": pct, "ocr_pct": ocr_pct, "y": round(y), "flags": flags,
                     "box": [round(y - rowh * 0.46), round(y + rowh * 0.46), 0, round(name_edge)],
                     "cols": [round(c) for c in cols], "cw": round(W * 0.1)})
    return month, district, rows


def ocr_region(img_path, y0, y1, x0, x1):
    scale = max(3, round(240 / max(1, y1 - y0)))
    out = subprocess.run([str(OCR_BIN), str(img_path.resolve()), str(y0), str(y1), str(x0), str(x1), str(scale)],
                         capture_output=True, text=True).stdout.splitlines()[1:]
    return re.sub(r"[\s'‘’\"|]", "", "".join(ln.split("\t")[3] for ln in sorted(out, key=lambda ln: float(ln.split("\t")[1]))))


def refine_prices(img_path, rows):
    """缺本月均价、两个均价算出的环比离谱（>25%，多半是某个数字丢了一位），或和识别出的环比对不上（水印压住数字认错）：
    裁出均价格去掉浅色后重新识别"""
    cache_f = img_path.with_suffix(".prices.json")
    cache = load(cache_f, {})
    changed = False
    for r in rows:
        bad = r["cur"] is None or (r["cur"] and r["prev"] and abs(r["cur"] / r["prev"] - 1) > 0.25) \
            or any(f.startswith("环比识别") for f in r["flags"])
        if not bad:
            continue
        k = str(r["y"])
        if k not in cache:
            y0, y1 = r["box"][:2]
            cache[k] = [ocr_region(img_path, y0, y1, c - r["cw"], c + r["cw"]) for c in r["cols"]]
            changed = True
        vals = [int(v) if re.fullmatch(r"\d{4,6}", v.replace(",", "")) else None for v in (s.replace(",", "") for s in cache[k])]
        ocr_pct = r["pct"]
        if vals and vals[0]:
            r["cur"] = vals[0]
        if len(vals) > 1 and vals[1]:
            r["prev"] = vals[1]
        r["flags"] = [f for f in r["flags"] if f != "缺本月" and not f.startswith("环比")]
        if r["cur"] is None:
            r["flags"].append("缺本月")
        elif r["cur"] and r["prev"]:
            calc = round((r["cur"] / r["prev"] - 1) * 100, 1)
            if abs(calc) > 25:
                r["flags"].append(f"环比{calc}过大")
            elif r["ocr_pct"] is not None and abs(calc - r["ocr_pct"]) > 0.15:
                r["flags"].append(f"环比识别{r['ocr_pct']}≠计算{calc}")
            r["pct"] = calc
    if changed:
        cache_f.write_text(json.dumps(cache, ensure_ascii=False))
    return rows


ODD = re.compile(r"[^一-鿿（）()·]")


def refine_names(img_path, rows):
    """名字为空（被「潮」字水印盖住）或含非中文字符（水印 NEWS 粘进来、生僻字误识别）的行：裁出名字格，放大、去浅色后重新识别"""
    cache_f = img_path.with_suffix(".names.json")
    cache = load(cache_f, {})
    changed = False
    for r in rows:
        if r["name"] and not ODD.search(r["name"]):
            continue
        k = str(r["y"])
        if k not in cache:
            cache[k] = ocr_region(img_path, *r["box"])
            changed = True
        if cache[k]:
            r["name"] = cache[k]
            r["flags"] = [f for f in r["flags"] if f != "名称"]
    if changed:
        cache_f.write_text(json.dumps(cache, ensure_ascii=False))
    return rows


def month_of(title_month, published):
    y, m = int(published[:4]), int(published[5:7])
    return f"{y if title_month <= m else y - 1}-{title_month:02d}"


# ── 高德地理编码 ────────────────────────────────
def amap_key():
    import os
    k = os.environ.get("AMAP_KEY", "").strip()
    for p in (ROOT / ".amap_key", ROOT.parent / "hz-weekend" / ".amap_key"):
        if not k and p.exists():
            k = p.read_text().strip()
    return k


class Quota(Exception):
    pass


def amap(path, **q):
    q["key"] = KEY
    d = json.loads(fetch(f"https://restapi.amap.com/{path}?{urllib.parse.urlencode(q)}"))
    if str(d.get("status")) != "1":
        info = d.get("info", "")
        if any(s in info for s in ("LIMIT", "QUOTA", "INVALID_USER_KEY", "USERKEY_PLAT_NOMATCH")):
            raise Quota(info)
    time.sleep(0.25)
    return d


def geocode(name, district, budget):
    """返回 (结果, 用掉的搜索次数)。结果 {"loc": [lng, lat], "how": ..., "addr": ...} 或 {"miss": 原因}"""
    d = amap("v3/geocode/geo", address=f"杭州市{district}{name}", city="330100")
    g = (d.get("geocodes") or [{}])[0]
    if g.get("location") and g.get("level") in GEO_LEVELS and (not district or g.get("district") == district):
        lng, lat = map(float, g["location"].split(","))
        return {"loc": [lng, lat], "how": "geo:" + g["level"], "addr": g.get("formatted_address", "")}, 0
    why = f"geo:{g.get('level') or '无结果'}/{g.get('district') or '-'}"
    if budget <= 0 or district not in ADCODE:
        return {"miss": why, "retry": True}, 0
    d = amap("v5/place/text", keywords=name, region=ADCODE[district], city_limit="true", types="120300|120000", page_size=1)
    p = (d.get("pois") or [{}])[0]
    if p.get("location") and p.get("adname") == district:
        lng, lat = map(float, p["location"].split(","))
        return {"loc": [lng, lat], "how": "poi", "addr": p.get("name", "") + " " + (p.get("address") or "")}, 1
    return {"miss": why + " poi:无结果"}, 1


TYPE_WORDS = re.compile(r"[（(]|别墅|排屋|叠墅|洋房|公寓")


def merge_aliases(items):
    """同一小区在不同期用了不同叫法（「候潮府」/「钱江候潮府」、「万科天空之城」/「地铁万科天空之城」）：
    坐标完全相同、一个名字包含另一个、从没在同一期同时出现 → 合并到出现期数最多的名字"""
    by_loc = {}
    for k, it in items.items():
        if it.get("loc"):
            by_loc.setdefault(tuple(it["loc"]), []).append(k)
    for keys in by_loc.values():
        keys.sort(key=lambda k: (-len(items[k]["p"]), -len(items[k]["n"])))
        for k in keys[1:]:
            a = items.get(k)
            for main_k in keys:
                b = items.get(main_k)
                if main_k == k or b is None or a is None:
                    continue
                if TYPE_WORDS.search(a["n"] + b["n"]) or not (a["n"] in b["n"] or b["n"] in a["n"]) or set(a["p"]) & set(b["p"]):
                    continue
                # 别名每一期的均价都要和主名字最近一期相差 15% 以内（防止把排屋 / 分期 / 大片区统称并进来）
                mb = sorted(b["p"])
                near = lambda m: b["p"][min(mb, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(m[:4]) * 12 - int(m[5:])))][0]
                if all(abs(v[0] / near(m) - 1) < 0.15 for m, v in a["p"].items()):
                    b["p"].update(a["p"])
                    b["p"] = dict(sorted(b["p"].items()))
                    print(f"  合并别名：{a['d']} {a['n']} → {b['n']}")
                    del items[k]
                    break


# ── 主流程 ──────────────────────────────────────
def main(new_urls):
    old = load(OUT, {})
    fix = load(FIX, {})
    rename, drop, manual = fix.get("rename", {}), set(fix.get("drop", [])), fix.get("geo", {})
    articles = {a["id"]: a for a in old.get("articles", [])}
    for u in new_urls:
        cid, _, _ = article_key(u)
        articles.setdefault(cid, {"id": cid, "url": u})
    if not articles:
        sys.exit("没有登记的文章：python3 xiaoqu.py <杭州网涨跌榜文章网址>")

    months, items, review = {}, {}, []
    for cid, a in sorted(articles.items()):
        if not a.get("images"):
            print("读取文章", a["url"])
            a.update(article_images(a["url"]))
        d = CACHE / cid
        d.mkdir(parents=True, exist_ok=True)
        district, tm, rows = None, Counter(), []
        for i, u in enumerate(a["images"]):
            p = d / f"{i:02d}{Path(urllib.parse.urlparse(u).path).suffix}"
            if not p.exists():
                p.write_bytes(fetch(u, binary=True, referer=a["url"]))
                time.sleep(0.5)
            m, district, rs = parse_image(*ocr(p), district)
            rs = refine_prices(p, refine_names(p, rs))
            if m:
                tm[m] += 1
            for r in rs:
                r["img"] = p.name
            rows += rs
        if not tm:
            print(f"  ⚠ {cid} 没识别出月份，跳过")
            continue
        month = month_of(tm.most_common(1)[0][0], a["published"])
        a["month"] = month
        n = 0
        for r in rows:
            k0 = f"{r['district']}|{r['name']}"
            k = f"{r['district']}|{rename.get(f'{month}|{k0}', rename.get(k0, r['name']))}"
            if k0 in drop or f"{month}|{k0}" in drop or r["district"] is None:
                continue
            if r["flags"] and f"{month}|{k0}" not in fix.get("ok", []):
                review.append(f"{month} {r['img']} y={r['y']} {k0} 本月={r['cur']} 上月={r['prev']} 环比={r['pct']} ⚠{'/'.join(r['flags'])}")
                if "名称" in r["flags"] or "缺本月" in r["flags"]:
                    continue
            it = items.setdefault(k, {"n": k.split("|", 1)[1], "d": r["district"], "p": {}})
            if month not in it["p"]:
                it["p"][month] = [r["cur"], r["prev"], r["pct"]]
                n += 1
        months[month] = {"url": a["url"], "published": a["published"], "title": a.get("title", ""), "count": n}
        print(f"  {month}（{a['published']} 发布）{len(a['images'])} 张图，{n} 个小区")

    # 坐标
    geo = load(GEO, {})
    global KEY
    KEY = amap_key()
    budget, calls, err = SEARCH_BUDGET, 0, None
    todo = [k for k in items if k not in manual and (k not in geo or geo[k].get("retry"))]
    if todo and not KEY:
        err = "缺少高德 key（AMAP_KEY / .amap_key / ../hz-weekend/.amap_key）"
    for k in todo if KEY else []:
        it = items[k]
        try:
            geo[k], used = geocode(it["n"], it["d"], budget)
            budget -= used
            calls += 1
        except Quota as e:
            err = f"高德额度 / key 问题：{e}，已查 {calls} 个，下次运行接着查"
            break
        if calls % 100 == 0:
            GEO.write_text(json.dumps(geo, ensure_ascii=False, indent=0, sort_keys=True))
            print(f"  已查坐标 {calls}/{len(todo)}")
    GEO.write_text(json.dumps(geo, ensure_ascii=False, indent=0, sort_keys=True))
    for k, it in items.items():
        loc = manual.get(k) or geo.get(k, {}).get("loc")
        it["loc"] = [round(loc[0], 5), round(loc[1], 5)] if loc else None

    merge_aliases(items)
    lst = sorted(items.values(), key=lambda it: (it["d"], it["n"]))
    out = {
        "updated": datetime.now(CST).isoformat(timespec="seconds"),
        "city": "杭州",
        "source": "潮新闻美好生活研究院「杭州二手房价涨跌榜」（杭州网刊登，数据来源中指研究院）",
        "months": dict(sorted(months.items())),
        "items": lst,
        "geo": {"ok": sum(1 for it in lst if it["loc"]), "miss": sum(1 for it in lst if not it["loc"]), "error": err},
        "articles": [{k: a[k] for k in ("id", "url", "published", "title", "month", "images") if k in a} for a in articles.values()],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    (CACHE / "review.txt").write_text("\n".join(review))
    print(f"小区 {len(lst)} 个，有坐标 {out['geo']['ok']}，未定位 {out['geo']['miss']}；待核对 {len(review)} 行 → .cache/xiaoqu/review.txt")
    if err:
        print("⚠", err)


KEY = ""
if __name__ == "__main__":
    main(sys.argv[1:])
