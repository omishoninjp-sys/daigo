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
import asyncio
import json
import base64 as _b64
import httpx
from config import SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, DAIGO_COLLECTION_ID, STORE_DOMAIN
from pricing import calculate_selling_price

# ★ 版本標記：啟動時會印一次。若 log 看不到這行，代表跑的不是這支檔案。
print("[shopify_client] LOADED build=GRAPHQL-PRODUCTSET-v2 (2026-06-11)")

# 每商品變體上限不寫死：向 Shopify 查本店實際值（shop.resourceLimits.maxProductVariants）。
# 寫死會過期——舊的 100 上限自 2024 起已改為 2048，之後也可能再變。
# 查一次快取整個 process；查不到就不做上限警告（絕不因此擋下上架）。
_variant_limit_cache = {"value": None, "fetched": False}


def next_page_info(link_header: str) -> str:
    """
    從 Shopify 的 Link header 取「下一頁」的 page_info；沒有下一頁回空字串。

    ★ 不可以用 re.search(r'page_info=([^&>]+).*?rel="next"') 對整個 header 比對。
      第 2 頁起 header 長這樣：
        <...page_info=PREV>; rel="previous", <...page_info=NEXT>; rel="next"
      那個 regex 會抓到 **previous** 的 cursor，於是在第 1、2 頁之間來回，
      永遠掃不完（2026-08-30 實測：一支掃描腳本因此跑了 80 分鐘沒有結束）。
      正確做法是先用逗號切段，只看含 rel="next" 的那一段。
    """
    import re as _re
    for seg in (link_header or "").split(","):
        if 'rel="next"' in seg:
            m = _re.search(r'page_info=([^&>]+)', seg)
            return m.group(1) if m else ""
    return ""


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
        """
        送一次 GraphQL；429 / THROTTLED / 5xx 會退避重試（與 _get_with_retry 同一套
        參數：最多 RETRY_MAX_ATTEMPTS 次，1→2→4→8 秒，上限 16，看 Retry-After）。

        會重試的：HTTP 429、HTTP 5xx、errors 裡帶 extensions.code = THROTTLED、
                  以及連線層例外（超時、斷線）。
        不重試的：其他 GraphQL errors（欄位寫錯、權限不足…）與其他 HTTP 狀態碼 ——
                  重試也不會變好，而且會蓋掉真正的錯誤原文。

        ★ 重試一個 mutation 的前提是它可以安全重來。這個專案實際會重來的是
          productDelete / tagsAdd / 各種查詢，都是冪等的。**唯一要小心的是
          create_daigo_product 的 productSet 建立商品**：如果 Shopify 其實已經
          建好了才回 5xx，重試會建出第二件。發生機率低，但看到重複商品時
          要想到這條。
        """
        delay = 1.0
        last = ""
        for attempt in range(1, self.RETRY_MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        self.graphql_url, headers=self.headers,
                        json={"query": query, "variables": variables or {}},
                    )
            except Exception as e:
                last = f"{type(e).__name__}: {e}"          # 連線層失敗，值得重試
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    errs = data.get("errors")
                    if not errs:
                        return data
                    # 不截斷：Shopify 的錯誤原文是唯一能指出「哪個欄位/哪個變體」出問題的線索
                    text = json.dumps(errs, ensure_ascii=False)
                    codes = [str(((e or {}).get("extensions") or {}).get("code") or "")
                             for e in errs if isinstance(e, dict)]
                    if not any(c.upper() == "THROTTLED" for c in codes):
                        raise Exception(f"Shopify GraphQL errors: {text}")
                    last = "THROTTLED"
                elif resp.status_code in self.RETRY_STATUSES:
                    last = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                else:
                    raise Exception(f"Shopify GraphQL HTTP {resp.status_code}: {resp.text}")

            if attempt < self.RETRY_MAX_ATTEMPTS:
                print(f"[Shopify] ⏳ GraphQL 失敗（{last}），{delay:.0f}s 後重試"
                      f"（第 {attempt}/{self.RETRY_MAX_ATTEMPTS - 1} 次）")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 16)

        raise Exception(f"Shopify GraphQL 重試 {self.RETRY_MAX_ATTEMPTS} 次仍失敗：{last}")

    async def _fetch_all_variant_nodes(self, product_gid: str) -> list:
        """
        分頁抓某商品的完整變體清單（id + selectedOptions）。

        productSet 的回傳只帶 variants(first: 100)，超過 100 的變體拿不到 id，
        顏色圖就連動不上——客人選了顏色但圖不換，而且不會有任何錯誤訊息，是
        靜默缺陷。把 first 調大（250）只是把問題推遠：每商品上限是 2048。

        任一頁失敗就回傳已取得的部分，由呼叫端印警告；絕不因此擋下上架。
        """
        query = """
        query VariantPage($id: ID!, $cursor: String) {
          product(id: $id) {
            variants(first: 250, after: $cursor) {
              pageInfo { hasNextPage endCursor }
              nodes { id selectedOptions { name value } }
            }
          }
        }"""
        nodes, cursor = [], None
        for _ in range(20):          # 2048 / 250 ≈ 9 頁，20 頁是防呆上限
            try:
                data = await self._graphql(query, {"id": product_gid, "cursor": cursor})
            except Exception as e:
                print(f"[Shopify] ⚠️ 變體分頁查詢失敗（已取得 {len(nodes)} 筆）："
                      f"{type(e).__name__}: {e}")
                return nodes
            conn = (((data.get("data") or {}).get("product") or {}).get("variants") or {})
            nodes.extend(conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            cursor = page.get("endCursor")
            if not page.get("hasNextPage") or not cursor:
                return nodes
        print(f"[Shopify] ⚠️ 變體分頁達到 20 頁上限，取得 {len(nodes)} 筆後停止")
        return nodes

    async def _max_product_variants(self):
        """
        本店每商品的變體上限，向 Shopify 查實際值後快取。

        查不到（欄位被移除、權限不足、網路失敗…）回 None —— 呼叫端就略過上限警告，
        照常送出讓 Shopify 自己裁決。這道保護只負責「先喊一聲」，不負責擋下上架。
        """
        if _variant_limit_cache["fetched"]:
            return _variant_limit_cache["value"]
        _variant_limit_cache["fetched"] = True
        try:
            data = await self._graphql("{ shop { resourceLimits { maxProductVariants } } }")
            limit = (((data.get("data") or {}).get("shop") or {})
                     .get("resourceLimits") or {}).get("maxProductVariants")
            limit = int(limit) if limit else None
            _variant_limit_cache["value"] = limit
            print(f"[Shopify] 本店每商品變體上限（查得）：{limit}")
        except Exception as e:
            _variant_limit_cache["value"] = None
            print(f"[Shopify] ⚠️ 查不到變體上限（{type(e).__name__}: {e}）→ 略過上限警告，照常送出")
        return _variant_limit_cache["value"]

    async def create_daigo_product(self, title, price_jpy, image_url="", description="",
                                    source_url="", original_price_jpy=0, brand="", extra_images=None,
                                    variants=None, image_base64="", extra_tags=None,
                                    seo_title="", seo_tags=None, in_stock=True, platform_id=""):
        print(f"[Shopify] ▶ create_daigo_product build=GRAPHQL-PRODUCTSET-v2 | variants_in={len(variants) if variants else 0}")
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

        # 單品 fallback：productSet 規則「有 variants 就必須有 productOptions」(codependent)，
        #   且每個 variant 都要有 optionValues。用 Shopify 預設的隱藏選項 Title / Default Title
        #   （大小寫必須剛好是 "Default Title"），主題會自動隱藏 → 商品頁呈現為無變體單品。
        if not variant_specs:
            variant_specs = [{"ov": [("Title", "Default Title")], "price": price_jpy, "sku": "", "color": ""}]
            option_names = ["Title"]

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
        # ── 來源標記（轉型藍圖 #2：按來源算營收）──
        # platform_id 由 Platform 層在 scrape 時設定；未帶時從 source_url 反推。
        src_id = (platform_id or "").strip()
        if not src_id and source_url:
            try:
                from scrapers.base import detect_platform
                src_id = detect_platform(source_url)
            except Exception:
                src_id = ""
        if src_id:
            src_tag = f"source:{src_id}"
            if src_tag not in final_tags:
                final_tags.append(src_tag)

        metafields = [mf for mf in [
            {"namespace": "daigo", "key": "source_url", "value": source_url, "type": "url"} if source_url else None,
            {"namespace": "daigo", "key": "original_price_jpy", "value": str(original_price_jpy), "type": "number_integer"},
            {"namespace": "custom", "key": "link", "value": source_url, "type": "url"} if source_url else None,
            {"namespace": "daigo", "key": "platform", "value": src_id, "type": "single_line_text_field"} if src_id else None,
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
        # 變體數不做截斷：截掉就是靜默丟掉款式，客人看到的清單不完整卻沒有任何跡象。
        # 寧可讓 Shopify 明確拒絕（錯誤原文照印），也不要默默少賣幾款。
        # 上限向 Shopify 查（_max_product_variants），不寫死。
        n_variants = len(gql_variants)
        variant_limit = await self._max_product_variants()
        if variant_limit and n_variants > variant_limit:
            print(f"[Shopify] ⚠️ 變體數 {n_variants} 已超過本店上限 {variant_limit}"
                  f" —— 不截斷，照常送出讓 Shopify 明確裁決")
            print(f"[Shopify] ⚠️   商品：{final_title}")
            print(f"[Shopify] ⚠️   來源：{source_url}")

        mutation = """mutation CreateDaigo($input: ProductSetInput!) {
          productSet(synchronous: true, input: $input) {
            product {
              id
              handle
              variantsCount { count }
              variants(first: 100) { nodes { id selectedOptions { name value } } }
            }
            userErrors { field message }
          }
        }"""

        data = await self._graphql(mutation, {"input": ps_input})
        ps = data.get("data", {}).get("productSet", {})
        errs = ps.get("userErrors", [])
        if errs:
            # 原封不動印出來（不截斷）——錯誤訊息被切掉就查不出是哪個變體出問題
            print(f"[Shopify] ❌ productSet userErrors（{n_variants} 個變體）："
                  f"{json.dumps(errs, ensure_ascii=False)}")
            print(f"[Shopify] ❌   商品：{final_title}")
            print(f"[Shopify] ❌   來源：{source_url}")
            raise Exception(f"productSet userErrors: {json.dumps(errs, ensure_ascii=False)}")
        product = ps.get("product")
        if not product:
            print(f"[Shopify] ❌ productSet 無回傳 product：{json.dumps(data, ensure_ascii=False)}")
            raise Exception(f"productSet 無回傳 product: {json.dumps(data, ensure_ascii=False)}")

        product_id = int(product["id"].split("/")[-1])
        handle = product["handle"]
        # 下方第 6 段的顏色圖連動要用這份 node 清單（含 variant id 與 selectedOptions），
        # 不要為了算數量就把它拿掉——這行被刪掉造成過線上 NameError。
        gql_nodes = product.get("variants", {}).get("nodes", [])
        # 數量另外用 variantsCount：variants(first:100) 只回前 100 筆，
        # 拿它的長度當數量會誤導（實際可能更多）。
        created_n = (product.get("variantsCount") or {}).get("count")
        if created_n is None:
            created_n = len(gql_nodes)
        print(f"[Shopify] 商品已建立(GraphQL): {product_id} / {handle} / variants: {created_n}")
        if created_n != n_variants:
            print(f"[Shopify] ⚠️ 送出 {n_variants} 個變體但實際建立 {created_n} 個 —— 請人工確認")
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
        if color_image_map:
            # productSet 只回前 100 筆變體。超過的部分要另外分頁補齊，否則第 101 個
            # 之後的變體不會有顏色連動圖（客人選了顏色圖卻不變，且無任何錯誤訊息）。
            if created_n and created_n > len(gql_nodes):
                print(f"[Shopify] 變體 {created_n} 個 > productSet 回傳的 "
                      f"{len(gql_nodes)} 筆 → 分頁補齊完整清單")
                full = await self._fetch_all_variant_nodes(product["id"])
                if len(full) > len(gql_nodes):
                    gql_nodes = full
                print(f"[Shopify] 分頁後取得 {len(gql_nodes)} 筆變體")
            if created_n and len(gql_nodes) < created_n:
                print(f"[Shopify] ⚠️ 只取得 {len(gql_nodes)}/{created_n} 個變體，"
                      f"其餘變體不會有顏色連動圖 —— 請人工確認")

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

    # ──────────────────────────────────────────────────────────────────
    # 訂單保護：被下單過的商品永不刪除
    # ──────────────────────────────────────────────────────────────────
    # 作法：每次清理前撈近期訂單，把有被下單的商品打上 PROTECT_TAG。
    # 標籤永久留在商品上，所以即使日後訂單超出 API 可查範圍（read_orders
    # 只能看近 60 天），標籤仍然保護得住。
    PROTECT_TAG = "已下單"

    # 退避重試的共用參數（REST 分頁與 _graphql 都用這組）。
    # ★ 2026-08-30 之前這支專案一處重試都沒有 —— _graphql 一撞錯就 raise。
    #   CLAUDE.md 寫的「重試要涵蓋 429、THROTTLED 和 5xx」當時只是慣例，沒有實作。
    RETRY_STATUSES = (429, 500, 502, 503, 504)
    RETRY_MAX_ATTEMPTS = 5

    async def _get_with_retry(self, client, url, params, what="請求"):
        """
        GET + 429/5xx 退避重試。回傳 (resp, err)：成功時 err 為空字串；
        重試用盡、或遇到重試也沒用的狀態碼時，resp 為 None、err 說明原因。

        ★ 不可以「一撞 429 就 break 當成做完」。2026-08-30 的自動清理刪到第 262 件
          撞上限，break 之後照樣印「完成」—— log 是唯一的觀測管道，給的卻是假訊號，
          611 件該刪的留在站上，從外面完全看不出來。
        """
        delay = 1.0
        last = ""
        for attempt in range(1, self.RETRY_MAX_ATTEMPTS + 1):
            resp = None
            try:
                resp = await client.get(url, headers=self.headers, params=params)
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    return resp, ""
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in self.RETRY_STATUSES:
                    return None, last          # 400/401/404 重試也沒用，直接回報
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
            if attempt < self.RETRY_MAX_ATTEMPTS:
                print(f"[Cleanup] ⏳ {what} 失敗（{last}），{delay:.0f}s 後重試"
                      f"（第 {attempt}/{self.RETRY_MAX_ATTEMPTS - 1} 次）")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 16)
        return None, last

    async def _fetch_ordered_product_ids(self, days: int = 60) -> set:
        """回傳近 N 天內任何訂單（含已取消／已退款）碰過的商品 id 集合。

        失敗時直接拋出例外，由呼叫端決定中止清理——寧可不刪，
        也不能因為查不到訂單就把客人下單過的商品頁刪掉。
        """
        from datetime import datetime, timezone, timedelta

        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        query = """
        query OrderedProducts($cursor: String, $q: String!) {
          orders(first: 50, after: $cursor, query: $q) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              lineItems(first: 100) { nodes { product { id } } }
            }
          }
        }
        """
        ids = set()
        cursor = None
        order_count = 0
        while True:
            data = await self._graphql(query, {"cursor": cursor, "q": f"created_at:>={since}"})
            conn = data["data"]["orders"]
            for o in conn["nodes"]:
                order_count += 1
                for li in (o.get("lineItems") or {}).get("nodes", []):
                    prod = li.get("product")
                    if prod and prod.get("id"):
                        ids.add(int(prod["id"].split("/")[-1]))
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
            await asyncio.sleep(0.3)

        print(f"[Cleanup] 訂單掃描：近 {days} 天 {order_count} 筆訂單，涉及 {len(ids)} 件商品")
        return ids

    async def _fetch_tagged_ids(self, product_ids: set, tag: str):
        """
        回傳 (已經有該標籤的 id, 目前還存在的 id)。

        一次查 250 件（GraphQL nodes），不是一件一次 —— 534 件只要 3 次查詢。
        nodes 對已被刪掉的商品回 null，那些 id 不會出現在「還存在」裡，
        後面就不會白打一次注定失敗的 mutation。
        """
        query = """
        query TaggedProducts($ids: [ID!]!) {
          nodes(ids: $ids) { ... on Product { id tags } }
        }
        """
        have, alive = set(), set()
        ids = list(product_ids)
        for i in range(0, len(ids), 250):
            chunk = [f"gid://shopify/Product/{pid}" for pid in ids[i:i + 250]]
            data = await self._graphql(query, {"ids": chunk})
            for node in (data.get("data", {}) or {}).get("nodes") or []:
                if not node or not node.get("id"):
                    continue                     # 商品已不存在
                pid = int(node["id"].split("/")[-1])
                alive.add(pid)
                if tag in [t.strip() for t in (node.get("tags") or [])]:
                    have.add(pid)
            await asyncio.sleep(0.3)
        return have, alive

    async def _protect_products(self, product_ids: set) -> int:
        """
        替被下單過、**而且還沒有**保護標籤的商品補上標籤。

        ★ 一定要先過濾。以前每輪對全部（2026-08 是 534 件）重打一次 tagsAdd，
          每件 0.25s 間隔 ≈ 5–9 分鐘，而且**這段期間一件都還沒開始刪** ——
          容器在這時被部署／重啟打斷，整輪清理等於白跑，下次啟動又從頭來。
          而 534 這個數字只會往上長，某天會拉到跑不完。
        標籤是永久的，所以過濾之後這段自然變成「只補新的」，中斷也不會退回原點。
        """
        mutation = """
        mutation AddProtectTag($id: ID!, $tags: [String!]!) {
          tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
        }
        """
        todo = product_ids
        try:
            have, alive = await self._fetch_tagged_ids(product_ids, self.PROTECT_TAG)
            todo = alive - have
            print(f"[Cleanup] 標籤檢查：涉及訂單 {len(product_ids)} 件，已有標籤 {len(have)} 件，"
                  f"已不存在 {len(product_ids) - len(alive)} 件 → 要補 {len(todo)} 件")
        except Exception as e:
            # 查不到就退回舊行為（全部重打一次）。這裡不能中止清理：
            # 刪除保護看的是「id 在訂單集合 or 有標籤」，訂單集合此時已經拿到了。
            print(f"[Cleanup] ⚠️ 標籤查詢失敗，改為全部重打: {type(e).__name__}: {e}")

        tagged = 0
        for pid in todo:
            try:
                await self._graphql(mutation, {
                    "id": f"gid://shopify/Product/{pid}",
                    "tags": [self.PROTECT_TAG],
                })
                tagged += 1
                await asyncio.sleep(0.25)
            except Exception as e:
                # 打標籤失敗不致命：本次仍會用 id 集合擋下刪除
                print(f"[Cleanup] ⚠️ 標籤寫入失敗 {pid}: {type(e).__name__}: {e}")
        return tagged

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
                "cutoff_date": "", "completed": False,
                "incomplete_reason": "DAIGO_COLLECTION_ID 未設定",
            }

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = []
        errors = []
        skipped = 0
        protected = 0
        page_info = None
        fetched = 0
        page = 0
        completed = True          # 這一輪有沒有把整個系列掃完
        incomplete_reason = ""

        print(f"[Cleanup] 開始清理：Collection {DAIGO_COLLECTION_ID}，刪除 {days} 天前 ({cutoff.strftime('%Y-%m-%d %H:%M UTC')}) 的商品")

        # ── 先取得「被下單過」的商品，查不到就整個中止 ──
        try:
            ordered_ids = await self._fetch_ordered_product_ids(days=60)
        except Exception as e:
            msg = f"無法取得訂單資料，為避免誤刪已下單商品，本次清理中止：{type(e).__name__}: {e}"
            print(f"[Cleanup] ❌ {msg}")
            return {
                "deleted_count": 0, "deleted_ids": [], "skipped_count": 0,
                "protected_count": 0, "error_count": 1, "errors": [msg],
                "cutoff_date": cutoff.strftime("%Y-%m-%d %H:%M UTC"),
                "completed": False, "incomplete_reason": "訂單查詢失敗，fail-closed 中止",
            }

        if ordered_ids:
            tagged = await self._protect_products(ordered_ids)
            print(f"[Cleanup] 保護標籤：已標記 {tagged}/{len(ordered_ids)} 件")

        seen_pages = set()
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {
                    "collection_id": DAIGO_COLLECTION_ID,
                    "fields": "id,title,created_at,status,tags",
                    "limit": 250,
                }
                if page_info:
                    params = {"page_info": page_info, "limit": 250, "fields": "id,title,created_at,status,tags"}

                page += 1
                resp, err = await self._get_with_retry(
                    client, f"{self.base_url}/products.json", params,
                    what=f"商品分頁第 {page} 頁",
                )
                if resp is None:
                    completed = False
                    incomplete_reason = f"分頁在第 {page} 頁失敗（{err}）"
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

                    # 被下單過的商品：不論多舊都保留
                    tag_list = [t.strip() for t in (p.get("tags") or "").split(",")]
                    if pid in ordered_ids or self.PROTECT_TAG in tag_list:
                        protected += 1
                        print(f"[Cleanup] 🔒 保留（已下單）: {pid} {title_short}")
                        continue

                    age_days = (datetime.now(timezone.utc) - created_at).days
                    print(f"[Cleanup] 🗑️  刪除商品 {pid}（{age_days} 天前）: {title_short}")

                    # GraphQL productDelete：REST 對 >100 變體商品會 422 拒刪，GraphQL 無此限制
                    try:
                        gql = await self._graphql(
                            """mutation daigoDelete($input: ProductDeleteInput!) {
                                productDelete(input: $input) { deletedProductId userErrors { field message } }
                            }""",
                            {"input": {"id": f"gid://shopify/Product/{pid}"}},
                        )
                        pd = gql.get("data", {}).get("productDelete", {})
                        uerrs = pd.get("userErrors") or []
                        if pd.get("deletedProductId"):
                            deleted.append(pid)
                            print(f"[Cleanup] ✅ 已刪除: {pid}")
                        else:
                            msg = f"product_id={pid}, userErrors={json.dumps(uerrs, ensure_ascii=False)[:150]}"
                            errors.append(msg)
                            print(f"[Cleanup] ❌ 刪除失敗: {msg}")
                    except Exception as e:
                        msg = f"product_id={pid}, {type(e).__name__}: {e}"
                        errors.append(msg)
                        print(f"[Cleanup] ❌ 刪除失敗: {msg}")

                page_info = next_page_info(resp.headers.get("Link", ""))
                if not page_info or not products:
                    break
                if page_info in seen_pages:
                    # 保險：cursor 重複代表分頁又解析錯了，寧可少掃也不要無限迴圈
                    completed = False
                    incomplete_reason = f"分頁 cursor 在第 {page} 頁重複，停止分頁"
                    break
                seen_pages.add(page_info)

        if completed:
            print(f"[Cleanup] 完成：掃描 {fetched} 件，刪除 {len(deleted)} 件，跳過 {skipped} 件，"
                  f"保護 {protected} 件，錯誤 {len(errors)} 件")
        else:
            # ★ 中途放棄絕對不可以印「完成」：log 是唯一的觀測管道，
            #   假訊號比沒有訊號更糟。
            errors.append(incomplete_reason)
            print(f"[Cleanup] ⚠️ 中止：{incomplete_reason}，已刪除 {len(deleted)} 件，剩餘未處理"
                  f"（掃描 {fetched} 件，跳過 {skipped} 件，保護 {protected} 件，"
                  f"錯誤 {len(errors)} 件）")
        return {
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "skipped_count": skipped,
            "protected_count": protected,
            "error_count": len(errors),
            "errors": errors,
            "cutoff_date": cutoff.strftime("%Y-%m-%d %H:%M UTC"),
            "completed": completed,
            "incomplete_reason": incomplete_reason,
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
            + '<p style="margin:0 0 6px;font-size:13px;color:#444;">✓ 含關稅　✓ 含台灣配送費　✓ 依實重計費　✓ 材積重在實重 3 倍內不加收材積費</p>'
            + '<p style="margin:0 0 6px;font-size:13px;color:#444;">起運 2 kg，未滿 2 kg 以 2 kg 計算，每增加 0.5 kg 加收 ¥500。</p>'
            + '<p style="margin:0 0 12px;font-size:13px;color:#444;">體積較大的商品，若材積重超過實重 3 倍，則改以材積重計費。</p>'
            + '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;">'
            + '<tbody>'
            + '<tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">≦ 2.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,000 <span style="color:#888;font-weight:400;">≈ NT$400</span></td></tr>'
            + '<tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.1 ～ 2.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,500 <span style="color:#888;font-weight:400;">≈ NT$500</span></td></tr>'
            + '<tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.6 ～ 3.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥3,000 <span style="color:#888;font-weight:400;">≈ NT$600</span></td></tr>'
            + '<tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">3.1 ～ 3.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥3,500 <span style="color:#888;font-weight:400;">≈ NT$700</span></td></tr>'
            + '<tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">3.6 ～ 4.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥4,000 <span style="color:#888;font-weight:400;">≈ NT$800</span></td></tr>'
            + '<tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">每增加 0.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">+¥500　 <span style="color:#888;font-weight:400;">+≈ NT$100</span></td></tr>'
            + '</tbody></table>'
            + '<p style="margin:0 0 8px;font-size:12px;color:#999;">NT$ 匯率僅供參考，實際以下單當日匯率為準。運費於商品到倉後出貨前確認重量後統一請款。</p>'
            + '<p style="margin:0 0 28px;font-size:12px;color:#999;">※ 自 2026/09/01 起，最低計費重量由 1 kg 調整為 2 kg（日本出口端新增爆裂物檢查料金），每公斤單價不變。</p>'
            
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
