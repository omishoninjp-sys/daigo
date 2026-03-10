"""
Shopify 日本商店爬蟲 Mixin
使用 Shopify JSON API（/products/handle.json）
"""
import re
from urllib.parse import urlparse

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo, normalize_price


class ShopifyJpMixin:

    async def _scrape_shopify_jp(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.hostname}"

        path_parts = parsed.path.strip("/").split("/")
        if "products" in path_parts:
            idx = path_parts.index("products")
            handle = path_parts[idx + 1] if idx + 1 < len(path_parts) else ""
        else:
            handle = ""

        if not handle:
            return await self._scrape_with_playwright(url)

        json_url = f"{base_url}/products/{handle}.js"
        print(f"[Shopify] 嘗試 JSON API: {json_url}")

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={
                    'User-Agent': USER_AGENT,
                    'Accept': 'application/json',
                    'Accept-Language': 'ja,en-US;q=0.9',
                },
            ) as client:
                resp = await client.get(json_url)

                if resp.status_code == 200:
                    data = resp.json()
                    # .js 直接回傳 product 物件，.json 有 product wrapper
                    prod = data.get("product", data) if "product" in data else data

                    product.title = prod.get("title", "")
                    product.brand = prod.get("vendor", "")
                    product.description = (prod.get("body_html") or "")[:500]
                    if product.description:
                        product.description = re.sub(r'<[^>]+>', '', product.description).strip()

                    images = prod.get("images", [])
                    if images:
                        product.image_url = images[0].get("src", "")
                        product.extra_images = [img.get("src", "") for img in images[1:5] if img.get("src")]

                    variants = prod.get("variants", [])
                    if variants:
                        first_price = variants[0].get("price", "")
                        if first_price:
                            product.price_jpy = normalize_price(first_price)

                        options = prod.get("options", [])
                        image_id_map = {}
                        for img in images:
                            image_id_map[img.get("id")] = img.get("src", "")

                        color_image_seen = {}

                        for v in variants:
                            option1 = v.get("option1", "") or ""
                            option2 = v.get("option2", "") or ""
                            option3 = v.get("option3", "") or ""
                            available = v.get("available", False)

                            variant_info = {"color": "", "size": "", "in_stock": available, "image": ""}

                            for i, opt in enumerate(options):
                                # .js: options 是字串陣列; .json: options 是 dict 陣列
                                if isinstance(opt, dict):
                                    opt_name = (opt.get("name", "") or "").lower()
                                    opt_pos = opt.get("position", i + 1)
                                else:
                                    opt_name = str(opt).lower()
                                    opt_pos = i + 1
                                val = ""
                                if opt_pos == 1: val = option1
                                elif opt_pos == 2: val = option2
                                elif opt_pos == 3: val = option3

                                if any(k in opt_name for k in ["色", "color", "カラー", "colour"]):
                                    variant_info["color"] = val
                                elif any(k in opt_name for k in ["サイズ", "size", "寸"]):
                                    variant_info["size"] = val
                                elif not variant_info["color"]:
                                    variant_info["color"] = val

                            title = v.get("title", "")
                            if not variant_info["color"] and not variant_info["size"] and title:
                                if re.match(r'^[XSML0-9]+$', title.upper().strip()):
                                    variant_info["size"] = title
                                else:
                                    variant_info["color"] = title

                            v_image_id = v.get("image_id")
                            featured = v.get("featured_image", {}) or {}

                            img_src = ""
                            if v_image_id and v_image_id in image_id_map:
                                img_src = image_id_map[v_image_id]
                            elif featured and featured.get("src"):
                                img_src = featured["src"]

                            color = variant_info["color"]
                            if img_src and color and color not in color_image_seen:
                                color_image_seen[color] = img_src

                            if color and color in color_image_seen:
                                variant_info["image"] = color_image_seen[color]
                            elif img_src:
                                variant_info["image"] = img_src

                            product.variants.append(variant_info)

                    print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy}" if product.price_jpy else f"[Shopify] ✅ {product.title[:40]}")
                    return product
        except Exception as e:
            print(f"[Shopify] JSON API 失敗: {type(e).__name__}: {e}")

        return await self._scrape_with_playwright(url)
