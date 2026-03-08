"""
WTAPS 爬蟲 Mixin
wtaps.com 用 Shopify，但：
1. 價格是 JPY * 100（6930000 = ¥69,300）
2. 庫存從 input class="disabled" 判斷
3. 需要 SeleniumBase 渲染 JS
"""
import re
import json
import time as _time

from scrapers.base import ProductInfo


class WtapsMixin:

    async def _scrape_wtaps(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            html = await self._wtaps_fetch_html(url)
            if not html:
                print(f"[WTAPS] ❌ 無法取得 HTML")
                return product

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題 ──────────────────────────────────────
            h1 = soup.find("h1", class_=re.compile(r'product.*title|title.*product', re.I))
            if not h1:
                h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)

            # ── 品牌 ──────────────────────────────────────
            product.brand = "WTAPS"

            # ── 價格（從 data-selected-variant JSON 取，除以100）──
            variant_script = soup.find("script", attrs={"data-selected-variant": True})
            if variant_script and variant_script.string:
                try:
                    vdata = json.loads(variant_script.string.strip())
                    raw_price = vdata.get("price", 0)
                    if raw_price:
                        product.price_jpy = int(raw_price) // 100
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

            # fallback：從頁面文字抓 ¥xx,xxx
            if not product.price_jpy:
                m = re.search(r'¥([\d,]+)', html)
                if m:
                    try:
                        product.price_jpy = int(m.group(1).replace(',', ''))
                    except ValueError:
                        pass

            # ── 圖片 ──────────────────────────────────────
            imgs = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if not src.startswith("http"):
                    src = "https:" + src if src.startswith("//") else src
                if ("wtaps.com" in src or "cdn.shopify.com" in src) and any(
                    ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]
                ):
                    src = re.sub(r'\?.*$', '', src)  # 去 query string
                    if src not in imgs:
                        imgs.append(src)
            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:5]

            # ── 庫存：從 input disabled class 判斷 ───────
            # disabled = 賣完，無 disabled = 有庫存
            variant_selects = soup.find("variant-selects") or soup.find(id=re.compile(r'variant-selects'))

            colors, sizes = [], []
            color_disabled, size_disabled = {}, {}

            if variant_selects:
                for fieldset in variant_selects.find_all("fieldset"):
                    opt_name = (fieldset.get("data-option-name", "") or "").upper()
                    for inp in fieldset.find_all("input", type="radio"):
                        val = inp.get("value", "").strip()
                        if not val:
                            continue
                        is_disabled = "disabled" in (inp.get("class") or [])
                        if "COLOR" in opt_name or "COLOUR" in opt_name or "カラー" in opt_name:
                            if val not in colors:
                                colors.append(val)
                            color_disabled[val] = is_disabled
                        elif "SIZE" in opt_name or "サイズ" in opt_name:
                            if val not in sizes:
                                sizes.append(val)
                            size_disabled[val] = is_disabled

            # ── 組合 variants ─────────────────────────────
            # 圖片：從各顏色 input 的 data-image 取
            color_image = {}
            if variant_selects:
                for inp in variant_selects.find_all("input", type="radio"):
                    val = inp.get("value", "").strip()
                    img_src = inp.get("data-image", "")
                    if img_src and val and val not in color_image:
                        if img_src.startswith("//"):
                            img_src = "https:" + img_src
                        color_image[val] = img_src

            if colors or sizes:
                if not colors: colors = [""]
                if not sizes:  sizes  = [""]

                for color in colors:
                    for size in sizes:
                        # 有任一方 disabled = 無庫存（保守判斷）
                        c_out = color_disabled.get(color, True)
                        s_out = size_disabled.get(size, True)
                        in_stock = not c_out and not s_out

                        product.variants.append({
                            "color": color,
                            "size": size,
                            "sku": f"wtaps-{color}-{size}".lower().replace(" ", "-"),
                            "price": product.price_jpy or 0,
                            "in_stock": in_stock,
                            "image": color_image.get(color, product.image_url),
                        })

            print(f"[WTAPS] ✅ {product.title} / ¥{product.price_jpy} / {len(product.variants)} variants")

        except Exception as e:
            import traceback
            print(f"[WTAPS] ❌ {type(e).__name__}: {e}")
            traceback.print_exc()

        return product

    async def _wtaps_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase Chrome driver 取得 JS 渲染後的 HTML"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1
                    self._clean_driver_tabs()

                    try:
                        driver.uc_open_with_reconnect(url, reconnect_time=6)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__ or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    html = ""
                    session_dead = False
                    for i in range(8):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        if i >= 1 and 'variant-selects' in html and len(html) > 5000:
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 5000:
                        return html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None
        return None
