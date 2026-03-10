"""
Shopify 日本商店爬蟲 Mixin
資料來源：HTML 內嵌 <script type="application/ld+json">
- price: 已是日圓字串（"29700"），priceCurrency 確認為 JPY
- availability: InStock / OutOfStock
- image: 直接完整 URL
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

                # ── 抓 JSON-LD（含完整 price JPY、availability、image）
                # 抓所有 ld+json script，找 @type=ProductGroup 的那個
                all_ld_scripts = re.findall(
                    r'<script\s+type="application/ld\+json">(.*?)</script>',
                    html, re.DOTALL
                )
                ld = None
                for s in all_ld_scripts:
                    try:
                        obj = json.loads(s.strip())
                        if obj.get("@type") == "ProductGroup":
                            ld = obj
                            break
                    except Exception:
                        pass

                if not ld:
                    print("[Shopify] 找不到 JSON-LD ProductGroup，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                product.title = ld.get("name", "")
                brand_obj = ld.get("brand", {})
                product.brand = brand_obj.get("name", "") if isinstance(brand_obj, dict) else ""
                product.description = ld.get("description", "")[:500]

                variants_ld = ld.get("hasVariant", [])
                if not variants_ld:
                    print("[Shopify] JSON-LD 無 hasVariant，改用 Playwright")
                    return await self._scrape_with_playwright(url)

                # 第一個 variant 的價格（已是 JPY 字串，直接轉 int）
                first_offers = variants_ld[0].get("offers", {})
                price_str = first_offers.get("price", "0")
                currency = first_offers.get("priceCurrency", "JPY")
                product.price_jpy = int(float(price_str))
                print(f"[Shopify DEBUG] price={price_str!r} {currency} → ¥{product.price_jpy}")

                # 第一張圖片
                first_img = variants_ld[0].get("image", "")
                if first_img:
                    # 移除 width 參數，取原圖
                    product.image_url = first_img.split("&width=")[0].split("?width=")[0]

                # 額外圖片：從 modal img 標籤抓（避免重複）
                modal_imgs = re.findall(
                    r'<img[^>]+class="global-media-settings[^"]*"[^>]+src="([^"?]+)',
                    html
                )
                seen = {product.image_url}
                for src in modal_imgs:
                    s = "https:" + src if src.startswith("//") else src
                    if s and s not in seen and len(product.extra_images) < 4:
                        seen.add(s)
                        product.extra_images.append(s)

                # Variants
                for v in variants_ld:
                    offers = v.get("offers", {})
                    avail = offers.get("availability", "")
                    in_stock = "InStock" in avail

                    # name 格式：「PRODUCT NAME - option1 / option2」
                    full_name = v.get("name", "")
                    options_part = full_name.split(" - ", 1)[-1] if " - " in full_name else ""
                    parts = [p.strip() for p in options_part.split("/")] if options_part else []

                    vinfo = {
                        "color": "",
                        "size": "",
                        "in_stock": in_stock,
                        "image": v.get("image", "").split("&width=")[0].split("?width=")[0],
                        "sku": v.get("sku", ""),
                    }

                    # 判斷 size vs color
                    for p in parts:
                        p = p.strip()
                        if re.match(r'^[0-9XSMLxsml]+$', p):
                            vinfo["size"] = p
                        elif not vinfo["color"]:
                            vinfo["color"] = p
                        else:
                            vinfo["size"] = p

                    product.variants.append(vinfo)

                print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy} ({len(product.variants)} variants)")
                return product

        except Exception as e:
            print(f"[Shopify] 例外: {type(e).__name__}: {e}，改用 Playwright")

        return await self._scrape_with_playwright(url)
