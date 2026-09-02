"""
Source 的「return None 靜默失敗」埋點驗證（離線，不連外）
==========================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_silent_none.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_silent_none.py`）

★ 這支存在的理由（2026-09-02）：
   Source 一律用 `return None` 表示失敗 —— 不是 raise。
   於是 `Platform.fetch` 的 try/except（platform.py:91）**永遠攔不到**，
   `_note_error` 對每一支真 Platform 都不會觸發，網路層失敗一件都進不了紀錄。

   實測：MUJI 每一筆都走 httpx 被 Akamai 擋的退路（Selenium 才成功、31 秒），
   而當天正式環境的 JSONL 那筆 `warnings` 是**空字串**。
   AST 掃過 12 支 Source 的 get()：能逃出去的只有 parser 與 URL 解析（程式 bug），
   `_fetch` / `_via_yahoo` / `find_by_code` / `_fetch_with_selenium`
   全部在 Source 內部被 except 接住 → print → return None。

   同一個形狀今天已經遇到三次：cleanup 四條中止路徑、GU 三條、jsonld httpx。

補了三類共 23 處（C「URL 抽不出識別碼」與 D「解析不出價」刻意不補：
C 多半是客人貼錯連結、每天都會響；D 在最終失敗時 error_brief 本來就會說）：

  A（15）網路層被擋／逾時／非 200
         訊息要**分得出三種**：被擋（買住宅代理）／逾時（先觀察）／非 200（站改版）
  B（6） 引擎能力不存在 —— 最危險的一類，整支 Source 靜默跳過
         訊息要寫出**是哪個方法不見了**
  E（2） 設定缺失（未設 YAHOO_APP_ID）

不連外：httpx 整個換成假的，engine 也是假的。
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="silentnone_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import httpx

import scrape_monitor as sm
from scrapers.base import ProductInfo
from scrapers.platform import (Platform, http_fail_brief, missing_method_brief,
                               net_error_brief)

import scrapers.jsonld as jl
import scrapers.platform_amiami as pa
import scrapers.platform_bookoff as pb
import scrapers.platform_yahoo_store as py_
import scrapers.platform_zozotown as pz

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# ─────────────────────────────────────────────────────────────────────
# 假的 httpx：整顆換掉，測試永遠不連外
# ─────────────────────────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status, text, url=""):
        self.status_code = status
        self.text = text
        self.url = url


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        if self._exc is not None:
            raise self._exc
        r = self._resp
        return FakeResp(r.status_code, r.text, r.url or url)


class FakeHttpx:
    """只提供 AsyncClient；其餘屬性（例外類別等）轉給真的 httpx。"""

    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    def AsyncClient(self, *a, **kw):
        return FakeClient(self._resp, self._exc)

    def __getattr__(self, k):
        return getattr(httpx, k)


class Swap:
    """把某個模組的 httpx 暫時換掉，離開時還原。"""

    def __init__(self, mod, resp=None, exc=None):
        self.mod, self.fake = mod, FakeHttpx(resp, exc)

    def __enter__(self):
        self.orig = self.mod.httpx
        self.mod.httpx = self.fake

    def __exit__(self, *a):
        self.mod.httpx = self.orig
        return False


class NoEngine:
    """完全沒有任何爬取方法的引擎 —— 模擬「方法被改名／Mixin 被拿掉」。"""


def errs():
    """本次 start() 之後累積的 note_error 內容。"""
    state = sm._ctx.get() or {}
    return " | ".join(state.get("errors") or [])


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


URL_JSONLD = "https://www.muji.com/jp/ja/store/cmdty/detail/4550584865374"
URL_BOOKOFF = "https://shopping.bookoff.co.jp/used/0016000000/000000000000"
URL_YAHOO = "https://store.shopping.yahoo.co.jp/testshop/abc-123.html"
URL_ZOZO = "https://zozo.jp/shop/brand/goods/12345678/"
URL_AMIAMI = "https://www.amiami.jp/top/detail/detail?gcode=FIGURE-001234"


# ═══════════════════════════════════════════════════════════════════
# B 類（6 處）—— 引擎能力不存在，整支 Source 靜默跳過
# ═══════════════════════════════════════════════════════════════════
async def test_b_missing_methods():
    print()
    print("【B】引擎能力不存在 —— 6 處都要說出是哪個方法不見了")

    cases = [
        ("jsonld  Selenium 退路", jl.JsonLdSeleniumSource(tag="MUJI"),
         URL_JSONLD, "_fetch_with_selenium"),
        ("bookoff Selenium 退路", pb.BookoffSeleniumSource(),
         URL_BOOKOFF, "_fetch_with_selenium"),
        ("yahoo   Selenium 退路", py_.YahooStoreSeleniumSource(),
         URL_YAHOO, "_fetch_with_selenium"),
        ("yahoo   generic 退路", py_.YahooStoreGenericSource(),
         URL_YAHOO, "_scrape_with_playwright"),
        ("amiami  UC 直爬退路", pa.AmiamiUcSource(),
         URL_AMIAMI, "_amiami_scrape_jp"),
        ("zozo    legacy 退路", pz.ZozoLegacySource(),
         URL_ZOZO, "_scrape_zozotown_legacy"),
    ]
    for label, src, url, method in cases:
        sm.start(url)
        r = await src.get(url, None, NoEngine())
        got = errs()
        check(f"{label} 回 None 且留下訊號", r is None and bool(got), got[:60])
        check(f"{label} 訊息寫出方法名 {method}", method in got, got[:80])


# ═══════════════════════════════════════════════════════════════════
# A 類（15 處）—— 被擋 / 逾時 / 非 200 三種要分得出來
# ═══════════════════════════════════════════════════════════════════
async def test_a_blocked():
    print()
    print("【A-1】被擋（403 / 401 / 429）→ 要說得出是被擋，且能分類成 blocked")

    sm.start(URL_JSONLD)
    with Swap(jl, resp=FakeResp(403, "Access Denied")):
        r = await jl.JsonLdHttpxSource(tag="MUJI")._fetch(URL_JSONLD)
    got = errs()
    check("jsonld 403 回 None", r is None)
    check("★ 訊息說「被擋」且帶狀態碼", "被擋" in got and "403" in got, got[:80])
    check("訊息指出可用 PROXY_URL", "PROXY_URL" in got, got[:80])

    # BookOff 這支本來完全沒有 note_http —— 補了才分得到 blocked
    sm.start(URL_BOOKOFF)
    with Swap(pb, resp=FakeResp(403, "Access Denied")):
        r = await pb.BookoffJsonLdSource()._fetch(URL_BOOKOFF)
    got = errs()
    state = sm._ctx.get() or {}
    check("bookoff 403 回 None", r is None)
    check("★ 訊息說「被擋」", "被擋" in got and "403" in got, got[:80])
    check("★ http_status 有記到 403（本來完全沒有 note_http）",
          state.get("http_status") == 403, str(state.get("http_status")))
    check("★ classify_failure → blocked",
          sm.classify_failure(http_status=state.get("http_status")) == "blocked",
          sm.classify_failure(http_status=state.get("http_status")))

    sm.start(URL_YAHOO)
    with Swap(py_, resp=FakeResp(429, "Too Many Requests")):
        r = await py_.YahooStoreHttpxSource()._fetch(URL_YAHOO)
    check("yahoo 429 也算被擋", "被擋" in errs() and "429" in errs(), errs()[:70])


async def test_a_timeout():
    print()
    print("【A-2】逾時 → 要獨立成一類（MUJI 的 Akamai 在 httpx 端就是 ReadTimeout）")

    sm.start(URL_JSONLD)
    with Swap(jl, exc=httpx.ReadTimeout("timed out")):
        r = await jl.JsonLdHttpxSource(tag="MUJI")._fetch(URL_JSONLD)
    got = errs()
    check("jsonld ReadTimeout 回 None", r is None)
    check("★ 訊息說「逾時」並帶例外類別", "逾時" in got and "ReadTimeout" in got,
          got[:80])
    check("★ 沒有被誤判成「被擋」（真的慢的站不可以被打成 blocked）",
          "被擋" not in got, got[:80])

    sm.start(URL_BOOKOFF)
    with Swap(pb, exc=httpx.ConnectTimeout("")):
        await pb.BookoffJsonLdSource()._fetch(URL_BOOKOFF)
    check("bookoff ConnectTimeout 也歸「逾時」", "逾時" in errs(), errs()[:70])

    sm.start(URL_YAHOO)
    with Swap(py_, exc=httpx.ConnectError("dns fail")):
        await py_.YahooStoreHttpxSource()._fetch(URL_YAHOO)
    got = errs()
    check("★ yahoo ConnectError 歸「連線失敗」不是「逾時」",
          "連線失敗" in got and "逾時" not in got, got[:80])


async def test_a_non200():
    print()
    print("【A-3】非 200 → 站改版或下架，處置與被擋完全不同")

    sm.start(URL_JSONLD)
    with Swap(jl, resp=FakeResp(500, "oops")):
        await jl.JsonLdHttpxSource(tag="MUJI")._fetch(URL_JSONLD)
    got = errs()
    check("★ 500 說「非 200」不說「被擋」",
          "非 200" in got and "被擋" not in got, got[:80])

    sm.start(URL_JSONLD)
    with Swap(jl, resp=FakeResp(404, "not found")):
        await jl.JsonLdHttpxSource(tag="MUJI")._fetch(URL_JSONLD)
    check("404 說「頁面不存在」", "頁面不存在" in errs(), errs()[:70])

    # ZOZO 的 _via_yahoo 回的是 False 不是 None，形狀一樣
    sm.start(URL_ZOZO)
    with Swap(pz, resp=FakeResp(404, "not found")):
        p = ProductInfo(source_url=URL_ZOZO)
        ok = await pz.ZozoYahooSource()._via_yahoo(URL_ZOZO, "12345678", p)
    got = errs()
    state = sm._ctx.get() or {}
    check("zozo 非 200 回 False", ok is False)
    check("★ return False 那條也有訊號", "頁面不存在" in got and "404" in got, got[:80])
    check("★ zozo 也補了 note_http（本來完全沒有）",
          state.get("http_status") == 404, str(state.get("http_status")))

    sm.start(URL_ZOZO)
    with Swap(pz, exc=httpx.ReadTimeout("")):
        p = ProductInfo(source_url=URL_ZOZO)
        await pz.ZozoYahooSource()._via_yahoo(URL_ZOZO, "12345678", p)
    check("zozo httpx 例外也有訊號", "逾時" in errs(), errs()[:70])


async def test_a_selenium_and_legacy():
    print()
    print("【A-4】Selenium / legacy / UC 退路的例外")

    class BoomEngine:
        def _fetch_with_selenium(self, url):
            raise RuntimeError("driver crashed")

        async def _scrape_zozotown_legacy(self, url):
            raise RuntimeError("legacy boom")

        async def _amiami_scrape_jp(self, url, product):
            raise RuntimeError("uc boom")

    sm.start(URL_JSONLD)
    r = await jl.JsonLdSeleniumSource(tag="MUJI").get(URL_JSONLD, None, BoomEngine())
    check("jsonld Selenium 例外回 None 且有訊號",
          r is None and "driver crashed" in errs(), errs()[:70])

    sm.start(URL_ZOZO)
    r = await pz.ZozoLegacySource().get(URL_ZOZO, None, BoomEngine())
    check("zozo legacy 例外有訊號", r is None and "legacy boom" in errs(), errs()[:70])

    sm.start(URL_AMIAMI)
    r = await pa.AmiamiUcSource().get(URL_AMIAMI, None, BoomEngine())
    check("amiami UC 例外有訊號", r is None and "uc boom" in errs(), errs()[:70])


# ═══════════════════════════════════════════════════════════════════
# E 類（2 處）—— 設定缺失
# ═══════════════════════════════════════════════════════════════════
async def test_e_config():
    print()
    print("【E】設定缺失：官方 API 救援永久空轉，本來只有一行 print")

    orig_ready = py_._api_ready
    py_._api_ready = lambda: False
    try:
        sm.start(URL_YAHOO)
        r = await py_.YahooStoreApiSource().get(URL_YAHOO, None, NoEngine())
    finally:
        py_._api_ready = orig_ready
    got = errs()
    check("未設 APP_ID 時回 None", r is None)
    check("★ 訊息點名 YAHOO_APP_ID", "YAHOO_APP_ID" in got, got[:80])
    check("訊息說明後果（整支跳過）", "跳過" in got, got[:80])

    # API 有結果但沒有網址相符的商品
    async def fake_search(code, seller_id=None, hits=30):
        p = ProductInfo(source_url="https://store.shopping.yahoo.co.jp/other/zzz.html")
        p.title, p.price_jpy = "別的商品", 1000
        return [p]

    orig_ready, orig_search = py_._api_ready, py_._api_search
    py_._api_ready, py_._api_search = (lambda: True), fake_search
    try:
        sm.start(URL_YAHOO)
        r = await py_.YahooStoreApiSource().get(URL_YAHOO, None, NoEngine())
    finally:
        py_._api_ready, py_._api_search = orig_ready, orig_search
    got = errs()
    check("API 無相符時回 None", r is None)
    check("★ 訊息說得出「搜到幾筆但比對不上」",
          "1 筆" in got and "相符" in got, got[:80])


# ═══════════════════════════════════════════════════════════════════
# 走完整的 Platform.fetch + record()：訊號真的進得了 JSONL
# ═══════════════════════════════════════════════════════════════════
async def test_end_to_end():
    print()
    print("【端到端】訊號要真的落到 JSONL 的 warnings / error_brief")

    class TwoSourcePlatform(Platform):
        """模擬 MUJI：httpx 被擋（A 類），Selenium 成功。"""
        id = "muji"
        sources = [jl.JsonLdHttpxSource(tag="MUJI"), None]   # 第二支在下面填

        def matches(self, url):
            return True

    class OkSource(jl.JsonLdSeleniumSource):
        async def get(self, url, ref, engine):
            p = ProductInfo(source_url=url)
            p.title, p.price_jpy = "行動電源", 5990
            return p

    plat = TwoSourcePlatform()
    plat.sources = [jl.JsonLdHttpxSource(tag="MUJI"), OkSource(tag="MUJI")]

    sm.start(URL_JSONLD)
    with Swap(jl, exc=httpx.ReadTimeout("")):
        p = await plat.fetch(URL_JSONLD, NoEngine())
    sm.record(URL_JSONLD, product=p, elapsed_ms=31494)
    e = sm.read_day()[-1]
    check("成功筆 ok=True", e["ok"] is True)
    check("★ warnings 有值（修正前正式環境是空的）", bool(e["warnings"]),
          e["warnings"][:70])
    check("★ warnings 說得出是逾時、來自 MUJI/httpx",
          "逾時" in e["warnings"] and "MUJI/httpx" in e["warnings"],
          e["warnings"][:80])
    check("error_brief 仍為空（兩欄互斥）", e["error_brief"] == "")

    # 全部失敗 → error_brief 要有值
    class AllFailPlatform(Platform):
        id = "bookoff"
        sources = [pb.BookoffJsonLdSource(), pb.BookoffSeleniumSource()]

        def matches(self, url):
            return True

    sm.start(URL_BOOKOFF)
    with Swap(pb, resp=FakeResp(403, "Access Denied")):
        p = await AllFailPlatform().fetch(URL_BOOKOFF, NoEngine())
    sm.record(URL_BOOKOFF, product=p, elapsed_ms=800)
    e = sm.read_day()[-1]
    check("失敗筆 ok=False", e["ok"] is False)
    check("★ error_brief 有值且說得出被擋", "被擋" in e["error_brief"],
          e["error_brief"][:80])
    check("★ 同一筆也看得到 B 類（Selenium 退路整支不見）",
          "_fetch_with_selenium" in e["error_brief"], e["error_brief"][:100])
    check("★ failure_kind = blocked（本來會是 other）",
          e["failure_kind"] == "blocked", e["failure_kind"])


# ═══════════════════════════════════════════════════════════════════
# helper 本身
# ═══════════════════════════════════════════════════════════════════
def test_helpers():
    print()
    print("【helper】分類函式本身")
    check("403 → 被擋", "被擋" in http_fail_brief(403))
    check("401 → 被擋", "被擋" in http_fail_brief(401))
    check("429 → 被擋", "被擋" in http_fail_brief(429))
    check("404 → 頁面不存在", "頁面不存在" in http_fail_brief(404))
    check("500 → 非 200", "非 200" in http_fail_brief(500))
    check("200 + 空 body → 講清楚是空回應",
          "空的" in http_fail_brief(200, ""), http_fail_brief(200, ""))
    check("狀態碼不是數字也不 raise", bool(http_fail_brief(None)),
          http_fail_brief(None))

    check("ReadTimeout → 逾時", "逾時" in net_error_brief(httpx.ReadTimeout("")))
    check("ConnectError → 連線失敗",
          "連線失敗" in net_error_brief(httpx.ConnectError("x")))
    check("不認得的例外照原文不亂歸類",
          net_error_brief(ValueError("weird")).startswith("ValueError"),
          net_error_brief(ValueError("weird")))
    check("訊息裡帶 timeout 字樣也算逾時",
          "逾時" in net_error_brief(RuntimeError("Read timeout after 20s")))
    check("net_error_brief 限長 160", len(net_error_brief(RuntimeError("x" * 500))) <= 160)
    check("missing_method_brief 帶方法名",
          "_foo" in missing_method_brief("_foo", "某退路"),
          missing_method_brief("_foo", "某退路"))


# ═══════════════════════════════════════════════════════════════════
# fail-safe：埋點壞掉不可以影響爬取
# ═══════════════════════════════════════════════════════════════════
async def test_failsafe():
    print()
    print("【fail-safe】沒有 start() 過也不可以炸")
    sm._ctx.set(None)
    with Swap(jl, resp=FakeResp(403, "Access Denied")):
        r = await jl.JsonLdHttpxSource(tag="MUJI")._fetch(URL_JSONLD)
    check("沒有 ctx 時仍正常回 None，不 raise", r is None)


async def main():
    print("=" * 74)
    print("Source 靜默 return None 的埋點")
    print("=" * 74)
    await test_b_missing_methods()
    await test_a_blocked()
    await test_a_timeout()
    await test_a_non200()
    await test_a_selenium_and_legacy()
    await test_e_config()
    await test_end_to_end()
    test_helpers()
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
