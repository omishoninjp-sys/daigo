"""
Bellemaison (ベルメゾンネット) 爬蟲 Mixin
URL 格式：https://www.bellemaison.jp/shop/commodity/0000/{id}

HTML 結構：
- 價格/variants：div.standard-info[data-price, data-stock-status, data-standard-detail1(size), data-standard-detail2(color)]
- 圖片：og:image / pic2.bellemaison.jp
- 標題：h1 / og:title
"""

import re
import time as _time
from bs4 import BeautifulSoup
from scrapers.base import ProductInfo, normalize_price


class BelleMaisonMixin:

    async def _scrape_bellemaison(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        html = self._bellemaison_fetch(url)
        if not html or len(html) < 5000:
            print(f"[BelleMaison] ❌ HTML 太短或空白: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # 標題
            h1 = soup.select_one("h1.product-name, h1.commodity-name, h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                og = soup.find("meta", property="og:title")
                if og:
                    product.title = og.get("content", "")

            # 品牌（bellemaison 自有品牌，從 breadcrumb 抓）
            brand_el = soup.select_one(".breadcrumb li:nth-child(2) a, .breadcrumb-list li:nth-child(2) a")
            if brand_el:
                product.brand = brand_el.get_text(strip=True)

            # 圖片
            og_img = soup.find("meta", property="og:image")
            if og_img:
                product.image_url = og_img.get("content", "")
            if not product.image_url:
                main_img = soup.select_one("img.main-img, img.product-img, .product-img-wrapper img")
                if main_img:
                    product.image_url = main_img.get("src", "")

            # 額外圖片
            seen = {product.image_url}
            for img in soup.select("img[src*='bellemaison.jp/shop/cms/images']"):
                src = img.get("src", "")
                if src and src not in seen and "Resize" not in src:
                    seen.add(src)
                    product.extra_images.append(src)
                    if len(product.extra_images) >= 8:
                        break

            # Variants：從 .standard-info data-* 屬性抓
            standard_divs = soup.select("div.standard-info")
            variants = []
            prices = []

            for div in standard_divs:
                price_str = div.get("data-price", "").replace(",", "").strip()
                stock_status = div.get("data-stock-status", "")
                size = div.get("data-standard-detail1", "").strip()
                color = div.get("data-standard-detail2", "").strip()
                in_stock = stock_status == "在庫あり"

                price = normalize_price(price_str) if price_str else None
                if price and 100 <= price <= 2000000:
                    prices.append(price)

                if size or color:
                    variants.append({
                        "color": color,
                        "size": size,
                        "price": price,
                        "in_stock": in_stock,
                    })

            if variants:
                product.variants = variants
                product.price_jpy = min(prices) if prices else None
                product.in_stock = any(v["in_stock"] for v in variants)
            else:
                # fallback：從頁面抓最低價
                if prices:
                    product.price_jpy = min(prices)
                else:
                    price_el = soup.select_one("#selectedSkuPrice, .product-price_amount")
                    if price_el:
                        product.price_jpy = normalize_price(
                            re.sub(r"[^\d]", "", price_el.get_text())
                        )

            print(
                f"[BelleMaison] ✅ {product.title[:40]} / ¥{product.price_jpy} / "
                f"variants={len(product.variants)}"
            )

        except Exception as e:
            import traceback
            print(f"[BelleMaison] ❌ 解析失敗: {e}")
            print(traceback.format_exc())

        return product

    def _bellemaison_fetch(self, url: str) -> str:
        """Selenium UC driver 抓取"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return ""
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
                    for i in range(8):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception:
                            break
                        if (i >= 1
                                and "standard-info" in html
                                and len(html) > 10000):
                            return html
                    return html
                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[BelleMaison] fetch 失敗: {e}")
                    return ""
        return ""
