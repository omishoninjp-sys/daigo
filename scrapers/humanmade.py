"""
Human Made (humanmade.jp) 爬蟲 Mixin

平台：Shopify（2026-04 從 SFCC 遷移過來）
策略：完全從 HTML 解析（.json 端點被擋）

v3.1 新增：
- 診斷日誌：印出 HTML 大小、特徵字眼出現次數，協助精準診斷
- 等待條件加強：等到 ProductJson 或 cart/add 真的出現才回傳 HTML
- variants 多種 form 結構支援
- 價格 fallback：抓 Shopify CDN price-money 元素
"""
import json
import re
import time as _time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_HANDLE_FROM_PRODUCTS = re.compile(r'/products/([^/?#]+)', re.IGNORECASE)
_HANDLE_FROM_LEGACY = re.compile(r'/[a-z]+/([A-Za-z0-9]+)\.html', re.IGNORECASE)

_MIN_VALID_PRICE = 800
_MAX_VALID_PRICE = 5_000_000


def _extract_handle_from_url(url: str) -> str | None:
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

        url = re.sub(r'humanmade\.jp/(?:en|zh-CHT|zh-CN|ko)/', 'humanmade.jp/', url, flags=re.IGNORECASE)

        html = await self._humanmade_fetch_html(url)
        if not html:
            print(f"[HumanMade] ❌ 無法取得 HTML: {url}")
            return product

        # ── 診斷日誌（關鍵！）──────────────────────────────────
        self._humanmade_debug_html(html, url)

        try:
            soup = BeautifulSoup(html, "html.parser")

            # 1. canonical handle
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

            # 2. ProductJson
            shopify_data = self._humanmade_find_shopify_product_json(soup, html)
            if shopify_data:
                self._humanmade_parse_shopify_product(shopify_data, product, handle)
                if product.is_valid and product.variants:
                    print(
                        f"[HumanMade] ✅ ProductJson: {product.title} / "
                        f"¥{product.price_jpy} / variants={len(product.variants)}"
                    )
                    return product

            # 3. window.meta
            meta_data = self._humanmade_find_window_meta(html)
            if meta_data and not (product.is_valid and product.variants):
                self._humanmade_parse_shopify_product(meta_data, product, handle)
                if product.is_valid and product.variants:
                    print(
                        f"[HumanMade] ✅ window.meta: {product.title} / "
                        f"¥{product.price_jpy} / variants={len(product.variants)}"
                    )
                    return product

            # 4. HTML fallback (og + form + JSON-LD)
            self._humanmade_parse_html_full(soup, html, product, handle)

            print(
                f"[HumanMade] {'✅' if product.is_valid else '⚠️'} HTML 解析: "
                f"{product.title} / ¥{product.price_jpy} / variants={len(product.variants)}"
            )

        except Exception as e:
            print(f"[HumanMade] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # ★ 診斷日誌 ★
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _humanmade_debug_html(html: str, url: str) -> None:
        """印出 HTML 特徵，協助診斷哪個資料源缺失"""
        size_kb = len(html) // 1024
        features = {
            'og:title': html.count('og:title'),
            'og:price': html.count('og:price'),
            'og:image': html.count('og:image'),
            'application/json': html.count('application/json'),
            'application/ld+json': html.count('application/ld+json'),
            'ProductJson': html.count('ProductJson'),
            'product-template': html.count('product-template'),
            'data-product-json': html.count('data-product-json'),
            'data-product': html.count('data-product'),
            '"variants"': html.count('"variants"'),
            '"options"': html.count('"options"'),
            '"option1"': html.count('"option1"'),
            'cart/add': html.count('cart/add'),
            'select name': html.count('select name'),
            'var meta': html.count('var meta'),
            'ShopifyAnalytics': html.count('ShopifyAnalytics'),
            '<form': html.count('<form'),
            'price-item': html.count('price-item'),
            'product__price': html.count('product__price'),
            '￥': html.count('￥'),
            '¥': html.count('¥'),
        }
        non_zero = {k: v for k, v in features.items() if v > 0}
        print(f"[HumanMade][debug] HTML size={size_kb}KB, url={url}")
        print(f"[HumanMade][debug] 特徵: {non_zero}")

        # 抽出前 3 個 ¥xxx 的上下文
        for i, m in enumerate(re.finditer(r'[¥￥]\s*[\d,]+', html)):
            if i >= 3:
                break
            start = max(0, m.start() - 40)
            end = min(len(html), m.end() + 40)
            ctx = html[start:end].replace('\n', ' ').replace('\t', ' ')
            print(f"[HumanMade][debug] ¥ 上下文 #{i}: ...{ctx}...")

    # ─────────────────────────────────────────────────────────────────
    # 找 Shopify ProductJson（強化版）
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_find_shopify_product_json(self, soup: BeautifulSoup, html: str) -> dict | None:
        # Pattern 1+2: 任何 application/json script
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            attrs = script.attrs
            sid = (attrs.get("id") or "").lower()

            # 命名特徵
            is_product_script = (
                'productjson' in sid.replace('-', '').replace('_', '')
                or 'product-json' in sid
                or 'product-template' in sid
                or 'data-product-json' in attrs
                or 'data-product' in attrs
                or 'product' in (attrs.get('data-section-type', '') or '').lower()
            )

            text = script.string or ''
            # 內容特徵
            has_variants = '"variants"' in text and ('"options"' in text or '"option1"' in text)

            if not (is_product_script or has_variants):
                continue

            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue

            # 可能是 {product: {...}} 或直接 {...}
            candidates = []
            if isinstance(data, dict):
                if self._is_valid_product_data(data):
                    candidates.append(data)
                if 'product' in data and isinstance(data['product'], dict):
                    candidates.append(data['product'])

            for c in candidates:
                if self._is_valid_product_data(c):
                    print(f"[HumanMade] 命中 ProductJson: id={sid or '(none)'}, attrs={list(attrs.keys())}")
                    return c

        # Pattern 3: 直接從 raw HTML regex（萬一 script 被切碎）
        # Shopify 的 product JSON 通常是 {"id":NUMBER,"title":"...","handle":"...","variants":[...]
        m = re.search(
            r'\{"id":\d+,"title":"[^"]+","handle":"[^"]+"[^{]*?"variants":\[.*?\}\s*\]',
            html,
            re.DOTALL,
        )
        if m:
            try:
                # 試著找完整的 JSON 物件（從 { 開始平衡 brackets）
                start = m.start()
                depth = 0
                for i in range(start, min(start + 200_000, len(html))):
                    if html[i] == '{':
                        depth += 1
                    elif html[i] == '}':
                        depth -= 1
                        if depth == 0:
                            chunk = html[start:i + 1]
                            data = json.loads(chunk)
                            if self._is_valid_product_data(data):
                                print(f"[HumanMade] 命中 raw HTML regex ProductJson")
                                return data
                            break
            except (json.JSONDecodeError, ValueError):
                pass

        return None

    @staticmethod
    def _is_valid_product_data(data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        variants = data.get("variants")
        if not isinstance(variants, list) or len(variants) == 0:
            return False
        return bool(data.get("title") or data.get("handle"))

    # ─────────────────────────────────────────────────────────────────
    # window.meta
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_find_window_meta(self, html: str) -> dict | None:
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
        try:
            title = p.get("title") or ""
            if title:
                product.title = title

            vendor = p.get("vendor") or ""
            if vendor:
                product.brand = vendor

            body = p.get("body_html") or p.get("description") or ""
            if body:
                product.description = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)[:3000]

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
                if any(k in name for k in ["color", "colour", "カラー", "色"]):
                    color_idx = i
                elif any(k in name for k in ["size", "サイズ", "尺寸"]):
                    size_idx = i

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

            prices = []
            for v in variants_raw:
                if isinstance(v, dict):
                    val = self._humanmade_price_to_int(v.get("price"))
                    if val:
                        prices.append(val)
            if prices:
                product.price_jpy = min(prices)

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

                if not color and not size:
                    o1 = (v.get("option1") or "").strip()
                    if o1:
                        size = o1

                v_price = self._humanmade_price_to_int(v.get("price")) or product.price_jpy or 0
                v_avail = v.get("available", True)

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
    # 完全 fallback
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_parse_html_full(self, soup: BeautifulSoup, html: str, product: ProductInfo, handle: str | None) -> None:
        # 標題
        if not product.title:
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                product.title = og_title["content"].strip()
            elif soup.find("h1"):
                product.title = soup.find("h1").get_text(strip=True)

        # 圖片
        if not product.image_url:
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_img and og_img.get("content"):
                product.image_url = og_img["content"]

        # 價格 candidates
        price_candidates = []

        for sel in [
            {"property": "og:price:amount"},
            {"property": "product:price:amount"},
        ]:
            el = soup.find("meta", attrs=sel)
            if el and el.get("content"):
                v = self._humanmade_price_to_int(el["content"])
                if v:
                    price_candidates.append(("og:price", v))

        # JSON-LD（含 title 比對）
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") not in ("Product", "ProductGroup"):
                        continue
                    item_name = (item.get("name") or "").lower()
                    if product.title and item_name:
                        # 寬鬆比對：互相包含或共享關鍵字
                        title_lower = product.title.lower()
                        if item_name not in title_lower and title_lower not in item_name:
                            # 至少要共享 3 個字以上的關鍵詞
                            shared = set(re.findall(r'[a-z0-9]+', item_name)) & set(re.findall(r'[a-z0-9]+', title_lower))
                            if not any(len(s) >= 3 for s in shared):
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

        # Shopify CDN 元素：.product__price, .price-item, [data-price]
        for sel in ['.product__price', '.price-item', '.price', '[data-price]', '.money']:
            try:
                for el in soup.select(sel):
                    text = el.get_text(strip=True)
                    data_price = el.get('data-price', '')
                    for src in [data_price, text]:
                        if not src:
                            continue
                        m = re.search(r'([\d,]{4,})', src)
                        if m:
                            v = self._humanmade_price_to_int(m.group(1))
                            if v:
                                price_candidates.append((sel, v))
                                break
            except Exception:
                pass

        # 文字 ¥xxxx（嚴格 4-7 位數）
        page_text = soup.get_text(" ", strip=True)
        for m in re.finditer(r'[¥￥]\s*([1-9]\d{0,2}(?:,\d{3})+|\d{4,7})', page_text):
            v = self._humanmade_price_to_int(m.group(1))
            if v:
                price_candidates.append(("text", v))

        if price_candidates:
            from collections import Counter
            counter = Counter(v for _, v in price_candidates)
            max_count = max(counter.values())
            top_prices = [v for v, c in counter.items() if c == max_count]
            product.price_jpy = max(top_prices)
            print(f"[HumanMade] price candidates: {price_candidates} → 選 {product.price_jpy}")

        # Variants
        if not product.variants:
            self._humanmade_extract_variants_from_form(soup, product, handle)

    def _humanmade_extract_variants_from_form(self, soup: BeautifulSoup, product: ProductInfo, handle: str | None) -> None:
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

                lower = label.lower()
                available = not any(kw in lower for kw in ["sold out", "売り切れ", "在庫切れ", "soldout", "完売"])
                clean_label = re.sub(
                    r'\s*[-–—]?\s*(sold\s*out|売り切れ|在庫切れ|完売)\s*$',
                    '',
                    label,
                    flags=re.I,
                ).strip()

                parts = [p.strip() for p in re.split(r'\s*/\s*', clean_label) if p.strip()]
                if len(parts) >= 2:
                    color, size = parts[0], parts[1]
                elif len(parts) == 1:
                    p0 = parts[0]
                    is_size = bool(re.match(r'^[\d.]+(?:cm|inch)?$|^(XS|S|M|L|XL|XXL|2XL|3XL|FREE|ONE\s*SIZE)$', p0, re.I))
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

        # form 也找不到的話，嘗試 [data-variants] 或 swatch 元素
        # 從 <option> 直接掃（不限 form 內）
        select = soup.find("select", attrs={"name": re.compile(r'^id$|variants', re.I)})
        if select:
            print(f"[HumanMade] 找到 select 但不在 form 內，嘗試解析")
            base_handle = handle or "hm"
            variants = []
            for opt in select.find_all("option"):
                value = opt.get("value", "").strip()
                label = opt.get_text(strip=True)
                if value and value.isdigit() and label:
                    variants.append({
                        "color": "",
                        "size": label,
                        "sku": f"{base_handle}-{value}",
                        "price": product.price_jpy or 0,
                        "in_stock": True,
                        "image": product.image_url,
                    })
            if variants:
                product.variants = variants

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取（強化等待 + 滾動）
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

                    for i in range(10):
                        _time.sleep(2)
                        try:
                            # 關閉彈窗 + 滾動觸發 lazy load
                            driver.execute_script("""
                                const ge = document.getElementById('globalePopupWrapper');
                                if (ge) ge.remove();
                                document.querySelectorAll('[class*="globale"], [id*="globale"]').forEach(el => {
                                    try {
                                        if (getComputedStyle(el).position === 'fixed') el.remove();
                                    } catch(e) {}
                                });
                                window.scrollTo(0, document.body.scrollHeight / 2);
                                window.scrollTo(0, 0);
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

                        # 評分：哪些關鍵特徵已經出現
                        score = 0
                        if 'ProductJson' in html: score += 5
                        if '"variants"' in html: score += 5
                        if 'cart/add' in html: score += 3
                        if '<select name="id"' in html: score += 3
                        if 'og:price' in html: score += 2
                        if 'product__price' in html: score += 2
                        if 'application/ld+json' in html: score += 1

                        if score > best_score:
                            best_score = score
                            best_html = html

                        # 命中關鍵特徵就提早回傳
                        if i >= 1 and score >= 5 and len(html) > 5000:
                            print(f"[HumanMade][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    # 沒命中高分，回傳分數最高的版本
                    if best_html and len(best_html) > 5000:
                        print(f"[HumanMade][fetch] 用最佳版本: score={best_score}, size={len(best_html)//1024}KB")
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
