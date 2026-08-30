"""
created_via 來源標記驗證（爬取 auto / 手動 manual）
==================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_created_via.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_created_via.py`）

為什麼要有這支：2026-08-30 用「source_url 是首頁」掃出可疑商品並刪掉 4 件，
事後比對才發現至少 2 件是工作人員手動建的（手填的 source_url 常常是首頁或不完整，
但商品是真的）。當時**系統完全沒有記錄商品是怎麼來的** —— 兩條路徑走同一支
create_daigo_product，tags 與 metafields 一模一樣，連 source: 標籤都有 fallback。

所以三件事都要釘住：
  1. /api/create-order（爬取）傳 created_via="auto"
  2. /api/create-manual（手動）傳 created_via="manual"
  3. create_daigo_product 真的把它寫進 metafield（daigo.created_via）

★ 兩個端點都要驗，不可以只驗一邊 —— 這正是 2026-08-30 漏掉 _auto_cleanup_loop
  的同型失誤：改了一個呼叫點，另一個沒改，而沒改的那個才是會出事的。
不連外：Shopify 與 SEO 都換成假的。
"""
import sys
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as m
from scrapers.base import ProductInfo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


CAPTURED = {}


async def fake_create(**kw):
    CAPTURED.clear()
    CAPTURED.update(kw)
    return {"product_id": 1, "storefront_url": "https://x/y", "admin_url": "https://a/b"}


async def fake_seo(original_title="", source_url="", **kw):
    return {"title": original_title or "標題", "tags": []}


async def fake_scrape(url):
    p = ProductInfo(source_url=url, title="測試商品", price_jpy=5000)
    p.platform_id = "rakuten"
    return p


async def with_stubs(coro_factory):
    orig = (m.shopify.create_daigo_product, m.generate_seo_title, m.scrape_with_queue)
    m.shopify.create_daigo_product = fake_create
    m.generate_seo_title = fake_seo
    m.scrape_with_queue = fake_scrape
    try:
        return await coro_factory()
    finally:
        (m.shopify.create_daigo_product, m.generate_seo_title,
         m.scrape_with_queue) = orig


# ─────────────────────────────────────────────────────────────────────
async def test_manual_endpoint():
    print("\n【1】/api/create-manual → created_via='manual'")
    req = m.ManualOrderRequest(title="手動商品", price_jpy=2217,
                               source_url="http://www.yokumoku.jp")
    resp = await with_stubs(lambda: m.create_manual_order(req))
    check("端點回成功", resp.success is True, str(resp.error)[:60])
    check("有傳 created_via", "created_via" in CAPTURED, str(sorted(CAPTURED))[:80])
    check("值是 manual", CAPTURED.get("created_via") == "manual",
          repr(CAPTURED.get("created_via")))
    check("手填的首頁 source_url 照樣送得出去（不可被首頁規則擋）",
          CAPTURED.get("source_url") == "http://www.yokumoku.jp",
          repr(CAPTURED.get("source_url")))


async def test_order_endpoint():
    print("\n【2】/api/create-order → created_via='auto'")
    req = m.CreateOrderRequest(url="https://item.rakuten.co.jp/shop/code/")
    resp = await with_stubs(lambda: m.create_order(req))
    check("端點回成功", resp.success is True, str(resp.error)[:60])
    check("有傳 created_via", "created_via" in CAPTURED, str(sorted(CAPTURED))[:80])
    check("值是 auto", CAPTURED.get("created_via") == "auto",
          repr(CAPTURED.get("created_via")))


async def test_metafield_written():
    print("\n【3】create_daigo_product 真的把它寫進 daigo.created_via metafield")
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

    orig_gql = sc.ShopifyClient._graphql
    orig_up = sc.ShopifyClient._upload_images
    orig_col = sc.ShopifyClient._add_to_collection
    orig_pub = sc.ShopifyClient._publish_to_all_channels
    sc.ShopifyClient._graphql = fake_graphql

    async def noop(self, *a, **k):
        return None

    sc.ShopifyClient._upload_images = noop
    sc.ShopifyClient._add_to_collection = noop
    sc.ShopifyClient._publish_to_all_channels = noop
    sc._variant_limit_cache.update({"value": None, "fetched": False})
    try:
        await sc.ShopifyClient().create_daigo_product(
            title="測試", price_jpy=1000, source_url="https://item.rakuten.co.jp/s/c/",
            created_via="manual")
    finally:
        sc.ShopifyClient._graphql = orig_gql
        sc.ShopifyClient._upload_images = orig_up
        sc.ShopifyClient._add_to_collection = orig_col
        sc.ShopifyClient._publish_to_all_channels = orig_pub

    mfs = sent.get("input", {}).get("metafields") or []
    via = [mf for mf in mfs if mf.get("key") == "created_via"]
    check("metafields 裡有 created_via", bool(via), str([mf.get("key") for mf in mfs]))
    check("namespace 是 daigo", via and via[0].get("namespace") == "daigo",
          str(via[:1]))
    check("值正確", via and via[0].get("value") == "manual", str(via[:1]))
    check("★ 用 metafield 不是 tag（tag 會出現在前台、容易被別的邏輯掃到）",
          all("created_via" not in t and "manual" != t
              for t in (sent.get("input", {}).get("tags") or [])),
          str(sent.get("input", {}).get("tags")))

    # 沒傳 created_via 的舊呼叫端不可以憑空多出一個空值欄位
    sent.clear()
    sc.ShopifyClient._graphql = fake_graphql
    sc.ShopifyClient._upload_images = noop
    sc.ShopifyClient._add_to_collection = noop
    sc.ShopifyClient._publish_to_all_channels = noop
    sc._variant_limit_cache.update({"value": None, "fetched": False})
    try:
        await sc.ShopifyClient().create_daigo_product(
            title="測試", price_jpy=1000, source_url="https://item.rakuten.co.jp/s/c/")
    finally:
        sc.ShopifyClient._graphql = orig_gql
        sc.ShopifyClient._upload_images = orig_up
        sc.ShopifyClient._add_to_collection = orig_col
        sc.ShopifyClient._publish_to_all_channels = orig_pub
    mfs2 = sent.get("input", {}).get("metafields") or []
    check("沒傳就不寫（不要塞空字串進 metafield）",
          not [mf for mf in mfs2 if mf.get("key") == "created_via"],
          str([mf.get("key") for mf in mfs2]))


# ─────────────────────────────────────────────────────────────────────
async def main_():
    print("=" * 74)
    print("created_via 來源標記驗證（不連外）")
    print("=" * 74)
    await test_manual_endpoint()
    await test_order_endpoint()
    await test_metafield_written()

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_()))
