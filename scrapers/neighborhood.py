"""
NEIGHBORHOOD 爬蟲 Mixin
neighborhood.jp 需要 Playwright（JS 渲染），庫存從 qua JSON 讀取
"""
import re
import json

from scrapers.base import ProductInfo


class NeighborhoodMixin:

    async def _scrape_neighborhood(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            from playwright.async_api import async_playwright
            import asyncio

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    locale="ja-JP",
                )
                page = await ctx.new_page()

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # 等待價格元素出現
                    try:
                        await page.wait_for_selector(".product-price", timeout=10000)
                    except Exception:
                        pass
                    html = await page.content()
                finally:
                    await browser.close()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題 ──────────────────────────────────────
            h1 = soup.find("h1", class_="product-detail-inner-title")
            if h1:
                product.title = h1.get_text(strip=True)

            # ── 品牌 ──────────────────────────────────────
            vendor = soup.find("p", class_="product-detail-inner-vendor")
            product.brand = vendor.get_text(strip=True) if vendor else "NEIGHBORHOOD"

            # ── 價格 ──────────────────────────────────────
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
                    src = re.sub(r'_\d+x\d*(\.\w+)$', r'\1', src)
                    if src not in imgs:
                        imgs.append(src)
            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:5]

            # ── 庫存 JSON（qua 欄位）─────────────────────
            stock_map = {}
            for script in soup.find_all("script", type="application/json"):
                raw = (script.string or "").strip()
                if not raw.startswith("["):
                    continue
                try:
                    items = json.loads(raw)
                    if isinstance(items, list) and items and "qua" in items[0]:
                        for item in items:
                            name = item.get("name", "")
                            try:
                                stock_map[name] = int(item.get("qua", "0")) > 0
                            except (ValueError, TypeError):
                                stock_map[name] = False
                        break
                except (json.JSONDecodeError, KeyError):
                    continue

            # ── Colors / Sizes ────────────────────────────
            colors, sizes = [], []

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
                if not colors: colors = [""]
                if not sizes:  sizes  = [""]

                for color in colors:
                    for size in sizes:
                        key = f"{color} {size}".strip()
                        in_stock = stock_map.get(key, False)
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
