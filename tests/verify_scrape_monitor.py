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
    check("欄位齊全（規格第一節）",
          set(r) == {"ts", "domain", "platform_id", "source", "ok", "failure_kind",
                     "http_status", "elapsed_ms", "error_brief", "url_path"},
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
async def main():
    print("=" * 74)
    print("爬取監控驗證（只記錄、不寄信階段）")
    print(f"紀錄寫到暫存目錄：{_TMP}")
    print("=" * 74)
    test_classify_table()
    await test_real_urls()
    test_write_and_fields()
    await test_failsafe()

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
