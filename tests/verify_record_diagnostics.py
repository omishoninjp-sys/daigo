"""
爬取紀錄的診斷欄位驗證（error_brief / platform_id / domain）
============================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_record_diagnostics.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_record_diagnostics.py`）

跑了兩天真實資料後發現三個記錄本身的問題，這支把三個都釘住：

  1. error_brief 全是空字串 —— Source 的例外在 Platform.fetch 裡被 print 掉之後
     回傳一個空的 ProductInfo，main.py 走的是「成功路徑」，record() 收到 error=None。
     沒有這欄，parse_failed 無從判斷該怎麼修。
  2. 網域被拆成好幾列 —— 尾端點的 FQDN 形式（jp.mercari.com.）與一般形式
     被當成兩個站。
  3. platform_id 有時是空的 —— timeout／例外路徑沒有 product，而 platform_id
     以前只從 product 上拿。

不連外：全部用假 Platform／假 Source／假 product。
"""
import os
import sys
import shutil
import asyncio
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 寫入導到暫存目錄（必須在 import scrape_monitor 之前設）
_TMP = tempfile.mkdtemp(prefix="recorddiag_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import scrape_monitor as sm
from scrapers.base import ProductInfo, detect_invalid_link
from scrapers.platform import Platform, Source

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


def last_entry():
    return sm.read_day()[-1]


# ─────────────────────────────────────────────────────────────────────
# 假 Platform：模擬「Source 炸了但 fetch 吞掉、回傳空 ProductInfo」
# ─────────────────────────────────────────────────────────────────────
class BoomSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        raise ValueError("模擬 Source 內部失敗")


class EmptySource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        return ProductInfo(source_url=url)      # 有回東西但抽不出欄位


class BoomPlatform(Platform):
    id = "fakeshop"
    sources = [BoomSource()]

    def matches(self, url: str) -> bool:
        return True


class EmptyPlatform(Platform):
    id = "emptyshop"
    sources = [EmptySource()]

    def matches(self, url: str) -> bool:
        return True


# ─────────────────────────────────────────────────────────────────────
async def test_error_brief_from_source():
    print("\n【1】Source 吞掉的例外要進 error_brief")
    url = "https://fakeshop.example/item/1"
    sm.start(url)
    product = await BoomPlatform().fetch(url, engine=None)
    check("fetch 仍照常回傳（不因監控而改變行為）",
          isinstance(product, ProductInfo) and not product.is_valid)

    sm.record(url, product=product, elapsed_ms=120)
    r = last_entry()
    check("error_brief 不再是空字串", r["error_brief"] != "", repr(r["error_brief"]))
    check("error_brief 含例外類型與訊息",
          "ValueError" in r["error_brief"] and "模擬 Source 內部失敗" in r["error_brief"],
          r["error_brief"])
    check("error_brief 指出是哪個 Source 失敗",
          "BoomSource" in r["error_brief"], r["error_brief"])
    check("error_brief 仍不超過 200 字", len(r["error_brief"]) <= 200,
          f'{len(r["error_brief"])} 字')


async def test_error_brief_no_exception():
    print("\n【2】沒有例外、只是抽不出欄位 → 也要說得出「缺什麼」")
    url = "https://emptyshop.example/item/2"
    sm.start(url)
    sm.note_http(200, "<html>正常頁面</html>")
    product = await EmptyPlatform().fetch(url, engine=None)
    sm.record(url, product=product, elapsed_ms=800)
    r = last_entry()
    check("分類是 parse_failed", r["failure_kind"] == "parse_failed", r["failure_kind"])
    check("error_brief 不是空的（parse_failed 最需要這欄）",
          r["error_brief"] != "", repr(r["error_brief"]))
    check("說得出標題與價格各自有沒有抓到",
          "title" in r["error_brief"] and "price" in r["error_brief"], r["error_brief"])

    # 有標題沒價格：最常見的 parse_failed，要分得出來
    url2 = "https://emptyshop.example/item/3"
    sm.start(url2)
    sm.note_http(200, "<html>正常頁面</html>")
    p = ProductInfo(source_url=url2, title="某商品")
    sm.record(url2, product=p, elapsed_ms=800)
    r2 = last_entry()
    check("有標題沒價格 → error_brief 反映得出來",
          "title=有" in r2["error_brief"] and "price=無" in r2["error_brief"],
          r2["error_brief"])


async def test_platform_id():
    print("\n【3】platform_id 不可因為沒有 product 就變空")
    url = "https://coldbeer.example/zh"
    sm.start(url)
    sm.note_platform("legacy:generic")
    sm.record(url, elapsed_ms=60000, timed_out=True)      # timeout：沒有 product
    r = last_entry()
    check("timeout（無 product）仍記得到 platform_id",
          r["platform_id"] == "legacy:generic", repr(r["platform_id"]))
    check("timeout 分類不受影響", r["failure_kind"] == "timeout", r["failure_kind"])

    # Platform.fetch 應該自己回報 platform_id，不必等 product
    url2 = "https://fakeshop.example/item/4"
    sm.start(url2)
    await BoomPlatform().fetch(url2, engine=None)
    sm.record(url2, error=RuntimeError("上層再炸一次"), elapsed_ms=10)
    r2 = last_entry()
    check("Platform.fetch 會回報 platform_id（例外路徑也拿得到）",
          r2["platform_id"] == "fakeshop", repr(r2["platform_id"]))

    # 明確傳進來的 platform_id 優先
    url3 = "https://x.example/item/5"
    sm.start(url3)
    sm.note_platform("legacy:generic")
    sm.record(url3, elapsed_ms=1, platform_id="rakuten")
    check("呼叫端明確給的 platform_id 優先",
          last_entry()["platform_id"] == "rakuten", last_entry()["platform_id"])


def test_domain_normalization():
    print("\n【4】網域正規化：同一個站不可以被拆成好幾列")
    cases = [
        ("https://jp.mercari.com/item/m1", "jp.mercari.com", "一般形式"),
        ("https://JP.Mercari.com/item/m1", "jp.mercari.com", "大小寫"),
        ("https://www.jp.mercari.com/item/m1", "jp.mercari.com", "www 前綴"),
        ("https://jp.mercari.com./item/m1", "jp.mercari.com", "尾端點的 FQDN 形式"),
        ("https://jp.mercari.com:443/item/m1", "jp.mercari.com", "帶埠號"),
        ("https://user@jp.mercari.com/item/m1", "jp.mercari.com", "帶使用者資訊"),
    ]
    for url, expect, why in cases:
        got = sm._domain(url)
        check(f"{why} → {expect}", got == expect, f"{url} → {got}")

    # 結構壞掉的主機名不要「安靜地」被正規化成正常網域：那會把證據抹掉，
    # 之後再也查不出客人到底貼了什麼。這種連結該在 detect_invalid_link 就擋掉。
    check("開頭是點的壞主機名保持原樣（保留證據）",
          sm._domain("https://.mercari.com/item/m1") == ".mercari.com",
          sm._domain("https://.mercari.com/item/m1"))


def test_malformed_host_blocked():
    print("\n【5】結構不成立的主機名要擋在爬取之前（才不會生出幽靈網域）")
    should_block = [
        ("https://.mercari.com/item/m1", "開頭是點（空 label）"),
        ("https://jp..mercari.com/item/m1", "中間有空 label"),
        ("https://mercari/item/m1", "沒有點，不是網域"),
        ("https://jp.mercari.c/item/m1", "TLD 只有一個字"),
        ("https://jp.mercari.123/item/m1", "TLD 是數字"),
    ]
    for url, why in should_block:
        check(f"擋掉：{why}", detect_invalid_link(url) is not None, url)

    # 不擋「不認識的 TLD」：要擋就得帶一份 TLD 清單，那會誤擋 .shop / .store /
    # .tokyo 這類日本商店真的在用的新 gTLD。jp.mercari.（尾點）去掉尾點後就是
    # jp.mercari，形狀上合法，只是連不上 —— 那會由 error_brief 講出 DNS 失敗。
    should_pass = [
        "https://jp.mercari.com/item/m1",
        "https://jp.mercari./item/m1",              # 尾點去掉後 = 未知 TLD，不擋
        "https://someshop.tokyo/item/1",            # 新 gTLD
        "https://someshop.shop/item/1",
        "https://jp.mercari.com./item/m1",          # 尾端點是合法的 FQDN 寫法
        "https://item.rakuten.co.jp/shop/item/",
        "https://xn--eckwd4c7c.xn--zckzah/item/1",  # punycode
        "https://日本店.jp/item/1",                  # IDN
        # 2026-08 誤擋過的 7 家，永遠不可以再被擋
        "https://tocco-closet.co.jp/item/1",
        "https://www.golfdigest.co.jp/item/1",
        "https://dot-st.com/item/1",
        "https://newart.co.jp/item/1",
        "https://uniformnext.com/item/1",
        "https://lilith-soft.com/item/1",
        "https://store.plusmember.jp/item/1",
    ]
    for url in should_pass:
        check(f"放行：{url}", detect_invalid_link(url) is None,
              str(detect_invalid_link(url))[:40])


async def test_failsafe_still_holds():
    print("\n【6】新增的埋點一樣不可以影響爬取")
    import builtins
    real_open = builtins.open
    builtins.open = lambda *a, **k: (_ for _ in ()).throw(OSError("模擬磁碟寫入失敗"))
    try:
        sm.start("https://x.example/1")
        sm.note_platform("legacy:generic")
        sm.note_error(ValueError("boom"), "SomeSource")
        sm.record("https://x.example/1", elapsed_ms=1)
        check("寫入失敗時 note_platform / note_error / record 都不擲例外", True)
    except Exception as e:
        check("寫入失敗時 note_platform / note_error / record 都不擲例外", False,
              f"{type(e).__name__}: {e}")
    finally:
        builtins.open = real_open

    # 沒呼叫 start() 就用（例如監控本身壞掉的路徑）也不能炸
    try:
        sm._ctx.set(None)
        sm.note_platform("x")
        sm.note_error(ValueError("y"))
        check("沒有 start() 就呼叫 note_* 不擲例外", True)
    except Exception as e:
        check("沒有 start() 就呼叫 note_* 不擲例外", False, f"{type(e).__name__}: {e}")


async def test_blocked_heuristic_real_page():
    print("\n【7】擋頁判斷不可以被正常頁面裡的 captcha 字樣騙到（連外）")
    from scrapers.generic import _looks_blocked

    # 真實樣本：coldbeer.jp 是 Shopify 商店，頁面內嵌 <script id="captcha-bootstrap">。
    # 舊寫法用子字串比對 "captcha"，433KB 的正常頁面也被判定「被擋」→ 白跑一次
    # Selenium → 60 秒逾時。那筆 timeout 紀錄就是這樣來的。
    import httpx
    from config import USER_AGENT
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            resp = await c.get("https://coldbeer.jp/zh")
        html = resp.text
    except Exception as e:
        print(f"  ⚠️ 連不到 coldbeer.jp，跳過真實樣本（{type(e).__name__}）")
        html = ""

    if html:
        check("真實 Shopify 頁面確實含 captcha 字樣（這是重點）",
              "captcha" in html.lower(), f"{len(html)} bytes")
        check("但不可以被判定為擋頁", _looks_blocked(html) is False,
              f"len={len(html)}")
        check("監控端的分類也不可以標成 blocked",
              (sm.start("https://coldbeer.jp/zh"),
               sm.note_http(200, html),
               sm.classify_failure(http_status=200,
                                   block_hint=bool((sm._ctx.get() or {}).get("block_hint")),
                                   got_page=True))[2] == "parse_failed",
              "應為 parse_failed")

    # 真的擋頁（小小一頁）還是要判得出來，不然這個修正會把 Selenium 退路關掉
    challenge = "<html><head><title>Access Denied</title></head><body>"                 "You don't have permission to access this resource. Reference #18.abc</body></html>"
    check("真的擋頁仍判定為擋頁（Selenium 退路要留著）", _looks_blocked(challenge) is True)
    cf = "<html>" + "x" * 3000 + "cloudflare captcha</html>"
    check("小頁面的 cloudflare/captcha 仍算擋頁", _looks_blocked(cf) is True)


# ─────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 74)
    print("爬取紀錄診斷欄位驗證（error_brief / platform_id / domain）")
    print(f"紀錄寫到暫存目錄：{_TMP}")
    print("=" * 74)
    await test_error_brief_from_source()
    await test_error_brief_no_exception()
    await test_platform_id()
    test_domain_normalization()
    test_malformed_host_blocked()
    await test_failsafe_still_holds()
    await test_blocked_heuristic_real_page()

    print("\n" + "=" * 74)
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
