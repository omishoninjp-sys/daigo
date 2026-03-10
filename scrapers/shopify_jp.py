"""
Shopify 日本商店爬蟲 Mixin
直接解析 HTML 內嵌的 <script type="application/json"> variant 資料
價格格式：JPY cents（5170000 ÷ 100 = ¥51,700）
"""
import json
import re
from urllib.parse import urlparse

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


class ShopifyJpMixin:

    async def _scrape_shopify_jp(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.hostname}"

        print(f"[Shopify] 抓取 HTML: {url}")

        def _abs(src):
            if not src: return ""
            if src.startswith("http"): return src
            return base_url + (src if src.startswith("/") else "/" + src)

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ja,en-US;q=0.9",
                },
            ) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    print(f"[Shopify] HTTP {resp.status_code}，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                html = resp.text

                # ── 1. 從 data-selected-variant 取預設 variant（含 price cents）
                selected_json = re.search(
                    r'<script[^>]+data-selected-variant[^>]*>(.*?)</script>',
                    html, re.DOTALL
                )
                if not selected_json:
                    print("[Shopify] 找不到 data-selected-variant，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                selected = json.loads(selected_json.group(1).strip())
                price_cents = selected.get("price", 0)
                product.price_jpy = price_cents // 100
                print(f"[Shopify DEBUG] price_cents={price_cents} → ¥{product.price_jpy}")

                # ── 2. 商品名稱：從 <li class="title"> 或 <title>
                title_match = re.search(r'<li[^>]+class="title"[^>]*>\s*(.*?)\s*</li>', html, re.DOTALL)
                if title_match:
                    product.title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                if not product.title:
                    og_title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
                    product.title = og_title.group(1).strip() if og_title else selected.get("name", "")

                # ── 3. 品牌
                vendor_match = re.search(r'"vendor"\s*:\s*"([^"]+)"', html)
                product.brand = vendor_match.group(1) if vendor_match else ""

                # ── 4. 圖片：og:image
                og_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
                if og_img:
                    product.image_url = _abs(og_img.group(1))

                # 額外圖片：所有 product image src
                extra_imgs = re.findall(
                    r'<img[^>]+class="[^"]*product[^"]*"[^>]+src="([^"]+)"', html
                )
                seen = {product.image_url}
                for src in extra_imgs:
                    s = _abs(src.split("?")[0])
                    if s and s not in seen and len(product.extra_images) < 4:
                        seen.add(s)
                        product.extra_images.append(s)

                # ── 5. 描述
                desc_match = re.search(
                    r'class="[^"]*description[^"]*"[^>]*>(.*?)</(?:li|div|p)>',
                    html, re.DOTALL
                )
                if desc_match:
                    product.description = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()[:500]

                # ── 6. Variants：從所有 radio input + disabled class 判斷庫存
                #    <input ... value="3" class="disabled" ...>  → 缺貨
                #    <input ... value="3" class="checked-style" ...> → 有庫存
                #    data-option-value-id 對應 variant

                # 先抓所有 variant JSON（頁面內可能有多個 script data-selected-variant）
                all_variant_jsons = re.findall(
                    r'<script[^>]+type="application/json"[^>]*>({"id":\d+[^<]+)</script>',
                    html
                )
                # 從 /products/handle.js 取全部 variants
                path_parts = parsed.path.strip("/").split("/")
                handle = ""
                if "products" in path_parts:
                    idx = path_parts.index("products")
                    handle = path_parts[idx + 1] if idx + 1 < len(path_parts) else ""

                all_variants = []
                if handle:
                    try:
                        js_resp = await client.get(f"{base_url}/products/{handle}.js")
                        if js_resp.status_code == 200:
                            js_prod = js_resp.json()
                            all_variants = js_prod.get("variants", [])
                    except Exception:
                        pass

                # 從 HTML radio input 取庫存狀態
                # <input ... value="SIZE" class="disabled ..."> → sold out
                radio_pattern = re.compile(
                    r'<input[^>]+type="radio"[^>]+value="([^"]+)"[^>]+class="([^"]*)"',
                    re.DOTALL
                )
                radio_stock = {}  # value → in_stock
                for m in radio_pattern.finditer(html):
                    val = m.group(1)
                    cls = m.group(2)
                    in_stock = "disabled" not in cls
                    radio_stock[val] = in_stock

                if all_variants:
                    options = js_prod.get("options", [])
                    for v in all_variants:
                        option1 = v.get("option1", "") or ""
                        option2 = v.get("option2", "") or ""
                        option3 = v.get("option3", "") or ""

                        # 庫存：優先用 radio HTML，fallback 用 .js available
                        in_stock = v.get("available", True)
                        for val in [option1, option2, option3]:
                            if val in radio_stock:
                                in_stock = radio_stock[val]
                                break

                        vinfo = {"color": "", "size": "", "in_stock": in_stock, "image": ""}

                        for i, opt in enumerate(options):
                            opt_name = (opt if isinstance(opt, str) else opt.get("name", "")).lower()
                            opt_pos  = (i + 1) if isinstance(opt, str) else opt.get("position", i + 1)
                            val = option1 if opt_pos == 1 else (option2 if opt_pos == 2 else option3)

                            if any(k in opt_name for k in ["色", "color", "カラー", "colour"]):
                                vinfo["color"] = val
                            elif any(k in opt_name for k in ["サイズ", "size", "寸"]):
                                vinfo["size"] = val
                            elif not vinfo["color"]:
                                vinfo["color"] = val

                        product.variants.append(vinfo)
                else:
                    # fallback：用 selected variant 建單一 variant
                    product.variants.append({
                        "color": selected.get("option2", ""),
                        "size": selected.get("option1", ""),
                        "in_stock": selected.get("available", True),
                        "image": "",
                    })

                print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy} ({len(product.variants)} variants)")
                return product

        except Exception as e:
            print(f"[Shopify] 例外: {type(e).__name__}: {e}，改用 Playwright")

        return await self._scrape_with_playwright(url)
