"""
宇治 丸久小山園 (marukyu-koyamaen.co.jp/motoan-shop) 商品爬取 Mixin

平台特性：
- 京都宇治抹茶老舖的官方網店，使用 WordPress + WooCommerce
- UTF-8 編碼
- JSON-LD 只有 AggregateOffer（lowPrice/highPrice/offerCount，無 hasVariant）
- 真正的 variants 結構：頁面內每個 variant 一個 block，含：
    <dl class="pa pa-sku"><dt>SKU</dt><dd>1191040C1</dd></dl>
    <dl class="pa pa-size"><dt>セット名</dt><dd>40g缶</dd></dl>
    <div class="price">¥2,592</div>
- 庫存：<p class="stock in-stock">／<p class="stock out-of-stock">
  - 「sold-individually」(お一人様１点まで) 是限購不是缺貨

URL 範例：
  https://www.marukyu-koyamaen.co.jp/motoan-shop/products/1191040c1/
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


class MarukyuKoyamaenMixin:

    async def _scrape_marukyukoyamaen(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="宇治 丸久小山園")
        clean_url = url.split("#")[0].split("?")[0].strip()

        html = await asyncio.to_thread(self._marukyu_get_html, clean_url)
        if not html:
            print(f"[Marukyu] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題：og:title 或 <title> ──
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                product.title = og_title["content"].strip()
            if not product.title:
                title_el = soup.find("title")
                if title_el:
                    raw = title_el.get_text(strip=True)
                    # 去掉「| 抹茶 | 宇治 丸久小山園」尾巴
                    raw = re.split(r'\s*[｜\|]\s*', raw)[0].strip()
                    product.title = raw

            # ── 描述：短說明區 ──
            desc_el = soup.select_one(".woocommerce-product-details__short-description")
            if desc_el:
                desc_text = desc_el.get_text(" ", strip=True)
                product.description = self._marukyu_clean_description(desc_text)

            # 描述為空 → 用 og:description fallback
            if not product.description:
                og_desc = soup.find("meta", attrs={"property": "og:description"})
                if og_desc and og_desc.get("content"):
                    product.description = self._marukyu_clean_description(
                        og_desc["content"].strip()
                    )

            # ── 主圖 + 額外圖 ──
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_img and og_img.get("content"):
                product.image_url = og_img["content"].strip()

            product.extra_images = self._marukyu_extract_images(
                soup, main_image=product.image_url
            )

            # ── variants ──
            variants = self._marukyu_extract_variants(soup)
            if variants:
                product.variants = variants
                prices_seen = [v["price"] for v in variants if v["price"] > 0]
                in_stock_count = sum(1 for v in variants if v["in_stock"])
                # 主價：取所有 variant 最低価（讓客人在 Shopify 看到「¥2,592 起」感覺）
                if prices_seen:
                    product.price_jpy = min(prices_seen)
                print(
                    f"[Marukyu] variants={len(variants)} "
                    f"(in_stock={in_stock_count}, prices={sorted(set(prices_seen))})"
                )
                if in_stock_count == 0:
                    product.in_stock = False
            else:
                # 單品 fallback：從 JSON-LD AggregateOffer 抓 lowPrice
                ld_price = self._marukyu_jsonld_low_price(soup)
                if ld_price:
                    product.price_jpy = ld_price

            # 都沒抓到價格 → 頁面 (税込) fallback
            if not product.price_jpy:
                fp = self._marukyu_price_fallback(html)
                if fp:
                    product.price_jpy = fp

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Marukyu] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"images={1 + len(product.extra_images)} | variants={len(product.variants)}"
                )
            else:
                print(
                    f"[Marukyu] ⚠️ 部分資料缺失 ({title_short!r}) | price={product.price_jpy}"
                )

        except Exception as e:
            print(f"[Marukyu] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # variants 抓取
    # ─────────────────────────────────────────────────────────────────
    def _marukyu_extract_variants(self, soup: BeautifulSoup) -> list:
        """
        從頁面 <dl class="pa-sku"> 出發，往上找包住該變體的容器，
        在容器內找 <dl class="pa-size"> 與 <div class="price"> 與 <p class="stock">
        """
        variants = []
        skus = soup.select(".pa-sku")

        for sku_el in skus:
            dd_sku = sku_el.find("dd")
            sku_val = dd_sku.get_text(strip=True) if dd_sku else ""

            # 往上找包住變體三件套的最近容器
            parent = sku_el.parent
            variant_data = None
            while parent and parent.name not in ("form", "body", None):
                size_el = parent.find("dl", class_="pa-size")
                price_el = parent.find("div", class_="price")
                if size_el and price_el:
                    dd_size = size_el.find("dd")
                    size_val = dd_size.get_text(strip=True) if dd_size else ""

                    # 価格：amount bdi 內的數字（去掉 ¥ 跟逗號）
                    price_amount = price_el.find("span", class_="amount")
                    price_text = (
                        price_amount.get_text(strip=True) if price_amount else price_el.get_text(strip=True)
                    )
                    price_num = re.search(r'([\d,]+)', price_text)
                    price = self._marukyu_to_int(price_num.group(1)) if price_num else 0

                    # 庫存判斷：
                    # <p class="stock in-stock">       → 有貨
                    # <p class="stock out-of-stock">   → 無貨
                    # <p class="stock in-stock sold-individually"> → 限購但有貨
                    in_stock = True
                    stock_el = parent.find("p", class_="stock")
                    if stock_el:
                        classes = stock_el.get("class", [])
                        if "out-of-stock" in classes or "outofstock" in classes:
                            in_stock = False

                    variant_data = {
                        "color": "",  # 無顏色維度
                        "size": size_val,  # セット名（例：40g缶、100g袋）
                        "price": price or 0,
                        "in_stock": in_stock,
                        "image": "",
                        "sku": sku_val.lower() if sku_val else "",
                    }
                    break
                parent = parent.parent

            if variant_data:
                variants.append(variant_data)

        return variants

    # ─────────────────────────────────────────────────────────────────
    # 圖片
    # ─────────────────────────────────────────────────────────────────
    def _marukyu_extract_images(self, soup_or_html, main_image: str = "") -> list:
        """
        從頁面抓商品圖片
        策略：
        1. 優先抓 .product-images 區塊內 img 的 srcset，取最大尺寸（這才是商品本體圖）
        2. 抓不到時 fallback：抓全頁所有 wp-content/uploads 圖（會抓到頁面推薦/分類圖）
        """
        # 統一處理參數：可能傳 soup 或 html string
        if isinstance(soup_or_html, BeautifulSoup):
            soup = soup_or_html
            html = str(soup)
        else:
            html = soup_or_html
            soup = BeautifulSoup(html, "html.parser")

        # ── 主策略：從 .product-images 區塊抓商品本體圖 ──
        gallery = soup.select_one(".product-images, .woocommerce-product-gallery, .images")
        if gallery:
            result = []
            seen = set()
            main_key = self._marukyu_image_basename(main_image)
            if main_key:
                seen.add(main_key)

            for img in gallery.find_all("img"):
                # 從 srcset 取最大尺寸（srcset 格式："url1 480w, url2 1024w"）
                srcset = img.get("srcset", "")
                best_url = ""
                best_w = 0
                if srcset:
                    for entry in srcset.split(","):
                        parts = entry.strip().split()
                        if len(parts) >= 2:
                            u = parts[0].strip()
                            w_match = re.match(r'(\d+)', parts[1])
                            w = int(w_match.group(1)) if w_match else 0
                            if w > best_w and "marukyu-koyamaen.co.jp" in u:
                                best_w = w
                                best_url = u
                # srcset 沒有 → 取 src（要是線上 URL）
                if not best_url:
                    src = img.get("src") or img.get("data-src") or ""
                    if "marukyu-koyamaen.co.jp" in src:
                        best_url = src

                if not best_url:
                    continue

                key = self._marukyu_image_basename(best_url)
                if key and key not in seen:
                    seen.add(key)
                    result.append(best_url)
                if len(result) >= 9:
                    break

            if result:
                return result

        # ── Fallback：全頁找 wp-content/uploads 圖（可能含推薦圖）──
        all_imgs = re.findall(
            r'https?://www\.marukyu-koyamaen\.co\.jp/[^\s"\'<>]+wp-content/uploads/'
            r'[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
            html,
        )
        seen = set()
        main_key = self._marukyu_image_basename(main_image)
        if main_key:
            seen.add(main_key)

        bucket = {}  # base_name → 最大 URL
        for url in all_imgs:
            key = self._marukyu_image_basename(url)
            if not key or key in seen:
                continue
            m = re.search(r'-(\d+)x\d+\.', url)
            w = int(m.group(1)) if m else 99999
            if key not in bucket or bucket[key][0] < w:
                bucket[key] = (w, url)

        return [u for (_, u) in bucket.values()][:9]

    @staticmethod
    def _marukyu_image_basename(url: str) -> str:
        """從圖片 URL 抽出 basename（去掉尺寸後綴與副檔名）作為去重 key"""
        if not url:
            return ""
        m = re.search(r'/([^/]+?)(?:-\d+x\d+)?\.(?:jpg|jpeg|png|webp)', url, re.IGNORECASE)
        return m.group(1) if m else ""

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD AggregateOffer fallback
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _marukyu_jsonld_low_price(soup: BeautifulSoup) -> int | None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    lp = offers.get("lowPrice") or offers.get("price")
                    if lp:
                        try:
                            v = int(float(str(lp).replace(",", "")))
                            if _MIN_PRICE <= v <= _MAX_PRICE:
                                return v
                        except (ValueError, TypeError):
                            pass
        return None

    @staticmethod
    def _marukyu_price_fallback(html: str) -> int | None:
        """全頁 fallback：找最低 (税込) 価或 ¥ 数字"""
        prices = re.findall(r'¥\s*([\d,]+)', html)
        candidates = []
        for p in prices:
            try:
                v = int(p.replace(',', ''))
                if _MIN_PRICE <= v <= _MAX_PRICE:
                    candidates.append(v)
            except ValueError:
                pass
        return min(candidates) if candidates else None

    @staticmethod
    def _marukyu_to_int(value) -> int | None:
        if value is None:
            return None
        s = str(value).strip().replace(",", "").replace("，", "")
        if not s:
            return None
        try:
            v = int(float(s))
        except (ValueError, TypeError):
            return None
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
        return None

    # ─────────────────────────────────────────────────────────────────
    # 描述清理
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _marukyu_clean_description(desc: str) -> str:
        """清理 marukyu 描述：去掉抹茶限購聲明、英文段等雜訊"""
        # 去掉「※必ずお読みください...」之後的限購聲明
        desc = re.split(r'※必ずお読みください', desc)[0]
        desc = re.split(r'Due to matcha shortage', desc)[0]
        desc = re.sub(r'\s+', ' ', desc).strip()
        return desc[:1500]

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _marukyu_get_html(self, url: str) -> str:
        """用 SeleniumBase UC 抓 marukyu 頁面"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()

            try:
                driver.uc_open_with_reconnect(url, reconnect_time=5)
            except Exception:
                driver.get(url)
            time.sleep(2)

            best_html = ""
            best_score = 0

            for i in range(5):
                time.sleep(1.5)
                try:
                    html = driver.page_source
                except Exception:
                    continue

                score = 0
                if 'application/ld+json' in html: score += 3
                if 'variations_form' in html: score += 5
                if 'pa-sku' in html: score += 5
                if 'woocommerce-Price-amount' in html: score += 3
                if 'og:title' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 13 and len(html) > 30000:
                    print(f"[Marukyu][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 10000:
                print(f"[Marukyu][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Marukyu][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Marukyu] driver 失敗: {type(e).__name__}: {e}")
            return ""
