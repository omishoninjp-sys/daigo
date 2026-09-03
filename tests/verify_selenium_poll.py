"""
Selenium 輪詢迴圈：頁面大小穩定就停（離線，不開瀏覽器）
=========================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_selenium_poll.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_selenium_poll.py`）

★ 這支存在的理由（2026-09-03）：
   `_fetch_with_selenium` 本來唯一的提早跳出條件是 `len(html) > 5000`。
   Akamai 的擋頁只有幾 KB，所以每次都跑滿 6 圈 × 2 秒 = 12 秒，
   **全程佔著全域的 _driver_lock**，同時段其他要用 Selenium 的客人整個卡住。

   生產環境 7 天實測 dior.com 被擋那幾筆：
       18283 / 18361 / 18594 / 19426 / 19821 ms
       ＝ 6 秒 uc_open_with_reconnect ＋ 12 秒輪詢跑滿

🔴 為什麼不在 httpx 拿到 403 時就中止
   同一份紀錄裡 dior.com 有 **4 筆 ok=True 而且 http_status=403** ——
   403 只發生在 httpx 那一層，Selenium 的 TLS 指紋是另一條路，常常過得去
   （跟 MUJI 被 Akamai 擋的形狀完全一樣）。
   在 403 就中止會把那兩筆真商品一起殺掉。要修的是輪詢等太久，不是要不要退。

🔴 為什麼不用 _looks_blocked() 判斷擋頁
   它的第一條是 `len(html) < 5000 → True`。那是給「httpx 抓完之後值不值得
   再花一次 Selenium」用的，放進輪詢迴圈等於**第一次輪詢一律判定被擋**，
   慢載入的真頁面會全部被誤殺。所以只看「還在不在變」，不看內容。
   擋頁特徵只拿來留診斷訊號（_has_block_markers），不參與控制流。

不連外：driver 與 sleep 全部換成假的。
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="selpoll_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import scrape_monitor as sm
from scrapers.generic import GenericMixin, _has_block_markers, _looks_blocked

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


class FakeDriver:
    """依序吐出指定的 page_source；用完最後一個就一直回它。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.reads = 0
        self.opens = []

    def uc_open_with_reconnect(self, url, reconnect_time=6):
        self.opens.append((url, reconnect_time))

    @property
    def page_source(self):
        self.reads += 1
        return self.pages[min(self.reads - 1, len(self.pages) - 1)]


class Eng(GenericMixin):
    def __init__(self, driver):
        self._driver_lock = threading.Lock()
        self._driver = driver
        self._driver_use_count = 0

    def _ensure_driver(self):
        return self._driver

    def _clean_driver_tabs(self):
        pass

    def _create_driver(self):
        pass


_SLEPT = []


def fetch(pages, url="https://www.dior.com/ja_jp/fashion/products/X"):
    """跑一次 _fetch_with_selenium，回 (html, driver, 輪詢次數)。"""
    drv = FakeDriver(pages)
    eng = Eng(drv)
    _SLEPT.clear()
    orig = time.sleep
    time.sleep = lambda s: _SLEPT.append(s)
    try:
        html = eng._fetch_with_selenium(url)
    finally:
        time.sleep = orig
    return html, drv, drv.reads


def page(n, body=""):
    """做一個長度剛好 n 的假頁面。"""
    head = "<html><head><title>t</title></head><body>" + body
    tail = "</body></html>"
    pad = max(0, n - len(head) - len(tail))
    return head + ("x" * pad) + tail


# ═══════════════════════════════════════════════════════════════════
def test_blocked_page_stops_early():
    print()
    print("【1】★ 擋頁（3KB 不變）第 2 次輪詢就跳出，不再跑滿 6 圈")
    blocked = page(3000, "Access Denied")
    html, drv, reads = fetch([blocked] * 6)
    check("★ 只輪詢 2 次（本來是 6 次）", reads == 2, f"{reads} 次")
    check("★ 只 sleep 2 次 = 4 秒（本來 12 秒）", len(_SLEPT) == 2 and sum(_SLEPT) == 4,
          f"{_SLEPT}")
    check("仍然把拿到的 html 回傳（不是回空字串）", html == blocked, f"{len(html)} 字元")
    check("uc_open_with_reconnect 的 6 秒沒有被動到",
          drv.opens and drv.opens[0][1] == 6, str(drv.opens[:1]))


def test_progressive_render_unaffected():
    print()
    print("【2】漸進渲染的真頁面行為完全不變")
    pages = [page(1000), page(3000), page(6000)]
    html, drv, reads = fetch(pages)
    check("★ 讀到第 3 次（>5000 才回）", reads == 3, f"{reads} 次")
    check("回的是最後那個大頁面", len(html) == 6000, f"{len(html)} 字元")

    # 第一次就很大 → 立刻回
    html, drv, reads = fetch([page(20000)])
    check("第一次就 >5000 → 只輪詢 1 次", reads == 1, f"{reads} 次")
    check("回的是那個大頁面", len(html) == 20000, f"{len(html)} 字元")


def test_still_changing_runs_full():
    print()
    print("【3】★ 一直在變但始終 <5000 → 仍然跑滿 6 次（不可以提早放棄）")
    pages = [page(n) for n in (1000, 2000, 3000, 4000, 4500, 4800)]
    html, drv, reads = fetch(pages)
    check("★ 輪詢 6 次都跑完", reads == 6, f"{reads} 次")
    check("回最後一次的內容", len(html) == 4800, f"{len(html)} 字元")

    # 只有中間一次剛好相同 —— 連兩次一樣才算穩定
    pages = [page(1000), page(2000), page(2000), page(6000)]
    html, drv, reads = fetch(pages)
    check("中間出現一次相同就會停（連兩次一樣 = 已載完）", reads == 3, f"{reads} 次")


def test_empty_page():
    print()
    print("【4】driver 一直回空字串也要早點放手")
    html, drv, reads = fetch(["", "", "", ""])
    check("★ 空頁面第 2 次就跳出", reads == 2, f"{reads} 次")
    check("回空字串", html == "", repr(html))


# ═══════════════════════════════════════════════════════════════════
def test_diagnostic_signal():
    print()
    print("【5】★ 診斷：穩定後命中擋頁特徵才記一句，且不影響回傳")
    blocked = page(3000, "Access Denied")

    sm.start("https://www.dior.com/x")
    html, drv, reads = fetch([blocked] * 6)
    errs = " | ".join((sm._ctx.get() or {}).get("errors") or [])
    check("★ 有留下訊號", "challenge 特徵" in errs, errs[:80])
    # ★ 訊息只能講它驗證得到的事。2026-09-03 dior 的軟性擋頁
    #   （3KB 的 "Page unavailable"）一個特徵字都沒有，卻確實是被擋 ——
    #   說「被擋」是誇大，會讓看 log 的人做出錯的採購決定。
    check("★ 訊息不宣稱「被擋」（它只驗證了有沒有 challenge 特徵）",
          "也被擋" not in errs, errs[:90])
    check("★ 訊號不影響回傳（html 照樣拿到）", html == blocked)

    # 正常的小頁面不可以誤觸
    sm.start("https://shop.example.jp/x")
    normal = page(3000, "商品名稱 價格 ¥1,980")
    html, drv, reads = fetch([normal] * 6)
    errs2 = " | ".join((sm._ctx.get() or {}).get("errors") or [])
    check("★ 正常的 3KB 小頁面不記訊號（沒用 _looks_blocked 的 <5000 那條）",
          errs2 == "", errs2[:80])
    check("但一樣提早跳出（穩定判斷跟內容無關）", reads == 2, f"{reads} 次")


def test_marker_helpers():
    print()
    print("【6】_has_block_markers 與 _looks_blocked 的差別")
    small_normal = page(3000, "商品名稱")
    check("★ _looks_blocked 對任何 <5000 的頁面都回 True（所以不能拿來用）",
          _looks_blocked(small_normal) is True)
    check("★ _has_block_markers 對同一頁回 False（只看特徵字）",
          _has_block_markers(small_normal) is False)

    check("強特徵命中", _has_block_markers(page(3000, "Access Denied")) is True)
    check("強特徵不看大小（大頁面也算）",
          _has_block_markers(page(200000, "Bot detected")) is True)
    check("弱特徵在小頁面算數", _has_block_markers(page(3000, "cloudflare")) is True)
    # ★ 2026-08-30 的教訓：Shopify 正常商品頁內嵌 captcha-bootstrap，433KB 也命中
    big_shopify = page(200000, '<script id="captcha-bootstrap"></script>')
    check("★ 弱特徵在大頁面不算（Shopify 的 captcha-bootstrap 不可以誤判）",
          _has_block_markers(big_shopify) is False, f"{len(big_shopify)} 字元")


def test_failsafe():
    print()
    print("【7】fail-safe：訊號機制壞掉不可以影響抓取")
    blocked = page(3000, "Access Denied")
    import scrapers.generic as g
    orig = g._has_block_markers
    g._has_block_markers = lambda h: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        html, drv, reads = fetch([blocked] * 6)
        check("★ 特徵判斷爆掉時仍正常回傳 html", html == blocked, f"{len(html)} 字元")
        check("仍然提早跳出", reads == 2, f"{reads} 次")
    finally:
        g._has_block_markers = orig


# ═══════════════════════════════════════════════════════════════════
# 「兩條路都不通」的結構判準（2026-09-03）
# ═══════════════════════════════════════════════════════════════════
def _signal(status, page_bytes, body=""):
    """跑一次完整流程：httpx 記狀態碼 → Selenium 載完 → 看留下什麼訊號。"""
    sm.start("https://www.dior.com/ja_jp/fashion/products/X")
    if status is not None:
        sm.note_http(status)
    pages = [page(page_bytes, body)] * 6
    fetch(pages)
    return " | ".join((sm._ctx.get() or {}).get("errors") or [])


def test_both_paths_blocked():
    print()
    print("【8】★ 結構判準：httpx 狀態碼 + 瀏覽器載完的大小")
    # ① 兩條路都不通
    s = _signal(403, 3000)
    check("★ httpx 403 + 頁面穩定 3KB → 判定兩條路都不通",
          "兩條路都不通" in s, s[:90])
    check("訊息帶得出狀態碼與實際大小",
          "403" in s and "KB" in s, s[:90])
    check("★ 訊息直接回答「要不要買住宅代理」", "住宅代理" in s, s[:90])

    # ② 瀏覽器過得去 → 不判定（買了代理也沒有多賺）
    s = _signal(403, 20000)
    check("★ httpx 403 + 頁面 20KB → 不判定（Selenium 過得去）",
          "兩條路都不通" not in s, s[:90])

    # ③ 沒有被擋的狀態碼 → 不判定（頁面小只是頁面小）
    s = _signal(200, 3000)
    check("★ httpx 200 + 頁面穩定 3KB → 不判定（不是被擋）",
          "兩條路都不通" not in s, s[:90])

    # ④ 邊界
    check("401 也算被擋", "兩條路都不通" in _signal(401, 3000))
    check("★ 404 不算（那是 not_found 不是 blocked）",
          "兩條路都不通" not in _signal(404, 3000))
    check("429 不算（那是節流，重試就好，不用買代理）",
          "兩條路都不通" not in _signal(429, 3000))
    check("完全沒有狀態碼時不判定（httpx 連回應都沒拿到，資訊不足）",
          "兩條路都不通" not in _signal(None, 3000))
    check("剛好 5000 bytes 不算小", "兩條路都不通" not in _signal(403, 5000))
    check("4999 bytes 算小", "兩條路都不通" in _signal(403, 4999))


def test_signal_layering():
    print()
    print("【9】兩個訊號各自獨立：特徵字與結構判準互不依賴")
    # 有特徵字但 httpx 是 200 → 只有 challenge 那條，沒有「兩條路都不通」
    s = _signal(200, 3000, "Access Denied")
    check("★ 有特徵字但 httpx 200 → 只報 challenge，不報兩條路都不通",
          "challenge 特徵" in s and "兩條路都不通" not in s, s[:100])
    # 沒有特徵字但 httpx 403 + 頁面小 → 只有結構判準（dior 的實際情況）
    s = _signal(403, 3000, "Page unavailable")
    check("★ dior 的實際情況：沒有特徵字，仍然判定兩條路都不通",
          "兩條路都不通" in s and "challenge 特徵" not in s, s[:100])
    # 兩個都成立 → 兩句都在
    s = _signal(403, 3000, "Access Denied")
    check("兩個都成立時兩句都留",
          "兩條路都不通" in s and "challenge 特徵" in s, s[:110])


def test_module_boundary():
    print()
    print("【10】★ 爬取路徑不可以讀監控的狀態（判斷留在 scrape_monitor）")
    import io as _io
    import re as _re
    src = _io.open("scrapers/generic.py", encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    # 只准呼叫 note_*，不准讀狀態
    bad = _re.findall(r"scrape_monitor\.(?!note_)(\w+)", code)
    check("★ generic.py 只呼叫 note_*，沒有讀 scrape_monitor 的任何狀態",
          not bad, str(sorted(set(bad))))
    check("★ 沒有碰 _ctx（那是監控的內部狀態）", "_ctx" not in code)
    # ★ 這裡要驗的是「判斷邏輯不在爬取路徑」，不是「那個詞不能出現在說明裡」——
    #   docstring 講清楚為什麼判斷不放這裡，是好事不是違規。
    #   真正的界線是：generic.py 完全不碰 httpx 的狀態碼。
    check("★ generic.py 從頭到尾沒有碰 http_status（判斷的另一半在監控那邊）",
          "http_status" not in code, "generic.py 不該知道狀態碼")
    check("回報的是大小這個事實，不是判斷結果",
          "note_page_settled(len(" in code, "")

    mon = _io.open("scrape_monitor.py", encoding="utf-8").read()
    check("scrape_monitor 才是做判斷的地方", "兩條路都不通" in mon)
    check("★ 判準用狀態碼常數，不是字串比對",
          "_PROXY_NEEDED_HTTP_STATUS" in mon and "_SETTLED_SMALL_BYTES" in mon)
    # ★ 2026-09-03：「被擋」與「要買代理」拆成兩份清單，各自回答不同的問題。
    #   這裡用的必須是代理那份 —— 429 是節流，重試就會過，買代理沒有用。
    check("★ 用的是代理那份清單（429 不在裡面）",
          "if status not in _PROXY_NEEDED_HTTP_STATUS" in mon)


def test_settled_failsafe():
    print()
    print("【11】fail-safe：note_page_settled 壞掉不可以影響抓取")
    sm._ctx.set(None)
    sm.note_page_settled(3000)          # 沒有 ctx
    check("沒有 ctx 時不 raise", True)
    sm.start("https://x.jp/p")
    sm.note_http(403)
    sm.note_page_settled("不是數字")
    errs = " | ".join((sm._ctx.get() or {}).get("errors") or [])
    check("★ 大小不是數字時安靜略過（不可以誤判成被擋）",
          "兩條路都不通" not in errs, errs[:80])

    # 監控整支爆掉，抓取仍要正常
    import scrapers.generic as g
    orig = g._note_page_settled
    g._note_page_settled = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        blocked = page(3000, "x")
        html, drv, reads = fetch([blocked] * 6)
        check("★ 回報函式爆掉時 html 照樣回得來", html == blocked, f"{len(html)} 字元")
        check("仍然提早跳出", reads == 2, f"{reads} 次")
    finally:
        g._note_page_settled = orig


def main_():
    print("=" * 74)
    print("Selenium 輪詢：頁面大小穩定就停")
    print("=" * 74)
    test_blocked_page_stops_early()
    test_progressive_render_unaffected()
    test_still_changing_runs_full()
    test_empty_page()
    test_diagnostic_signal()
    test_marker_helpers()
    test_failsafe()
    test_both_paths_blocked()
    test_signal_layering()
    test_module_boundary()
    test_settled_failsafe()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main_()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
