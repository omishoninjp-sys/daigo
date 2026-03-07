"""
NEIGHBORHOOD 爬蟲 Mixin
neighborhood.jp 使用自訂庫存 JSON（qua 欄位），不能用 Shopify available
"""
import re
import json

import httpx
from bs4 import BeautifulSoup

from config import USER_AGENT
from scrapers.base import ProductInfo


class NeighborhoodMixin:

    async def _scrape_neighborhood(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.neighborhood.jp/",
            }

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    print(f"[NEIGHBORHOOD] HTTP {resp.status_code}")
                    return product
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # ── 標題 ──────────────────────────────────────
            h1 = soup.find("h1", class_="product-detail-inner-title")
            if h1:
                product.title = h1.get_text(strip=True)

            # ── 品牌 ──────────────────────────────────────
            vendor = soup.find("p", class_="product-detail-inner-vendor")
            product.brand = vendor.get_text(strip=True) if vendor else "NEIGHBORHOOD"

            # ── 價格 ──────────────────────────────────────
            # <span class="product-price ...">¥12,100</span>
            price_el = soup.find("span", class_=re.compile(r'product-price'))
            if price_el:
                price_text = price_el.get_text(strip=True)
                m = re.search(r'[\d,]+', price_text.replace('¥', '').replace('￥', ''))
                if m:
                    try:
                        product.price_jpy = int(m.group(0).replace(',', ''))
                    except ValueError:
                        pass

            # ── 圖片 ──────────────────────────────────────
            imgs = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if "cdn.shopify.com" in src and "/products/" in src:
                    # 升至最大尺寸
                    src = re.sub(r'_\d+x\d*(\.\w+)$', r'\1', src)
                    if src not in imgs:
                        imgs.append(src)
            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:5]

            # ── 庫存 JSON（qua 欄位）─────────────────────
            # <script type="application/json">
            # [{"qua": "1", "name": "OLIVE DRAB XS"}, ...]
            # </script>
            stock_map = {}  # "COLOR SIZE" -> bool
            for script in soup.find_all("script", type="application/json"):
                raw = script.string or ""
                raw = raw.strip()
                if not raw.startswith("["):
                    continue
                try:
                    items = json.loads(raw)
                    if isinstance(items, list) and items and "qua" in items[0]:
                        for item in items:
                            name = item.get("name", "")
                            qua = item.get("qua", "0")
                            try:
                                stock_map[name] = int(qua) > 0
                            except (ValueError, TypeError):
                                stock_map[name] = False
                        break
                except (json.JSONDecodeError, KeyError):
                    continue

            # ── Colors / Sizes ────────────────────────────
            colors = []
            sizes = []

            color_div = soup.find("div", id="colorOptions")
            if color_div:
                for inp in color_div.find_all("input", type="radio"):
                    val = inp.get("value", "").strip()
                    if val and val not in colors:
                        colors.append(val)

            size_div = soup.find("div", id="sizeOptions")
            if size_div:
                for inp in size_div.find_all("input", type="radio"):
                    val = inp.get("value", "").strip()
                    if val and val not in sizes:
                        sizes.append(val)

            # ── 組合 variants ─────────────────────────────
            if colors or sizes:
                if not colors:
                    colors = [""]
                if not sizes:
                    sizes = [""]

                for color in colors:
                    for size in sizes:
                        key = f"{color} {size}".strip()
                        if stock_map:
                            in_stock = stock_map.get(key, False)
                        else:
                            # 沒有庫存 JSON 時，從 soldout class 判斷
                            # 保守預設 False
                            in_stock = False

                        product.variants.append({
                            "color": color,
                            "size": size,
                            "sku": f"nh-{color}-{size}".lower().replace(" ", "-"),
                            "price": product.price_jpy or 0,
                            "in_stock": in_stock,
                            "image": product.image_url,
                        })

            print(f"[NEIGHBORHOOD] ✅ {product.title} / ¥{product.price_jpy} / {len(product.variants)} variants")

        except Exception as e:
            print(f"[NEIGHBORHOOD] ❌ {type(e).__name__}: {e}")

        return product
