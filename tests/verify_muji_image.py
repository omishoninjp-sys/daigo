"""
MUJI 圖片 base64 鉤子驗證（離線，不連外）
==========================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_muji_image.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_muji_image.py`）

★ 這支存在的理由（2026-09-02）：
   MujiPlatform 於 2026-07-21（aa9afdc）註冊時，scrapers/muji.py 的 MujiMixin
   就成為死碼 —— 檔案沒刪、沒有委派、沒有註記，於是它看起來還在用。
   MujiMixin 裡的**圖片 base64 機制隨之靜默消失**，7/21 之後建立的 MUJI 商品
   全部無圖（線上 22 件裡 15 件無圖，7/21 之前的 7 件都有圖），六週後才發現。

🔴 為什麼圖片只能走瀏覽器
   www.muji.com 走 Akamai，**依 TLS 指紋擋非瀏覽器請求**。實測 httpx 與 curl
   對整個網域（含首頁）都是 TCP+TLS 握手成功、首位元組永遠不來，
   而同一台機器的 Chrome UC 正常。所以圖片 URL 原樣交給 Shopify 伺服器端抓
   一定失敗，用 httpx 自己抓也一樣 —— 只有在已經通過 Akamai 的瀏覽器 session
   裡用 fetch() 抓 blob 才行。

這支釘住四件事：
  1. 鉤子**預設關閉**，其他用 JsonLdSeleniumSource 的平台（snidel）不受影響
  2. MujiPlatform 的 Selenium source 打開鉤子，且用**自己的 sources 實例**
     （沿用 JsonLdPlatform 的共用實例會連 snidel 一起打開）
  3. 鉤子只在同源時才抓圖 —— driver 若已離開該網域，fetch() 會變成跨來源請求
  4. httpx 路徑成功、沒走到 Selenium 時要留下訊號
     （不強制走 Selenium，但不可以再靜默）

不連外：driver 與 engine 全部換成假的。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="mujiimg_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

import scrape_monitor as sm
from scrapers.base import ProductInfo
from scrapers.jsonld import (JsonLdSeleniumSource, JsonLdPlatform,
                             _browser_image_b64)
from scrapers.platform_muji import MujiPlatform

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


class FakeDriver:
    def __init__(self, current_url, b64="QUJD" * 60):
        self.current_url = current_url
        self._b64 = b64
        self.calls = []

    def execute_script(self, script, *args):
        self.calls.append(args[0] if args else None)
        if isinstance(self._b64, Exception):
            raise self._b64
        return self._b64


class FakeEngine:
    """最小的 engine 替身：driver + lock + _fetch_with_selenium。"""

    def __init__(self, html="", driver=None):
        self._driver = driver
        self._driver_lock = threading.Lock()
        self._html = html

    def _fetch_with_selenium(self, url):
        return self._html


IMG = "https://www.muji.com/public/media/img/item/4548076959182_org.jpg"
PAGE = "https://www.muji.com/jp/ja/store/cmdty/detail/4548076959182"

# 最小的 JSON-LD 商品頁（parse_jsonld_product 吃得下）
HTML = ('<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"UV\\u30ab\\u30c3\\u30c8\\u5e3d\\u5b50",'
        '"image":["' + IMG + '"],'
        '"offers":{"@type":"Offer","price":"2490","priceCurrency":"JPY",'
        '"availability":"https://schema.org/InStock"}}'
        '</script></head><body></body></html>')


def test_default_off():
    print()
    print("【1】鉤子預設關閉，其他平台不受影響")
    s = JsonLdSeleniumSource()
    check("JsonLdSeleniumSource() 預設 image_b64=False", s.image_b64 is False,
          str(s.image_b64))
    for src in JsonLdPlatform.sources:
        if type(src).__name__ == "JsonLdSeleniumSource":
            check("JsonLdPlatform 的共用 source 也是關的", src.image_b64 is False,
                  str(src.image_b64))

    from scrapers.platform_snidel import SnidelPlatform
    for src in SnidelPlatform.sources:
        if type(src).__name__ == "JsonLdSeleniumSource":
            check("★ SnidelPlatform 沒有被打開（共用實例的陷阱）",
                  src.image_b64 is False, str(src.image_b64))


def test_muji_on():
    print()
    print("【2】MujiPlatform 打開鉤子，且用自己的 sources 實例")
    sel = [s for s in MujiPlatform.sources
           if type(s).__name__ == "JsonLdSeleniumSource"]
    check("MujiPlatform 有 Selenium source", len(sel) == 1, str(len(sel)))
    if sel:
        check("★ image_b64=True", sel[0].image_b64 is True, str(sel[0].image_b64))
    check("★ 不是沿用 JsonLdPlatform 的共用實例（那會連 snidel 一起開）",
          MujiPlatform.sources is not JsonLdPlatform.sources)


async def test_hook_fetches_image():
    print()
    print("【3】鉤子開著時真的把圖抓成 base64")
    drv = FakeDriver(current_url=PAGE)
    eng = FakeEngine(html=HTML, driver=drv)
    src = JsonLdSeleniumSource(tag="MUJI", image_b64=True)
    p = await src.get(PAGE, None, eng)
    check("有解析出商品", p is not None and p.price_jpy == 2490,
          str(p.price_jpy if p else None))
    check("★ image_base64 非空", bool(getattr(p, "image_base64", "")),
          f"{len(getattr(p, 'image_base64', '') or '')} 字元")
    check("fetch() 抓的是主圖 URL", drv.calls[:1] == [IMG], str(drv.calls[:1]))


async def test_hook_off_no_image():
    print()
    print("【4】鉤子關著時不抓圖（不可以偷偷付瀏覽器往返的成本）")
    drv = FakeDriver(current_url=PAGE)
    eng = FakeEngine(html=HTML, driver=drv)
    src = JsonLdSeleniumSource(tag="SNIDEL")          # 預設關閉
    p = await src.get(PAGE, None, eng)
    check("仍然解析得出商品", p is not None and p.price_jpy == 2490)
    check("image_base64 是空的", not getattr(p, "image_base64", ""))
    check("★ 完全沒有呼叫 execute_script", drv.calls == [], str(drv.calls))


def test_same_origin_guard():
    print()
    print("【5】同源保險：driver 離開該網域就不抓")
    # 同源 → 抓
    drv = FakeDriver(current_url=PAGE)
    b64 = _browser_image_b64(FakeEngine(driver=drv), IMG, "MUJI")
    check("同源時抓得到", bool(b64), f"{len(b64 or '')} 字元")

    # 跨來源 → 不抓
    drv2 = FakeDriver(current_url="https://www.zozo.jp/shop/x/goods/1/")
    b64b = _browser_image_b64(FakeEngine(driver=drv2), IMG, "MUJI")
    check("★ driver 已導到別站時不抓（fetch 會變跨來源，CORS 擋 blob）",
          b64b is None, repr(b64b))
    check("★ 跨來源時完全沒呼叫 execute_script", drv2.calls == [], str(drv2.calls))

    # driver 不存在
    check("driver 是 None 時回 None（不 raise）",
          _browser_image_b64(FakeEngine(driver=None), IMG, "MUJI") is None)
    # engine 沒有 lock
    check("engine 沒有 _driver_lock 時回 None（不 raise）",
          _browser_image_b64(object(), IMG, "MUJI") is None)
    # execute_script 爆掉
    drv3 = FakeDriver(current_url=PAGE, b64=RuntimeError("boom"))
    check("execute_script 爆掉時回 None（不 raise）",
          _browser_image_b64(FakeEngine(driver=drv3), IMG, "MUJI") is None)


async def test_httpx_path_signal():
    print()
    print("【6】★ httpx 路徑成功但無圖時要留訊號（不可以再靜默）")

    class HttpxOnlyPlatform(MujiPlatform):
        """模擬 httpx 那條就成功：直接回一個有效但沒有 base64 的商品。"""

        async def _base(self, url, engine):
            p = ProductInfo(source_url=url, title="測試", price_jpy=2490)
            p.image_url = IMG
            return p

    plat = HttpxOnlyPlatform()
    # 換掉 super().fetch 的來源
    import scrapers.jsonld as jl
    orig = jl.JsonLdPlatform.fetch

    async def fake_fetch(self, url, engine):
        p = ProductInfo(source_url=url, title="測試", price_jpy=2490)
        p.image_url = IMG
        return p

    # 同時側錄 print 與 note_error 兩個管道
    import contextlib
    import io as _io
    noted = []
    orig_note = sm.note_error
    sm.note_error = lambda e, where="": noted.append((str(e), where))

    jl.JsonLdPlatform.fetch = fake_fetch
    sm.start(PAGE)
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            p = await plat.fetch(PAGE, FakeEngine())
    finally:
        jl.JsonLdPlatform.fetch = orig
        sm.note_error = orig_note
    out = buf.getvalue()

    check("商品仍然正常回傳（訊號不可以影響結果）",
          p is not None and p.price_jpy == 2490)
    check("★ 有印 log", "httpx 路徑成功但無 base64 圖片" in out,
          out.strip()[:80])
    check("log 說得出後果（Akamai / 可能無圖）",
          "Akamai" in out and "無圖" in out, out.strip()[:80])
    check("★ 有呼叫 note_error", len(noted) == 1, str(len(noted)))
    if noted:
        msg, where = noted[0]
        check("note_error 的訊息寫明 httpx 路徑與 base64",
              "httpx" in msg and "base64" in msg, msg[:70])
        check("note_error 標記來源是 MUJI", where == "MUJI", where)

    # 🔴 已知限制：record() 在 ok=True 時強制 error_brief=""
    #    （scrape_monitor.py：'error_brief': "" if ok else _failure_brief(...)），
    #    所以這個訊號**只會出現在 Zeabur log，不會進 JSONL**。
    #    這條斷言把那個限制釘住 —— 哪天 record() 改成保留成功筆的 errors，
    #    這裡會紅，提醒回來把上面的斷言換成查 error_brief。
    sm.start(PAGE)
    sm.note_error("測試訊號", "MUJI")
    sm.record(PAGE, product=p, elapsed_ms=1)
    e = sm.read_day()[-1]
    check("（已知限制）ok=True 時 error_brief 被清空，訊號進不了 JSONL",
          e["ok"] is True and e["error_brief"] == "",
          f"ok={e['ok']} brief={e['error_brief']!r}")


async def main():
    print("=" * 74)
    print("MUJI 圖片 base64 鉤子")
    print("=" * 74)
    test_default_off()
    test_muji_on()
    await test_hook_fetches_image()
    await test_hook_off_no_image()
    test_same_origin_guard()
    await test_httpx_path_signal()
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
