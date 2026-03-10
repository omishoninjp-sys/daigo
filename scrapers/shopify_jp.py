"""
Shopify 日本商店爬蟲 Mixin
- 價格：HTML data-selected-variant price（JPY cents ÷ 100）
  → .json 會依 IP 回傳外幣（SGD），不可用
- 圖片/選項：.json API
- 庫存：.js API（available bool）
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

        path_parts = parsed.path.strip("/").split("/")
        handle = ""
        if "products" in path_parts:
            idx = path_parts.index("products")
            handle = path_parts[idx + 1] if idx + 1 < len(path_parts) else ""

        if not handle:
            return await self._scrape_with_playwright(url)

        json_url = f"{base_url}/products/{handle}.json"
        js_url   = f"{base_url}/products/{handle}.js"
        print(f"[Shopify] 抓取頁面: {url}")

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/json",
                    "Accept-Language": "ja,en-US;q=0.9",
                },
            ) as client:

                # ── 1. 抓 HTML，取 data-selected-variant（永遠是 JPY cents）
                html_resp = await client.get(url)
                if html_resp.status_code != 200:
                    return await self._scrape_with_playwright(url)
                html = html_resp.text

                sv_match = re.search(r'data-selected-variant[^>]*>(\{[^<]+\})', html)
                if not sv_match:
                    print("[Shopify] 找不到 data-selected-variant，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                sv = json.loads(sv_match.group(1))
                price_cents = sv.get("price", 0)
                product.price_jpy = price_cents // 100
                print(f"[Shopify DEBUG] data-selected-variant cents={price_cents} → ¥{product.price_jpy}")

                # ── 2. .json 取圖片/選項/商品資訊（不取 price）
                images = []
                options = []
                variants_json = []
                try:
                    jr = await client.get(json_url, headers={"Accept": "application/json"})
                    if jr.status_code == 200:
                        prod = jr.json().get("product", {})
                        product.title = prod.get("title", "")
                        product.brand = prod.get("vendor", "")
                        body = prod.get("body_html") or ""
                        product.description = re.sub(r'<[^>]+>', '', body).strip()[:500]
                        images = prod.get("images", [])
                        options = prod.get("options", [])
                        variants_json = prod.get("variants", [])
                        if images:
                            product.image_url = images[0]["src"]
                            product.extra_images = [img["src"] for img in images[1:5]]
                except Exception as e:
                    print(f"[Shopify] .json 失敗: {e}")

                # title fallback from HTML
                if not product.title:
                    t = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
                    product.title = t.group(1).strip() if t else ""

                # ── 3. .js 取庫存 available
                available_map = {}
                try:
                    jsr = await client.get(js_url, headers={"Accept": "application/json"})
                    if jsr.status_code == 200:
                        for v in jsr.json().get("variants", []):
                            available_map[v["id"]] = v.get("available", False)
                except Exception:
                    pass

                # ── 4. 建立 variants
                for v in variants_json:
                    vid = v["id"]
                    in_stock = available_map.get(vid, True)

                    option1 = v.get("option1", "") or ""
                    option2 = v.get("option2", "") or ""
                    option3 = v.get("option3", "") or ""
                    color, size = "", ""

                    for opt in options:
                        name = opt.get("name", "").lower()
                        pos  = opt.get("position", 1)
                        val  = option1 if pos == 1 else (option2 if pos == 2 else option3)
                        if any(k in name for k in ["color", "colour", "カラー", "色"]):
                            color = val
                        elif any(k in name for k in ["size", "サイズ", "寸"]):
                            size = val
                        elif not color:
                            color = val

                    img_src = ""
                    img_id = v.get("image_id")
                    if img_id:
                        for img in images:
                            if img["id"] == img_id:
                                img_src = img["src"]
                                break
                    if not img_src and images:
                        img_src = images[0]["src"]

                    product.variants.append({
                        "color":    color,
                        "size":     size,
                        "in_stock": in_stock,
                        "image":    img_src,
                        "sku":      v.get("sku", ""),
                    })

                print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy} ({len(product.variants)} variants)")
                return product

        except Exception as e:
            print(f"[Shopify] 例外: {type(e).__name__}: {e}，改用 Playwright")

        return await self._scrape_with_playwright(url)
