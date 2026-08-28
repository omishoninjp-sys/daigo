"""
create_daigo_product 的回歸測試 —— 實際執行函式本體，不是只 import。

怎麼跑（在專案根目錄）：
    PYTHONPATH=. python -X utf8 tests/verify_create_order.py
全部通過 exit 0，任一 case 失敗 exit 1。不會連到 Shopify，也不會建任何商品。

為什麼需要這支：
py_compile 與 import 檢查抓不到「只在某個分支才炸」的錯。實例：
  if color_image_map and gql_nodes:      # gql_nodes 未定義
`and` 會短路——color_image_map 為空時根本不會去取 gql_nodes，所以在「變體沒有
顏色圖」的商品上永遠不會出錯。等到樂天開始帶出逐變體圖片，這個潛伏的 NameError
才在客人下單路徑上炸開（見 34a0361）。這種錯只有「真的把函式從頭跑到尾、而且
把會分歧的分支都走過一遍」才擋得住。

因此下面每個 case 都是刻意挑會走到不同分支的輸入：

  case 1  變體帶顏色圖      → color_image_map 非空 → 進顏色連動（34a0361 炸的那條）
  case 2  變體無圖          → color_image_map 空 → 短路（先前唯一被跑到的路徑）
  case 3  單品無變體        → Default Title 路徑
  case 4  >100 變體帶圖     → 觸發變體分頁補齊，顏色連動要涵蓋全部變體
  case 5  >上限變體         → 印警告後照常送出，不截斷
  case 6  productSet 報錯   → 錯誤原文照印且不吞

只 stub 對外的網路邊界（GraphQL / 圖片上傳 / collection / 發佈），
其餘全部走真的程式碼。
"""
import sys
import json
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import shopify_client as sc


class FakeShopify(sc.ShopifyClient):
    """把對外呼叫換成假的，其餘邏輯完全走真的程式碼。"""

    #: productSet 只回這麼多筆變體 —— 與 Shopify 實際行為一致（first: 100）
    PRODUCT_SET_PAGE = 100

    def __init__(self, n_created=None, user_errors=None, variant_limit=2048):
        super().__init__()
        self.n_created = n_created
        self.user_errors = user_errors
        self.variant_limit = variant_limit
        self.color_image_calls = []
        self.sent_variants = []
        self.variant_pages = 0

    def _nodes_for(self, sent, start, count):
        out = []
        for i, v in enumerate(sent[start:start + count], start=start):
            out.append({
                "id": f"gid://shopify/ProductVariant/{9000 + i}",
                "selectedOptions": [{"name": o["optionName"], "value": o["name"]}
                                    for o in v.get("optionValues", [])],
            })
        return out

    async def _graphql(self, query, variables=None):
        if "resourceLimits" in query:
            return {"data": {"shop": {"resourceLimits":
                    {"maxProductVariants": self.variant_limit}}}}

        if "productSet" in query:
            if self.user_errors:
                return {"data": {"productSet": {"product": None,
                                                "userErrors": self.user_errors}}}
            self.sent_variants = (variables or {}).get("input", {}).get("variants", [])
            n = self.n_created if self.n_created is not None else len(self.sent_variants)
            return {"data": {"productSet": {
                "product": {"id": "gid://shopify/Product/12345", "handle": "test-handle",
                            "variantsCount": {"count": n},
                            "variants": {"nodes": self._nodes_for(
                                self.sent_variants, 0, self.PRODUCT_SET_PAGE)}},
                "userErrors": []}}}

        # 變體分頁查詢（_fetch_all_variant_nodes）
        if "VariantPage" in query:
            self.variant_pages += 1
            cursor = (variables or {}).get("cursor")
            start = int(cursor) if cursor else 0
            page = self._nodes_for(self.sent_variants, start, 250)
            nxt = start + len(page)
            has_next = nxt < len(self.sent_variants)
            return {"data": {"product": {"variants": {
                "pageInfo": {"hasNextPage": has_next,
                             "endCursor": str(nxt) if has_next else None},
                "nodes": page}}}}

        return {"data": {}}

    async def _upload_images(self, *a, **k):
        return None

    async def _upload_color_images(self, product_id, color_to_variant_ids, color_image_map):
        self.color_image_calls.append(color_to_variant_ids)
        return None

    async def _add_to_collection(self, *a, **k):
        return None

    async def _publish_to_all_channels(self, *a, **k):
        return None


def make_variants(n, with_image, colors=3):
    out = []
    for i in range(n):
        c = f"色{i % colors}"
        out.append({
            "color": c,
            "size": f"S{i}",
            "sku": f"sku-{i}",
            "price": 5000 + i,
            "in_stock": True,
            "image": f"https://img.example/{c}.jpg" if with_image else "",
        })
    return out


async def run_case(name, expect_ok=True, expect_linked=None, **kw):
    sc._variant_limit_cache.update({"value": None, "fetched": False})
    client = FakeShopify(n_created=kw.pop("n_created", None),
                         user_errors=kw.pop("user_errors", None),
                         variant_limit=kw.pop("variant_limit", 2048))
    print("\n" + "=" * 74)
    print(f"CASE: {name}")
    print("=" * 74)
    try:
        result = await client.create_daigo_product(
            title="測試商品", price_jpy=6000,
            image_url="https://img.example/main.jpg",
            description="desc", source_url="https://item.rakuten.co.jp/shop/code/",
            original_price_jpy=5000, brand="TestBrand",
            extra_images=[], platform_id="rakuten", **kw)
    except Exception as e:
        if expect_ok:
            print(f"  ❌ FAIL：{type(e).__name__}: {str(e)[:400]}")
            return False
        print(f"  ✅ 如預期擲出：{type(e).__name__}: {str(e)[:160]}")
        return True

    if not expect_ok:
        print("  ❌ FAIL：預期要擲出例外，卻正常回傳")
        return False

    linked = sum(len(v) for m in client.color_image_calls for v in m.values())
    print(f"  走完全程，product_id={result.get('product_id')}"
          f"，顏色連動變體 {linked} 個，分頁查詢 {client.variant_pages} 次")

    if expect_linked is not None and linked != expect_linked:
        print(f"  ❌ FAIL：預期顏色連動涵蓋 {expect_linked} 個變體，實際 {linked} 個")
        return False
    print("  ✅ PASS")
    return True


async def main():
    cases = [
        ("變體帶顏色圖（34a0361 炸的那條）",
         run_case("6 個變體帶顏色圖 → 進顏色連動分支",
                  variants=make_variants(6, True), expect_linked=6)),
        ("變體無圖（短路路徑）",
         run_case("6 個變體無圖 → color_image_map 空",
                  variants=make_variants(6, False), expect_linked=0)),
        ("單品無變體",
         run_case("單品（Default Title 路徑）", variants=[], expect_linked=0)),
        ("150 變體 → 分頁補齊，顏色連動不可只涵蓋前 100",
         run_case("150 個變體帶顏色圖 → 觸發分頁",
                  variants=make_variants(150, True), expect_linked=150)),
        ("超過上限仍照常送出",
         run_case("150 變體 vs 上限 100 → 警告後照常嘗試",
                  variants=make_variants(150, True), variant_limit=100,
                  expect_linked=150)),
        ("userErrors 不吞",
         run_case("productSet 回 userErrors", expect_ok=False,
                  variants=make_variants(4, True),
                  user_errors=[{"field": ["variants", "0", "price"],
                                "message": "Price must be greater than 0"}])),
    ]

    results = []
    for label, coro in cases:
        results.append((label, await coro))

    print("\n" + "=" * 74)
    print("總結")
    print("=" * 74)
    bad = 0
    for label, ok in results:
        print(f"  {'✅' if ok else '❌'} {label}")
        if not ok:
            bad += 1
    print(f"\n{len(results) - bad}/{len(results)} 通過")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
