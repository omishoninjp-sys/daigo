"""
Shopify 日本商店爬蟲 Mixin
價格策略（依序嘗試）：
  1. Cookie localization=JP + Accept-Language: ja
  2. URL 加 ?currency=JPY
  兩種方式都加 DEBUG log 顯示實際抓到的 raw price
"""
import json
import re
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, urljoin

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


def _extract_price_from_html(html: str) -> tuple[int, str]:
    """從 data-selected-variant 取 price cents，回傳 (cents, raw_str)"""
    sv_match = re.search(r'data-selected-variant[^>]*>(\{[^<]+\})', html)
    if not sv_match:
        return 0, "NOT_FOUND"
    try:
        sv = json.loads(sv_match.group(1))
        cents = sv.get("price", 0)
        return cents, str(cents)
    except Exception as e:
        return 0, f"PARSE_ERROR:{e}"


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

        # ── 方法1：Cookie localization=JP
        headers_with_cookie = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Cookie": "localization=JP; cart_currency=JPY",
        }

        # ── 方法2：URL + currency=JPY
        url_with_currency = f"{url}{'&' if '?' in url else '?'}currency=JPY"

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:

                # ══ 方法1：Cookie ══
                print(f"[Shopify] 方法1 Cookie(localization=JP): {url}")
                r1 = await client.get(url, headers=headers_with_cookie)
                cents1, raw1 = _extract_price_from_html(r1.text)
                price1 = cents1 // 100 if cents1 > 10000 else cents1
                print(f"[Shopify DEBUG] 方法1 raw={raw1!r} → ¥{price1}")

                # ══ 方法2：?currency=JPY ══
                print(f"[Shopify] 方法2 ?currency=JPY: {url_with_currency}")
                r2 = await client.get(url_with_currency, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ja-JP,ja;q=0.9",
                })
                cents2, raw2 = _extract_price_from_html(r2.text)
                price2 = cents2 // 100 if cents2 > 10000 else cents2
                print(f"[Shopify DEBUG] 方法2 raw={raw2!r} → ¥{price2}")

                # ══ 決定使用哪個 ══
                # 優先選較大的（SGD 會比 JPY 小很多）
                if price1 > price2:
                    product.price_jpy = price1
                    print(f"[Shopify DEBUG] 採用方法1 ¥{price1}")
                    html = r1.text
                elif price2 > price1:
                    product.price_jpy = price2
                    print(f"[Shopify DEBUG] 採用方法2 ¥{price2}")
                    html = r2.text
                else:
                    product.price_jpy = price1
                    print(f"[Shopify DEBUG] 兩方法相同 ¥{price1}")
                    html = r1.text

                # ── .json 取圖片/選項/商品資訊
                images, options, variants_json = [], [], []
                try:
                    jr = await client.get(json_url, headers={
                        "Accept": "application/json",
                        "Cookie": "localization=JP; cart_currency=JPY",
                    })
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

                if not product.title:
                    t = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
                    product.title = t.group(1).strip() if t else ""

                # ── .js 取庫存
                available_map = {}
                try:
                    jsr = await client.get(js_url, headers={"Accept": "application/json"})
                    if jsr.status_code == 200:
                        for v in jsr.json().get("variants", []):
                            available_map[v["id"]] = v.get("available", False)
                except Exception:
                    pass

                # ── 建立 variants
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
