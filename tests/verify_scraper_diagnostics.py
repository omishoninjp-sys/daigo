"""
gu / uniqlo / amazon 三支 scraper 的失敗診斷埋點驗證
====================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_scraper_diagnostics.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_scraper_diagnostics.py`）

★ 這支存在的理由（2026-09-02）：
   gu / uniqlo / amazon 三支 scraper 的失敗路徑**一條都沒有呼叫 note_error**
   （generic 7 次、jsonld 3 次、rakuten 3 次，只有這三支是 0）。
   失敗時 record() 收到的是一個空的 ProductInfo（error=None），JSONL 只留下
   failure_kind='other'、error_brief='' 的空紀錄 —— 每一條路徑的處置完全不同，
   分不出來等於沒記：

     GU   403 → 機房 IP 被擋，要住宅代理
          items 為空 → 商品下架或 API 改版
          番號解析失敗 → 客人貼的 URL 格式不對

   其中 Amazon 的 A2（URL 不含 ASIN）與 A5（被導向登入頁）**連 print 都沒有**，
   連 Zeabur Runtime Log 都查不到。Amazon 佔營收 10.6%。

★ note_http 也一起補了。少了它，403/429 會被 classify_failure 分成 other
  而不是 blocked —— **blocked 這個分類存在的目的就是回答「要不要買住宅代理」**。
  2026-09-02 GU 那次疑似 403 的失敗，就是因為沒有 note_http 而永遠查不出來。

不連外：httpx 全部換成假的，一次真實請求都不會發出。
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 寫入導到暫存目錄（必須在 import scrape_monitor 之前設）
_TMP = tempfile.mkdtemp(prefix="scraperdiag_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import httpx

import scrape_monitor as sm

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


class FakeResp:
    def __init__(self, status=200, text="", url="https://example.com/", payload=None):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {}
        self.history = []
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """httpx.AsyncClient 的替身；每次 get 依序回 responses 裡的下一個。"""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        r = FakeClient.responses.pop(0) if FakeClient.responses else FakeResp(200, "")
        if isinstance(r, Exception):
            raise r
        return r


FakeClient.responses = []


async def run_scrape(coro_factory, url, responses):
    """跑一次 scraper 的失敗路徑，回傳 record() 寫出的那一筆。"""
    FakeClient.responses = list(responses)
    orig = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    sm.start(url)
    try:
        product = await coro_factory(url)
    finally:
        httpx.AsyncClient = orig
    sm.record(url, product=product, elapsed_ms=1)
    return sm.read_day()[-1]


def brief_of(entry):
    return entry.get("error_brief") or ""


# ═══════════════════════════════════════════════════════════════════
async def test_gu():
    print()
    print("【1】GU 三條失敗路徑")
    from scrapers.gu import GUMixin

    class Eng(GUMixin):
        pass

    eng = Eng()

    # G1 無法解析商品番號
    e = await run_scrape(eng._scrape_gu,
                         "https://www.gu-global.com/jp/ja/search?q=shirt", [])
    b = brief_of(e)
    check("G1 番號解析失敗 error_brief 非空", bool(b), b[:70])
    check("G1 說得出是 URL 格式問題", "番號" in b or "URL" in b, b[:70])

    # G2 API 非 200
    url = "https://www.gu-global.com/jp/ja/products/E360324-000/00"
    e = await run_scrape(eng._scrape_gu, url,
                         [FakeResp(403, "Forbidden", url)])
    b = brief_of(e)
    check("G2 API 403 error_brief 非空", bool(b), b[:70])
    check("G2 說得出狀態碼 403", "403" in b, b[:70])
    check("★ G2 有 note_http，分類是 blocked 不是 other",
          e["failure_kind"] == "blocked", e["failure_kind"])

    # G3 items 為空
    e = await run_scrape(eng._scrape_gu, url,
                         [FakeResp(200, "{}", url, payload={"result": {"items": []}})])
    b = brief_of(e)
    check("G3 items 為空 error_brief 非空", bool(b), b[:70])
    check("G3 說得出是下架或 API 改版",
          "items" in b and ("下架" in b or "改版" in b), b[:70])
    # ★ 2026-09-02：GU 的 API 對查無商品回 HTTP 200 + items:[]，
    #   classify_failure 走到「http_status == 200 → parse_failed」那行，
    #   把「商品下架」判成「我們的 parser 壞了」——兩者的處置完全相反。
    #   實證：E360475-000 本機 838ms、線上 67ms 都是 200 + items=0。
    check("★ G3 分類是 not_found 不是 parse_failed（商品下架不是解析失敗）",
          e["failure_kind"] == "not_found", e["failure_kind"])

    # 三條的訊息要互不相同（分辨得出來才有意義）
    briefs = set()
    for u, rs in [("https://www.gu-global.com/jp/ja/search?q=x", []),
                  (url, [FakeResp(403, "", url)]),
                  (url, [FakeResp(200, "{}", url, payload={"result": {"items": []}})])]:
        briefs.add(brief_of(await run_scrape(eng._scrape_gu, u, rs)))
    check("★ 三條的 error_brief 互不相同", len(briefs) == 3, f"{len(briefs)} 種")

    # ── items 正常時行為完全不變（gone 訊號不可以誤傷正常商品）──
    ok_payload = {"result": {"items": [{
        "name": "ブラフィールナローストラップキャミソール",
        "prices": {"base": {"value": 1490}},
        "colors": [{"displayCode": "01", "name": "OFF WHITE"}],
        "sizes": [{"name": "M"}],
        "images": {"main": {"01": {"image": "https://img.example/a.jpg"}}, "sub": []},
    }]}}
    e = await run_scrape(eng._scrape_gu, url,
                         [FakeResp(200, "{}", url, payload=ok_payload)])
    check("★ items 正常 → ok=True", e["ok"] is True, str(e["ok"]))
    check("★ items 正常 → failure_kind 是空的（沒有被 gone 訊號誤傷）",
          e["failure_kind"] == "", e["failure_kind"])
    check("items 正常 → 價格照常抓到", e["price_jpy"] == 1490, str(e["price_jpy"]))

    # ── G2（403）不可以被改成 not_found ──
    e = await run_scrape(eng._scrape_gu, url, [FakeResp(403, "Forbidden", url)])
    check("★ 403 仍是 blocked（gone 訊號沒有蓋掉狀態碼判定）",
          e["failure_kind"] == "blocked", e["failure_kind"])


async def test_gone_hint_scope():
    """note_gone 只影響有呼叫它的那條路徑，其他 Platform 一律不受影響。"""
    print()
    print("【1b】note_gone 的作用範圍")

    # 沒有人呼叫 note_gone 時，200 + 抽不到欄位 仍然是 parse_failed
    sm.start("https://x.jp/p/1")
    sm.note_http(200, "<html>正常頁面</html>")
    state = sm._ctx.get() or {}
    check("★ 沒呼叫 note_gone → 200 仍判 parse_failed（其他平台不受影響）",
          sm.classify_failure(http_status=200,
                              gone_hint=bool(state.get("gone_hint"))) == "parse_failed",
          sm.classify_failure(http_status=200,
                              gone_hint=bool(state.get("gone_hint"))))

    # 呼叫之後才會變 not_found
    sm.note_gone()
    state = sm._ctx.get() or {}
    check("★ 呼叫 note_gone → 200 變 not_found",
          sm.classify_failure(http_status=200,
                              gone_hint=bool(state.get("gone_hint"))) == "not_found",
          sm.classify_failure(http_status=200,
                              gone_hint=bool(state.get("gone_hint"))))

    # 判斷順序：403 比 gone_hint 優先（狀態碼先於內容特徵）
    check("403 + gone_hint 仍是 blocked（既有順序不變）",
          sm.classify_failure(http_status=403, gone_hint=True) == "blocked",
          sm.classify_failure(http_status=403, gone_hint=True))

    # ★ 這一條原本是釘「不可以為了下架判定新增參數」。
    #   2026-09-03 classify_failure 確實多了一個 noted_kinds，但那是**另一件事**
    #   （Source 用 return None 吞掉的失敗原因要能決定 failure_kind），
    #   與下架判定無關 —— 下架仍然走既有的 gone_hint。
    #   斷言分成兩段：原意保留，簽章整個釘住，日後再有人想為某個分類
    #   新增參數會在這裡紅，必須有意識地改這行。
    import inspect
    params = list(inspect.signature(sm.classify_failure).parameters)
    check("★ 下架判定沿用既有的 gone_hint，沒有為它新增參數",
          "gone_hint" in params, str(params))
    check("★ 簽章就是這 7 個（新增參數要有意識地改這裡）",
          params == ["http_status", "error", "timed_out", "block_hint",
                     "gone_hint", "got_page", "noted_kinds"], str(params))

    # fail-safe：沒 start() 過也不可以炸
    sm._ctx.set(None)
    sm.note_gone()
    check("沒有 ctx 時 note_gone 不 raise", True)

    # 只有 gu 呼叫（掃全 repo，避免日後有人到處灑）
    import pathlib
    callers = sorted(f.name for f in pathlib.Path("scrapers").glob("*.py")
                     if "_note_gone()" in f.read_text(encoding="utf-8"))
    check("★ 目前只有 gu.py 埋 note_gone", callers == ["gu.py"], str(callers))


async def test_amazon():
    print()
    print("【2】Amazon 失敗路徑（A2 與 A5 原本連 print 都沒有）")
    from scrapers.amazon import AmazonMixin

    class Eng(AmazonMixin):
        pass

    eng = Eng()

    # A2 URL 不含 ASIN —— 原本完全靜默
    e = await run_scrape(eng._scrape_amazon,
                         "https://www.amazon.co.jp/s?k=coffee", [])
    b = brief_of(e)
    check("★ A2 URL 不含 ASIN error_brief 非空（原本完全靜默）", bool(b), b[:70])
    check("A2 說得出可能是搜尋頁或分類頁",
          "ASIN" in b and ("搜尋" in b or "分類" in b), b[:70])

    url = "https://www.amazon.co.jp/dp/B00IMRC6T6"

    # A3 HTTP 非 200
    e = await run_scrape(eng._scrape_amazon, url, [FakeResp(503, "err", url)])
    b = brief_of(e)
    check("A3 HTTP 503 error_brief 非空", bool(b), b[:70])
    check("A3 說得出狀態碼", "503" in b, b[:70])

    # A3 403 → blocked（note_http 生效）
    e = await run_scrape(eng._scrape_amazon, url, [FakeResp(403, "Access Denied", url)])
    check("★ A3 403 分類是 blocked 不是 other（note_http 生效）",
          e["failure_kind"] == "blocked", e["failure_kind"])

    # A4 CAPTCHA（看最終網址，不是頁面內容）
    e = await run_scrape(eng._scrape_amazon, url,
                         [FakeResp(200, "<html></html>",
                                   "https://www.amazon.co.jp/errors/validateCaptcha")])
    b = brief_of(e)
    check("A4 CAPTCHA error_brief 非空", bool(b), b[:70])
    check("A4 說得出是被導向 CAPTCHA", "CAPTCHA" in b, b[:70])

    # A5 登入頁 —— 原本完全靜默
    e = await run_scrape(eng._scrape_amazon, url,
                         [FakeResp(200, '<html><form name="signIn"></form></html>', url)])
    b = brief_of(e)
    check("★ A5 登入頁 error_brief 非空（原本完全靜默）", bool(b), b[:70])
    check("A5 說得出是成人商品或地區限制",
          "登入" in b and ("成人" in b or "地區" in b), b[:70])

    # A6 價格未找到 —— 訊息要帶出「13 個選擇器 + 3 個 regex」
    e = await run_scrape(eng._scrape_amazon, url,
                         [FakeResp(200,
                                   '<html><span id="productTitle">測試商品</span></html>',
                                   url)])
    b = brief_of(e)
    check("A6 價格未找到 error_brief 非空", bool(b), b[:70])
    check("★ A6 訊息帶出 13 個選擇器 + 3 個 regex（數字本身是診斷資訊）",
          "13" in b and "3" in b, b[:80])
    check("A6 分類是 parse_failed（有頁面沒價格）",
          e["failure_kind"] == "parse_failed", e["failure_kind"])


async def test_uniqlo():
    print()
    print("【3】Uniqlo 失敗路徑")
    from scrapers.uniqlo import UniqloMixin

    class Eng(UniqloMixin):
        pass

    eng = Eng()

    # U1 無法提取商品代碼
    e = await run_scrape(eng._scrape_uniqlo,
                         "https://www.uniqlo.com/jp/ja/search?q=shirt", [])
    b = brief_of(e)
    check("U1 商品代碼解析失敗 error_brief 非空", bool(b), b[:70])
    check("U1 說得出是 URL 格式", "代碼" in b or "URL" in b, b[:70])


async def test_pure_addition():
    print()
    print("【4】純加法：埋點不可以改變成功路徑")
    from scrapers.gu import GUMixin

    class Eng(GUMixin):
        pass

    payload = {"result": {"items": [{
        "name": "測試商品",
        "prices": {"base": {"value": 1490}},
        "colors": [{"displayCode": "01", "name": "OFF WHITE"}],
        "sizes": [{"name": "M"}],
        "images": {"main": {}, "sub": []},
    }]}}
    url = "https://www.gu-global.com/jp/ja/products/E360324-000/00"
    e = await run_scrape(Eng()._scrape_gu, url,
                         [FakeResp(200, "{}", url, payload=payload)])
    check("成功路徑仍然成功", e["ok"] is True, str(e["ok"]))
    check("成功時 error_brief 是空的（沒有被埋點污染）",
          e["error_brief"] == "", repr(e["error_brief"]))
    check("成功時 failure_kind 是空的", e["failure_kind"] == "", e["failure_kind"])
    check("價格有記到", e["price_jpy"] == 1490, str(e["price_jpy"]))
    check("brand 有記到（gu.py 寫死 GU）", e["brand"] == "GU", str(e["brand"]))


async def test_failsafe():
    print()
    print("【5】fail-safe：監控壞掉不可以影響爬取")
    from scrapers.gu import GUMixin

    class Eng(GUMixin):
        pass

    orig = sm.note_error

    def boom(*a, **k):
        raise RuntimeError("monitor down")

    sm.note_error = boom
    try:
        FakeClient.responses = []
        c = httpx.AsyncClient
        httpx.AsyncClient = FakeClient
        try:
            p = await Eng()._scrape_gu("https://www.gu-global.com/jp/ja/search?q=x")
            check("note_error 爆掉時 scraper 仍正常回傳", p is not None)
        finally:
            httpx.AsyncClient = c
    except Exception as e:
        check("note_error 爆掉時 scraper 仍正常回傳", False,
              f"{type(e).__name__}: {e}")
    finally:
        sm.note_error = orig


async def main():
    print("=" * 74)
    print("gu / uniqlo / amazon 失敗診斷埋點")
    print(f"紀錄寫到暫存目錄：{_TMP}")
    print("=" * 74)
    await test_gu()
    await test_gone_hint_scope()
    await test_amazon()
    await test_uniqlo()
    await test_pure_addition()
    await test_failsafe()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
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
