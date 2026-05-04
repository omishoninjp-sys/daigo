"""
SNKRDUNK (snkrdunk.com) 商品爬取 Mixin - スニダン

平台特性：
- 球鞋二手交易平台（StockX 日本版）
- 每個 size 是獨立 offer，價格各自不同（最低 ~ 最高可能差 5x）
- 無 color variant（一鞋一色）
- JSON-LD 含完整商品資訊（含 sub-offers 各 size 價格）

⚠️ 業務注意：
- 商品價格時時變動，客戶下單後可能價格已變或已售出
- 此 scraper 抓的是「當下最低價」快照
- 上架後不重新更新價格，下單時若搶不到 → 退款處理

URL 範例：
  https://snkrdunk.com/products/IO8765-100
  https://snkrdunk.com/zh-tw/products/IO8765-100  (不同語系也支援)
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


class SnkrdunkMixin:

    async def _scrape_snkrdunk(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="")
        clean_url = url.split("#")[0].strip()

        # 去語系前綴 (snkrdunk.com/zh-tw/... → snkrdunk.com/...)
        clean_url = re.sub(
            r'snkrdunk\.com/(?:zh-tw|zh-cn|en|ja|ko)/',
            'snkrdunk.com/',
            clean_url,
            flags=re.IGNORECASE,
        )

        # ── URL 標準化：所有 snkrdunk URL 都轉成 /products/<sku> 商品入口頁 ──
        # snkrdunk 有多種 URL 格式：
        #   /products/IO8765-100         ← 商品入口頁（含完整 JSON-LD，目標）
        #   /buy/IO8765-100/size/        ← 購買選 size 頁（沒商品資料）
        #   /buy/IO8765-100/size/26.5/   ← 購買特定 size 頁
        #   /sell/IO8765-100             ← 出售頁
        # 一律抽出 SKU 後重組為 /products/<sku>
        sku_match = re.search(
            r'snkrdunk\.com/(?:products|buy|sell)/([A-Z0-9_-]+)',
            clean_url,
            re.IGNORECASE,
        )
        if sku_match:
            sku = sku_match.group(1)
            standardized = f"https://snkrdunk.com/products/{sku}"
            if standardized != clean_url:
                print(f"[Snkrdunk] URL 標準化: {clean_url} → {standardized}")
                clean_url = standardized
                # 同時更新 source_url，讓商品頁顯示正確入口頁
                product.source_url = clean_url

        html = await asyncio.to_thread(self._snkrdunk_get_html, clean_url)
        if not html:
            print(f"[Snkrdunk] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 主資料源：JSON-LD Product schema ──
            ld = self._snkrdunk_find_product_jsonld(soup)
            if ld:
                self._snkrdunk_apply_jsonld(ld, product, clean_url)
            else:
                print(f"[Snkrdunk] ⚠️ 找不到 JSON-LD Product schema（可能仍在 size picker 頁面）")

            # ── 標題 fallback ──
            if not product.title:
                og = soup.find("meta", attrs={"property": "og:title"})
                if og and og.get("content"):
                    title = og["content"].strip()
                    # 跳過 size picker 頁的無意義標題
                    if "サイズを選択" in title or "選擇尺寸" in title or "Select Size" in title.lower():
                        print(f"[Snkrdunk] ⚠️ 仍在 size picker 頁，標題無效: {title}")
                    else:
                        # 去掉「｜スニダン」等尾巴
                        title = re.split(r'\s*[｜\|]\s*(?:スニダン|SNKRDUNK|スニーカーダンク)', title, flags=re.I)[0].strip()
                        product.title = title

            # ── 圖片 fallback：og:image 主圖 ──
            if not product.image_url:
                og_img = soup.find("meta", attrs={"property": "og:image"})
                if og_img and og_img.get("content"):
                    img = og_img["content"].strip()
                    # 排除 snkrdunk 通用 logo
                    if "og-image.png" not in img and "favicon" not in img:
                        product.image_url = img

            # 主圖 size 改成 large（snkrdunk CDN 支援 ?size=l）
            if product.image_url and 'cdn.snkrdunk.com' in product.image_url:
                product.image_url = re.sub(r'[?&]size=[a-z]+', '', product.image_url)
                sep = '&' if '?' in product.image_url else '?'
                product.image_url = f"{product.image_url}{sep}size=l"

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Snkrdunk] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"sizes={len(product.variants)} | sku={ld.get('sku') if ld else 'N/A'} | "
                    f"in_stock={product.in_stock}"
                )
            else:
                print(
                    f"[Snkrdunk] ⚠️ 部分資料缺失 ({title_short!r}) | "
                    f"price={product.price_jpy} | variants={len(product.variants)}"
                )

        except Exception as e:
            print(f"[Snkrdunk] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _snkrdunk_find_product_jsonld(soup: BeautifulSoup) -> dict | None:
        """找頁面內 JSON-LD 的 Product schema"""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            # snkrdunk LD 可能是單一物件或 @graph 陣列
            candidates = []
            if isinstance(data, dict):
                if data.get("@type") == "Product":
                    candidates.append(data)
                elif "@graph" in data and isinstance(data["@graph"], list):
                    for item in data["@graph"]:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            candidates.append(item)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        candidates.append(item)

            if candidates:
                return candidates[0]
        return None

    def _snkrdunk_apply_jsonld(self, ld: dict, product: ProductInfo, url: str) -> None:
        # 標題
        name = ld.get("name", "").strip()
        if name:
            product.title = name

        # 品牌
        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str):
            product.brand = brand.strip()
        else:
            product.brand = "Snkrdunk"

        # 描述
        desc = ld.get("description") or ""
        if desc:
            # snkrdunk 描述常含「スニダン」SEO 模板，截短
            product.description = str(desc).strip()[:1500]

        # 主圖（單張 webp）
        img = ld.get("image")
        if isinstance(img, str):
            product.image_url = img.strip()
        elif isinstance(img, list) and img:
            product.image_url = str(img[0]).strip()

        # SKU 識別
        sku = ld.get("sku") or ld.get("productID") or ld.get("mpn") or ""
        sku = str(sku).strip()

        # ── offers 處理 ──
        offers = ld.get("offers")
        if not isinstance(offers, dict):
            return

        # 整體庫存（AggregateOffer 級別）
        agg_avail = (offers.get("availability") or "").lower()
        if "outofstock" in agg_avail or "soldout" in agg_avail:
            product.in_stock = False

        # ⚠️ snkrdunk 標示的價格是「未稅價」（消費税 10% 未含）
        # 必須先 ×1.1 變成稅後價，再交給 shopify_client 套你的代購費率
        # （日本消費税 2026 年仍為 10%）
        TAX_MULTIPLIER = 1.10

        def _add_tax(raw_price: int | None) -> int | None:
            """未稅價 → 稅後價（×1.1，向上取整避免少收稅）"""
            if raw_price is None:
                return None
            import math
            return math.ceil(raw_price * TAX_MULTIPLIER)

        # 主價格用 lowPrice（最便宜的 size）→ 加稅
        v = self._snkrdunk_to_int(offers.get("lowPrice"))
        if v:
            product.price_jpy = _add_tax(v)

        # ── sub-offers 拆 size variants ──
        sub_offers = offers.get("offers") or []
        if not isinstance(sub_offers, list):
            return

        # 收集每個 size 的「最低價 + 庫存」(因為 snkrdunk 同 size 可能有多個賣家報價)
        # 注意：這裡的 price 還是未稅，比對最低價時用未稅比；產出 variant 時才加稅
        size_map: dict[str, dict] = {}  # size -> {price_pretax, in_stock}

        for o in sub_offers:
            if not isinstance(o, dict):
                continue
            size = (o.get("description") or "").strip()
            if not size:
                continue
            price = self._snkrdunk_to_int(o.get("price"))
            if not price:
                continue
            avail = (o.get("availability") or "").lower()
            in_stock = "outofstock" not in avail and "soldout" not in avail

            existing = size_map.get(size)
            if existing is None or price < existing["price_pretax"]:
                # 留每個 size 的最低價（未稅）
                size_map[size] = {"price_pretax": price, "in_stock": in_stock}
            elif price == existing["price_pretax"] and in_stock and not existing["in_stock"]:
                # 同價但有庫存的優先
                size_map[size] = {"price_pretax": price, "in_stock": in_stock}

        if not size_map:
            return

        # ── 排序 sizes（球鞋慣例：23.0, 23.5, 24.0, ... 由小到大）──
        def _size_key(s: str) -> tuple:
            # 抽數字部分排序，非數字（如 ONE SIZE）放最後
            m = re.match(r'^(\d+(?:\.\d+)?)', s)
            if m:
                return (0, float(m.group(1)), s)
            return (1, 0, s)

        sorted_sizes = sorted(size_map.keys(), key=_size_key)

        # ── 組 variants（每個 variant.price 都是「稅後價」，給 shopify_client 套費率用）──
        base = sku.lower().replace(" ", "-") or "snkrdunk"
        variants = []
        for size in sorted_sizes:
            info = size_map[size]
            sku_v = f"{base}-{size}".lower().replace(" ", "-").replace(".", "-").replace("/", "-")
            price_with_tax = _add_tax(info["price_pretax"])
            variants.append({
                "color": "",
                "size": size,
                "sku": sku_v,
                "price": price_with_tax,
                "in_stock": info["in_stock"],
                "image": product.image_url,
            })

        product.variants = variants

        # 印出稅前→稅後對照表（前 3 個 size）
        sample = sorted_sizes[:3]
        log_parts = []
        for s in sample:
            pretax = size_map[s]["price_pretax"]
            posttax = _add_tax(pretax)
            log_parts.append(f"{s}: ¥{pretax}→¥{posttax}")
        print(f"[Snkrdunk] 各 size 已加 10% 消費稅: {', '.join(log_parts)}...")

        # 整體 in_stock：任一 size 有貨即視為有貨
        any_in_stock = any(v["in_stock"] for v in variants)
        if not any_in_stock:
            product.in_stock = False
        elif product.in_stock:
            # AggregateOffer 沒明確 OutOfStock 時，按 variants 結果
            product.in_stock = True

    @staticmethod
    def _snkrdunk_to_int(value) -> int | None:
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
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
        return None

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _snkrdunk_get_html(self, url: str) -> str:
        """SeleniumBase UC 取得 HTML"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()

            try:
                driver.uc_open_with_reconnect(url, reconnect_time=6)
            except Exception as e:
                print(f"[Snkrdunk][fetch] uc_open 失敗 → 改用 driver.get: {type(e).__name__}: {e}")
                try:
                    driver.get(url)
                except Exception as e2:
                    print(f"[Snkrdunk][fetch] driver.get 失敗: {e2}")
                    return ""

            time.sleep(3)

            # 評分式等待：等到 JSON-LD 出現
            best_html = ""
            best_score = 0

            for i in range(8):
                time.sleep(2)
                try:
                    html = driver.page_source
                except Exception:
                    continue

                score = 0
                if 'application/ld+json' in html: score += 5
                if '"@type":"Product"' in html or '"@type": "Product"' in html: score += 5
                if '"offers"' in html: score += 3
                if '"lowPrice"' in html: score += 3

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 8 and len(html) > 5000:
                    print(f"[Snkrdunk][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1

            if best_html and len(best_html) > 5000:
                print(f"[Snkrdunk][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Snkrdunk][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Snkrdunk] driver 失敗: {type(e).__name__}: {e}")
            return ""
