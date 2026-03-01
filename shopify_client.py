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

        title = f"客製化代購 {title}"

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

        # === 圖片：先只放主圖和額外圖（不放顏色圖片，之後用 variant_ids 綁定）===
        images = []
        added_urls = set()
        color_img_urls = set(color_image_map.values())

        if image_url:
            images.append({"src": image_url, "position": 1})
            added_urls.add(image_url)

        if extra_images:
            pos = 2
            for img in extra_images[:9]:
                if img and img not in added_urls and img not in color_img_urls:
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
            print(f"[Shopify] 商品已建立: {product_id} / {handle} / variants: {len(created_variants)}")

        # === 用 variant_ids 上傳顏色圖片並直接綁定 ===
        if color_image_map and created_variants:
            await self._upload_color_images(product_id, created_variants, color_image_map)

        # === 加入 Collection ===
        if DAIGO_COLLECTION_ID:
            await self._add_to_collection(product_id)
        else:
            print(f"[Shopify] ⚠️ DAIGO_COLLECTION_ID 未設定，跳過 collection")

        # === 發布到所有銷售管道 ===
        await self._publish_to_all_channels(product_id)

        return {
            "product_id": product_id,
            "handle": handle,
            "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
            "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
        }

    async def _upload_color_images(self, product_id, created_variants, color_image_map):
        """每個顏色上傳圖片，用 variant_ids 直接綁定到對應的 variants"""
        try:
            # 建立 color → [variant_id, ...] 對照表
            color_to_variant_ids = {}
            for var in created_variants:
                color = var.get("option1", "")
                if color and color in color_image_map:
                    color_to_variant_ids.setdefault(color, []).append(var["id"])

            if not color_to_variant_ids:
                print(f"[Shopify] ⚠️ 無顏色需要綁定圖片")
                return

            print(f"[Shopify] 上傳 {len(color_to_variant_ids)} 個顏色圖片...")

            async with httpx.AsyncClient(timeout=30) as client:
                linked = 0
                for color, variant_ids in color_to_variant_ids.items():
                    img_url = color_image_map[color]

                    # POST /products/{id}/images.json 帶 variant_ids → 上傳 + 綁定一步完成
                    resp = await client.post(
                        f"{self.base_url}/products/{product_id}/images.json",
                        headers=self.headers,
                        json={
                            "image": {
                                "src": img_url,
                                "variant_ids": variant_ids,
                            }
                        },
                    )
                    if resp.status_code in (200, 201):
                        linked += 1
                        img_data = resp.json().get("image", {})
                        print(f"[Shopify]   ✅ {color}: image_id={img_data.get('id')} → {len(variant_ids)} variants")
                    else:
                        print(f"[Shopify]   ⚠️ {color} 上傳失敗 ({resp.status_code}): {resp.text[:100]}")

                print(f"[Shopify] ✅ 顏色圖片連動完成: {linked}/{len(color_to_variant_ids)} 顏色")

        except Exception as e:
            print(f"[Shopify] 顏色圖片連動錯誤: {e}")

    async def _publish_to_all_channels(self, product_id):
        """發布商品到所有銷售管道（GraphQL）"""
        try:
            graphql_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
            gql_headers = {
                "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient(timeout=15) as client:
                # 1. 取得所有 publication
                resp = await client.post(graphql_url, headers=gql_headers, json={
                    "query": "{ publications(first:20){ edges{ node{ id name }}}}"
                })
                if resp.status_code != 200:
                    print(f"[Shopify] ⚠️ 無法取得銷售管道: {resp.status_code}")
                    return

                pubs = resp.json().get("data", {}).get("publications", {}).get("edges", [])
                if not pubs:
                    print(f"[Shopify] ⚠️ 沒有找到銷售管道")
                    return

                # 去重
                seen = set()
                unique_pubs = []
                for p in pubs:
                    name = p["node"]["name"]
                    if name not in seen:
                        seen.add(name)
                        unique_pubs.append(p["node"])

                # 2. 發布到所有管道
                mutation = """mutation publishablePublish($id:ID!,$input:[PublicationInput!]!){
                    publishablePublish(id:$id,input:$input){
                        userErrors{field message}
                    }
                }"""
                resp = await client.post(graphql_url, headers=gql_headers, json={
                    "query": mutation,
                    "variables": {
                        "id": f"gid://shopify/Product/{product_id}",
                        "input": [{"publicationId": p["id"]} for p in unique_pubs],
                    }
                })

                errors = resp.json().get("data", {}).get("publishablePublish", {}).get("userErrors", [])
                if errors:
                    print(f"[Shopify] ⚠️ 發布部分失敗: {errors}")
                else:
                    print(f"[Shopify] ✅ 已發布到 {len(unique_pubs)} 個銷售管道")

        except Exception as e:
            print(f"[Shopify] 發布銷售管道錯誤: {e}")

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
