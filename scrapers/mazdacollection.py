"""
MAZDA COLLECTION ONLINE SHOP (mazdacollection.jp) 商品爬取 Mixin

平台特性：
- マツダ（Mazda）官方周邊商店，使用「aishipR」電商系統
- UTF-8 編碼
- og: meta 只有網站通用資訊（無商品資訊）→ 不可用
- 主資料源：JSON-LD ProductGroup schema
  - name / brand / image(陣列) / hasVariant(各尺寸)
  - 每個 variant 有 sku / offers.price / offers.availability
- 標題用 <title>
- variant.name 格式為「商品名 + 尺寸」，尺寸字元被空格隔開
  例：「SP-MX5 WHITE/BLACK X S ( 2 2 . 5 c m )」需清理

URL 範例：
  https://www.mazdacollection.jp/i/O030
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


class MazdaCollectionMixin:

    async def _scrape_mazdacollection(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="MAZDA COLLECTION")
        clean_url = url.split("#")[0].split("?")[0].strip()

        html = await asyncio.to_thread(self._mazda_get_html, clean_url)
        if not html:
            print(f"[Mazda] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 主資料源：JSON-LD ProductGroup ──
            ld = self._mazda_find_productgroup_jsonld(soup)
            if ld:
                self._mazda_apply_jsonld(ld, product)
            else:
                print(f"[Mazda] ⚠️ 找不到 JSON-LD ProductGroup")

            # ── 標題：JSON-LD 沒給就用 <title> ──
            if not product.title:
                title_el = soup.find("title")
                if title_el:
                    product.title = title_el.get_text(strip=True)

            # ── 價格 fallback：頁面 data-retail-price ──
            if not product.price_jpy:
                price = self._mazda_extract_price_from_html(soup, html)
                if price:
                    product.price_jpy = price

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Mazda] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"brand={product.brand!r} | images={1 + len(product.extra_images)} | "
                    f"variants={len(product.variants)}"
                )
            else:
                print(
                    f"[Mazda] ⚠️ 部分資料缺失 ({title_short!r}) | price={product.price_jpy}"
                )

        except Exception as e:
            print(f"[Mazda] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD ProductGroup
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _mazda_find_productgroup_jsonld(soup: BeautifulSoup) -> dict | None:
        """找 JSON-LD 的 ProductGroup 或 Product schema"""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            candidates = []
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                t = item.get("@type", "")
                if t in ("ProductGroup", "Product"):
                    candidates.append(item)
                elif "@graph" in item and isinstance(item["@graph"], list):
                    for g in item["@graph"]:
                        if isinstance(g, dict) and g.get("@type") in ("ProductGroup", "Product"):
                            candidates.append(g)

            if candidates:
                # 優先 ProductGroup（含 variants）
                for c in candidates:
                    if c.get("@type") == "ProductGroup":
                        return c
                return candidates[0]
        return None

    def _mazda_apply_jsonld(self, ld: dict, product: ProductInfo) -> None:
        # 標題
        name = (ld.get("name") or "").strip()
        if name:
            product.title = name

        # 品牌
        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str) and brand.strip():
            product.brand = brand.strip()

        # 描述
        desc = (ld.get("description") or "").strip()
        if desc:
            product.description = desc[:1500]

        # 圖片：image 是陣列
        images = ld.get("image")
        if isinstance(images, str):
            images = [images]
        if isinstance(images, list) and images:
            product.image_url = str(images[0]).strip()
            # 額外圖（去重，最多 9 張）
            seen = {product.image_url}
            extra = []
            for img in images[1:]:
                u = str(img).strip()
                if u and u not in seen:
                    seen.add(u)
                    extra.append(u)
                if len(extra) >= 9:
                    break
            product.extra_images = extra

        # ── variants：hasVariant ──
        has_variant = ld.get("hasVariant", [])
        if isinstance(has_variant, list) and has_variant:
            base_name = name  # ProductGroup name 當作前綴
            variants = []
            prices_seen = []

            for hv in has_variant:
                if not isinstance(hv, dict):
                    continue
                v_name = (hv.get("name") or "").strip()
                sku = (hv.get("sku") or "").strip()

                offers = hv.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = 0
                in_stock = True
                if isinstance(offers, dict):
                    price = self._mazda_to_int(offers.get("price")) or 0
                    avail = (offers.get("availability") or "").lower()
                    if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
                        in_stock = False
                if price:
                    prices_seen.append(price)

                # 從 variant name 抽出尺寸（去掉商品名前綴）
                size = self._mazda_extract_size(v_name, base_name)

                variants.append({
                    "color": "",
                    "size": size,
                    "price": 0,  # 同商品同價，用主價 fallback
                    "in_stock": in_stock,
                    "image": "",
                    "sku": sku.lower() if sku else "",
                })

            product.variants = variants

            # 主價：取 variants 最常見價格
            if prices_seen:
                product.price_jpy = max(set(prices_seen), key=prices_seen.count)

            in_stock_count = sum(1 for v in variants if v["in_stock"])
            print(f"[Mazda] variants={len(variants)} (in_stock={in_stock_count})")
            if in_stock_count == 0:
                product.in_stock = False

        # 單品 Product（無 hasVariant）→ 從自身 offers 取價
        elif ld.get("@type") == "Product":
            offers = ld.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                v = self._mazda_to_int(offers.get("price"))
                if v:
                    product.price_jpy = v
                avail = (offers.get("availability") or "").lower()
                if "outofstock" in avail or "soldout" in avail:
                    product.in_stock = False

    @staticmethod
    def _mazda_extract_size(variant_name: str, base_name: str) -> str:
        """
        從 variant name 抽出尺寸並清理
        例：「SP-MX5 WHITE/BLACK X S ( 2 2 . 5 c m )」+ base「SP-MX5 WHITE/BLACK」
            → 「XS (22.5cm)」

        aishipR 的 variant name 字元被空格隔開，需壓縮
        """
        size = variant_name
        # 去掉商品名前綴
        if base_name and size.startswith(base_name):
            size = size[len(base_name):].strip()

        if not size:
            return ""

        # 壓縮：把「X S」「2 2 . 5 c m」這種被拆開的字元接回
        # 策略：移除「單一字元/數字 之間的空格」
        # 「X S ( 2 2 . 5 c m )」→「XS(22.5cm)」
        size = re.sub(r'(?<=\S)\s+(?=\S)', '', size)
        # 補回括號內外的可讀性：( → 空格(
        size = size.replace('(', ' (').strip()
        # 多重空白壓縮
        size = re.sub(r'\s+', ' ', size).strip()

        return size

    @staticmethod
    def _mazda_to_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip().replace(",", "").replace("，", "").replace("¥", "").replace("円", "")
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
    # 價格 fallback（HTML）
    # ─────────────────────────────────────────────────────────────────
    def _mazda_extract_price_from_html(self, soup: BeautifulSoup, html: str) -> int | None:
        """JSON-LD 沒價格時，從頁面 data-retail-price 或 (税込) 文字抓"""
        # data-retail-price="26500"
        m = re.search(r'data-retail-price="(\d+)"', html)
        if m:
            v = self._mazda_to_int(m.group(1))
            if v:
                return v

        # 「26,500円（税込）」
        tax_prices = re.findall(r'([\d,]+)\s*円\s*[（(]?\s*税込', html)
        candidates = [self._mazda_to_int(p) for p in tax_prices]
        candidates = [c for c in candidates if c]
        if candidates:
            return min(candidates)

        return None

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _mazda_get_html(self, url: str) -> str:
        """用 SeleniumBase UC 抓 MAZDA COLLECTION 頁面"""
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
                if 'application/ld+json' in html: score += 5
                if 'ProductGroup' in html or '"@type":"Product"' in html: score += 5
                if 'data-retail-price' in html: score += 3
                if '税込' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 12 and len(html) > 30000:
                    print(f"[Mazda][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 10000:
                print(f"[Mazda][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Mazda][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Mazda] driver 失敗: {type(e).__name__}: {e}")
            return ""
