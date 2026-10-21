# Created by 青云 on 2026/10/21.
"""Home Prices 数据抓取：统计局 70 个大中城市商品住宅销售价格指数。只用标准库。

每月一份发布稿（统计局「最新发布」），含新建商品住宅、二手住宅的环比 / 同比 / 年内平均，以及按面积分段的指数。
首次运行从 START 月起回填历史；之后只看最新几页列表，有新的一期才抓。
"""
import gzip, html, json, re, time, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
BASE = "https://www.stats.gov.cn/sj/zxfb/"
START = "2022-01"                   # 回填起点
SIZE_MONTHS = 13                    # 面积分段只保留最近 13 个月
TIER1 = ["北京", "上海", "广州", "深圳"]
TIER2 = ["天津", "石家庄", "太原", "呼和浩特", "沈阳", "大连", "长春", "哈尔滨", "南京", "杭州", "宁波", "合肥", "福州",
         "厦门", "南昌", "济南", "青岛", "郑州", "武汉", "长沙", "南宁", "海口", "重庆", "成都", "贵阳", "昆明", "西安",
         "兰州", "西宁", "银川", "乌鲁木齐"]


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "ignore")


def clean(s):
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", s)))


def num(s):
    try:
        return float(s)
    except ValueError:
        return None


# ── 列表页：找出每期 70 城发布稿的地址和发布日期 ───────────
def list_page(n):
    t = fetch(BASE + ("index.html" if n == 0 else f"index_{n}.html"))
    out = []
    for m in re.finditer(r'href="(\./\d{6}/t\d+_\d+\.html)"[^>]*title=\'(\d{4})年(\d{1,2})月份70个大中城市商品住宅销售价格变动情况\'', t):
        url, y, mo = m.groups()
        tail = t[m.end():m.end() + 800]
        pub = re.search(r"(20\d\d-\d\d-\d\d)", tail)
        out.append({"month": f"{y}-{int(mo):02d}", "url": BASE + url[2:], "published": pub.group(1) if pub else ""})
    return out


# ── 发布稿：表 0 新建、表 1 二手（每行两个城市），表 2~5 按面积分段 ──
def parse_release(url, tries=3):
    # 连续下载时偶尔拿到不完整的页面（疑似限流），重试并逐次拉长间隔
    for k in range(tries):
        t = fetch(url)
        tables = re.findall(r"<table.*?</table>", t, re.S)
        if len(tables) >= 6 and t.count("北京") >= 2:
            break
        print(f"  retry {k + 1}: len={len(t)} tables={len(tables)}")
        time.sleep(5 * (k + 1))
    else:
        raise ValueError(f"tables={len(tables)} len={len(t)}")
    rows = lambda tb: [[clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)] for r in re.findall(r"<tr.*?</tr>", tb, re.S)]

    def main_table(tb):
        out = {}
        for r in rows(tb):
            step = 3 if len(r) == 6 else 4                 # 1 月份没有「年内平均」列，每城只有 3 格
            for i in range(0, len(r) - step + 1, step):
                city, vals = r[i], [num(x) for x in r[i + 1:i + step]]
                if step == 3:
                    vals.append(vals[1])                   # 1 月的年内平均就等于同比
                if city and re.fullmatch(r"[一-龥]{2,5}", city) and None not in vals:
                    out[city] = vals                       # [环比, 同比, 年内平均]，上月 / 上年同月 / 上年同期 = 100
        return out

    def size_tables(tbs):
        out = {}
        for tb in tbs:
            for r in rows(tb):
                if len(r) in (7, 10) and re.fullmatch(r"[一-龥]{2,5}", r[0]):
                    v = [num(x) for x in r[1:]]
                    if len(v) == 6:                          # 1 月份每段只有环比、同比，年内平均用同比补
                        v = v[0:2] + v[1:2] + v[2:4] + v[3:4] + v[4:6] + v[5:6]
                    if None not in v:
                        out[r[0]] = [v[0:3], v[3:6], v[6:9]]   # 90㎡以下 / 90~144㎡ / 144㎡以上
        return out

    new, used = main_table(tables[0]), main_table(tables[1])
    if len(new) < 60 or len(used) < 60:
        raise ValueError(f"cities new={len(new)} used={len(used)}")
    return {"new": new, "used": used, "new_size": size_tables(tables[2:4]), "used_size": size_tables(tables[4:6])}


def tier(city):
    return 1 if city in TIER1 else 2 if city in TIER2 else 3


def main():
    now = datetime.now(CST)
    stamp = now.isoformat(timespec="seconds")
    old = json.loads(DATA_FILE.read_text("utf-8")) if DATA_FILE.exists() else {}
    months = dict(old.get("months", {}))
    sources = []

    # 1. 翻列表：已有数据时只看前 3 页；回填时一直翻到 START 之前
    found, n, err = {}, 0, None
    def expected(upto):
        y, m = map(int, START.split("-"))
        while f"{y}-{m:02d}" <= upto:
            yield f"{y}-{m:02d}"
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    # 没数据、起点不够早、或中间有缺月（比如上次某期抓失败）时，翻到 START 为止
    need_backfill = not months or any(m not in months for m in expected(max(months)))
    try:
        while True:
            items = list_page(n)
            for it in items:
                found.setdefault(it["month"], it)
            oldest = min(found) if found else "9999"
            if (not need_backfill and n >= 2) or oldest < START:
                break
            n += 1
            time.sleep(1)
    except Exception as e:
        err = e                                         # 翻到不存在的页（404）或网络错误：用已找到的
    sources.append({"name": "统计局 发布列表", "ok": bool(found), "count": len(found), "updated": stamp,
                    **({"error": str(err)[:160]} if not found and err else {})})

    # 2. 抓缺的月份（最新一期总是重抓，防止统计局更正）
    todo = sorted(m for m in found if m >= START and (m not in months or m == max(found)))
    ok, fail = 0, []
    for m in todo:
        try:
            r = parse_release(found[m]["url"])
            months[m] = {"url": found[m]["url"], "published": found[m]["published"], **r}
            ok += 1
        except Exception as e:
            fail.append(f"{m}: {e}")
            print("FAIL", m, e)
        time.sleep(1.5)
    sources.append({"name": "统计局 70 城发布稿", "ok": not fail, "count": ok, "updated": stamp,
                    **({"error": "; ".join(fail)[:300]} if fail else {})})

    # 面积分段只留最近 SIZE_MONTHS 个月
    keep = set(sorted(months)[-SIZE_MONTHS:])
    for m, v in months.items():
        if m not in keep:
            v.pop("new_size", None)
            v.pop("used_size", None)

    # 3. 摘要（给页面顶部和 pages-dashboard）
    summary = {}
    if months:
        last = max(months)
        L = months[last]

        def counts(kind, mm):
            vals = [v[0] for v in months[mm][kind].values()]
            return {"up": sum(x > 100 for x in vals), "flat": sum(x == 100 for x in vals), "down": sum(x < 100 for x in vals)}

        def tier_avg(kind, mm):
            out = {}
            for t in (1, 2, 3):
                vs = [v for c, v in months[mm][kind].items() if tier(c) == t]
                out[t] = [round(sum(x[0] for x in vs) / len(vs), 2), round(sum(x[1] for x in vs) / len(vs), 2)] if vs else None
            return out

        summary = {
            "month": last, "published": L["published"],
            "counts": {k: counts(k, last) for k in ("new", "used")},
            "tiers": {k: tier_avg(k, last) for k in ("new", "used")},
            "tier1": {c: {"new": L["new"].get(c), "used": L["used"].get(c)} for c in TIER1},
        }

    data = {
        "updated": stamp,               # 每次都变 = 心跳，保证每次运行都有提交
        "tiers": {"1": TIER1, "2": TIER2},
        "months": dict(sorted(months.items())),
        "summary": summary,             # 字段只增不减，pages-dashboard 会读
        "sources": sources,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print("months", len(months), min(months, default=None), "→", max(months, default=None), "fetched", ok, "fail", fail)
    if summary:
        print("summary", summary["month"], summary["counts"], summary["tiers"]["used"])


if __name__ == "__main__":
    main()
