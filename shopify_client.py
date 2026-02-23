"""
Shopify Admin API 整合模組
- 建立代購商品
- 加入指定 Collection
- 設定 Metafields（原始連結、原始價格等）
"""
import httpx
from config import SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, DAIKO_COLLECTION_ID


class ShopifyClient:
    def __init__(self):
        self.base_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }

    async def create_daiko_product(
        self,
        title: str,
        price_jpy: int,
        image_url: str = "",
        description: str = "",
        source_url: str = "",
        original_price_jpy: int = 0,
        brand: str = "",
        extra_images: list = None,
    ) -> dict:
        """
        在 Shopify 建立代購商品

        Returns:
            {"product_id": ..., "handle": ..., "checkout_url": ...}
        """
        # 組裝商品資料
        product_data = {
            "product": {
                "title": title,
                "body_html": self._build_description(description, source_url, original_price_jpy),
                "vendor": brand or "代購商品",
                "product_type": "代購",
                "tags": ["代購", "daiko"],
                "status": "active",
                # 價格以日幣為單位（你的 Shopify 主要貨幣是 JPY）
                "variants": [
                    {
                        "price": str(price_jpy),
                        "inventory_management": None,  # 代購不追蹤庫存
                        "inventory_policy": "continue",  # 允許超賣（代購都是下單後才買）
                        "requires_shipping": True,
                    }
                ],
                # Metafields 儲存原始資訊
                "metafields": [
                    {
                        "namespace": "daiko",
                        "key": "source_url",
                        "value": source_url,
                        "type": "url",
                    },
                    {
                        "namespace": "daiko",
                        "key": "original_price_jpy",
                        "value": str(original_price_jpy),
                        "type": "number_integer",
                    },
                ],
            }
        }

        # 加入品牌 tag
        if brand:
            product_data["product"]["tags"].append(brand)

        # 加入圖片
        images = []
        if image_url:
            images.append({"src": image_url, "position": 1})
        if extra_images:
            for i, img in enumerate(extra_images[:9], start=2):  # Shopify 最多 250 張，這裡限制 10 張
                images.append({"src": img, "position": i})
        if images:
            product_data["product"]["images"] = images

        # 呼叫 Shopify API 建立商品
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/products.json",
                headers=self.headers,
                json=product_data,
            )

            if resp.status_code not in (200, 201):
                error_body = resp.text
                raise Exception(f"Shopify API 錯誤 ({resp.status_code}): {error_body}")

            result = resp.json()
            product = result["product"]
            product_id = product["id"]
            handle = product["handle"]

        # 加入指定 Collection
        if DAIKO_COLLECTION_ID:
            await self._add_to_collection(product_id)

        return {
            "product_id": product_id,
            "handle": handle,
            "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
            "storefront_url": f"https://{SHOPIFY_STORE.replace('.myshopify.com', '')}.com/products/{handle}",
        }

    async def _add_to_collection(self, product_id: int):
        """將商品加入代購 Collection"""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{self.base_url}/collects.json",
                    headers=self.headers,
                    json={
                        "collect": {
                            "product_id": product_id,
                            "collection_id": int(DAIKO_COLLECTION_ID),
                        }
                    },
                )
        except Exception as e:
            print(f"[Shopify] 加入 Collection 失敗: {e}")

    def _build_description(self, description: str, source_url: str, original_price_jpy: int) -> str:
        """組裝商品描述 HTML"""
        html_parts = []

        if description:
            html_parts.append(f"<p>{description}</p>")

        html_parts.append('<div class="daiko-info" style="margin-top:16px; padding:12px; background:#f9f9f9; border-radius:8px; font-size:14px;">')
        html_parts.append('<p style="margin:0 0 8px 0;"><strong>🛒 代購商品資訊</strong></p>')

        if original_price_jpy:
            html_parts.append(f'<p style="margin:0 0 4px 0;">日本原價：¥{original_price_jpy:,}</p>')

        if source_url:
            html_parts.append(
                f'<p style="margin:0;"><a href="{source_url}" target="_blank" rel="nofollow">查看原始商品頁面 →</a></p>'
            )

        html_parts.append("</div>")

        html_parts.append(
            '<p style="margin-top:12px; font-size:13px; color:#666;">'
            "※ 本商品為日本代購，下單後約 7-14 個工作天到貨。"
            "</p>"
        )

        return "\n".join(html_parts)
