"""
Shopify 日本商店爬蟲 Mixin
改用 Playwright 直接抓頁面，完全不依賴 .json/.js API
"""
import re
from urllib.parse import urlparse

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo, normalize_price


class ShopifyJpMixin:

    async def _scrape_shopify_jp(self, url: str) -> ProductInfo:
        """
        先嘗試用 .js API（price 為 JPY cents，除以 100）
        若失敗或價格異常，fallback 到 Playwright
        """
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

        js_url = f"{base_url}/products/{handle}.js"
        print(f"[Shopify] 嘗試 .js API: {js_url}")

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

                js_resp = await client.get(js_url)
                if js_resp.status_code != 200:
                    print(f"[Shopify] .js 失敗 ({js_resp.status_code})，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                js = js_resp.json()
                prod = js.get("product", js) if "product" in js else js

                variants = prod.get("variants", [])
                if not variants:
                    print("[Shopify] .js 無 variants，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                # .js price 是 JPY cents（整數字串，如 "3870000" = ¥38,700）
                raw_str = str(variants[0].get("price", "0"))
                print(f"[Shopify DEBUG] .js raw price = {raw_str!r}")
                raw = normalize_price(raw_str)

                if not raw or raw <= 0:
                    print("[Shopify] price 為空，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                # cents 判斷：JPY 沒有小數，cents 值通常 > 100000
                if raw > 100000:
                    price_jpy = raw // 100
                elif raw > 1000:
                    # 可能已經是日圓（如 38700）
                    price_jpy = raw
                else:
                    # 異常小值（如 387），很可能是 USD，改用 Playwright
                    print(f"[Shopify] price 異常小 ({raw})，疑似外幣，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                product.title = prod.get("title", "")
                product.brand = prod.get("vendor", "")
                product.price_jpy = price_jpy
                product.description = (prod.get("body_html") or "")[:500]
                if product.description:
                    product.description = re.sub(r'<[^>]+>', '', product.description).strip()

                def _img_src(img):
                    return img if isinstance(img, str) else (img.get("src") or "")

                js_images = prod.get("images", [])
                if js_images:
                    product.image_url = _abs(_img_src(js_images[0]))
                    product.extra_images = [_abs(_img_src(i)) for i in js_images[1:5] if _img_src(i)]

                # image_id → src 對應（從 .js featured_image 建立）
                image_id_map = {}
                for img in js_images:
                    iid = None if isinstance(img, str) else img.get("id")
                    src = _img_src(img)
                    if iid and src:
                        image_id_map[iid] = _abs(src)

                options = prod.get("options", [])
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
                        img_src = image_id_map[v_img_id]
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

                print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy}")
                return product

        except Exception as e:
            print(f"[Shopify] .js API 例外: {type(e).__name__}: {e}，改用 Playwright")

        return await self._scrape_with_playwright(url)
