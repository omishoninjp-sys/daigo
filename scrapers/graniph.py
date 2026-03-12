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
        """
        graniph の実際の DOM 構造：
        <div class="p-products-detail-option-size__list item-detail-cart">
            <label class="few " for="02">       ← few=残りわずか, soldout=在庫なし
                <span translate="no">S</span>
                <span class="...">残り1点</span>
            </label>
            <label class="soldout " for="03">
                <span translate="no">M</span>
                <span class="...">在庫なし</span>
            </label>
        """
        color = ""
        price = product.price_jpy or 0
        variants = []
        seen = set()

        # ターゲットコンテナを直接指定（class 名で完全一致）
        container = soup.find(class_=re.compile(r'p-products-detail-option-size__list'))
        if not container:
            print(f"[Graniph] ⚠️ 找不到尺寸容器")
            return variants

        for label in container.find_all("label"):
            # サイズ名: translate="no" の span から取得
            size_span = label.find("span", attrs={"translate": "no"})
            if not size_span:
                continue
            size_txt = size_span.get_text(strip=True)
            if not size_txt or size_txt in seen:
                continue

            # 在庫判定: label の class に "soldout" が含まれるか
            label_classes = " ".join(label.get("class") or []).lower()
            in_stock = "soldout" not in label_classes

            seen.add(size_txt)
            variants.append({
                "color": color,
                "size": size_txt,
                "price": price,
                "in_stock": in_stock,
            })

        if variants:
            product.in_stock = any(v["in_stock"] for v in variants)
            out_str = ", ".join(
                f"{v['size']}({'○' if v['in_stock'] else '✕'})" for v in variants
            )
            print(f"[Graniph] 尺寸: {out_str}")
        else:
            print(f"[Graniph] ⚠️ 找不到尺寸（container 存在但無 label）")

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

            # 等待 JS 渲染：price + title + size 全部出現才算 ready（最多 20s）
            for i in range(10):
                _time.sleep(2)
                html = driver.page_source or ""
                has_price = bool(re.search(r'[¥￥]\s*[\d,]{4,}', html))
                has_title = "<h1" in html
                has_size = bool(re.search(
                    r'(?:class=["\'][^"\']*size[^"\']*["\']|>(?:S|M|L|XL|SS|XS|FREE)<)',
                    html, re.I
                ))
                if i == 1:
                    try:
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 3);")
                    except Exception:
                        pass
                # 最低 3 回（6 秒）は待つ。その後 price + title + size が揃えば終了
                if i >= 2 and has_price and has_title and has_size:
                    print(f"[Graniph] HTML ready (i={i})")
                    break
                if i == 9:
                    print(f"[Graniph] HTML ready (timeout, i={i}) price={has_price} title={has_title} size={has_size}")

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
