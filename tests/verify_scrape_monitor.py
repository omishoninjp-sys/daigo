"""
爬取監控紀錄的驗證（spec-scrape-monitoring.md 第七節）
======================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_scrape_monitor.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_scrape_monitor.py`）

規格第七節要求的三件事：
  1. 不要只跑 py_compile
  2. 用真實會失敗的網址測每一種 failure_kind，確認分類正確
  3. 記錄層壞掉時不影響爬取

會連外（用真實網址測分類）。寫入導到暫存目錄，不碰 /data 也不碰專案目錄。
"""
import os
import sys
import json
import shutil
import asyncio
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 寫入導到暫存目錄（必須在 import scrape_monitor 之前設）
_TMP = tempfile.mkdtemp(prefix="scrapelog_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import scrape_monitor as sm
from scrapers.platform_yahoo_store import YahooStoreHttpxSource

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# ─────────────────────────────────────────────────────────────────────
def test_classify_table():
    print("\n【1】failure_kind 分類表（純函式）")
    cases = [
        ("403 → blocked",      dict(http_status=403), "blocked"),
        ("429 → blocked",      dict(http_status=429), "blocked"),
        ("404 → not_found",    dict(http_status=404), "not_found"),
        ("410 → not_found",    dict(http_status=410), "not_found"),
        ("200 抽不出欄位 → parse_failed", dict(http_status=200), "parse_failed"),
        ("逾時旗標 → timeout", dict(timed_out=True), "timeout"),
        ("例外訊息含 timeout → timeout",
         dict(error=asyncio.TimeoutError()), "timeout"),
        ("內容含 Cloudflare 特徵 → blocked",
         dict(http_status=200, block_hint=True), "blocked"),
        ("內容含販売終了 → not_found",
         dict(http_status=200, gone_hint=True), "not_found"),
        ("什麼都沒有 → other",  dict(), "other"),
        # 順序：403 同時有 gone_hint 仍應是 blocked（狀態碼優先於內容特徵）
        ("403 + gone_hint → blocked（狀態碼優先）",
         dict(http_status=403, gone_hint=True), "blocked"),
    ]
    for name, kw, expect in cases:
        got = sm.classify_failure(**kw)
        check(name, got == expect, f"得到 {got}")


# ─────────────────────────────────────────────────────────────────────
async def _fetch_and_classify(url):
    """走真的 Source._fetch（已埋 note_http），再用真的 record 邏輯分類。"""
    sm.start(url)
    src = YahooStoreHttpxSource()
    await src._fetch(url)
    state = sm._ctx.get() or {}
    kind = sm.classify_failure(
        http_status=state.get("http_status"),
        block_hint=bool(state.get("block_hint")),
        gone_hint=bool(state.get("gone_hint")),
        got_page=state.get("http_status") is not None,
    )
    return state.get("http_status"), kind


async def test_real_urls():
    print("\n【2】真實網址 → 分類（實際連外，走已埋點的 Source）")

    # not_found：真實已下架的樂天商品（先前掃 152 個網址時實測 404）
    status, kind = await _fetch_and_classify(
        "https://item.rakuten.co.jp/mighty-liquors/10000339/")
    check("真實已下架網址 → not_found", kind == "not_found",
          f"HTTP {status} → {kind}")

    # parse_failed：真實存在但不是商品頁（店鋪首頁，200 但抽不出商品）
    status, kind = await _fetch_and_classify(
        "https://store.shopping.yahoo.co.jp/soukai/")
    check("店鋪首頁（200 非商品頁）→ parse_failed", kind == "parse_failed",
          f"HTTP {status} → {kind}")

    # blocked：真實回 403 的端點
    status, kind = await _fetch_and_classify("https://httpbin.org/status/403")
    if status is None:
        print("  ⚠️ 403 端點連不到，改用內容特徵驗證 blocked（見第 1 組）")
    else:
        check("真實 403 → blocked", kind == "blocked", f"HTTP {status} → {kind}")

    # 成功對照組：真實商品頁應該是 200 且拿得到頁面
    status, kind = await _fetch_and_classify(
        "https://store.shopping.yahoo.co.jp/queensshop/yol106.html")
    check("真實商品頁 → HTTP 200", status == 200, f"HTTP {status}")


# ─────────────────────────────────────────────────────────────────────
class _FakeProduct:
    def __init__(self, ok):
        self.title = "商品" if ok else ""
        self.price_jpy = 5000 if ok else None
        self.is_valid = ok
        self.platform_id = "yahoo_store"


def test_write_and_fields():
    print("\n【3】寫入內容與隱私欄位")
    url = ("https://store.shopping.yahoo.co.jp/queensshop/yol106.html"
           "?sc_i=shopping-pc&sessionid=SECRET123&uid=who")
    sm.start(url)
    sm.note_http(200)
    sm.note_source("YahooStoreHttpxSource(scraper)")
    sm.record(url, product=_FakeProduct(True), elapsed_ms=1234)

    rows = sm.read_day()
    check("有寫進 JSONL", len(rows) >= 1, f"{len(rows)} 筆")
    if not rows:
        return
    r = rows[-1]
    check("domain 去掉 www 並小寫", r["domain"] == "store.shopping.yahoo.co.jp", r["domain"])
    check("ok=True", r["ok"] is True)
    check("成功時 failure_kind 留空", r["failure_kind"] == "")
    check("有記 source", r["source"] == "YahooStoreHttpxSource(scraper)")
    check("有記 elapsed_ms", r["elapsed_ms"] == 1234)
    check("url_path 不含 query string",
          "?" not in r["url_path"] and "SECRET123" not in json.dumps(r), r["url_path"])
    check("沒有記 IP/session/身分欄位",
          not any(k in r for k in ("ip", "session", "user", "customer")))
    check("欄位齊全（規格第一節 + 內容欄位）",
          set(r) == {"ts", "domain", "platform_id", "source", "ok", "failure_kind",
                     "http_status", "elapsed_ms", "error_brief", "url_path",
                     # 2026-09-02 加：規格第一節只記「爬取過程」，記不到「值對不對」
                     "price_jpy", "brand",
                     # 2026-09-02 加：error_brief 在 ok=True 時被清空，
                     # 成功路徑上的 note_error（退路／降級）全被丟掉
                     "warnings"},
          str(sorted(r)))

    # 失敗筆：error_brief 不可含 traceback
    sm.start(url)
    sm.note_http(500)
    try:
        raise ValueError("boom " + "x" * 500)      # 長訊息，驗證截斷
    except ValueError as e:
        sm.record(url, product=_FakeProduct(False), error=e, elapsed_ms=10)
    r = sm.read_day()[-1]
    check("失敗筆有 failure_kind", r["failure_kind"] != "", r["failure_kind"])
    brief = r["error_brief"]
    check("error_brief = 例外類型 + 訊息", brief.startswith("ValueError: boom"), brief[:40])
    check("error_brief 截到 200 字", len(brief) <= 200, f"{len(brief)} 字")
    check("error_brief 不含換行（不是整份 traceback）", "\n" not in brief)
    check("error_brief 不含堆疊框", 'File "' not in brief, brief[:60])


# ─────────────────────────────────────────────────────────────────────
def test_content_fields():
    """
    price_jpy / brand 兩個內容欄位（2026-09-02 加）。

    ★ 為什麼要記內容：2026-09-02 查到的三個實際損失（Amazon brand 污染 67%
      持續三個月、generic 取價抓到代引手数料、metafield 原價是垃圾值）
      **全部是 ok=True 的成功爬取**，五個 failure_kind 一個都涵蓋不到；
      而這份 log 當時連價格都沒記，所有調查只能繞道 Shopify Admin API。
      有了這兩欄，「同一網域的商品價格全部相同」這類掃描才做得起來
      （suqqu 9 件全 ¥550 就是這樣挖出來的）。

    🔴 最容易被日後重構改掉的是「product=None 時整筆仍要寫」。
      timeout 與例外路徑沒有 product，那正是最需要紀錄的時候 ——
      若因為取不到值就 return，逾時與例外會從 log 裡整批消失，
      失敗率統計反而憑空變好看。下面第 1 組就是釘這件事。
    """
    print("\n【5】內容欄位 price_jpy / brand")
    url = "https://item.rakuten.co.jp/shop/code/"

    # ── 1. product=None（timeout / 例外路徑）：兩欄 null，整筆仍要完整寫出
    before = len(sm.read_day())
    sm.start(url)
    sm.note_platform("rakuten")
    sm.record(url, error=TimeoutError("driver timeout after 60s"),
              elapsed_ms=60000, timed_out=True)
    rows = sm.read_day()
    check("★ product=None 仍然寫出一整筆（不可以因為取不到值就略過）",
          len(rows) == before + 1, f"{before} -> {len(rows)}")
    if len(rows) > before:
        r = rows[-1]
        check("product=None 時 price_jpy 是 null", r["price_jpy"] is None,
              repr(r["price_jpy"]))
        check("product=None 時 brand 是 null", r["brand"] is None, repr(r["brand"]))
        check("其餘欄位照常（failure_kind 仍分類得出來）",
              r["failure_kind"] == "timeout", r["failure_kind"])
        check("其餘欄位照常（platform_id 靠 note_platform 拿得到）",
              r["platform_id"] == "rakuten", r["platform_id"])
        check("欄位數與成功筆一致（不是缺欄位）", len(r) == 13, str(len(r)))

    # ── 2. 正常路徑：兩欄有值
    p = _FakeProduct(True)
    p.price_jpy = 18800
    p.brand = "京セラ(Kyocera)"
    sm.start(url)
    sm.record(url, product=p, elapsed_ms=100)
    r = sm.read_day()[-1]
    check("正常路徑 price_jpy 有值", r["price_jpy"] == 18800, repr(r["price_jpy"]))
    check("正常路徑 brand 有值", r["brand"] == "京セラ(Kyocera)", repr(r["brand"]))

    # ── 3. price_jpy <= 0 一律 null
    #    0 與「沒有」意義不同，寫成 0 會讓「同網域同價」那類掃描被一堆 0 污染
    for bad_price, label in ((0, "0"), (-1, "負數"), (None, "None"),
                             ("abc", "非數字"), (True, "布林 True")):
        p = _FakeProduct(True)
        p.price_jpy = bad_price
        sm.start(url)
        sm.record(url, product=p, elapsed_ms=1)
        got = sm.read_day()[-1]["price_jpy"]
        check(f"price_jpy = {label} -> null（不寫 0）", got is None, repr(got))

    p = _FakeProduct(True)
    p.price_jpy = 1
    sm.start(url)
    sm.record(url, product=p, elapsed_ms=1)
    check("price_jpy = 1 要保留（合法價格，不是假值）",
          sm.read_day()[-1]["price_jpy"] == 1)

    # ── 4. brand 的各種異常輸入
    cases = [
        ("", None, "空字串"),
        ("   ", None, "只有空白"),
        (None, None, "None"),
        (123, None, "非字串"),
        ("  Panasonic  ", "Panasonic", "前後空白要 strip"),
        ("京セラ" + chr(10) + "(Kyocera)", "京セラ (Kyocera)", "換行壓成空白"),
        ("Brother" + chr(13) + "Japan", "Brother Japan", "CR 壓成空白"),
    ]
    for raw, expect, label in cases:
        p = _FakeProduct(True)
        p.brand = raw
        sm.start(url)
        sm.record(url, product=p, elapsed_ms=1)
        got = sm.read_day()[-1]["brand"]
        check(f"brand {label} -> {expect!r}", got == expect, repr(got))

    p = _FakeProduct(True)
    p.brand = "A" * 200
    sm.start(url)
    sm.record(url, product=p, elapsed_ms=1)
    got = sm.read_day()[-1]["brand"]
    check("brand 超長要截斷（避免撐大 JSONL）",
          got is not None and len(got) == 80, f"{len(got) if got else 0} 字")

    # ── 5. 取值時炸掉也不可以害整筆寫不出來（fail-safe）
    class Exploding:
        is_valid = True
        title = "x"
        platform_id = ""

        @property
        def price_jpy(self):
            raise RuntimeError("boom")

        @property
        def brand(self):
            raise RuntimeError("boom")

    before = len(sm.read_day())
    sm.start(url)
    sm.record(url, product=Exploding(), elapsed_ms=1)
    check("product 屬性會炸時仍寫得出紀錄（fail-safe）",
          len(sm.read_day()) == before + 1,
          f"{before} -> {len(sm.read_day())}")


# ─────────────────────────────────────────────────────────────────────
async def test_failsafe():
    print("\n【4】記錄層壞掉不可影響爬取（規格硬性要求）")

    orig_open = sm.open if hasattr(sm, "open") else None
    import builtins
    real_open = builtins.open

    def exploding_open(*a, **k):
        raise OSError("模擬磁碟寫入失敗")

    # 4a. 寫入炸掉 → record() 不可 raise
    builtins.open = exploding_open
    try:
        sm.start("https://example.com/x")
        sm.record("https://example.com/x", product=_FakeProduct(True), elapsed_ms=1)
        check("寫入失敗時 record() 不擲例外", True)
    except Exception as e:
        check("寫入失敗時 record() 不擲例外", False, f"{type(e).__name__}: {e}")
    finally:
        builtins.open = real_open

    # 4b. 整個模組被搞壞 → 埋點端（Source）照常運作
    async def scrape_like():
        """模擬爬取流程：埋點呼叫全炸，但流程要照常回傳結果。"""
        broken = ["start", "note_http", "note_source", "record"]
        saved = {n: getattr(sm, n) for n in broken}
        def boom(*a, **k):
            raise RuntimeError("模擬監控整組壞掉")
        for n in broken:
            setattr(sm, n, boom)
        try:
            from scrapers.platform_yahoo_store import _note_http as yh_note
            from scrapers.platform import _note_source as pf_note
            yh_note(200, "body")     # 這兩個是 Source 端實際會呼叫的
            pf_note("SomeSource")
            return "爬取結果"
        finally:
            for n, f in saved.items():
                setattr(sm, n, f)

    try:
        result = await scrape_like()
        check("監控整組壞掉時，Source 埋點不影響流程", result == "爬取結果")
    except Exception as e:
        check("監控整組壞掉時，Source 埋點不影響流程", False,
              f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════
# Source 吞掉的失敗原因要能決定 failure_kind（2026-09-03）
# ═══════════════════════════════════════════════════════════════════
# Source 一律用 return None 表示失敗，網路層的原因只能靠 note_error 補回來，
# 而 classify_failure 只看 record() 傳進來的 error 參數（那是 None）。
# 於是「httpx 逾時但最後整條失敗」的紀錄 error_brief 寫「逾時」、
# failure_kind 卻是 other —— 而摘要與警報看的是 failure_kind。
#
# ★ 這一組要證明兩件事：
#   1. 對所有**原本不是 other** 的輸入，行為一個字都沒變（窮舉比對）
#   2. 分類與 scrapers/platform.py 那三個 helper 的措辭綁在一起，
#      措辭一改就會紅（不然分類會靜默失效）
_U = "https://example.jp/item/1"


def _old_classify(http_status=None, error=None, timed_out=False,
                  block_hint=False, gone_hint=False, got_page=False,
                  blocked=(401, 403, 429)):
    """
    ★ 改動前的 classify_failure 原樣抄過來，當作等價性比對的基準。

    blocked 可以換成 (403, 429) —— 那是 2026-09-03 把 401 補進去**之前**的
    清單，用來精確列出「加了 401 之後，哪些組合的分類改變了」。
    """
    if timed_out:
        return "timeout"
    err_name = type(error).__name__ if error is not None else ""
    err_text = f"{err_name}: {error}".lower() if error is not None else ""
    if "timeout" in err_text or "timedout" in err_text:
        return "timeout"
    if http_status in blocked:
        return "blocked"
    if http_status in (404, 410):
        return "not_found"
    if block_hint:
        return "blocked"
    if gone_hint:
        return "not_found"
    if got_page or http_status == 200:
        return "parse_failed"
    return "other"


def test_gate_equivalence():
    print("\n【8】★ 窮舉證明：原本不是 other 的輸入，結果完全沒變")
    import itertools
    STATUS = [None, 200, 401, 403, 404, 410, 429, 500]
    ERRORS = [None, TimeoutError("t"), ValueError("x")]
    KINDSETS = [(), ("blocked",), ("not_found",), ("timeout",),
                ("blocked", "timeout"), ("not_found", "timeout"),
                ("blocked", "not_found"), ("blocked", "not_found", "timeout"),
                ("something-else",)]
    changed, gated, total = [], 0, 0
    for st, err, to, bh, gh, gp in itertools.product(
            STATUS, ERRORS, (False, True), (False, True), (False, True), (False, True)):
        base = _old_classify(st, err, to, bh, gh, gp)
        for kinds in KINDSETS:
            total += 1
            got = sm.classify_failure(http_status=st, error=err, timed_out=to,
                                      block_hint=bh, gone_hint=gh, got_page=gp,
                                      noted_kinds=kinds)
            if base != "other":
                if got != base:
                    changed.append((st, type(err).__name__, to, bh, gh, gp,
                                    kinds, base, got))
            elif got != "other":
                gated += 1
    check(f"★ 原本不是 other 的組合完全沒變（跑了 {total} 組）",
          not changed, f"變了 {len(changed)} 組：{changed[:2]}")
    check("★ 只有原本會回 other 的才被接管", gated > 0, f"{gated} 組被接管")

    diff = []
    for st, err, to, bh, gh, gp in itertools.product(
            STATUS, ERRORS, (False, True), (False, True), (False, True), (False, True)):
        if (sm.classify_failure(http_status=st, error=err, timed_out=to,
                                block_hint=bh, gone_hint=gh, got_page=gp)
                != _old_classify(st, err, to, bh, gh, gp)):
            diff.append((st, type(err).__name__, to, bh, gh, gp))
    check("★ 不傳 noted_kinds 時等同改動前（預設值不改變行為）", not diff,
          f"{len(diff)} 組不同")


def test_401_now_blocked():
    print("\n【8b】★ 401 補進 blocked 清單：只有 401 的組合改變")
    import itertools
    STATUS = [None, 200, 401, 403, 404, 410, 429, 500]
    ERRORS = [None, TimeoutError("t"), ValueError("x")]
    changed = []
    for st, err, to, bh, gh, gp in itertools.product(
            STATUS, ERRORS, (False, True), (False, True), (False, True), (False, True)):
        before = _old_classify(st, err, to, bh, gh, gp, blocked=(403, 429))
        after = sm.classify_failure(http_status=st, error=err, timed_out=to,
                                    block_hint=bh, gone_hint=gh, got_page=gp)
        if before != after:
            changed.append((st, before, after))
    check("★ 有組合改變（這是刻意的行為變更，不是 gate）", bool(changed),
          f"{len(changed)} 組")
    check("★ 改變的**全部**是 401，沒有波及其他狀態碼",
          all(c[0] == 401 for c in changed),
          str(sorted({c[0] for c in changed})))
    transitions = sorted({(b, a) for _, b, a in changed})
    # ★ 三種轉換都是「往 blocked」，方向一致，沒有任何組合被改成別的東西：
    #   parse_failed→blocked  401 + 拿到頁面。這正是要修的：被擋卻報成
    #                         「我們的解析壞了」，會讓人去查錯的方向
    #   other→blocked         401 但沒有其他線索
    #   not_found→blocked     401 + gone_hint。既有的判斷順序本來就是
    #                         **狀態碼優先於內容特徵**（現有測試已有一條
    #                         「403 + gone_hint → blocked（狀態碼優先）」），
    #                         401 只是跟著同一條規則，不是新的例外
    check("★ 三種轉換全部是「往 blocked」，沒有組合被改成別的分類",
          all(a == "blocked" for _, a in transitions), str(transitions))
    check("轉換方向就是這三種",
          transitions == [("not_found", "blocked"), ("other", "blocked"),
                          ("parse_failed", "blocked")], str(transitions))
    check("★ 401 + gone_hint → blocked（與既有的 403 + gone_hint 同一條規則）",
          sm.classify_failure(http_status=401, gone_hint=True) == "blocked"
          and sm.classify_failure(http_status=403, gone_hint=True) == "blocked")
    check("401 本身 → blocked",
          sm.classify_failure(http_status=401) == "blocked",
          sm.classify_failure(http_status=401))
    check("★ 401 + 拿到頁面 → 仍是 blocked（本來會被誤判成 parse_failed）",
          sm.classify_failure(http_status=401, got_page=True) == "blocked",
          sm.classify_failure(http_status=401, got_page=True))
    check("403 / 429 不受影響",
          sm.classify_failure(http_status=403) == "blocked"
          and sm.classify_failure(http_status=429) == "blocked")
    check("404 / 410 不受影響",
          sm.classify_failure(http_status=404) == "not_found"
          and sm.classify_failure(http_status=410) == "not_found")
    check("200 不受影響", sm.classify_failure(http_status=200) == "parse_failed")


def test_status_lists_consistent():
    print("\n【8c】★ 兩份狀態碼清單：各自回答哪個問題，以及不可以分岔")
    from scrapers.platform import BLOCKED_HTTP_STATUS
    check("★ 爬取端與監控端的「被擋」清單一致（分岔就是今天這個 bug）",
          tuple(sorted(sm._BLOCKED_HTTP_STATUS)) == tuple(sorted(BLOCKED_HTTP_STATUS)),
          f"monitor={sm._BLOCKED_HTTP_STATUS} platform={BLOCKED_HTTP_STATUS}")
    check("被擋清單就是 401/403/429",
          tuple(sorted(sm._BLOCKED_HTTP_STATUS)) == (401, 403, 429),
          str(sm._BLOCKED_HTTP_STATUS))
    check("★ 代理判準是被擋清單的子集（不可能出現「要代理但不算被擋」）",
          set(sm._PROXY_NEEDED_HTTP_STATUS) <= set(sm._BLOCKED_HTTP_STATUS),
          f"{sm._PROXY_NEEDED_HTTP_STATUS} vs {sm._BLOCKED_HTTP_STATUS}")
    check("★ 429 算被擋、但不算「要買代理」（節流重試就會過）",
          429 in sm._BLOCKED_HTTP_STATUS
          and 429 not in sm._PROXY_NEEDED_HTTP_STATUS,
          str(sm._PROXY_NEEDED_HTTP_STATUS))
    check("401 兩份都算", 401 in sm._BLOCKED_HTTP_STATUS
          and 401 in sm._PROXY_NEEDED_HTTP_STATUS)

    # http_fail_brief 的預設值也要是同一份
    from scrapers.platform import http_fail_brief
    for s in BLOCKED_HTTP_STATUS:
        check(f"http_fail_brief({s}) 說「被擋」", "被擋" in http_fail_brief(s),
              http_fail_brief(s)[:30])
    check("500 不說被擋", "被擋" not in http_fail_brief(500))

    # 爬取端不可以再有裸的清單（重試邏輯那兩處不算，它們不是被擋判定）
    import io as _io
    import re as _re
    bare = []
    for f in ("scrapers/jsonld.py", "scrapers/platform_bookoff.py",
              "scrapers/platform_yahoo_store.py"):
        src = _io.open(f, encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        if _re.search(r"status_code in \(4\d\d", code):
            bare.append(f)
    check("★ 三個警告印出點都改用常數，沒有留裸清單", not bare, str(bare))


def test_gate_severity():
    print("\n【9】接管時取最嚴重的，不取最後一個")

    def k(kinds):
        return sm.classify_failure(noted_kinds=kinds)

    check("只有 timeout → timeout", k(("timeout",)) == "timeout")
    # 🔴 順序**必須反過來也測**：note_error 是照發生順序 append 的，
    #   ("timeout","blocked") 這種「最後一個剛好就是最嚴重的」根本分不出
    #   「取最嚴重」與「取最後一個」。2026-09-03 的負向驗證就是這樣被瞞過去的
    #   （注入「取最後一個」全綠）。下面每一對都刻意讓最後一個是**比較輕**的那個。
    for pair, want in ((("blocked", "timeout"), "blocked"),
                       (("blocked", "not_found"), "blocked"),
                       (("not_found", "timeout"), "not_found")):
        check(f"★ {pair} → {want}（最後一個是比較輕的那個，取最嚴重才會對）",
              k(pair) == want, k(pair))
    check("blocked + timeout（反序）也一樣",
          k(("timeout", "blocked")) == "blocked", k(("timeout", "blocked")))
    check("★ 順序與本函式自己的一致：blocked 先於 not_found",
          k(("not_found", "blocked")) == "blocked", k(("not_found", "blocked")))
    check("not_found 先於 timeout（反序）",
          k(("timeout", "not_found")) == "not_found", k(("timeout", "not_found")))
    check("三個都在 → blocked", k(("timeout", "not_found", "blocked")) == "blocked")
    check("認不得的分類不影響", k(("whatever",)) == "other", k(("whatever",)))
    check("空的照舊回 other", k(()) == "other" and k(None) == "other")


def test_note_kind_binding():
    print("\n【10】★ 分類與 helper 措辭綁死（措辭一改就要紅）")
    import httpx as _httpx
    from scrapers.platform import (http_fail_brief, net_error_brief,
                                   missing_method_brief)

    def kinds_of(brief_or_exc):
        sm.start(_U)
        sm.note_error(brief_or_exc, "Src")
        return list((sm._ctx.get() or {}).get("error_kinds") or [])

    # ★ 期望值由 helper 現算出來，不是抄一份字串在測試裡
    check("★ http_fail_brief(403) → blocked",
          kinds_of(http_fail_brief(403)) == ["blocked"], http_fail_brief(403)[:40])
    check("http_fail_brief(401) → blocked", kinds_of(http_fail_brief(401)) == ["blocked"])
    check("http_fail_brief(429) → blocked", kinds_of(http_fail_brief(429)) == ["blocked"])
    check("★ http_fail_brief(404) → not_found",
          kinds_of(http_fail_brief(404)) == ["not_found"], http_fail_brief(404)[:40])
    check("★ net_error_brief(ReadTimeout) → timeout",
          kinds_of(net_error_brief(_httpx.ReadTimeout("t"))) == ["timeout"],
          net_error_brief(_httpx.ReadTimeout("t"))[:40])

    # ★ 刻意不映射的幾類
    check("★ http_fail_brief(500) 不映射（非 200 映到哪類都不對）",
          kinds_of(http_fail_brief(500)) == [], http_fail_brief(500)[:40])
    check("★ net_error_brief(ConnectError) 不映射（連線失敗多半是網址錯）",
          kinds_of(net_error_brief(_httpx.ConnectError("x"))) == [])
    check("★ missing_method_brief 不映射（能力警告不是失敗原因）",
          kinds_of(missing_method_brief("_fetch_with_selenium", "Selenium 退路")) == [])
    check("★ 未設 YAHOO_APP_ID 那句不映射",
          kinds_of("未設 YAHOO_APP_ID，官方 API 救援整支跳過") == [])
    check("★ 今天新增的「兩條路都不通」不映射（它一定伴隨 403，本來就是 blocked）",
          kinds_of("兩條路都不通：httpx HTTP 403，瀏覽器載完仍只有 2.9KB") == [])

    check("原始 ReadTimeout 物件 → timeout",
          kinds_of(_httpx.ReadTimeout("t")) == ["timeout"])
    check("原始 ConnectError 物件 → 不映射",
          kinds_of(_httpx.ConnectError("x")) == [])
    check("★ 型別名是精確比對，不是子字串",
          kinds_of(RuntimeError("connect timeout happened")) == [],
          "訊息含 timeout 但型別不是逾時類，不可以誤判")

    sm.start(_U)
    sm.note_error(http_fail_brief(403), "MUJI/httpx")
    st = sm._ctx.get() or {}
    check("★ 加了 where 前綴仍然分得出來（分類要在加前綴之前算）",
          st.get("error_kinds") == ["blocked"], str(st.get("error_kinds")))
    check("error_brief 本身還是帶 where 前綴",
          (st.get("errors") or [""])[0].startswith("MUJI/httpx: "),
          (st.get("errors") or [""])[0][:30])


def test_end_to_end_kind():
    print("\n【11】★ 端到端：Source 吞掉的逾時最後真的變成 timeout")
    from scrapers.platform import net_error_brief
    import httpx as _httpx

    sm.start(_U)
    sm.note_error(net_error_brief(_httpx.ReadTimeout("")), "MUJI/httpx")
    sm.record(_U, product=_FakeProduct(False), elapsed_ms=31494)
    e = sm.read_day()[-1]
    check("ok=False", e["ok"] is False)
    check("★ failure_kind = timeout（修正前是 other）",
          e["failure_kind"] == "timeout", e["failure_kind"])
    check("error_brief 仍然說得出原因", "逾時" in e["error_brief"], e["error_brief"][:60])

    sm.start(_U)
    sm.note_http(404)
    sm.note_error(net_error_brief(_httpx.ReadTimeout("")), "Src")
    sm.record(_U, product=_FakeProduct(False), elapsed_ms=10)
    e = sm.read_day()[-1]
    check("★ 有 404 時仍是 not_found（既有訊號優先，gate 碰不到）",
          e["failure_kind"] == "not_found", e["failure_kind"])

    sm.start(_U)
    sm.note_error(net_error_brief(_httpx.ReadTimeout("")), "Src")
    sm.record(_U, product=_FakeProduct(True), elapsed_ms=10)
    e = sm.read_day()[-1]
    check("成功筆 failure_kind 仍是空的", e["ok"] is True and e["failure_kind"] == "")
    check("成功筆的原因進 warnings", "逾時" in e["warnings"], e["warnings"][:50])


def test_error_kinds_failsafe():
    print("\n【12】fail-safe 與欄位集合")
    sm.start(_U)
    st = sm._ctx.get()
    check("start() 有初始化 error_kinds", st.get("error_kinds") == [], str(st.get("error_kinds")))
    st["error_kinds"] = object()          # 弄壞它
    sm.note_error("被擋：HTTP 403（x）", "Src")
    before = len(sm.read_day())
    sm.record(_U, product=_FakeProduct(False), elapsed_ms=1)
    check("★ error_kinds 壞掉仍然寫得出紀錄", len(sm.read_day()) == before + 1,
          f"{before} -> {len(sm.read_day())}")
    e = sm.read_day()[-1]
    check("★ error_kinds 不進 JSONL（欄位集合不變）", "error_kinds" not in e,
          str(sorted(e)))


async def main():
    print("=" * 74)
    print("爬取監控驗證（只記錄、不寄信階段）")
    print(f"紀錄寫到暫存目錄：{_TMP}")
    print("=" * 74)
    test_classify_table()
    await test_real_urls()
    test_write_and_fields()
    test_content_fields()
    await test_failsafe()
    test_gate_equivalence()
    test_401_now_blocked()
    test_status_lists_consistent()
    test_gate_severity()
    test_note_kind_binding()
    test_end_to_end_kind()
    test_error_kinds_failsafe()

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
