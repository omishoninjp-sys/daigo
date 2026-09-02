"""
SEO 標題來源標記驗證（daigo.seo_source：gpt / fallback）
=======================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_seo_source.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_seo_source.py`）

為什麼要有這支：2026-08-28 16:52 UTC 起 OPENAI_API_KEY 失效，SEO 標題全部走
`_build_fallback_title` 降級版（保留日文原名、沒有繁中語意 tag），商品照常建立、
**完全沒有留下任何痕跡**。事後只能靠「核心 tag 數 <= 2」的統計特徵反推，
才數出 166 件。有了 metafield 就能一句 GraphQL 撈出來。

三件事都要釘住（跟 created_via 同型）：
  1. GPT 路徑回 seo_source="gpt"、降級路徑回 "fallback" —— **兩條都要明講**
  2. /api/create-order 與 /api/create-manual **兩個端點**都把它往下傳
  3. create_daigo_product 真的寫進 metafield daigo.seo_source

★ 第 2 點兩個端點都要驗，不可以只驗一邊 —— 這正是 2026-08-30 漏掉
  _auto_cleanup_loop 的同型失誤：改了一個呼叫點，另一個沒改，
  而沒改的那個才是會出事的。

★ 第 1 點的「兩條都要明講」是重點。**不可以只標降級那條**，靠「沒有標記
  就是 gpt」推論會把這個欄位之前的 1,341 件舊商品全部誤判成 gpt。
  CLAUDE.md 的 created_via 段落已經記過同樣的教訓。

不連外：Shopify 與 OpenAI 都換成假的。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as m
import seo_title as st
from scrapers.base import ProductInfo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail else ""))
    return cond


CAPTURED = {}


async def fake_create(**kw):
    CAPTURED.clear()
    CAPTURED.update(kw)
    return {"product_id": 1, "storefront_url": "https://x/y", "admin_url": "https://a/b"}


async def fake_seo_fallback(original_title="", source_url="", **kw):
    """模擬 OpenAI 掛掉：generate_seo_title 回降級結果。"""
    return {"title": original_title or "標題", "tags": [],
            "seo_source": st.SEO_SOURCE_FALLBACK}


async def fake_scrape(url):
    p = ProductInfo(source_url=url, title="測試商品", price_jpy=5000)
    p.platform_id = "rakuten"
    return p


async def with_stubs(coro_factory):
    orig = (m.shopify.create_daigo_product, m.generate_seo_title, m.scrape_with_queue)
    m.shopify.create_daigo_product = fake_create
    m.generate_seo_title = fake_seo_fallback
    m.scrape_with_queue = fake_scrape
    try:
        return await coro_factory()
    finally:
        (m.shopify.create_daigo_product, m.generate_seo_title,
         m.scrape_with_queue) = orig


# ─────────────────────────────────────────────────────────────────────
async def test_two_paths_both_marked():
    print()
    print("【1】兩條路徑都要明確標記（不可以只標降級那條）")

    fb = st._build_fallback_title("ちいかわ クリアファイル", "ちいかわ", "Mercari")
    check("降級結果帶 seo_source", "seo_source" in fb, str(sorted(fb))[:70])
    check("值是 fallback", fb.get("seo_source") == "fallback", repr(fb.get("seo_source")))

    gpt = st._build_title_from_gpt({
        "brand_zh": "吉伊卡哇", "character_zh": "", "product_type_zh": "資料夾",
        "clean_title_zh": "手感佳的資料夾", "extra_tags": ["文具", "周邊"],
    }, "ちいかわ クリアファイル", "Mercari")
    check("GPT 結果帶 seo_source", "seo_source" in gpt, str(sorted(gpt))[:70])
    check("值是 gpt", gpt.get("seo_source") == "gpt", repr(gpt.get("seo_source")))

    check("兩個值不同（分得出來）", fb.get("seo_source") != gpt.get("seo_source"))
    check("常數與字面值一致",
          (st.SEO_SOURCE_GPT, st.SEO_SOURCE_FALLBACK) == ("gpt", "fallback"),
          f"{st.SEO_SOURCE_GPT!r} / {st.SEO_SOURCE_FALLBACK!r}")


async def test_no_key_falls_back():
    print()
    print("【2】沒有 OPENAI_API_KEY 時，generate_seo_title 要回 fallback")
    orig = st.OPENAI_API_KEY
    st.OPENAI_API_KEY = ""
    try:
        r = await st.generate_seo_title(original_title="テスト商品", brand="B",
                                        source_url="https://jp.mercari.com/item/m1")
    finally:
        st.OPENAI_API_KEY = orig
    check("回 fallback", r.get("seo_source") == "fallback", repr(r.get("seo_source")))
    check("標題仍然產得出來（商品不會建不出來）", bool(r.get("title")),
          r.get("title", "")[:48])


async def test_order_endpoint():
    print()
    print("【3】/api/create-order 要把 seo_source 往下傳")
    req = m.CreateOrderRequest(url="https://item.rakuten.co.jp/shop/code/")
    resp = await with_stubs(lambda: m.create_order(req))
    check("端點回成功", resp.success is True, str(resp.error)[:60])
    check("有傳 seo_source", "seo_source" in CAPTURED, str(sorted(CAPTURED))[:80])
    check("值是 fallback", CAPTURED.get("seo_source") == "fallback",
          repr(CAPTURED.get("seo_source")))


async def test_manual_endpoint():
    print()
    print("【4】★ /api/create-manual 也要傳（兩個端點都要，不可以只改一邊）")
    req = m.ManualOrderRequest(title="手動商品", price_jpy=2217,
                               source_url="http://www.yokumoku.jp")
    resp = await with_stubs(lambda: m.create_manual_order(req))
    check("端點回成功", resp.success is True, str(resp.error)[:60])
    check("有傳 seo_source", "seo_source" in CAPTURED, str(sorted(CAPTURED))[:80])
    check("值是 fallback", CAPTURED.get("seo_source") == "fallback",
          repr(CAPTURED.get("seo_source")))


async def test_metafield_written():
    print()
    print("【5】create_daigo_product 真的寫進 metafield daigo.seo_source")
    import shopify_client as sc

    sent = {}

    async def fake_graphql(self, query, variables=None, idempotent=True):
        if "resourceLimits" in query:
            return {"data": {"shop": {"resourceLimits": {"maxProductVariants": 2048}}}}
        if "ProductSetInput" in query:
            sent["input"] = (variables or {}).get("input", {})
            return {"data": {"productSet": {
                "product": {"id": "gid://shopify/Product/1", "handle": "h", "variants":
                            {"nodes": [{"id": "gid://shopify/ProductVariant/9",
                                        "selectedOptions": []}]}},
                "userErrors": []}}}
        return {"data": {}}

    async def noop(self, *a, **k):
        return None

    orig = (sc.ShopifyClient._graphql, sc.ShopifyClient._upload_images,
            sc.ShopifyClient._add_to_collection,
            sc.ShopifyClient._publish_to_all_channels)
    sc.ShopifyClient._graphql = fake_graphql
    sc.ShopifyClient._upload_images = noop
    sc.ShopifyClient._add_to_collection = noop
    sc.ShopifyClient._publish_to_all_channels = noop

    async def build(**kw):
        sent.clear()
        sc._variant_limit_cache.update({"value": None, "fetched": False})
        await sc.ShopifyClient().create_daigo_product(
            title="測試", price_jpy=1000,
            source_url="https://item.rakuten.co.jp/s/c/", **kw)
        return sent.get("input") or {}

    try:
        in_fb = await build(seo_source="fallback")
        in_gpt = await build(seo_source="gpt")
        in_none = await build()
    finally:
        (sc.ShopifyClient._graphql, sc.ShopifyClient._upload_images,
         sc.ShopifyClient._add_to_collection,
         sc.ShopifyClient._publish_to_all_channels) = orig

    def pick(inp):
        return [mf for mf in (inp.get("metafields") or [])
                if mf.get("key") == "seo_source"]

    fb, gp, no = pick(in_fb), pick(in_gpt), pick(in_none)
    check("fallback 有寫進 metafields", bool(fb),
          str([mf.get("key") for mf in (in_fb.get("metafields") or [])]))
    check("namespace 是 daigo", bool(fb) and fb[0].get("namespace") == "daigo",
          str(fb[:1]))
    check("type 是 single_line_text_field",
          bool(fb) and fb[0].get("type") == "single_line_text_field", str(fb[:1]))
    check("值是 fallback", bool(fb) and fb[0].get("value") == "fallback", str(fb[:1]))
    check("gpt 也有寫進去（不是只標降級）",
          bool(gp) and gp[0].get("value") == "gpt", str(gp[:1]))
    check("沒傳 seo_source 時不寫這個欄位（舊呼叫點不受影響）", not no,
          str([mf.get("key") for mf in (in_none.get("metafields") or [])]))

    tags = in_fb.get("tags") or []
    check("seo_source 沒有跑進 tags（tag 會出現在前台）",
          not any("seo" in str(t).lower() for t in tags), str(tags)[:70])


async def main_async():
    print("=" * 74)
    print("daigo.seo_source 來源標記")
    print("=" * 74)
    await test_two_paths_both_marked()
    await test_no_key_falls_back()
    await test_order_endpoint()
    await test_manual_endpoint()
    await test_metafield_written()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
