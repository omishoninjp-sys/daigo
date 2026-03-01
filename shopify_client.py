"""Shopify Admin API 整合"""
import httpx
from config import SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, DAIGO_COLLECTION_ID, STORE_DOMAIN


class ShopifyClient:
    def __init__(self):
        self.base_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }

    async def create_daigo_product(self, title, price_jpy, image_url="", description="",
                                    source_url="", original_price_jpy=0, brand="", extra_images=None,
                                    variants=None):
        # 建立 Shopify variants
        shopify_variants = []
        options = []

        if variants and len(variants) > 0:
            # 判斷有哪些 option 維度
            has_color = any(v.get("color") for v in variants)
            has_size = any(v.get("size") for v in variants)

            if has_color:
                options.append({"name": "カラー"})
            if has_size:
                options.append({"name": "サイズ"})

            for v in variants:
                sv = {
                    "price": str(price_jpy),
                    "inventory_management": None,
                    "inventory_policy": "continue",
                    "requires_shipping": True,
                }
                if has_color and has_size:
                    sv["option1"] = v.get("color", "")
                    sv["option2"] = v.get("size", "")
                elif has_color:
                    sv["option1"] = v.get("color", "")
                elif has_size:
                    sv["option1"] = v.get("size", "")

                if v.get("sku"):
                    sv["sku"] = str(v["sku"])

                shopify_variants.append(sv)
        
        if not shopify_variants:
            shopify_variants = [{
                "price": str(price_jpy),
                "inventory_management": None,
                "inventory_policy": "continue",
                "requires_shipping": True,
            }]

        product_data = {
            "product": {
                "title": title,
                "body_html": self._build_description(description, source_url, original_price_jpy),
                "vendor": brand or "代購商品",
                "product_type": "代購",
                "tags": ["代購", "daigo"],
                "status": "active",
                "variants": shopify_variants,
                "metafields": [mf for mf in [
                    {"namespace": "daigo", "key": "source_url", "value": source_url, "type": "url"} if source_url else None,
                    {"namespace": "daigo", "key": "original_price_jpy", "value": str(original_price_jpy), "type": "number_integer"},
                    {"namespace": "custom", "key": "link", "value": source_url, "type": "url"} if source_url else None,
                ] if mf is not None],
            }
        }

        if options:
            product_data["product"]["options"] = options

        if brand:
            product_data["product"]["tags"].append(brand)

        images = []
        if image_url:
            images.append({"src": image_url, "position": 1})
        if extra_images:
            for i, img in enumerate(extra_images[:9], start=2):
                images.append({"src": img, "position": i})
        if images:
            product_data["product"]["images"] = images

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self.base_url}/products.json", headers=self.headers, json=product_data)
            if resp.status_code not in (200, 201):
                raise Exception(f"Shopify API error ({resp.status_code}): {resp.text}")
            result = resp.json()
            product = result["product"]
            product_id = product["id"]
            handle = product["handle"]
            print(f"[Shopify] 商品已建立: {product_id} / {handle} / variants: {len(shopify_variants)}")

        if DAIGO_COLLECTION_ID:
            print(f"[Shopify] 加入 Collection: {DAIGO_COLLECTION_ID}")
            await self._add_to_collection(product_id)
        else:
            print(f"[Shopify] ⚠️ DAIGO_COLLECTION_ID 未設定，跳過 collection")

        return {
            "product_id": product_id,
            "handle": handle,
            "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
            "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
        }

    async def _add_to_collection(self, product_id):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.base_url}/collects.json",
                    headers=self.headers,
                    json={"collect": {"product_id": product_id, "collection_id": int(DAIGO_COLLECTION_ID)}},
                )
                if resp.status_code in (200, 201):
                    print(f"[Shopify] ✅ 已加入 Collection {DAIGO_COLLECTION_ID}")
                else:
                    print(f"[Shopify] ⚠️ Collection 加入失敗 ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            print(f"[Shopify] Collection error: {e}")

    def _build_description(self, description, source_url, original_price_jpy):
        parts = []
        if description:
            parts.append(f"<p>{description}</p>")
        parts.append('<div class="daigo-info" style="margin-top:16px;padding:12px;background:#f9f9f9;border-radius:8px;font-size:14px;">')
        parts.append('<p style="margin:0 0 8px 0;"><strong>🛒 代購商品資訊</strong></p>')
        if original_price_jpy:
            parts.append(f'<p style="margin:0 0 4px 0;">日本原價：¥{original_price_jpy:,}</p>')
        if source_url:
            parts.append(f'<p style="margin:0;"><a href="{source_url}" target="_blank" rel="nofollow">查看原始商品頁面 →</a></p>')
        parts.append("</div>")
        parts.append('<p style="margin-top:12px;font-size:13px;color:#666;">※ 本商品為日本代購，下單後約 7-14 個工作天到貨。國際運費 ¥1,250/kg，貨到後另行請款。</p>')
        return "\n".join(parts)
