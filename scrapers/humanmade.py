"""
Human Made (humanmade.jp) 爬蟲 Mixin

平台：SFCC (Salesforce Commerce Cloud)
（之前以為改 Shopify，實際看了 HTML 才確認還是 SFCC）

實際 HTML 結構（2026-04 確認）：
- 標題: <h1 class="product-name h4 ls-custom">AIR FORCE 1 '01 / LO2</h1>
- 價格: JSON-LD offers.price = "22550" 最準
       fallback: <span class="value" content="22550"></span>
       fallback: <span class="sales">內的 ¥22,550 文字
- 顏色: div[data-attr="color"] > button.attribute-item--color
        > span[data-attr-value="NAVY"] (顏色名稱)
        > style="background-image: url(...)"  (顏色 swatch 圖片)
- 尺寸: button.attribute-item--size[data-attr-value="23.5cm"]
        button.selectable = 有貨；無 .selectable class = 缺貨
- 圖片: .primary-images .swiper-slide img[data-zoom]
- gtm-data: <input type="hidden" id="gtm-data" value="{...JSON...}"> 含完整 product JSON
"""
import html as html_lib
import json
import re
import time as _time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_VALID_PRICE = 800
_MAX_VALID_PRICE = 5_000_000

# SFCC 圖片 CDN base
_SFCC_IMAGE_BASE = "https://www.humanmade.jp"


class HumanMadeMixin:

    async def _scrape_humanmade(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Human Made")

        # 去語系前綴
        url = re.sub(r'humanmade\.jp/(?:en|zh-CHT|zh-CN|ko)/', 'humanmade.jp/', url, flags=re.IGNORECASE)

        html = await self._humanmade_fetch_html(url)
        if not html:
            print(f"[HumanMade] ❌ 無法取得 HTML: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題（h1.product-name 最準）──
            self._humanmade_extract_title(soup, product)

            # ── 價格（JSON-LD > span[content] > 文字 ¥）──
            self._humanmade_extract_price(soup, html, product)

            # ── 圖片 ──
            self._humanmade_extract_images(soup, product)

            # ── 顏色 ──
            colors, color_image_map = self._humanmade_extract_colors(soup)

            # ── 尺寸（含庫存）──
            sizes_with_stock = self._humanmade_extract_sizes(soup)

            # ── 描述（從 JSON-LD 或 .product-description）──
            self._humanmade_extract_description(soup, html, product)

            # ── 從 gtm-data 補全資料（pid, sku 等）──
            gtm_data = self._humanmade_parse_gtm_data(soup)
            pid = ""
            if gtm_data:
                p_info = (gtm_data.get("product") or {}) if isinstance(gtm_data.get("product"), dict) else {}
                pid = p_info.get("id", "") or gtm_data.get("productID", "") or ""
                if not product.title and p_info.get("name"):
                    product.title = p_info["name"]
                # gtm price 是 number，可作為 cross-check
                if not product.price_jpy and p_info.get("price"):
                    v = self._humanmade_price_to_int(p_info["price"])
                    if v:
                        product.price_jpy = v
                        print(f"[HumanMade] price from gtm-data: {v}")

            # ── 組 variants（color × size 矩陣）──
            base_handle = pid or "hm"
            variants = []
            if not colors:
                colors = [""]
            if not sizes_with_stock:
                sizes_with_stock = [("", True)]

            for color in colors:
                color_img = color_image_map.get(color) or product.image_url
                for size, in_stock in sizes_with_stock:
                    label_parts = [p for p in [color, size] if p]
                    sku = f"{base_handle}-{'-'.join(label_parts)}".lower().replace(" ", "-").replace(".", "-") if label_parts else f"{base_handle}".lower()
                    variants.append({
                        "color": color,
                        "size": size,
                        "sku": sku,
                        "price": product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image": color_img or "",
                    })

            # 過濾「無 color 無 size」的空 variant（如果同時有 colors 又有 sizes 就不會發生）
            if len(variants) == 1 and not variants[0]["color"] and not variants[0]["size"]:
                product.variants = []
            else:
                product.variants = variants

            product.in_stock = (
                any(v["in_stock"] for v in product.variants)
                if product.variants
                else True
            )

            print(
                f"[HumanMade] {'✅' if product.is_valid else '⚠️'} {product.title} / "
                f"¥{product.price_jpy} / colors={colors} / "
                f"sizes={[s for s, _ in sizes_with_stock]} / variants={len(product.variants)}"
            )

        except Exception as e:
            print(f"[HumanMade] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # 標題
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_extract_title(soup: BeautifulSoup, product: ProductInfo) -> None:
        # 優先 h1.product-name
        h1 = soup.find("h1", class_=re.compile(r'product-name'))
        if h1:
            text = h1.get_text(strip=True)
            if text:
                product.title = text
                return

        # 普通 h1
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text and "HUMAN MADE Inc" not in text:
                product.title = text
                return

        # og:title fallback（會包含 HUMAN MADE Inc. 後綴，要清理）
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            text = og["content"].strip()
            # 移除「HUMAN MADE AIR FORCE 1 '01 / LO2 – HUMAN MADE Inc.」尾綴
            text = re.sub(r'\s*[–\-]\s*HUMAN MADE Inc\.?\s*$', '', text, flags=re.I)
            text = re.sub(r'^HUMAN MADE\s+', '', text, flags=re.I)  # 去掉開頭 brand
            product.title = text.strip()

    # ─────────────────────────────────────────────────────────────────
    # 價格
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_extract_price(self, soup: BeautifulSoup, html: str, product: ProductInfo) -> None:
        # ① JSON-LD offers.price（最準）
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") not in ("Product", "ProductGroup"):
                        continue
                    offers = item.get("offers")
                    if isinstance(offers, dict):
                        v = self._humanmade_price_to_int(offers.get("price") or offers.get("lowPrice"))
                        if v:
                            product.price_jpy = v
                            print(f"[HumanMade] price from JSON-LD: {v}")
                            return
                    elif isinstance(offers, list):
                        for off in offers:
                            if isinstance(off, dict):
                                v = self._humanmade_price_to_int(off.get("price"))
                                if v:
                                    product.price_jpy = v
                                    print(f"[HumanMade] price from JSON-LD list: {v}")
                                    return
            except (json.JSONDecodeError, AttributeError):
                continue

        # ② <span class="value" content="22550"> （SFCC 標準）
        for span in soup.find_all("span", attrs={"content": True}):
            content = span.get("content", "")
            v = self._humanmade_price_to_int(content)
            if v:
                # 確認在 .sales 或 .price 區塊內，避免抓到別的 content 屬性
                parent_classes = " ".join(
                    " ".join(p.get("class", [])) for p in span.parents if p.name
                )
                if any(kw in parent_classes for kw in ["sales", "price", "value"]):
                    product.price_jpy = v
                    print(f"[HumanMade] price from span[content]: {v}")
                    return

        # ③ <span class="sales"> 內的文字
        sales_el = soup.find("span", class_="sales") or soup.find(class_=re.compile(r"price-sales|product-price"))
        if sales_el:
            text = sales_el.get_text(" ", strip=True)
            m = re.search(r'[¥￥]\s*([\d,]+)', text)
            if m:
                v = self._humanmade_price_to_int(m.group(1))
                if v:
                    product.price_jpy = v
                    print(f"[HumanMade] price from .sales text: {v}")
                    return

        # ④ gtm-data 含 product.price (number)
        gtm = self._humanmade_parse_gtm_data(soup)
        if gtm:
            p_info = gtm.get("product") or {}
            if isinstance(p_info, dict):
                v = self._humanmade_price_to_int(p_info.get("price"))
                if v:
                    product.price_jpy = v
                    print(f"[HumanMade] price from gtm-data: {v}")
                    return

        print(f"[HumanMade] ⚠️ 無法抓到價格")

    @staticmethod
    def _humanmade_price_to_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip().replace(",", "")
            if not s:
                return None
            try:
                v = int(float(s))
            except (ValueError, TypeError):
                return None
        if _MIN_VALID_PRICE <= v <= _MAX_VALID_PRICE:
            return v
        return None

    # ─────────────────────────────────────────────────────────────────
    # 圖片
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_extract_images(soup: BeautifulSoup, product: ProductInfo) -> None:
        urls: list[str] = []
        seen: set[str] = set()

        # 主要：.primary-images .swiper-slide img[data-zoom]
        primary = soup.find(class_="primary-images")
        if primary:
            for img in primary.find_all("img"):
                # data-zoom 是高解析版（2000x2000），優先
                src = img.get("data-zoom") or img.get("src") or ""
                if not src:
                    continue
                # 過濾本地檔案路徑（HTML 存檔時的 ./xxx_files/）
                if src.startswith("./") or "_files/" in src:
                    # 從 alt / data-zoom 找原始 URL
                    src = img.get("data-zoom", "")
                if not src or not src.startswith("http"):
                    continue
                if src in seen:
                    continue
                seen.add(src)
                urls.append(src)

        # Fallback: og:image
        if not urls:
            og = soup.find("meta", attrs={"property": "og:image"})
            if og and og.get("content"):
                urls.append(og["content"])

        # Fallback: JSON-LD image
        if not urls:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "{}")
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            img = item.get("image")
                            if isinstance(img, list):
                                urls.extend([u for u in img if isinstance(u, str)])
                            elif isinstance(img, str):
                                urls.append(img)
                except (json.JSONDecodeError, AttributeError):
                    continue

        if urls:
            product.image_url = urls[0]
            product.extra_images = urls[1:8]

    # ─────────────────────────────────────────────────────────────────
    # 顏色
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_extract_colors(soup: BeautifulSoup) -> tuple[list[str], dict[str, str]]:
        """
        回傳 (colors, color_image_map)
        SFCC 結構:
        <div data-attr="color">
          <button class="attribute-item--color">
            <span data-attr-value="NAVY" style="background-image: url(...)"></span>
            <span id="NAVY">NAVY</span>
          </button>
        </div>
        """
        colors: list[str] = []
        color_image_map: dict[str, str] = {}
        seen: set[str] = set()

        wrappers = soup.find_all(attrs={"data-attr": "color"})
        if not wrappers:
            wrappers = soup.find_all(class_=re.compile(r"attribute-values-wrapper--color"))

        for wrapper in wrappers:
            for btn in wrapper.find_all("button"):
                # 取顏色名稱
                swatch_span = btn.find("span", attrs={"data-attr-value": True})
                color_name = ""
                if swatch_span:
                    color_name = (swatch_span.get("data-attr-value") or "").strip()

                if not color_name:
                    aria = btn.get("aria-label", "")
                    m = re.search(r'Color\s+(\S+)', aria, re.I)
                    if m:
                        color_name = m.group(1).strip()

                if not color_name or color_name in seen:
                    continue
                seen.add(color_name)
                colors.append(color_name)

                # 顏色 swatch 圖
                if swatch_span:
                    style = swatch_span.get("style", "")
                    m_bg = re.search(
                        r'background-image\s*:\s*url\([\'"]?([^\'")\s]+)[\'"]?\)',
                        style,
                    )
                    if m_bg:
                        img_path = m_bg.group(1)
                        if img_path.startswith("/"):
                            img_path = _SFCC_IMAGE_BASE + img_path
                        color_image_map[color_name] = img_path

        return colors, color_image_map

    # ─────────────────────────────────────────────────────────────────
    # 尺寸（含庫存）
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_extract_sizes(soup: BeautifulSoup) -> list[tuple[str, bool]]:
        """
        回傳 [(size, in_stock), ...]
        SFCC 結構:
        <button class="attribute-item--size selectable" data-attr-value="23.5cm">  ← 有貨
        <button class="attribute-item--size">  (無 selectable) ← 缺貨
        """
        sizes: list[tuple[str, bool]] = []
        seen: set[str] = set()

        # 找所有 size 按鈕
        buttons = soup.find_all("button", class_=re.compile(r"attribute-item--size"))

        for btn in buttons:
            size_name = (btn.get("data-attr-value") or "").strip()
            if not size_name:
                # fallback: 從 aria-label 抽
                aria = btn.get("aria-label", "")
                m = re.search(r'Size\s+(\S+)', aria, re.I)
                if m:
                    size_name = m.group(1).strip()
            if not size_name:
                # fallback: span.size-value 的文字
                span = btn.find("span", class_=re.compile(r"size-value"))
                if span:
                    size_name = span.get_text(strip=True)

            if not size_name or size_name in seen:
                continue
            seen.add(size_name)

            # 庫存判斷：有 selectable class 視為有貨
            classes = btn.get("class", [])
            classes_str = " ".join(classes) if isinstance(classes, list) else str(classes)
            in_stock = "selectable" in classes_str

            sizes.append((size_name, in_stock))

        return sizes

    # ─────────────────────────────────────────────────────────────────
    # 描述
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_extract_description(soup: BeautifulSoup, html: str, product: ProductInfo) -> None:
        if product.description:
            return

        # 優先 JSON-LD（已含 HTML 格式）
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        desc = item.get("description", "")
                        if desc and len(desc) > 30:
                            product.description = BeautifulSoup(desc, "html.parser").get_text("\n", strip=True)[:3000]
                            return
            except (json.JSONDecodeError, AttributeError):
                continue

        # Fallback: .product-description / .description
        for sel in [
            {"class_": re.compile(r"product-description")},
            {"class_": re.compile(r"description")},
        ]:
            el = soup.find(**sel)
            if el:
                text = el.get_text("\n", strip=True)
                if 30 < len(text) < 5000:
                    product.description = text
                    return

    # ─────────────────────────────────────────────────────────────────
    # gtm-data 解析
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_parse_gtm_data(soup: BeautifulSoup) -> dict | None:
        el = soup.find("input", attrs={"id": "gtm-data"})
        if not el:
            return None
        raw = el.get("value", "")
        if not raw:
            return None
        # value 是 HTML-encoded JSON，BeautifulSoup 已自動 decode
        # 但可能有 &quot; 等殘留，先 unescape 一次保險
        raw = html_lib.unescape(raw)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    async def _humanmade_fetch_html(self, url: str) -> str | None:
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1
                    self._clean_driver_tabs()

                    try:
                        driver.uc_open_with_reconnect(url, reconnect_time=8)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__ or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    html = ""
                    session_dead = False
                    best_html = ""
                    best_score = 0

                    for i in range(8):
                        _time.sleep(2)
                        try:
                            # 關閉 Global-e 彈窗
                            driver.execute_script("""
                                const ge = document.getElementById('globalePopupWrapper');
                                if (ge) ge.remove();
                                document.querySelectorAll('[class*="globale"], [id*="globale"]').forEach(el => {
                                    try {
                                        if (getComputedStyle(el).position === 'fixed') el.remove();
                                    } catch(e) {}
                                });
                            """)
                        except Exception:
                            pass

                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # SFCC 特徵評分（這次精準！）
                        score = 0
                        if 'product-name' in html: score += 5
                        if 'attribute-item--size' in html: score += 5
                        if 'attribute-item--color' in html: score += 3
                        if 'gtm-data' in html: score += 3
                        if 'application/ld+json' in html: score += 2
                        if 'primary-images' in html: score += 2

                        if score > best_score:
                            best_score = score
                            best_html = html

                        if i >= 1 and score >= 8 and len(html) > 5000:
                            print(f"[HumanMade][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if best_html and len(best_html) > 5000:
                        print(f"[HumanMade][fetch] 最佳: score={best_score}, size={len(best_html)//1024}KB")
                        return best_html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[HumanMade] HTML fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
