"""Shopify Admin API 整合"""
import httpx
from config import SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, DAIGO_COLLECTION_ID, STORE_DOMAIN
from pricing import calculate_selling_price


class ShopifyClient:
    def __init__(self):
        self.base_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }

    async def _find_existing_product(self, source_url: str, title: str) -> dict | None:
        """
        查找已存在商品，優先用 source_url metafield，
        fallback 用 title 搜尋比對。回傳 {"id":..., "handle":...} 或 None。
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.base_url}/products.json",
                    headers=self.headers,
                    params={"title": title, "fields": "id,handle,title,metafields", "limit": 5},
                )
                if resp.status_code == 200:
                    products = resp.json().get("products", [])
                    if products:
                        p = products[0]
                        pid = p["id"]
                        handle = p["handle"]
                        print(f"[Shopify] 查重命中 (title): product_id={pid} / {handle}")
                        return {"id": pid, "handle": handle}
        except Exception as e:
            print(f"[Shopify] 查重失敗（忽略）: {e}")
        return None

    async def create_daigo_product(self, title, price_jpy, image_url="", description="",
                                    source_url="", original_price_jpy=0, brand="", extra_images=None,
                                    variants=None, image_base64="", extra_tags=None,
                                    seo_title="", seo_tags=None, in_stock=True):
        shopify_variants = []
        options = []
        color_image_map = {}

        if variants and len(variants) > 0:
            has_color = any(v.get("color") for v in variants)
            has_size = any(v.get("size") for v in variants)

            # ── 動態決定 option 名稱
            # Amazon 有時把尺寸放在「カラー」維度、顏色放在「サイズ」維度（命名錯誤）
            # 所以這裡根據值的實際內容判斷，而不是信任欄位名稱
            import re as _re

            def _vals_look_like_size(field):
                size_pats = [
                    r'\d+\s*(?:cm|mm|inch|インチ)',
                    r'[SsMmLlXx]{1,3}サイズ',
                    r'^\s*[SsMmLlXx]{1,3}\s*$',
                    r'^\s*F\s*$',           # フリーサイズ「F」
                    r'^\s*FREE\s*$',        # FREE
                    r'^\s*フリー\s*$',      # フリー
                    r'^\s*\d{1,3}\s*$',   # 数字サイズ（0, 1, 2, 3, 65, 70, 80...）
                    r'^[A-Z]{1,2}/\d*[SsMLlXx]{1,3}$',  # J/S, J/M, J/XL, J/2XL（adidas JP）
                    r'^\d+[SsMLlXx]$',                   # 25S, 32L 等
                ]
                color_words = [
                    "シルバー", "ブラック", "ホワイト", "レッド", "ブルー", "ゴールド",
                    "ピンク", "グレー", "グリーン", "ナチュラル", "ベージュ", "ブラウン",
                    "オレンジ", "イエロー", "ネイビー", "パープル", "クリア",
                    "silver", "black", "white", "red", "blue", "gold",
                ]
                vals = [v.get(field, "") for v in variants if v.get(field)]
                s, c = 0, 0
                for val in vals:
                    if any(_re.search(p, val, _re.IGNORECASE) for p in size_pats):
                        s += 1
                    if any(cw.lower() in val.lower() for cw in color_words):
                        c += 1
                return s > c

            color_is_actually_size = has_color and _vals_look_like_size("color")
            size_is_actually_color = has_size and not _vals_look_like_size("size")

            if has_color:
                label = "サイズ" if color_is_actually_size else "カラー"
                options.append({"name": label})
                print(f"[Shopify] option1 → {label} (color欄位值像{'尺寸' if color_is_actually_size else '顏色'})")
            if has_size:
                label = "カラー" if size_is_actually_color else "サイズ"
                options.append({"name": label})
                print(f"[Shopify] option2 → {label} (size欄位值像{'顏色' if size_is_actually_color else '尺寸'})")

            for v in variants:
                color = v.get("color", "")
                img = v.get("image", "")
                if color and img and color not in color_image_map:
                    color_image_map[color] = img

            for v in variants:
                variant_original_price = v.get("price", 0)
                if variant_original_price and variant_original_price > 0:
                    variant_pricing = calculate_selling_price(variant_original_price)
                    variant_selling_price = variant_pricing["selling_price_jpy"]
                else:
                    variant_selling_price = price_jpy

                v_in_stock = v.get("in_stock", True)
                sv = {
                    "price": str(variant_selling_price),
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "inventory_quantity": 1 if v_in_stock else 0,
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

        # ── 去除重複 variant（option1+option2 組合相同就保留第一個）
        seen_opts = set()
        deduped_variants = []
        for sv in shopify_variants:
            key = (sv.get("option1", ""), sv.get("option2", ""))
            if key not in seen_opts:
                seen_opts.add(key)
                deduped_variants.append(sv)
            else:
                print(f"[Shopify] ⚠️ 重複 variant 已移除: {key}")
        shopify_variants = deduped_variants

        if not shopify_variants:
            # variants 空の単品 → in_stock パラメータで庫存設定
            shopify_variants = [{
                "price": str(price_jpy),
                "inventory_management": "shopify",
                "inventory_policy": "deny",
                "inventory_quantity": 1 if in_stock else 0,
                "requires_shipping": True,
            }]

        final_title = seo_title if seo_title else f"日本代購｜{title}"

        final_tags = list(seo_tags) if seo_tags else ["日本代購", "代購", "daigo"]
        if brand and brand not in final_tags:
            final_tags.append(brand)
        if extra_tags:
            for t in extra_tags:
                if t not in final_tags:
                    final_tags.append(t)

        # === 庫存狀態判斷 → 缺貨仍設為 active，讓客人看到商品並聯繫店家 ===
        all_out_of_stock = (bool(variants) and all(not v.get("in_stock", True) for v in variants)) or (not variants and not in_stock)
        product_status = "active"
        if all_out_of_stock:
            print(f"[Shopify] ⚠️ 所有 variants 缺貨，仍設為 active（庫存為0，讓客人聯繫詢問）")

        product_data = {
            "product": {
                "title": final_title,
                "body_html": self._build_description(description, source_url, original_price_jpy),
                "vendor": brand or "代購商品",
                "product_type": "代購",
                "tags": final_tags,
                "status": product_status,
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

        images = []
        added_urls = set()
        color_img_urls = set(color_image_map.values())

        if image_base64:
            images.append({"attachment": image_base64, "position": 1, "filename": f"{title[:30]}.jpg"})
            print(f"[Shopify] 使用 base64 圖片上傳 ({len(image_base64)} chars)")
        elif image_url:
            import base64 as _b64
            _img_attachment = None
            try:
                _headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": image_url,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                }
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as _c:
                    _r = await _c.get(image_url, headers=_headers)
                    ct = _r.headers.get("content-type", "image/jpeg")
                    if _r.status_code == 200 and "image" in ct:
                        _img_attachment = _b64.b64encode(_r.content).decode()
            except Exception as _e:
                print(f"[Shopify] 圖片下載失敗，改用 src: {_e}")
            if _img_attachment:
                images.append({"attachment": _img_attachment, "position": 1})
            else:
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

        # ── 查重（建立前）
        existing = await self._find_existing_product(source_url, final_title)
        if existing:
            product_id = existing["id"]
            handle = existing["handle"]
            print(f"[Shopify] ⚠️ 商品已存在，跳過建立: {product_id} / {handle}")
            return {
                "product_id": product_id,
                "handle": handle,
                "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
                "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
                "already_exists": True,
            }

        # ── 建立商品
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self.base_url}/products.json", headers=self.headers, json=product_data)

            if resp.status_code == 422 and "already exists" in resp.text:
                print(f"[Shopify] ⚠️ 422 variant 重複，嘗試撈現有商品...")
                existing2 = await self._find_existing_product(source_url, final_title)
                if existing2:
                    product_id = existing2["id"]
                    handle = existing2["handle"]
                    print(f"[Shopify] ✅ 找到現有商品: {product_id} / {handle}")
                    return {
                        "product_id": product_id,
                        "handle": handle,
                        "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
                        "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
                        "already_exists": True,
                    }
                raise Exception(f"Shopify API error ({resp.status_code}): {resp.text}")

            if resp.status_code not in (200, 201):
                raise Exception(f"Shopify API error ({resp.status_code}): {resp.text}")

            result = resp.json()
            product = result["product"]
            product_id = product["id"]
            handle = product["handle"]
            created_variants = product.get("variants", [])
            print(f"[Shopify] 商品已建立: {product_id} / {handle} / variants: {len(created_variants)}")
            print(f"[Shopify] 標題: {final_title}")
            print(f"[Shopify] Tags: {final_tags}")
            for i, sv in enumerate(shopify_variants):
                label = sv.get("option1", "") or sv.get("option2", "") or f"variant {i+1}"
                print(f"[Shopify]   variant [{label}]: ¥{sv['price']}")

        if color_image_map and created_variants:
            await self._upload_color_images(product_id, created_variants, color_image_map)

        if DAIGO_COLLECTION_ID:
            await self._add_to_collection(product_id)
        else:
            print(f"[Shopify] ⚠️ DAIGO_COLLECTION_ID 未設定，跳過 collection")

        await self._publish_to_all_channels(product_id)

        return {
            "product_id": product_id,
            "handle": handle,
            "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
            "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
        }

    async def _upload_color_images(self, product_id, created_variants, color_image_map):
        try:
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
                    resp = await client.post(
                        f"{self.base_url}/products/{product_id}/images.json",
                        headers=self.headers,
                        json={"image": {"src": img_url, "variant_ids": variant_ids}},
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
        try:
            graphql_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
            gql_headers = {
                "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient(timeout=15) as client:
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

                seen = set()
                unique_pubs = []
                for p in pubs:
                    name = p["node"]["name"]
                    if name not in seen:
                        seen.add(name)
                        unique_pubs.append(p["node"])

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

    async def cleanup_old_daigo_products(self, days: int = 10) -> dict:
        """
        刪除指定系列（DAIGO_COLLECTION_ID）中超過 N 天的商品。
        只動這個系列的商品，不影響其他系列。
        """
        from datetime import datetime, timezone, timedelta

        if not DAIGO_COLLECTION_ID:
            return {
                "deleted_count": 0, "deleted_ids": [], "skipped_count": 0,
                "error_count": 1, "errors": ["DAIGO_COLLECTION_ID 未設定，中止清理"],
                "cutoff_date": "",
            }

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = []
        errors = []
        skipped = 0
        page_info = None
        fetched = 0

        print(f"[Cleanup] 開始清理：Collection {DAIGO_COLLECTION_ID}，刪除 {days} 天前 ({cutoff.strftime('%Y-%m-%d %H:%M UTC')}) 的商品")

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                # 只查詢指定 collection 內的商品
                params = {
                    "collection_id": DAIGO_COLLECTION_ID,
                    "fields": "id,title,created_at,status",
                    "limit": 250,
                }
                if page_info:
                    params = {"page_info": page_info, "limit": 250, "fields": "id,title,created_at,status"}

                resp = await client.get(
                    f"{self.base_url}/products.json",
                    headers=self.headers,
                    params=params,
                )
                if resp.status_code != 200:
                    print(f"[Cleanup] ❌ 無法取得商品列表: {resp.status_code}")
                    break

                products = resp.json().get("products", [])
                fetched += len(products)

                for p in products:
                    pid = p["id"]
                    created_raw = p.get("created_at", "")
                    title_short = p.get("title", "")[:40]

                    try:
                        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                    except Exception:
                        skipped += 1
                        continue

                    if created_at >= cutoff:
                        skipped += 1
                        continue

                    age_days = (datetime.now(timezone.utc) - created_at).days
                    print(f"[Cleanup] 🗑️  刪除商品 {pid}（{age_days} 天前）: {title_short}")

                    del_resp = await client.delete(
                        f"{self.base_url}/products/{pid}.json",
                        headers=self.headers,
                    )
                    if del_resp.status_code == 200:
                        deleted.append(pid)
                        print(f"[Cleanup] ✅ 已刪除: {pid}")
                    else:
                        msg = f"product_id={pid}, status={del_resp.status_code}, body={del_resp.text[:100]}"
                        errors.append(msg)
                        print(f"[Cleanup] ❌ 刪除失敗: {msg}")

                # 處理分頁 Link header
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    import re as _re
                    m = _re.search(r'page_info=([^&>]+).*?rel="next"', link_header)
                    page_info = m.group(1) if m else None
                else:
                    page_info = None

                if not page_info or not products:
                    break

        print(f"[Cleanup] 完成：掃描 {fetched} 件，刪除 {len(deleted)} 件，跳過 {skipped} 件，錯誤 {len(errors)} 件")
        return {
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "skipped_count": skipped,
            "error_count": len(errors),
            "errors": errors,
            "cutoff_date": cutoff.strftime("%Y-%m-%d %H:%M UTC"),
        }

    def _build_description(self, description, source_url, original_price_jpy):
        source_link = ""
        if source_url:
            source_link = f'<p><a href="{source_url}" target="_blank" rel="nofollow">查看原始商品頁面 →</a></p>'

        return f"""
<h2>服務說明</h2>
<p>此為代購商品，由本服務代為向日本購入後轉運至台灣，非現貨販售。下單後將依商品頁說明的運費結構另行收取國際運費。</p>

{source_link}

<h2>購買流程</h2>
<ol>
  <li><strong>提供商品連結或下單</strong><br>直接在本站下單，或私訊提供日本商品連結</li>
  <li><strong>本服務代購並集運至台灣倉</strong><br>商品可免費集運存放最長一個月</li>
  <li><strong>出貨通知 → 到府配送</strong><br>準備出貨時私訊客服，系統自動合併訂單一併出貨</li>
  <li><strong>台灣收件</strong><br>預計從日本出貨後 5~7 個工作天內到台灣</li>
</ol>

<h2>國際運費（空運・包稅）</h2>
<p>✓ 含關稅　✓ 含台灣配送費　✓ 只收實重　✓ 無材積費</p>
<p>起運 1 kg，未滿 1 kg 以 1 kg 計算，每增加 0.5 kg 加收 ¥500。</p>
<table>
  <tbody>
    <tr><td>≦ 1.0 kg</td><td>¥1,000 ≈ NT$200</td></tr>
    <tr><td>1.1 ~ 1.5 kg</td><td>¥1,500 ≈ NT$300</td></tr>
    <tr><td>1.6 ~ 2.0 kg</td><td>¥2,000 ≈ NT$400</td></tr>
    <tr><td>2.1 ~ 2.5 kg</td><td>¥2,500 ≈ NT$500</td></tr>
    <tr><td>2.6 ~ 3.0 kg</td><td>¥3,000 ≈ NT$600</td></tr>
    <tr><td>每增加 0.5 kg</td><td>+¥500　+≈ NT$100</td></tr>
  </tbody>
</table>
<p>NT$ 匯率僅供參考，實際以下單當日匯率為準。運費於商品確認後統一請款。</p>

<h2>集運說明</h2>
<p>多筆訂單可免費集中存放，合併出貨以節省運費。存放期限最長<strong>一個月</strong>，超過期限未出貨者請主動聯繫客服，以免影響商品保管。</p>

<h2>禁運 / 限運提醒</h2>
<p>⚠ 鋰電池　⚠ 液體 / 噴霧　⚠ 食品 / 生鮮　⚠ 仿冒品</p>
<p>以上類別商品涉及航空安全或法規限制，下單前請先私訊確認是否可代購，以免造成損失。</p>
""".strip()
