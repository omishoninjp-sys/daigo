"""
Human Made (humanmade.jp) 爬蟲 Mixin

平台：Shopify（2026-04 從 SFCC 遷移過來）
策略：完全從 HTML 解析（.json 端點被 humanmade.jp 封鎖，回傳首頁）

抓取優先順序（命中即停）：
1. <script id="ProductJson-..."> 或 <script data-product-json> ← Shopify ProductJson
2. <script>var meta = {product:...}</script> ← Shopify Analytics meta
3. <form action="/cart/add"> 內 <select name="id"> ← Shopify form variants
4. og:price:amount + JSON-LD（單純抓主商品價格用）
5. 文字 ¥xxxxx fallback

URL 標準化：
- /shoes/XX31GD063.html、/products/<handle> 都支援
- 從 <link rel="canonical"> 取真實 handle 更新 source_url
"""
import json
import re
import time as _time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


# 從各種 URL 格式抽出 Shopify handle
_HANDLE_FROM_PRODUCTS = re.compile(r'/products/([^/?#]+)', re.IGNORECASE)
_HANDLE_FROM_LEGACY = re.compile(r'/[a-z]+/([A-Za-z0-9]+)\.html', re.IGNORECASE)

# 合理價格範圍（過濾掉雜訊數字）
_MIN_VALID_PRICE = 800     # Human Made 最便宜的襪子也接近 ¥1,500，¥800 以下肯定是雜訊
_MAX_VALID_PRICE = 5_000_000


def _extract_handle_from_url(url: str) -> str | None:
    """從 URL 抽 Shopify handle"""
    parsed = urlparse(url)
    path = parsed.path
    m = _HANDLE_FROM_PRODUCTS.search(path)
    if m:
        return m.group(1).lower().replace('.html', '')
    m = _HANDLE_FROM_LEGACY.search(path)
    if m:
        return m.group(1).lower()
    return None


class HumanMadeMixin:

    async def _scrape_humanmade(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Human Made")

        # 去語系前綴
        url = re.sub(r'humanmade\.jp/(?:en|zh-CHT|zh-CN|ko)/', 'humanmade.jp/', url, flags=re.IGNORECASE)

        # 抓 HTML
        html = await self._humanmade_fetch_html(url)
        if not html:
            print(f"[HumanMade] ❌ 無法取得 HTML: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 1. canonical URL 找真實 handle ──
            handle = _extract_handle_from_url(url)
            canonical = soup.find("link", rel="canonical")
            if canonical and canonical.get("href"):
                canonical_url = canonical["href"]
                if "/products/" in canonical_url:
                    product.source_url = canonical_url
                    canon_handle = _extract_handle_from_url(canonical_url)
                    if canon_handle:
                        handle = canon_handle
                        print(f"[HumanMade] canonical handle: {handle}")

            # ── 2. 找 Shopify ProductJson ──（最理想）
            shopify_data = self._humanmade_find_shopify_product_json(soup)
            if shopify_data:
                self._humanmade_parse_shopify_product(shopify_data, product, handle)
                if product.is_valid and product.variants:
                    print(
                        f"[HumanMade] ✅ ProductJson: {product.title} / "
                        f"¥{product.price_jpy} / variants={len(product.variants)}"
                    )
                    return product

            # ── 3. fallback：window.meta JS 變數 ──
            meta_data = self._humanmade_find_window_meta(html)
            if meta_data and not product.is_valid:
                self._humanmade_parse_shopify_product(meta_data, product, handle)
                if product.is_valid and product.variants:
                    print(
                        f"[HumanMade] ✅ window.meta: {product.title} / "
                        f"¥{product.price_jpy} / variants={len(product.variants)}"
                    )
                    return product

            # ── 4. fallback：從 HTML form / meta / og:tags 拼湊 ──
            self._humanmade_parse_html_full(soup, product, handle)

            print(
                f"[HumanMade] {'✅' if product.is_valid else '⚠️'} HTML 解析: "
                f"{product.title} / ¥{product.price_jpy} / variants={len(product.variants)}"
            )

        except Exception as e:
            print(f"[HumanMade] ❌ 解析錯誤: {type(e).__name__}: {e}")

        return product

    # ─────────────────────────────────────────────────────────────────
    # 找 Shopify 內嵌 ProductJson
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_find_shopify_product_json(self, soup: BeautifulSoup) -> dict | None:
        """搜尋頁面內嵌的 Shopify product JSON（多種主題支援）"""

        # Pattern 1: <script id="ProductJson-..." type="application/json">
        for script in soup.find_all("script", type="application/json"):
            sid = (script.get("id") or "").lower()
            if "productjson" in sid.replace("-", "").replace("_", "") or "product-json" in sid:
                try:
                    data = json.loads(script.string or "{}")
                    if isinstance(data, dict) and self._is_valid_product_data(data):
                        print(f"[HumanMade] 命中 ProductJson script id={script.get('id')}")
                        return data
                except (json.JSONDecodeError, TypeError):
                    continue

        # Pattern 2: <script data-product-json> / <script data-section-type="product-template">
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            attrs = script.attrs
            if any(k in attrs for k in ["data-product-json", "data-product"]):
                try:
                    data = json.loads(script.string or "{}")
                    if isinstance(data, dict) and self._is_valid_product_data(data):
                        print(f"[HumanMade] 命中 data-product-json")
                        return data
                except (json.JSONDecodeError, TypeError):
                    continue
            section_type = script.get("data-section-type", "") or script.get("data-section", "")
            if "product" in section_type.lower():
                try:
                    data = json.loads(script.string or "{}")
                    # data-section 可能 nest 在 product key 下
                    if isinstance(data, dict):
                        if self._is_valid_product_data(data):
                            return data
                        if "product" in data and isinstance(data["product"], dict):
                            return data["product"]
                except (json.JSONDecodeError, TypeError):
                    continue

        # Pattern 3: 任何 application/json 含 variants + options + title
        for script in soup.find_all("script", type="application/json"):
            text = script.string or ""
            if '"variants"' in text and ('"options"' in text or '"option1"' in text) and '"title"' in text:
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        if self._is_valid_product_data(data):
                            print(f"[HumanMade] 命中泛用 application/json (含 variants)")
                            return data
                        if "product" in data and isinstance(data["product"], dict) and self._is_valid_product_data(data["product"]):
                            return data["product"]
                except (json.JSONDecodeError, TypeError):
                    continue

        return None

    @staticmethod
    def _is_valid_product_data(data: dict) -> bool:
        """檢查是否是合理的 Shopify product 物件"""
        if not isinstance(data, dict):
            return False
        # 必須有 variants array 且非空
        variants = data.get("variants")
        if not isinstance(variants, list) or len(variants) == 0:
            return False
        # 必須有 title 或 handle
        return bool(data.get("title") or data.get("handle"))

    # ─────────────────────────────────────────────────────────────────
    # 找 window.meta（Shopify Analytics 內嵌）
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_find_window_meta(self, html: str) -> dict | None:
        """從 HTML 找 var meta = {product: {...}}"""
        # Shopify 預設會輸出：var meta = {"product":{...},"page":...}
        patterns = [
            r'var\s+meta\s*=\s*({[^;]*?});',
            r'window\.meta\s*=\s*({[^;]*?});',
            r'window\.ShopifyAnalytics\.meta\s*=\s*({[^;]*?});',
        ]
        for pat in patterns:
            for m in re.finditer(pat, html, re.DOTALL):
                try:
                    data = json.loads(m.group(1))
                    if isinstance(data, dict) and "product" in data:
                        prod = data["product"]
                        if isinstance(prod, dict) and prod.get("variants"):
                            return prod
                except (json.JSONDecodeError, AttributeError):
                    continue
        return None

    # ─────────────────────────────────────────────────────────────────
    # 解析 Shopify product 物件
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_parse_shopify_product(self, p: dict, product: ProductInfo, handle: str | None) -> None:
        """把 Shopify product dict 轉成 ProductInfo"""
        try:
            # 標題
            title = p.get("title") or ""
            if title:
                product.title = title

            # 品牌
            vendor = p.get("vendor") or ""
            if vendor:
                product.brand = vendor

            # 描述
            body = p.get("body_html") or p.get("description") or ""
            if body:
                product.description = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)[:3000]

            # 圖片
            images = []
            for img in (p.get("images") or []):
                src = img.get("src") if isinstance(img, dict) else (img if isinstance(img, str) else None)
                if src:
                    if src.startswith("//"):
                        src = "https:" + src
                    images.append(src)

            if images:
                product.image_url = images[0]
                product.extra_images = images[1:8]

            # Options 順序
            options = p.get("options") or []
            color_idx = -1
            size_idx = -1
            for i, opt in enumerate(options):
                if isinstance(opt, dict):
                    name = (opt.get("name") or "").lower()
                elif isinstance(opt, str):
                    name = opt.lower()
                else:
                    continue
                if any(k in name for k in ["color", "colour", "カラー", "color名", "色"]):
                    color_idx = i
                elif any(k in name for k in ["size", "サイズ", "尺寸"]):
                    size_idx = i

            # 收集 variants
            variants_raw = p.get("variants") or []
            color_to_image: dict[str, str] = {}

            for v in variants_raw:
                if not isinstance(v, dict):
                    continue
                color = ""
                if color_idx >= 0:
                    color = (v.get(f"option{color_idx + 1}") or "").strip()
                feat = v.get("featured_image")
                if color and feat:
                    img_src = feat.get("src") if isinstance(feat, dict) else feat
                    if img_src and color not in color_to_image:
                        if img_src.startswith("//"):
                            img_src = "https:" + img_src
                        color_to_image[color] = img_src

            if color_to_image and not product.image_url:
                product.image_url = next(iter(color_to_image.values()))

            # 價格：取所有 variant.price 的 min（合理範圍內）
            prices = []
            for v in variants_raw:
                if isinstance(v, dict):
                    val = self._humanmade_price_to_int(v.get("price"))
                    if val:
                        prices.append(val)

            if prices:
                product.price_jpy = min(prices)

            # 組 variants
            variant_list = []
            for v in variants_raw:
                if not isinstance(v, dict):
                    continue
                color = ""
                size = ""
                if color_idx >= 0:
                    color = (v.get(f"option{color_idx + 1}") or "").strip()
                if size_idx >= 0:
                    size = (v.get(f"option{size_idx + 1}") or "").strip()

                # 如果沒有明確 color/size，但有 option1，預設當 size（鞋子常見）
                if not color and not size:
                    o1 = (v.get("option1") or "").strip()
                    if o1:
                        size = o1

                v_price = self._humanmade_price_to_int(v.get("price")) or product.price_jpy or 0
                v_avail = v.get("available", True)
                if v.get("inventory_quantity") is not None:
                    try:
                        v_avail = bool(v.get("inventory_quantity")) and v_avail
                    except Exception:
                        pass

                variant_image = color_to_image.get(color) or product.image_url

                label_parts = [pp for pp in [color, size] if pp]
                base_handle = handle or "hm"
                variant_list.append({
                    "color": color,
                    "size": size,
                    "sku": (v.get("sku") or f"{base_handle}-{'-'.join(label_parts)}").lower().replace(" ", "-"),
                    "price": v_price,
                    "in_stock": bool(v_avail),
                    "image": variant_image or "",
                })

            product.variants = variant_list
            product.in_stock = any(vv["in_stock"] for vv in variant_list) if variant_list else True

        except Exception as e:
            print(f"[HumanMade] ❌ Shopify product 解析錯誤: {type(e).__name__}: {e}")

    @staticmethod
    def _humanmade_price_to_int(value) -> int | None:
        """
        Shopify price 解析。注意：
        - 部分主題 price 是 cent (5500 = ¥55.00 → 不對，台灣/日本是 ¥5500)
        - humanmade.jp JPY 應該是整數日圓字串「5500」
        - 接受合理範圍 800~5,000,000
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip()
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
    # 完全 fallback：從 HTML 任意元素拼湊
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_parse_html_full(self, soup: BeautifulSoup, product: ProductInfo, handle: str | None) -> None:
        """ProductJson 都失敗時，用 og tags + form 變體拼湊"""

        # 標題：og:title 或 h1
        if not product.title:
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                product.title = og_title["content"].strip()
            elif soup.find("h1"):
                product.title = soup.find("h1").get_text(strip=True)

        # 圖片：og:image
        if not product.image_url:
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_img and og_img.get("content"):
                product.image_url = og_img["content"]

        # 價格：多源比對，取最大可信值（避免抓到 ¥185 雜訊）
        price_candidates = []

        # og:price:amount
        for sel in [
            {"property": "og:price:amount"},
            {"property": "product:price:amount"},
        ]:
            el = soup.find("meta", attrs=sel)
            if el and el.get("content"):
                v = self._humanmade_price_to_int(el["content"])
                if v:
                    price_candidates.append(("og:price", v))

        # JSON-LD（注意：可能有多個 product，要選 main 那個）
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") not in ("Product", "ProductGroup"):
                        continue
                    # 比對 title 是否符合（過濾掉 related products）
                    item_name = (item.get("name") or "").lower()
                    if product.title and item_name and item_name not in product.title.lower() and product.title.lower() not in item_name:
                        continue
                    offers = item.get("offers")
                    if isinstance(offers, dict):
                        v = self._humanmade_price_to_int(offers.get("price") or offers.get("lowPrice"))
                        if v:
                            price_candidates.append(("ld+json", v))
                    elif isinstance(offers, list):
                        for off in offers:
                            if isinstance(off, dict):
                                v = self._humanmade_price_to_int(off.get("price"))
                                if v:
                                    price_candidates.append(("ld+json", v))
            except (json.JSONDecodeError, AttributeError):
                continue

        # 文字 ¥xxxx（限制 4-7 位數，過濾掉 ¥185 這種噪音）
        page_text = soup.get_text(" ", strip=True)
        for m in re.finditer(r'[¥￥]\s*([1-9]\d{0,2}(?:,\d{3})+|\d{4,6})', page_text):
            v = self._humanmade_price_to_int(m.group(1))
            if v:
                price_candidates.append(("text", v))

        if price_candidates:
            # 取出現次數最多的，平手取最大（避免取到推薦商品的便宜價格）
            from collections import Counter
            counter = Counter(v for _, v in price_candidates)
            max_count = max(counter.values())
            top_prices = [v for v, c in counter.items() if c == max_count]
            product.price_jpy = max(top_prices)
            print(f"[HumanMade] price candidates: {price_candidates} → 選 {product.price_jpy}")

        # Variants：從 <form action="/cart/add"> 抽
        if not product.variants:
            self._humanmade_extract_variants_from_form(soup, product, handle)

    def _humanmade_extract_variants_from_form(self, soup: BeautifulSoup, product: ProductInfo, handle: str | None) -> None:
        """從 <form action='/cart/add'> 內 <select name='id'> 抽 variants"""
        forms = soup.find_all("form", action=re.compile(r'/cart/add'))
        for form in forms:
            select = (
                form.find("select", attrs={"name": "id"})
                or form.find("select", attrs={"name": re.compile(r'^id$|^variants', re.I)})
            )
            if not select:
                continue

            base_handle = handle or "hm"
            variants = []
            for opt in select.find_all("option"):
                value = opt.get("value", "").strip()
                if not value or not value.isdigit():
                    continue
                label = opt.get_text(strip=True)
                if not label:
                    continue

                # 偵測售完關鍵字
                lower = label.lower()
                available = not any(kw in lower for kw in ["sold out", "売り切れ", "在庫切れ", "soldout", "完売"])
                # 移除 "- Sold Out" 等尾綴
                clean_label = re.sub(
                    r'\s*[-–—]?\s*(sold\s*out|売り切れ|在庫切れ|完売)\s*$',
                    '',
                    label,
                    flags=re.I,
                ).strip()

                # 拆 color / size
                parts = [p.strip() for p in re.split(r'\s*/\s*', clean_label) if p.strip()]
                if len(parts) >= 2:
                    color, size = parts[0], parts[1]
                elif len(parts) == 1:
                    # 單欄：判斷是 size 還是 color
                    p0 = parts[0]
                    is_size = bool(re.match(r'^[\d.]+(?:cm|inch)?$|^[XS|S|M|L|XL|XXL|2XL|3XL|FREE|ONE\s*SIZE]+$', p0, re.I))
                    if is_size:
                        color, size = "", p0
                    else:
                        color, size = p0, ""
                else:
                    color, size = "", ""

                label_parts = [pp for pp in [color, size] if pp]
                variants.append({
                    "color": color,
                    "size": size,
                    "sku": f"{base_handle}-{'-'.join(label_parts)}".lower().replace(" ", "-") if label_parts else f"{base_handle}-{value}",
                    "price": product.price_jpy or 0,
                    "in_stock": available,
                    "image": product.image_url,
                })

            if variants:
                product.variants = variants
                product.in_stock = any(v["in_stock"] for v in variants)
                print(f"[HumanMade] 從 <form> 抽出 {len(variants)} 個 variants")
                return

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取（保留原 driver 邏輯，只改善等待條件）
    # ─────────────────────────────────────────────────────────────────
    async def _humanmade_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase UC driver 取得 JS 渲染後的 HTML"""
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

                        # 等到頁面有 ProductJson / variants 或 og:price 才算 ready
                        if i >= 1 and len(html) > 5000 and (
                            'ProductJson' in html
                            or '"variants"' in html
                            or 'og:price' in html
                            or 'cart/add' in html
                        ):
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
                    print(f"[HumanMade] HTML fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
