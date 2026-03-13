"""
SHEL'TTER WEB STORE (ec-store.net) 爬蟲
平台：ShopServe 系統

確認的 HTML 結構：
- 標題：h1.block-goods-name--text
- 價格：JSON-LD offers.price
- 圖片：img.block-src-1--image（排除 magnifier-large）
- 顏色框：.block-color-size-with-cart--color-frame
- 顏色名：.block-color-size-with-cart--color-item-term-color
- 尺寸行：.block-color-size-with-cart--color-line
- 庫存：.block-color-size-with-cart--size-item-stock（no-stock class = 無庫存）
- 品牌：#size_guide 文字
"""

import re
import json
import time as _time

from bs4 import BeautifulSoup
from scrapers.base import ProductInfo, normalize_price


class EcStoreMixin:

    async def _scrape_ecstore(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        html = self._ecstore_fetch_html(url)
        if not html:
            print(f"[EcStore] ❌ 無法取得 HTML: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            print(f"[EcStore-DEBUG] HTML={len(html)} "
                  f"cf={len(soup.select('.block-color-size-with-cart--color-frame'))} "
                  f"img={len(soup.select('img.block-src-1--image'))} "
                  f"sg={soup.select_one('#size_guide') is not None}", flush=True)

            product.title       = self._ecstore_title(soup)
            product.brand       = self._ecstore_brand(soup)
            product.price_jpy   = self._ecstore_price(soup)
            product.image_url, product.extra_images = self._ecstore_images(soup)
            product.description = self._ecstore_description(soup)
            product.variants    = self._ecstore_variants(soup)
            product.in_stock    = any(v.get("in_stock") for v in product.variants) if product.variants else True

            print(f"[EcStore] ✅ {product.title} / ¥{product.price_jpy} / "
                  f"品牌={product.brand} / 變體={len(product.variants)}", flush=True)

        except Exception as e:
            import traceback
            print(f"[EcStore] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    def _ecstore_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase UC driver 取得 JS 渲染後 HTML"""
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

                        # 等變體區塊和價格載入完成
                        if (i >= 1
                                and "block-color-size-with-cart" in html
                                and "block-goods-name" in html
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
                    print(f"[EcStore] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None

    # ── 標題 ──────────────────────────────────
    def _ecstore_title(self, soup: BeautifulSoup) -> str:
        el = soup.select_one("h1.block-goods-name--text")
        if el:
            return el.get_text(strip=True)
        el = soup.select_one("h1[class*='goods-name']")
        if el:
            return el.get_text(strip=True)
        return ""

    # ── 品牌 ──────────────────────────────────
    def _ecstore_brand(self, soup: BeautifulSoup) -> str:
        # #size_guide 文字格式：「サイズガイドBRAND名ワイドサイズガイド...」
        guide = soup.select_one("#size_guide")
        if guide:
            text = guide.get_text(strip=True)
            m = re.search(r"サイズガイド(.+?)(?:ワイド|サイズガイド|SIZE GUIDE|の注意)", text)
            if m:
                brand = m.group(1).strip()
                if brand and len(brand) < 50:
                    return brand

        # fallback：JSON-LD brand
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(script.string or "")
                brand = d.get("brand", {})
                if isinstance(brand, dict):
                    return brand.get("name", "")
                if isinstance(brand, str):
                    return brand
            except Exception:
                pass

        return ""

    # ── 價格 ──────────────────────────────────
    def _ecstore_price(self, soup: BeautifulSoup) -> int | None:
        # JSON-LD 最可靠
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(script.string or "")
                offers = d.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price")
                    if price:
                        return normalize_price(price)
                elif isinstance(offers, list) and offers:
                    price = offers[0].get("price")
                    if price:
                        return normalize_price(price)
            except Exception:
                pass

        # fallback：.price 含稅
        el = soup.select_one(".price")
        if el:
            text = el.get_text(strip=True)
            m = re.search(r"[¥￥]([0-9,]+)", text)
            if m:
                return normalize_price(m.group(1))

        return None

    # ── 圖片 ──────────────────────────────────
    def _ecstore_images(self, soup: BeautifulSoup) -> tuple[str, list[str]]:
        seen = set()
        images = []

        for img in soup.select("img.block-src-1--image"):
            classes = img.get("class") or []
            if "magnifier-large" in classes or "hidden" in classes:
                continue
            src = img.get("src") or img.get("data-src") or ""
            if src and "lazyloading" not in src and src.startswith("http") and src not in seen:
                seen.add(src)
                images.append(src)

        # fallback：visumo CDN
        if not images:
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if "visumo.io" in src and src not in seen:
                    seen.add(src)
                    images.append(src)

        # /S/ → /L/ 提升解析度
        images = [re.sub(r"/S/", "/L/", s) for s in images]

        main = images[0] if images else ""
        extra = images[1:10]
        return main, extra

    # ── 描述 ──────────────────────────────────
    def _ecstore_description(self, soup: BeautifulSoup) -> str:
        parts = []
        for dl in soup.select("dl.goods-detail-description"):
            text = dl.get_text(" ", strip=True)
            if text:
                parts.append(text)
        return " / ".join(parts) if parts else ""

    # ── 變體（顏色 × 尺寸 × 庫存） ────────────
    def _ecstore_variants(self, soup: BeautifulSoup) -> list[dict]:
        variants = []

        for frame in soup.select(".block-color-size-with-cart--color-frame"):
            color_el = frame.select_one(".block-color-size-with-cart--color-item-term-color")
            color = color_el.get_text(strip=True) if color_el else ""

            for line in frame.select(".block-color-size-with-cart--color-line"):
                size_el  = line.select_one(".block-color-size-with-cart--size-item-size")
                stock_el = line.select_one(".block-color-size-with-cart--size-item-stock")

                size = size_el.get_text(strip=True) if size_el else ""
                if not size:
                    continue

                in_stock = True
                if stock_el:
                    classes = stock_el.get("class") or []
                    stock_text = stock_el.get_text(strip=True)
                    if "no-stock" in classes or "在庫なし" in stock_text:
                        in_stock = False

                variants.append({
                    "color":    color,
                    "size":     size,
                    "in_stock": in_stock,
                })

        # fallback：#size_color_select
        if not variants:
            select = soup.select_one("#size_color_select")
            if select:
                for opt in select.find_all("option"):
                    val = opt.get("value", "").strip()
                    text = opt.get_text(strip=True)
                    if not val or val == "0":
                        continue
                    parts = [p.strip() for p in text.split("/")]
                    color = parts[0] if len(parts) >= 1 else ""
                    size  = parts[-1] if len(parts) >= 2 else ""
                    variants.append({"color": color, "size": size, "in_stock": True})

        return variants
