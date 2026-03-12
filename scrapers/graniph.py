"""
graniph (graniph.com) 爬蟲 Mixin
graniph 為 SPA，需要 UC driver 等待 JS 渲染。
圖片可由 item code 直接構造，price/size 從渲染後 HTML 抓取。
"""
import re
import time as _time
import json

from scrapers.base import ProductInfo


class GraniphMixin:

    async def _scrape_graniph(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="graniph")

        # 從 URL 取得 item code
        # e.g. https://www.graniph.com/item-detail/035001317102
        m = re.search(r'/item-detail/(\d+)', url)
        item_code = m.group(1) if m else None

        html = await self._graniph_fetch_html(url)
        if not html:
            print(f"[Graniph] ❌ 無法取得 HTML: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 優先：JSON-LD ===
            self._graniph_parse_jsonld(soup, product)

            # === 優先：__NEXT_DATA__ / 內嵌 JSON ===
            if not product.title or not product.price_jpy:
                self._graniph_parse_script_json(soup, product)

            # === Fallback：HTML selector ===
            if not product.title:
                for sel in [
                    ("h1", {}),
                    ("h1", {"class": re.compile(r"product|item|title", re.I)}),
                    ("div", {"class": re.compile(r"product.?name|item.?name", re.I)}),
                ]:
                    el = soup.find(sel[0], sel[1])
                    if el and el.get_text(strip=True):
                        product.title = el.get_text(strip=True)
                        break

            if not product.price_jpy:
                # 掃全部文字找 ¥XXXX
                for text in soup.stripped_strings:
                    m_price = re.match(r'^[¥￥]\s*([1-9][\d,]{3,})$', text.strip())
                    if m_price:
                        val = int(m_price.group(1).replace(',', ''))
                        if 1000 <= val <= 500000:
                            product.price_jpy = val
                            break

            # === 尺寸 ===
            variants = self._graniph_parse_sizes(soup, product)
            if variants:
                product.variants = variants

            # === 圖片：從 item code 構造 ===
            if item_code:
                imgs = self._graniph_build_image_urls(item_code, soup)
                if imgs:
                    product.image_url = imgs[0]
                    product.extra_images = imgs[1:]

            # image_url fallback：從 soup 抓
            if not product.image_url:
                for img in soup.find_all("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if "cf.graniph.com" in src and "null.gif" not in src:
                        product.image_url = src
                        break

            print(f"[Graniph] ✅ {product.title} / ¥{product.price_jpy} / "
                  f"{len(product.variants)} variants / images={1 + len(product.extra_images)}")

        except Exception as e:
            import traceback
            print(f"[Graniph] ❌ parse error: {e}")
            traceback.print_exc()

        return product

    # ── JSON-LD ──────────────────────────────────────────────
    def _graniph_parse_jsonld(self, soup, product: ProductInfo):
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") == "Product"), {})
                if data.get("@type") != "Product":
                    continue
                if not product.title:
                    product.title = data.get("name", "")
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if not product.price_jpy:
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        product.price_jpy = int(float(str(price).replace(",", "")))
                if not product.image_url:
                    img = data.get("image")
                    if isinstance(img, list):
                        img = img[0]
                    product.image_url = img or ""
                return
            except Exception:
                continue

    # ── Script JSON（__NEXT_DATA__ 等）───────────────────────
    def _graniph_parse_script_json(self, soup, product: ProductInfo):
        for tag in soup.find_all("script"):
            text = tag.string or ""
            # graniph SPA 可能把商品資料放在 window.__INIT_DATA__ 等
            for pattern in [
                r'window\.__INIT_DATA__\s*=\s*(\{.*?\});',
                r'"itemName"\s*:\s*"([^"]+)"',
            ]:
                m = re.search(pattern, text, re.S)
                if m:
                    try:
                        if pattern.startswith(r'"itemName"'):
                            if not product.title:
                                product.title = m.group(1)
                        else:
                            data = json.loads(m.group(1))
                            if not product.title:
                                product.title = (
                                    data.get("itemName") or
                                    data.get("name") or
                                    data.get("title") or ""
                                )
                            if not product.price_jpy:
                                price = (
                                    data.get("price") or
                                    data.get("priceWithTax") or
                                    data.get("sellingPrice")
                                )
                                if price:
                                    product.price_jpy = int(float(str(price).replace(",", "")))
                    except Exception:
                        pass

            # 直接搜 itemName / price 字串（寬鬆 fallback）
            if not product.title:
                m_name = re.search(r'"itemName"\s*:\s*"([^"]+)"', text)
                if m_name:
                    product.title = m_name.group(1)
            if not product.price_jpy:
                m_price = re.search(r'"(?:priceWithTax|sellingPrice|price)"\s*:\s*(\d+)', text)
                if m_price:
                    val = int(m_price.group(1))
                    if 1000 <= val <= 500000:
                        product.price_jpy = val

    # ── 尺寸解析 ─────────────────────────────────────────────
    def _graniph_parse_sizes(self, soup, product: ProductInfo) -> list:
        color = ""  # graniph 通常單色
        price = product.price_jpy or 0
        variants = []
        seen = set()

        size_pat = re.compile(
            r'^(XXS|XS|SS|S|M|L|XL|2XL|3XL|4XL|LL|3L|4L|ONE\s*SIZE|FREE|OS|\d{2,3}(?:cm)?)$',
            re.IGNORECASE
        )

        # 找 size 相關容器
        size_containers = (
            soup.find_all(class_=re.compile(r'size', re.I)) +
            soup.find_all(attrs={"data-attr": "size"}) +
            soup.find_all("ul", class_=re.compile(r'size|variant', re.I))
        )

        for container in size_containers:
            for el in container.find_all(["button", "label", "li", "span", "a"]):
                txt = el.get_text(strip=True).upper()
                if size_pat.match(txt) and txt not in seen:
                    seen.add(txt)
                    # 判斷是否售完：class 含 sold-out / disabled / unavailable
                    cls = " ".join(el.get("class") or []).lower()
                    disabled = el.get("disabled") is not None
                    in_stock = not disabled and not any(
                        kw in cls for kw in ["sold-out", "soldout", "disabled", "unavailable", "out-of-stock"]
                    )
                    variants.append({
                        "color": color,
                        "size": txt,
                        "price": price,
                        "in_stock": in_stock,
                    })

        # 若找到 variants 則商品整體庫存 = 任一有貨
        if variants:
            product.in_stock = any(v["in_stock"] for v in variants)

        return variants

    # ── 圖片 URL 構造 ─────────────────────────────────────────
    def _graniph_build_image_urls(self, item_code: str, soup) -> list:
        """
        graniph 圖片規律：
        item_code = 035001317102
        → prefix = 035001317.102.-
        → https://cf.graniph.com/images/item/product_image/035001317.102.-_N.jpg

        先從 soup 的 <a href> 確認實際有幾張，再構造清單。
        """
        if len(item_code) < 12:
            return []

        prefix = f"{item_code[:9]}.{item_code[9:12]}.-"
        base = f"https://cf.graniph.com/images/item/product_image/{prefix}"

        # 從 soup 的 href 抓到的圖片編號最大值
        max_n = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(re.escape(prefix) + r'_(\d+)\.jpg', href)
            if m:
                max_n = max(max_n, int(m.group(1)))

        # 至少嘗試 10 張
        max_n = max(max_n, 10)

        imgs = [f"{base}_{i}.jpg" for i in range(1, max_n + 1)]
        return imgs

    # ── UC Driver 取 HTML ─────────────────────────────────────
    async def _graniph_fetch_html(self, url: str) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._graniph_fetch_html_sync, url)

    def _graniph_fetch_html_sync(self, url: str) -> str:
        async_lock = self._driver_lock
        import threading
        lock = threading.Lock()

        driver = None
        try:
            driver = self._create_driver()
            driver.uc_open_with_reconnect(url, reconnect_time=6)

            # 等待 JS 渲染：price 出現為止（最多 20s）
            for i in range(10):
                _time.sleep(2)
                html = driver.page_source or ""
                has_price = bool(re.search(r'[¥￥]\s*[\d,]{4,}', html))
                has_title = "<h1" in html
                # 第一次 scroll 觸發 lazy render
                if i == 1:
                    try:
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 3);")
                    except Exception:
                        pass
                if has_price and has_title:
                    print(f"[Graniph] HTML ready (i={i})")
                    break

            return driver.page_source or ""
        except Exception as e:
            print(f"[Graniph] driver error: {e}")
            return ""
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
