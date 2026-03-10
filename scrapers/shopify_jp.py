"""
Shopify 日本商店爬蟲 Mixin
使用 /products/handle.js（含 available 和 JPY cents 價格）
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

        js_url   = f"{base_url}/products/{handle}.js"
        json_url = f"{base_url}/products/{handle}.json"
        print(f"[Shopify] 嘗試 JSON API: {json_url}")

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
                    "Accept": "application/json",
                    "Accept-Language": "ja,en-US;q=0.9",
                },
            ) as client:

                # ── 抓 .js（主要資料，含 available 和 JPY cents 價格）
                js_resp = await client.get(js_url)
                if js_resp.status_code != 200:
                    return await self._scrape_with_playwright(url)

                js = js_resp.json()
                prod = js.get("product", js) if "product" in js else js

                product.title = prod.get("title", "")
                product.brand = prod.get("vendor", "")
                product.description = (prod.get("body_html") or "")[:500]
                if product.description:
                    product.description = re.sub(r'<[^>]+>', '', product.description).strip()

                def _img_src(img):
                    return img if isinstance(img, str) else (img.get("src") or "")
                def _img_id(img):
                    return None if isinstance(img, str) else img.get("id")

                js_images = prod.get("images", [])
                if js_images:
                    product.image_url = _abs(_img_src(js_images[0]))
                    product.extra_images = [_abs(_img_src(i)) for i in js_images[1:5] if _img_src(i)]

                # ── 補充 .json 的 image_id→src 對應表
                image_id_map = {}
                try:
                    json_resp = await client.get(json_url)
                    if json_resp.status_code == 200:
                        json_prod = json_resp.json().get("product", {})
                        for img in json_prod.get("images", []):
                            iid = img.get("id")
                            src = img.get("src", "")
                            if iid and src:
                                image_id_map[iid] = src
                        if not product.image_url and json_prod.get("images"):
                            product.image_url = _abs(json_prod["images"][0].get("src", ""))
                except Exception:
                    pass

                # ── Variants
                variants = prod.get("variants", [])
                options  = prod.get("options", [])

                if variants:
                    # .js price 是 JPY cents（如 4950000 = ¥49,500）
                    raw = normalize_price(variants[0].get("price", ""))
                    if raw:
                        product.price_jpy = raw // 100 if raw > 10000 else raw

                color_image_seen = {}

                for v in variants:
                    option1   = v.get("option1", "") or ""
                    option2   = v.get("option2", "") or ""
                    option3   = v.get("option3", "") or ""
                    available = v.get("available", True)

                    vinfo = {"color": "", "size": "", "in_stock": available, "image": ""}

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

                    title = v.get("title", "")
                    if not vinfo["color"] and not vinfo["size"] and title:
                        if re.match(r'^[XSML0-9]+$', title.upper().strip()):
                            vinfo["size"] = title
                        else:
                            vinfo["color"] = title

                    v_img_id = v.get("image_id")
                    featured = v.get("featured_image") or {}
                    img_src = ""
                    if v_img_id and v_img_id in image_id_map:
                        img_src = _abs(image_id_map[v_img_id])
                    elif featured:
                        img_src = _abs(featured if isinstance(featured, str) else featured.get("src", ""))

                    color = vinfo["color"]
                    if img_src and color and color not in color_image_seen:
                        color_image_seen[color] = img_src
                    if color and color in color_image_seen:
                        vinfo["image"] = color_image_seen[color]
                    elif img_src:
                        vinfo["image"] = img_src

                    product.variants.append(vinfo)

                print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy}" if product.price_jpy else f"[Shopify] ✅ {product.title[:40]}")
                return product

        except Exception as e:
            print(f"[Shopify] JSON API 失敗: {type(e).__name__}: {e}")

        return await self._scrape_with_playwright(url)
