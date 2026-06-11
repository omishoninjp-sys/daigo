"""Shopify Admin API 整合（v2 — 建商品改用 GraphQL productSet）

背景：Shopify REST `POST /products.json` 自 2024-04 起棄用、2024-10 版已不再從
payload materialize variants/options（實測 variants:1），導致代購商品上架後沒有
顏色/尺寸子類。本版把「建立商品 + options + variants」改用 GraphQL productSet。

保留 REST 的部分（這些端點未受影響）：商品圖片上傳、顏色圖連動、collection 加入。
發佈銷售管道本來就是 GraphQL，維持不變。

庫存策略：productSet 在「建立」階段會忽略 inventoryQuantities（Shopify 已知行為），
因此改用 inventoryItem.tracked=false（不追蹤庫存＝永遠可下單），符合代購非現貨本質，
並避開該 bug。若日後要逐變體擋缺貨，再加 tracked=true + inventorySetQuantities。
"""
import json
import base64 as _b64
import httpx
from config import SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, DAIGO_COLLECTION_ID, STORE_DOMAIN
from pricing import calculate_selling_price


class ShopifyClient:
    def __init__(self):
        self.base_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}"
        self.graphql_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }

    # ──────────────────────────────────────────────────────────────────
    # GraphQL helper
    # ──────────────────────────────────────────────────────────────────
    async def _graphql(self, query: str, variables: dict = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.graphql_url, headers=self.headers,
                json={"query": query, "variables": variables or {}},
            )
            if resp.status_code != 200:
                raise Exception(f"Shopify GraphQL HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if data.get("errors"):
                raise Exception(f"Shopify GraphQL errors: {json.dumps(data['errors'], ensure_ascii=False)[:400]}")
            return data

    async def create_daigo_product(self, title, price_jpy, image_url="", description="",
                                    source_url="", original_price_jpy=0, brand="", extra_images=None,
                                    variants=None, image_base64="", extra_tags=None,
                                    seo_title="", seo_tags=None, in_stock=True):
        # ══════════════════════════════════════════════════════════════
        # 1. 建立 option 名稱 + 變體規格（沿用原本的 色/尺寸 判斷邏輯）
        # ══════════════════════════════════════════════════════════════
        option_names = []          # 有序，如 ["カラー","サイズ"]
        opt1_name = opt2_name = None
        variant_specs = []         # {ov:[(optName,val)...], price, sku, color}
        color_image_map = {}

        if variants and len(variants) > 0:
            # 診斷：印出進來的變體（前 3 個），萬一再出問題可比對真實資料
            try:
                print(f"[Shopify] 收到 {len(variants)} 個變體，前3: {variants[:3]}")
            except Exception:
                pass

            # 正規化：None / 缺鍵 → ''，並去空白
            #   （防止 optionValues 出現 null/空字串 → productSet 報錯）
            vn = []
            for v in variants:
                vn.append({
                    "color": (v.get("color") or "").strip(),
                    "size": (v.get("size") or "").strip(),
                    "price": v.get("price", 0),
                    "sku": str(v["sku"]) if v.get("sku") else "",
                    "in_stock": v.get("in_stock", True),
                    "image": v.get("image", "") or "",
                })

            has_color = any(v["color"] for v in vn)
            has_size = any(v["size"] for v in vn)

            import re as _re

            def _vals_look_like_size(field):
                size_pats = [
                    r'\d+\s*(?:cm|mm|inch|インチ)',
                    r'[SsMmLlXx]{1,3}サイズ',
                    r'^\s*[SsMmLlXx]{1,3}\s*$',
                    r'^\s*F\s*$',
                    r'^\s*FREE\s*$',
                    r'^\s*フリー\s*$',
                    r'^\s*\d{1,3}\s*$',
                    r'^[A-Z]{1,2}/\d*[SsMLlXx]{1,3}$',
                    r'^\d+[SsMLlXx]$',
                    r'[\uff10-\uff19]+\s*[\xd7\uff38x]\s*[\uff10-\uff19]+',
                    r'\d+\s*[\xd7x]\s*\d+',
                    r'\u7d04[\uff10-\uff190-9]',
                    r'[\uff10-\uff19]{2,}',
                ]
                color_words = [
                    "\u30b7\u30eb\u30d0\u30fc", "\u30d6\u30e9\u30c3\u30af", "\u30db\u30ef\u30a4\u30c8",
                    "\u30ec\u30c3\u30c9", "\u30d6\u30eb\u30fc", "\u30b4\u30fc\u30eb\u30c9",
                    "\u30d4\u30f3\u30af", "\u30b0\u30ec\u30fc", "\u30b0\u30ea\u30fc\u30f3",
                    "\u30ca\u30c1\u30e5\u30e9\u30eb", "\u30d9\u30fc\u30b8\u30e5",
                    "\u30d6\u30e9\u30a6\u30f3", "\u30aa\u30ec\u30f3\u30b8",
                    "\u30a4\u30a8\u30ed\u30fc", "\u30cd\u30a4\u30d3\u30fc",
                    "\u30d1\u30fc\u30d7\u30eb", "\u30af\u30ea\u30a2",
                    "silver", "black", "white", "red", "blue", "gold",
                ]
                vals = [v[field] for v in vn if v[field]]
                s, c = 0, 0
                for val in vals:
                    if any(_re.search(p, val, _re.IGNORECASE) for p in size_pats):
                        s += 1
                    if any(cw.lower() in val.lower() for cw in color_words):
                        c += 1
                return s > c

            color_is_actually_size = has_color and _vals_look_like_size("color")
            size_is_actually_color = has_size and not _vals_look_like_size("size")

            # active = 真正的選項清單 [(欄位, 選項名)]（沿用相容命名/避免撞名）
            active = []
            if has_color:
                opt1_name = "サイズ" if color_is_actually_size else "カラー"
                active.append(("color", opt1_name))
            if has_size:
                lbl = "カラー" if size_is_actually_color else "サイズ"
                if any(name == lbl for _, name in active):
                    lbl = "サイズ" if lbl == "カラー" else "カラー"
                opt2_name = lbl
                active.append(("size", opt2_name))
            if active:
                print(f"[Shopify] options → {[name for _, name in active]}")

            # 顏色圖
            for v in vn:
                if v["color"] and v["image"] and v["color"] not in color_image_map:
                    color_image_map[v["color"]] = v["image"]

            # 建變體：缺任一 active 選項值的變體直接略過
            #   → 保證每個送出的變體都有完整 optionValues（無 null/空），滿足 codependency
            dropped = 0
            for v in vn:
                ov = []
                complete = True
                for field, oname in active:
                    val = v[field]
                    if not val:
                        complete = False
                        break
                    ov.append((oname, val))
                if active and not complete:
                    dropped += 1
                    continue
                vop = v["price"]
                sp = calculate_selling_price(vop)["selling_price_jpy"] if vop and vop > 0 else price_jpy
                variant_specs.append({"ov": ov, "price": sp, "sku": v["sku"], "color": v["color"]})
            if dropped:
                print(f"[Shopify] ⚠️ 略過 {dropped} 個選項值不完整的變體（避免 optionValues null）")

            # 去重（option 值組合相同保留第一個）
            seen = set()
            dd = []
            for s in variant_specs:
                key = tuple(val for _, val in s["ov"])
                if key not in seen:
                    seen.add(key)
                    dd.append(s)
                else:
                    print(f"[Shopify] ⚠️ 重複 variant 已移除: {key}")
            variant_specs = dd

            # 有完整變體才把 active 當真選項；全被略過則退回單品
            option_names = [name for _, name in active] if variant_specs else []

        # 單品 fallback（無 options、無 optionValues）
        if not variant_specs:
            variant_specs = [{"ov": [], "price": price_jpy, "sku": "", "color": ""}]
            option_names = []

        # ══════════════════════════════════════════════════════════════
        # 2. 組 productSet 的 productOptions + variants
        # ══════════════════════════════════════════════════════════════
        product_options = []
        for i, oname in enumerate(option_names):
            vals = []
            for s in variant_specs:
                for n, val in s["ov"]:
                    if n == oname and val and val not in vals:
                        vals.append(val)
            product_options.append({"name": oname, "position": i + 1,
                                     "values": [{"name": x} for x in vals]})

        gql_variants = []
        for s in variant_specs:
            inv_item = {"tracked": False}
            if s["sku"]:
                inv_item["sku"] = s["sku"]
            gv = {
                "price": str(s["price"]),
                "inventoryItem": inv_item,
                "inventoryPolicy": "CONTINUE",
            }
            if s["ov"]:
                gv["optionValues"] = [{"optionName": n, "name": val} for n, val in s["ov"]]
            gql_variants.append(gv)

        # ══════════════════════════════════════════════════════════════
        # 3. 標題 / tags / metafields
        # ══════════════════════════════════════════════════════════════
        final_title = seo_title if seo_title else f"日本代購｜{title}"

        final_tags = list(seo_tags) if seo_tags else ["日本代購", "代購", "daigo"]
        if brand and brand not in final_tags:
            final_tags.append(brand)
        if extra_tags:
            for t in extra_tags:
                if t not in final_tags:
                    final_tags.append(t)

        metafields = [mf for mf in [
            {"namespace": "daigo", "key": "source_url", "value": source_url, "type": "url"} if source_url else None,
            {"namespace": "daigo", "key": "original_price_jpy", "value": str(original_price_jpy), "type": "number_integer"},
            {"namespace": "custom", "key": "link", "value": source_url, "type": "url"} if source_url else None,
        ] if mf is not None]

        body_html = self._build_description(description, source_url, original_price_jpy,
                                            seo_title=final_title, brand=brand, tags=final_tags)

        ps_input = {
            "title": final_title,
            "descriptionHtml": body_html,
            "vendor": brand or "代購商品",
            "productType": "代購",
            "status": "ACTIVE",
            "tags": final_tags,
            "variants": gql_variants,
            "metafields": metafields,
        }
        if product_options:
            ps_input["productOptions"] = product_options

        # ══════════════════════════════════════════════════════════════
        # 4. productSet mutation（建立商品 + options + variants）
        # ══════════════════════════════════════════════════════════════
        mutation = """mutation CreateDaigo($input: ProductSetInput!) {
          productSet(synchronous: true, input: $input) {
            product {
              id
              handle
              variants(first: 100) { nodes { id selectedOptions { name value } } }
            }
            userErrors { field message }
          }
        }"""

        data = await self._graphql(mutation, {"input": ps_input})
        ps = data.get("data", {}).get("productSet", {})
        errs = ps.get("userErrors", [])
        if errs:
            raise Exception(f"productSet userErrors: {json.dumps(errs, ensure_ascii=False)[:400]}")
        product = ps.get("product")
        if not product:
            raise Exception(f"productSet 無回傳 product: {json.dumps(data, ensure_ascii=False)[:300]}")

        product_id = int(product["id"].split("/")[-1])
        handle = product["handle"]
        gql_nodes = product.get("variants", {}).get("nodes", [])
        print(f"[Shopify] 商品已建立(GraphQL): {product_id} / {handle} / variants: {len(gql_nodes)}")
        print(f"[Shopify] 標題: {final_title}")
        print(f"[Shopify] Tags: {final_tags}")

        # ══════════════════════════════════════════════════════════════
        # 5. 圖片上傳（REST，未受 products 棄用影響）
        # ══════════════════════════════════════════════════════════════
        color_img_urls = set(color_image_map.values())
        await self._upload_images(product_id, image_url, image_base64, extra_images, color_img_urls, title)

        # ══════════════════════════════════════════════════════════════
        # 6. 顏色圖連動（用 GraphQL 回傳的變體做 color → variant_ids 對映）
        # ══════════════════════════════════════════════════════════════
        if color_image_map and gql_nodes:
            color_to_variant_ids = {}
            for node in gql_nodes:
                try:
                    vid = int(node["id"].split("/")[-1])
                except Exception:
                    continue
                for so in node.get("selectedOptions", []):
                    if so.get("value") in color_image_map:
                        color_to_variant_ids.setdefault(so["value"], []).append(vid)
            if color_to_variant_ids:
                await self._upload_color_images(product_id, color_to_variant_ids, color_image_map)

        # ══════════════════════════════════════════════════════════════
        # 7. collection + 發佈
        # ══════════════════════════════════════════════════════════════
        if DAIGO_COLLECTION_ID:
            await self._add_to_collection(product_id)
        else:
            print(f"[Shopify] ⚠️ DAIGO_COLLECTION_ID 未設定,跳過 collection")

        await self._publish_to_all_channels(product_id)

        return {
            "product_id": product_id,
            "handle": handle,
            "admin_url": f"https://{SHOPIFY_STORE}/admin/products/{product_id}",
            "storefront_url": f"https://{STORE_DOMAIN}/products/{handle}",
        }

    # ──────────────────────────────────────────────────────────────────
    # 圖片上傳（主圖 + 額外圖）→ REST /products/{id}/images.json
    # ──────────────────────────────────────────────────────────────────
    async def _upload_images(self, product_id, image_url, image_base64, extra_images, color_img_urls, title=""):
        async def _post_image(payload):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(
                        f"{self.base_url}/products/{product_id}/images.json",
                        headers=self.headers, json={"image": payload},
                    )
                    if r.status_code not in (200, 201):
                        print(f"[Shopify] ⚠️ 圖片上傳失敗 ({r.status_code}): {r.text[:120]}")
                        return False
                    return True
            except Exception as e:
                print(f"[Shopify] 圖片上傳錯誤: {e}")
                return False

        added_urls = set()
        pos = 1

        # 主圖
        if image_base64:
            await _post_image({"attachment": image_base64, "position": pos, "filename": f"{title[:30]}.jpg"})
            print(f"[Shopify] 主圖 base64 上傳 ({len(image_base64)} chars)")
            pos += 1
        elif image_url:
            attach = await self._download_b64(image_url)
            if attach:
                await _post_image({"attachment": attach, "position": pos})
            else:
                await _post_image({"src": image_url, "position": pos})
            added_urls.add(image_url)
            pos += 1

        # 額外圖
        if extra_images:
            for img in extra_images[:9]:
                if img and img not in added_urls and img not in color_img_urls:
                    if img.startswith("data:image"):
                        b64e = img.split(",", 1)[1] if "," in img else None
                        if b64e:
                            await _post_image({"attachment": b64e, "position": pos})
                    else:
                        attach = await self._download_b64(img)
                        if attach:
                            await _post_image({"attachment": attach, "position": pos})
                        else:
                            await _post_image({"src": img, "position": pos})
                    added_urls.add(img)
                    pos += 1

    @staticmethod
    async def _download_b64(url):
        """下載圖片轉 base64（帶 Referer，繞過部分 CDN hotlink 阻擋）；失敗回 None。"""
        if not url or url.startswith("data:image"):
            return url.split(",", 1)[1] if url and "," in url else None
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": url,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            }
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(url, headers=headers)
                if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                    return _b64.b64encode(r.content).decode()
        except Exception as e:
            print(f"[Shopify] 圖片下載失敗，改用 src: {e}")
        return None

    async def _upload_color_images(self, product_id, color_to_variant_ids, color_image_map):
        try:
            if not color_to_variant_ids:
                print(f"[Shopify] ⚠️ 無顏色需要綁定圖片")
                return

            print(f"[Shopify] 上傳 {len(color_to_variant_ids)} 個顏色圖片...")

            async with httpx.AsyncClient(timeout=30) as client:
                linked = 0
                for color, variant_ids in color_to_variant_ids.items():
                    img_url = color_image_map[color]
                    b64 = await self._download_b64(img_url)
                    if b64:
                        img_payload = {"attachment": b64, "variant_ids": variant_ids}
                    else:
                        img_payload = {"src": img_url, "variant_ids": variant_ids}
                    resp = await client.post(
                        f"{self.base_url}/products/{product_id}/images.json",
                        headers=self.headers,
                        json={"image": img_payload},
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
            graphql_url = self.graphql_url
            gql_headers = self.headers

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

    def _build_description(self, description, source_url, original_price_jpy,
                            seo_title="", brand="", tags=None):

        # SEO 段：每件商品獨特內容
        kw_str = ""
        if tags:
            skip = {"日本代購", "代購", "daigo", "Amazon JP", "ZOZOTOWN", "Mercari JP"}
            kws = [t for t in tags if t not in skip]
            if kws:
                kw_str = "　".join(kws[:6])

        brand_str = f"品牌：{brand}　" if brand else ""
        sep = " | "
        kw_part = (sep + kw_str) if kw_str else ""

        seo_intro = ""
        if seo_title:
            seo_intro = (
                '<div style="margin-bottom:24px;">'
                f'<h2 style="font-size:20px;font-weight:800;color:#1a1a2e;margin:0 0 10px;line-height:1.4;">{seo_title}</h2>'
                f'<p style="margin:0;font-size:13px;color:#666;line-height:1.6;">{brand_str}由 GOYOUTATI 御用達代購自日本，空運含稅直送台灣。{kw_part}</p>'
                '</div>'
            )

        source_link = ""
        if source_url:
            source_link = (
                f'<p style="margin:0 0 20px;">'
                f'<a href="{source_url}" target="_blank" rel="nofollow" '
                f'style="display:inline-flex;align-items:center;gap:6px;color:#1a56db;font-size:13px;'
                f'text-decoration:none;border:1px solid #c3d4f5;border-radius:6px;padding:6px 12px;background:#f0f4ff;">'
                f'🔗 查看日本原始商品頁面 →</a></p>'
            )

        return (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#1a1a2e;max-width:700px;line-height:1.75;">'
            + seo_intro
            + source_link
            + '<div style="background:#f0f4ff;border-left:4px solid #1a56db;border-radius:0 8px 8px 0;padding:14px 18px;margin-bottom:28px;">'
            + '<p style="margin:0;font-size:14px;color:#333;">此為<strong>日本代購商品</strong>，由本服務代為向日本購入後空運至台灣，非現貨販售。<br>下單後依商品重量另行收取國際運費，商品到倉後統一請款出貨。</p>'
            + '</div>'
            + '<h2 style="font-size:16px;font-weight:700;color:#1a1a2e;border-bottom:2px solid #e8eaf0;padding-bottom:8px;margin:0 0 16px;">購買流程</h2>'
            + '<div style="margin-bottom:28px;">'
            + '<div style="display:flex;gap:12px;margin-bottom:12px;align-items:flex-start;"><span style="min-width:28px;height:28px;background:#1a56db;color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;">1</span><div><strong style="font-size:14px;">提供商品連結或下單</strong><br><span style="font-size:13px;color:#666;">直接在本站下單，或使用 <a href="https://goyoutati.com/pages/%E6%97%A5%E6%9C%AC%E4%BB%A3%E8%B3%BC-%E4%B8%80%E6%A2%9D%E9%80%A3%E7%B5%90-%E9%80%81%E5%88%B0%E4%BD%A0%E5%AE%B6" target="_blank" style="color:#1a56db;">貼上連結送到你家</a> 服務代購</span></div></div>'
            + '<div style="display:flex;gap:12px;margin-bottom:12px;align-items:flex-start;"><span style="min-width:28px;height:28px;background:#1a56db;color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;">2</span><div><strong style="font-size:14px;">本服務代購並集運至台灣倉</strong><br><span style="font-size:13px;color:#666;">商品可免費在日本倉庫集運存放最長一個月，到倉後 Email 通知</span></div></div>'
            + '<div style="display:flex;gap:12px;margin-bottom:12px;align-items:flex-start;"><span style="min-width:28px;height:28px;background:#1a56db;color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;">3</span><div><strong style="font-size:14px;">出貨通知 → 到府配送</strong><br><span style="font-size:13px;color:#666;">私訊客服確認出貨，系統自動合併訂單一併出貨</span></div></div>'
            + '<div style="display:flex;gap:12px;align-items:flex-start;"><span style="min-width:28px;height:28px;background:#1a56db;color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;">4</span><div><strong style="font-size:14px;">台灣收件</strong><br><span style="font-size:13px;color:#666;">預計從日本出貨後 5～7 個工作天內到台灣</span></div></div>'
            + '</div>'
            + '<h2 style="font-size:16px;font-weight:700;color:#1a1a2e;border-bottom:2px solid #e8eaf0;padding-bottom:8px;margin:0 0 16px;">國際運費（空運・包稅）</h2>'
            + '<p style="margin:0 0 6px;font-size:13px;color:#444;">✓ 含關稅　✓ 含台灣配送費　✓ 只收實重　✓ 無材積費</p>'
            + '<p style="margin:0 0 12px;font-size:13px;color:#444;">起運 1 kg，未滿 1 kg 以 1 kg 計算，每增加 0.5 kg 加收 ¥500。</p>'
            + '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;">'
            + '<tbody>'
            + '<tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">≦ 1.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,000 <span style="color:#888;font-weight:400;">≈ NT$200</span></td></tr>'
            + '<tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.1 ～ 1.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,500 <span style="color:#888;font-weight:400;">≈ NT$300</span></td></tr>'
            + '<tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.6 ～ 2.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,000 <span style="color:#888;font-weight:400;">≈ NT$400</span></td></tr>'
            + '<tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.1 ～ 2.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,500 <span style="color:#888;font-weight:400;">≈ NT$500</span></td></tr>'
            + '<tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.6 ～ 3.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥3,000 <span style="color:#888;font-weight:400;">≈ NT$600</span></td></tr>'
            + '<tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">每增加 0.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">+¥500　<span style="color:#888;">+≈ NT$100</span></td></tr>'
            + '</tbody></table>'
            + '<p style="margin:0 0 28px;font-size:12px;color:#999;">NT$ 匯率僅供參考，實際以下單當日匯率為準。運費於商品到倉後出貨前確認重量後統一請款。</p>'
            + '<h2 style="font-size:16px;font-weight:700;color:#1a1a2e;border-bottom:2px solid #e8eaf0;padding-bottom:8px;margin:0 0 16px;">集運說明</h2>'
            + '<p style="margin:0 0 28px;font-size:13px;color:#444;">多筆訂單可免費集中存放，合併出貨節省運費。存放期限最長 <strong>一個月</strong>，超過期限請主動聯繫客服。</p>'
            + '<div style="background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:14px 18px;margin-bottom:16px;">'
            + '<p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#7a5000;">⚠ 禁運 / 限運提醒</p>'
            + '<p style="margin:0 0 6px;font-size:13px;color:#555;">鋰電池・液體 / 噴霧・食品 / 生鮮・仿冒品</p>'
            + '<p style="margin:0;font-size:13px;color:#555;">以上類別涉及航空安全或法規限制，下單前請先私訊確認是否可代購。</p>'
            + '</div>'
            + '<div style="background:#f0fff4;border:1px solid #86efac;border-radius:8px;padding:14px 18px;">'
            + '<p style="margin:0;font-size:13px;color:#166534;">📬 商品到倉後將以 <strong>Email 通知</strong>，請留意信箱。如需 LINE 通知，請加 <a href="https://lin.ee/JejGv1M" target="_blank" style="color:#166534;font-weight:700;">官方 LINE @544kaytb</a> 並告知訂單號碼。</p>'
            + '</div>'
            + '</div>'
        )
