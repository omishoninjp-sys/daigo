"""
Shopify 日本商店爬蟲 Mixin
- 價格/圖片/選項：.json API（price 直接是 JPY 字串，price_currency 確認）
- 庫存 available：.js API（每個 variant 有 available bool）
"""
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
        print(f"[Shopify] 抓取 JSON API: {json_url}")

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "ja,en-US;q=0.9",
                },
            ) as client:

                # ── 主資料：.json
                resp = await client.get(json_url)
                if resp.status_code != 200:
                    print(f"[Shopify] .json HTTP {resp.status_code}，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                data = resp.json()
                prod = data.get("product", data)

                # ── 庫存：.js（variant id → available）
                available_map = {}
                try:
                    js_resp = await client.get(js_url)
                    if js_resp.status_code == 200:
                        for v in js_resp.json().get("variants", []):
                            available_map[v["id"]] = v.get("available", False)
                except Exception:
                    pass

                # ── 商品基本資料
                product.title = prod.get("title", "")
                product.brand = prod.get("vendor", "")
                body = prod.get("body_html") or ""
                product.description = re.sub(r'<[^>]+>', '', body).strip()[:500]

                # ── 圖片
                images = prod.get("images", [])
                if images:
                    product.image_url = images[0]["src"]
                    product.extra_images = [img["src"] for img in images[1:5]]

                # ── Variants
                options = prod.get("options", [])  # [{name, position, values}]

                for v in prod.get("variants", []):
                    vid = v["id"]

                    # 價格：直接是 JPY 字串
                    price_str = v.get("price", "0")
                    currency  = v.get("price_currency", "JPY")
                    price_jpy = int(float(price_str))
                    print(f"[Shopify DEBUG] variant {vid} price={price_str!r} {currency} → ¥{price_jpy}")

                    # 第一個 variant 設為商品價格
                    if not product.price_jpy:
                        product.price_jpy = price_jpy

                    # 庫存
                    in_stock = available_map.get(vid, True)

                    # Color / Size
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

                    # 圖片：根據 image_id 對應
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
