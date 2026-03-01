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
        shopify_variants = []
        options = []
        color_image_map = {}  # { "ブラウン": "https://..." }

        if variants and len(variants) > 0:
            has_color = any(v.get("color") for v in variants)
            has_size = any(v.get("size") for v in variants)

            if has_color:
                options.append({"name": "カラー"})
            if has_size:
                options.append({"name": "サイズ"})

            # 收集每個顏色的圖片
            for v in variants:
                color = v.get("color", "")
                img = v.get("image", "")
                if color and img and color not in color_image_map:
                    color_image_map[color] = img

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

        # === 圖片：主圖 + 顏色圖片（去重）+ 額外圖片 ===
        images = []
        added_urls = set()

        if image_url:
            images.append({"src": image_url, "position": 1})
            added_urls.add(image_url)

        pos = len(images) + 1
        for color, img_url in color_image_map.items():
            if img_url and img_url not in added_urls:
                images.append({"src": img_url, "position": pos})
                added_urls.add(img_url)
                pos += 1

        if extra_images:
            for img in extra_images[:9]:
                if img and img not in added_urls:
                    images.append({"src": img, "position": pos})
                    added_urls.add(img)
                    pos += 1

        if images:
            product_data["product"]["images"] = images

        # === 建立商品 ===
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self.base_url}/products.json", headers=self.headers, json=product_data)
            if resp.status_code not in (200, 201):
                raise Exception(f"Shopify API error ({resp.status_code}): {resp.text}")
            result = resp.json()
            product = result["product"]
            product_id = product["id"]
            handle = product["handle"]
            created_variants = product.get("variants", [])
            created_images = product.get("images", [])
            print(f"[Shopify] 商品已建立: {product_id} / {handle} / variants: {len(shopify_variants)} / images: {len(created_images)}")

        # === Variant-Image 連動 ===
        if color_image_map and created_variants and created_images:
            await self._link_variant_images(product_id, created_variants, created_images, color_image_map)

        # === 加入 Collection ===
        if DAIGO_COLLECTION_ID:
            await self._add_to_collection(product_id)
        else:
            print(f"[Shopify] ⚠️ DAIGO_COLLECTION_ID 未設定，跳過 collection")

        return {
            "product_id": product_id,
            "handle": handle,
            "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
            "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
        }

    async def _link_variant_images(self, product_id, created_variants, created_images, color_image_map):
        """把每個顏色的圖片綁定到對應的 variant，讓 Shopify 前台選色時自動切圖"""
        try:
            # Shopify CDN 會改 URL，用檔名做比對
            def get_filename(url):
                if not url:
                    return ""
                return url.split("?")[0].rsplit("/", 1)[-1]

            # 建立 filename → image_id
            fname_to_id = {}
            for img in created_images:
                fname = get_filename(img.get("src", ""))
                if fname:
                    fname_to_id[fname] = img["id"]

            # 建立 color → image_id
            color_to_image_id = {}
            for color, original_url in color_image_map.items():
                fname = get_filename(original_url)
                if fname and fname in fname_to_id:
                    color_to_image_id[color] = fname_to_id[fname]

            if not color_to_image_id:
                print(f"[Shopify] ⚠️ 無法匹配顏色圖片（檔名不符），跳過連動")
                return

            print(f"[Shopify] 連動 {len(color_to_image_id)} 個顏色圖片...")

            async with httpx.AsyncClient(timeout=30) as client:
                linked = 0
                for var in created_variants:
                    color = var.get("option1", "")
                    image_id = color_to_image_id.get(color)
                    if not image_id:
                        continue

                    variant_id = var["id"]
                    resp = await client.put(
                        f"{self.base_url}/variants/{variant_id}.json",
                        headers=self.headers,
                        json={"variant": {"id": variant_id, "image_id": image_id}},
                    )
                    if resp.status_code in (200, 201):
                        linked += 1
                    else:
                        print(f"[Shopify] ⚠️ variant {variant_id} 圖片連動失敗: {resp.status_code}")

                print(f"[Shopify] ✅ 圖片連動完成: {linked}/{len(created_variants)} variants")

        except Exception as e:
            print(f"[Shopify] 圖片連動錯誤: {e}")

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
