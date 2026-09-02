"""
成功筆的 warnings 欄位驗證（離線，不連外）
==========================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_warnings_field.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_warnings_field.py`）

★ 這支存在的理由（2026-09-02）：
   record() 在 ok=True 時強制 error_brief=""，於是**成功路徑上的 note_error
   全部被丟掉**。掃過 24 個呼叫點，有 14 個命中後最終仍可能成功：

     platform.py:91    每個 Source 失敗都 note_error，只要後面任一個成功，
                       前面的原因全部消失 —— 這是多 source 退路架構的核心
     uniqlo.py         四層 fallback，前三層的原因（含 API 403 機房 IP 被擋）
                       只要第四層成功就再也查不到
     amazon.py:146     短連結抽不到 ASIN，改用轉址後網址繼續跑
     jsonld.py:270-302 圖片 base64 沒抓到，但商品本身有效

   MUJI 那件事（圖片機制靜默消失六週）就是這一類：商品建出來了、看起來成功，
   而「圖沒抓到」這個資訊被丟在地上。

設計決定：
  · warnings 只在 ok=True 時寫，與 error_brief **互斥** ——
    兩者同一份 state["errors"]，都寫會讓同一句話出現兩次。
    警告只在成功筆有意義；失敗筆該看的是 error_brief。
  · 限長 200 字，比照 error_brief。note_error 的 del errs[:-3] 已經有
    天然上限（最多 3 條），限長是第二道。這是給人看的診斷摘要，不是完整紀錄。

不連外：全部用假 Platform / 假 Source / 假 product。
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="warnfield_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import scrape_monitor as sm
from scrapers.base import ProductInfo
from scrapers.platform import Platform, Source

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


def last():
    rows = sm.read_day()
    return rows[-1] if rows else {}


def good_product(url="https://x.jp/p/1"):
    p = ProductInfo(source_url=url, title="測試商品", price_jpy=1490)
    p.image_url = "https://x.jp/i.jpg"
    return p


URL = "https://x.jp/p/1"


# ═══════════════════════════════════════════════════════════════════
def test_success_carries_warnings():
    print()
    print("【1】成功筆帶 warnings、error_brief 空")
    sm.start(URL)
    sm.note_error("API 403 Forbidden（疑似機房 IP 被擋）", "Uniqlo")
    sm.record(URL, product=good_product(), elapsed_ms=10)
    r = last()
    check("ok=True", r["ok"] is True)
    check("★ warnings 非空", bool(r["warnings"]), r["warnings"][:70])
    check("warnings 內容就是 note_error 的訊息",
          "403" in r["warnings"] and "Uniqlo" in r["warnings"], r["warnings"][:70])
    check("★ error_brief 是空的（兩欄互斥）", r["error_brief"] == "",
          repr(r["error_brief"]))


def test_failure_is_opposite():
    print()
    print("【2】失敗筆相反：error_brief 有值、warnings 空")
    sm.start(URL)
    sm.note_error("API 403 Forbidden（疑似機房 IP 被擋）", "Uniqlo")
    sm.record(URL, product=ProductInfo(source_url=URL), elapsed_ms=10)
    r = last()
    check("ok=False", r["ok"] is False)
    check("★ error_brief 有值", bool(r["error_brief"]), r["error_brief"][:70])
    check("★ warnings 是空的（不重複同一句話）", r["warnings"] == "",
          repr(r["warnings"]))


def test_no_warning_when_clean():
    print()
    print("【3】一路順利時 warnings 是空的（不可以無中生有）")
    sm.start(URL)
    sm.record(URL, product=good_product(), elapsed_ms=10)
    r = last()
    check("ok=True", r["ok"] is True)
    check("warnings 空", r["warnings"] == "", repr(r["warnings"]))
    check("error_brief 也空", r["error_brief"] == "", repr(r["error_brief"]))


def test_length_and_cap():
    print()
    print("【4】限長 200 字 + note_error 的天然上限（最多 3 條）")
    sm.start(URL)
    for i in range(6):
        sm.note_error("第 %d 條警告：" % i + "詳細說明。" * 30, "Src")
    sm.record(URL, product=good_product(), elapsed_ms=10)
    r = last()
    check("★ warnings <= 200 字", len(r["warnings"]) <= 200, f"{len(r['warnings'])} 字")
    check("warnings 不含換行", chr(10) not in r["warnings"])
    # note_error 只留最後 3 條，所以最早那幾條不會出現
    check("★ 只留最後幾條（del errs[:-3] 的天然上限）",
          "第 0 條" not in r["warnings"] and "第 1 條" not in r["warnings"],
          r["warnings"][:60])


def test_no_dupe_within_run():
    print()
    print("【5】同一句話重複 note 只留一份")
    sm.start(URL)
    for _ in range(3):
        sm.note_error("重複的警告", "Src")
    sm.record(URL, product=good_product(), elapsed_ms=10)
    r = last()
    check("warnings 裡只出現一次", r["warnings"].count("重複的警告") == 1,
          r["warnings"][:70])


# ═══════════════════════════════════════════════════════════════════
# 真實退路情境：走正式的 Platform.fetch，不是自己拼 state
# ═══════════════════════════════════════════════════════════════════
class BoomSource(Source):
    kind = "scraper"

    def __init__(self, msg):
        self.msg = msg

    async def get(self, url, ref, engine):
        raise RuntimeError(self.msg)


class OkSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        return good_product(url)


class FourLayerPlatform(Platform):
    """模擬 uniqlo：四層 fallback，前三層失敗、第四層成功。"""
    id = "uniqlo"
    sources = [BoomSource("API 403 Forbidden（疑似機房 IP 被擋）"),
               BoomSource("API 404（商品下架或代碼錯誤）"),
               BoomSource("API 回 200 但解析不出價格"),
               OkSource()]

    def matches(self, url):
        return True


class TwoLayerPlatform(Platform):
    """模擬 MUJI：httpx 被 Akamai 擋，退 Selenium 成功。"""
    id = "muji"
    sources = [BoomSource("ReadTimeout"), OkSource()]

    def matches(self, url):
        return True


async def test_uniqlo_four_layers():
    print()
    print("【6】★ 退路情境一：四層 fallback 前三層失敗、第四層成功")
    sm.start(URL)
    p = await FourLayerPlatform().fetch(URL, engine=None)
    sm.record(URL, product=p, elapsed_ms=10)
    r = last()
    check("最終成功", r["ok"] is True)
    check("★ 403 沒有被丟掉（現行碼會丟）", "403" in r["warnings"], r["warnings"][:80])
    check("404 也留著", "404" in r["warnings"], r["warnings"][:80])
    check("error_brief 仍是空的", r["error_brief"] == "")
    check("看得出是哪個 Source 失敗", "BoomSource" in r["warnings"],
          r["warnings"][:80])


async def test_muji_selenium_fallback():
    print()
    print("【7】★ 退路情境二：httpx 被擋、退 Selenium 成功（MUJI 那條）")
    sm.start(URL)
    p = await TwoLayerPlatform().fetch(URL, engine=None)
    sm.record(URL, product=p, elapsed_ms=10)
    r = last()
    check("最終成功", r["ok"] is True)
    check("★ ReadTimeout 沒有被丟掉", "ReadTimeout" in r["warnings"],
          r["warnings"][:80])
    check("error_brief 仍是空的", r["error_brief"] == "")


def test_failsafe():
    print()
    print("【8】fail-safe：warnings 算不出來也不可以害整筆寫不出來")
    before = len(sm.read_day())
    sm.start(URL)
    state = sm._ctx.get()
    if state is not None:
        state["errors"] = object()          # 不是 list，join 會炸
    sm.record(URL, product=good_product(), elapsed_ms=10)
    check("仍然寫得出紀錄", len(sm.read_day()) == before + 1,
          f"{before} -> {len(sm.read_day())}")
    check("warnings 退成空字串", last().get("warnings") == "",
          repr(last().get("warnings")))


async def main():
    print("=" * 74)
    print("成功筆的 warnings 欄位")
    print("=" * 74)
    test_success_carries_warnings()
    test_failure_is_opposite()
    test_no_warning_when_clean()
    test_length_and_cap()
    test_no_dupe_within_run()
    await test_uniqlo_four_layers()
    await test_muji_selenium_fallback()
    test_failsafe()
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
