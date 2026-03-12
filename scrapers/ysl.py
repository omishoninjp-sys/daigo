"""
YSL Beauty Japan (yslb.jp) 爬蟲 Mixin
======================================
Salesforce Commerce Cloud (Demandware) 架構。

URL 範例：
  https://www.yslb.jp/makeup/makeup-complexion/makeup-powder/all-hours-blur-cushion/WW-51528YSL.html
"""

import re
import time as _time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo, normalize_price

BASE = "https://www.yslb.jp"


class YSLMixin:

    async def _scrape_ysl(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="YSL")

        html = await self._ysl_fetch_html(url)
        if not html:
            print(f"[YSL] ❌ 無法取得 HTML: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 商品名稱 ──────────────────────────────────
            en_name  = self._ysl_text(soup, "h5.c-product-top__name")
            ja_name  = self._ysl_text(soup, "h1.c-product-main__name")
            subtitle = self._ysl_text(soup, "h2.c-product-main__subtitle")

            if en_name and ja_name:
                product.title = f"{en_name} / {ja_name}"
            else:
                product.title = ja_name or en_name

            # ── 價格（JPY 含稅）────────────────────────────
            price_el = soup.select_one("span[data-js-saleprice]")
            if price_el:
                product.price_jpy = normalize_price(price_el.get_text())

            # ── 說明文字 ────────────────────────────────────
            desc_parts = []
            if subtitle:
                desc_parts.append(subtitle)
            desc_el = soup.select_one("p.c-product-main__short-description")
            if desc_el:
                for a in desc_el.select("a"):
                    a.decompose()
                short = re.sub(r"\s{2,}", " ", desc_el.get_text(separator=" ", strip=True))
                if short:
                    desc_parts.append(short)
            product.description = "\n".join(desc_parts)

            # ── 圖片 ────────────────────────────────────────
            images = self._ysl_extract_images(soup)
            if images:
                product.image_url    = images[0]
                product.extra_images = images[1:]

            # ── 庫存 ────────────────────────────────────────
            product.in_stock = bool(soup.select_one(".c-product-availability.m-in-stock"))

            # ── Variants（顏色）────────────────────────────
            product.variants = self._ysl_extract_variants(soup, product.price_jpy or 0)

            print(
                f"[YSL] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / "
                f"images={1 + len(product.extra_images)}"
            )

        except Exception as e:
            import traceback
            print(f"[YSL] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    # ── HTML 取得 ────────────────────────────────────────

    async def _ysl_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase UC driver 取得 JS 渲染後 HTML。"""
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

                        # 等價格與商品名稱都載入
                        if (i >= 1
                                and "c-product-main__name" in html
                                and "data-js-saleprice" in html
                                and len(html) > 8000):
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 8000:
                        return html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[YSL] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None

    # ── 解析輔助 ─────────────────────────────────────────

    def _ysl_text(self, soup: BeautifulSoup, selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    def _ysl_extract_images(self, soup: BeautifulSoup) -> list[str]:
        """從 carousel img 擷取高畫質圖片 URL。"""
        seen: set[str] = set()
        images: list[str] = []

        for img in soup.select(".c-product-detail-image__main .c-carousel__item img[src]"):
            src = img.get("src", "")
            if "dw/image/v2" not in src:
                continue
            base = src.split("?")[0]
            if base in seen:
                continue
            seen.add(base)
            images.append(f"{base}?sw=800&sfrm=jpg&q=90")
            if len(images) >= 8:
                break

        return images

    def _ysl_extract_variants(self, soup: BeautifulSoup, base_price: int) -> list[dict]:
        """
        從 swatch 擷取所有顏色 variant。
        m-disabled = 缺貨。
        """
        seen: set[str] = set()
        variants: list[dict] = []

        for swatch in soup.select(".c-swatches-grouped__group a[data-js-swatch]"):
            sku   = (swatch.get("data-js-pid")            or "").strip()
            color = (swatch.get("data-js-title")           or "").strip()
            img   = (swatch.get("data-js-productimgsrc")   or "").strip()
            img   = img.split("?")[0] + "?sw=800&sfrm=jpg&q=90" if img else ""

            if not sku or sku in seen:
                continue
            seen.add(sku)

            is_disabled = "m-disabled" in (swatch.get("class") or [])

            variants.append({
                "color":    color,
                "sku":      sku,
                "price":    base_price,
                "in_stock": not is_disabled,
                "image":    img,
            })

        return variants
