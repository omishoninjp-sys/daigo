"""
SHOPLIST (shop-list.com) 商品爬取 Mixin

平台特性：
- 日本平價時尚購物網站（CROOZ 子公司，4,000+ 品牌）
- UTF-8 編碼
- 主資料源 1：JSON-LD <script type="application/ld+json"> 的 Product schema
  - 完整 name / brand / price / availability / 多張原圖
- 主資料源 2：HTML 內 .p-product_item 區塊（variants 詳情）
  - color × size × 庫存 × 圖片 全部完整

URL 範例：
  https://shop-list.com/women/ulysses/UIF0738/
  https://shop-list.com/men/xxx/YY1234/

圖片 CDN: cdn.shop-list.com
  - __thum100__ / __thum200__: 縮圖（不要用）
  - __thum370__: 中圖
  - __basethum900__: 大圖（建議用這個）
  - 無 thum 前綴: 原圖
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


# 庫存狀態：日文 → in_stock 對照
# 「在庫あり」「在庫わずか」「予約注文」/「予約あり」「予約販売」→ 可下單
# 「完売」「売り切れ」「在庫なし」/「販売終了」 → 缺貨
_IN_STOCK_KEYWORDS = ["在庫あり", "在庫わずか", "予約注文", "予約あり", "予約販売", "入荷予定"]
_OUT_OF_STOCK_KEYWORDS = ["完売", "売り切れ", "在庫なし", "販売終了", "再入荷未定"]


class ShoplistMixin:

    async def _scrape_shoplist(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        clean_url = url.split("#")[0].strip()
        clean_url = re.sub(r'\?.*$', '', clean_url)  # 去 query

        if clean_url != url:
            product.source_url = clean_url
            print(f"[Shoplist] URL 標準化: {url} → {clean_url}")

        html = await asyncio.to_thread(self._shoplist_get_html, clean_url)
        if not html:
            print(f"[Shoplist] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 主資料源：JSON-LD Product schema ──
            ld = self._shoplist_find_product_jsonld(soup)
            if ld:
                self._shoplist_apply_jsonld(ld, product)
            else:
                print(f"[Shoplist] ⚠️ 找不到 JSON-LD Product schema，改用 fallback")
                self._shoplist_apply_html_fallback(soup, product)

            # ── 抓 variants（HTML 結構，JSON-LD 不含 variants）──
            variants = self._shoplist_extract_variants(soup, product.image_url)
            if variants:
                product.variants = variants
                colors = set(v["color"] for v in variants if v["color"])
                sizes = set(v["size"] for v in variants if v["size"])
                in_stock_count = sum(1 for v in variants if v["in_stock"])
                print(
                    f"[Shoplist] variants={len(variants)} (colors={len(colors)}, sizes={len(sizes)}, "
                    f"in_stock={in_stock_count})"
                )

                # 若所有 variants 缺貨，整體標記為缺貨
                if in_stock_count == 0:
                    product.in_stock = False
            else:
                print(f"[Shoplist] 無 variants（單品）")

            # ── 額外圖片：用 JSON-LD 提供的 image array 抓高解析版 ──
            if ld and isinstance(ld.get("image"), list):
                product.extra_images = self._shoplist_extract_extra_images(
                    ld["image"],
                    main_image=product.image_url,
                )

            # ── 升級主圖為高解析度 ──
            if product.image_url:
                product.image_url = self._shoplist_upgrade_image_url(product.image_url)

            # ── 描述清理（JSON-LD description 含太多分隔線）──
            if product.description:
                product.description = self._shoplist_clean_description(product.description)

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Shoplist] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"brand={product.brand!r} | images={1 + len(product.extra_images)} | "
                    f"variants={len(product.variants)}"
                )
            else:
                print(
                    f"[Shoplist] ⚠️ 部分資料缺失 ({title_short!r}) | "
                    f"price={product.price_jpy} | brand={product.brand!r}"
                )

        except Exception as e:
            print(f"[Shoplist] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD Product
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _shoplist_find_product_jsonld(soup: BeautifulSoup) -> dict | None:
        """找頁面內 JSON-LD 的 Product schema"""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

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

    def _shoplist_apply_jsonld(self, ld: dict, product: ProductInfo) -> None:
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
            product.description = desc[:2000]

        # 主圖：image 可能是 string 或 list
        img = ld.get("image")
        if isinstance(img, str) and img.strip():
            product.image_url = img.strip()
        elif isinstance(img, list) and img:
            product.image_url = str(img[0]).strip()

        # 價格與庫存
        offers = ld.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            v = self._shoplist_to_int(offers.get("price"))
            if v:
                product.price_jpy = v

            avail = (offers.get("availability") or "").lower()
            if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
                product.in_stock = False
            else:
                product.in_stock = True

    def _shoplist_apply_html_fallback(self, soup: BeautifulSoup, product: ProductInfo) -> None:
        """JSON-LD 失敗時用 og: meta + 頁面 HTML 抓"""
        # og:title
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
            # 移除「[品番：XXX]｜品牌名｜...通販ショップリスト」尾巴
            title = re.split(r'\s*\[品番[：:]', title)[0].strip()
            title = re.split(r'\s*[｜\|]', title)[0].strip()
            product.title = title

        # og:image
        og_img = soup.find("meta", attrs={"property": "og:image"})
        if og_img and og_img.get("content"):
            product.image_url = og_img["content"].strip()

        # og:description
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            product.description = og_desc["content"].strip()[:1000]

        # 從 data-pricedata 抓價格
        price_el = soup.find(attrs={"data-pricedata": True})
        if price_el:
            v = self._shoplist_to_int(price_el.get("data-pricedata"))
            if v:
                product.price_jpy = v

    @staticmethod
    def _shoplist_to_int(value) -> int | None:
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
    # Variants
    # ─────────────────────────────────────────────────────────────────
    def _shoplist_extract_variants(self, soup: BeautifulSoup, main_image: str = "") -> list:
        """
        從 HTML 內 .p-product_item li 抓所有 variants
        每個 li 內含：
        - <a data-axis-width-name="ブラック" data-axis-height-name="F" data-image="..." data-product_fku_id="..." />
        - <p>F<span> / </span><span>在庫あり</span></p>
        """
        variants = []

        for li in soup.select("li.p-product_item"):
            # 取 add-to-cart anchor 取屬性
            a = li.find("a", class_=lambda c: c and "p-product_add_to_cart_button" in c)
            if not a:
                continue

            color = (a.get("data-axis-width-name") or "").strip()
            size = (a.get("data-axis-height-name") or "").strip()
            sku = (a.get("data-product_fku_id") or "").strip()
            img = (a.get("data-image") or "").strip()

            # 取庫存狀態：<p>SIZE<span> / </span><span>STATUS</span></p>
            avail_text = ""
            for p in li.select("p"):
                txt = p.get_text(" ", strip=True)
                # 看是否含庫存關鍵字
                if any(kw in txt for kw in _IN_STOCK_KEYWORDS + _OUT_OF_STOCK_KEYWORDS):
                    avail_text = txt
                    break

            # 判斷 in_stock
            in_stock = True
            for kw in _OUT_OF_STOCK_KEYWORDS:
                if kw in avail_text:
                    in_stock = False
                    break
            else:
                if any(kw in avail_text for kw in _IN_STOCK_KEYWORDS):
                    in_stock = True

            # 升級圖片解析度
            if img:
                img = self._shoplist_upgrade_image_url(img)

            # 略過完全沒 size 也沒 color 的（例如壞掉的 li）
            if not color and not size:
                continue

            variants.append({
                "color": color,
                "size": size,
                "sku": sku.lower().replace("_", "-") if sku else "",
                "price": 0,  # shoplist 同一商品所有 variant 同價，用主價 fallback
                "in_stock": in_stock,
                "image": img if img != main_image else "",
            })

        return variants

    # ─────────────────────────────────────────────────────────────────
    # 圖片處理
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _shoplist_upgrade_image_url(url: str) -> str:
        """
        把 thum100 / thum200 / thum370 升級成 basethum900（高解析）
        例：.../shp/__thum100__/ulysses/image/X.jpg → .../shp/__basethum900__/ulysses/image/X.jpg
        """
        if not url or "cdn.shop-list.com" not in url:
            return url
        # 把 __thumXXX__ 都換成 __basethum900__
        return re.sub(
            r'/__thum\d+__/',
            '/__basethum900__/',
            url,
        )

    def _shoplist_extract_extra_images(self, image_list: list, main_image: str = "") -> list:
        """
        從 JSON-LD 的 image array 抓額外圖片
        - 升級成 basethum900
        - 去重（thum370 + basethum900 同一張）
        - 跳過主圖
        最多 9 張
        """
        result = []
        seen_basenames = set()

        # 取主圖檔名（不含尺寸前綴）
        main_base = ""
        if main_image:
            m = re.search(r'/([^/]+\.(?:jpg|jpeg|png|webp))$', main_image, re.IGNORECASE)
            if m:
                main_base = m.group(1)
                seen_basenames.add(main_base)

        for img in image_list:
            if not isinstance(img, str):
                continue
            url = self._shoplist_upgrade_image_url(img.strip())
            # 取檔名作為去重 key
            m = re.search(r'/([^/]+\.(?:jpg|jpeg|png|webp))$', url, re.IGNORECASE)
            if not m:
                continue
            basename = m.group(1)
            if basename in seen_basenames:
                continue
            seen_basenames.add(basename)
            result.append(url)
            if len(result) >= 9:
                break

        return result

    # ─────────────────────────────────────────────────────────────────
    # 描述清理
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _shoplist_clean_description(desc: str) -> str:
        """清掉 SHOPLIST 描述中的 ---- 分隔線、過長空格"""
        # 去掉「-----------」之類的長 dash line
        desc = re.sub(r'-{10,}', '\n', desc)
        # 把連續換行壓成兩個
        desc = re.sub(r'\n{3,}', '\n\n', desc)
        # 把 SHOPLIST 廣告字眼移除（如果有）
        desc = re.sub(r'※モニターにより.{0,150}', '', desc)
        return desc.strip()[:2000]

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _shoplist_get_html(self, url: str) -> str:
        """用 SeleniumBase UC 抓 SHOPLIST 頁面"""
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

                # 評分
                score = 0
                if 'application/ld+json' in html: score += 5
                if '"@type":"Product"' in html or '"@type": "Product"' in html: score += 5
                if 'p-product_item' in html: score += 5
                if 'data-axis-width-name' in html: score += 3
                if 'data-pricedata' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 15 and len(html) > 50000:
                    print(f"[Shoplist][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 5000:
                print(f"[Shoplist][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Shoplist][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Shoplist] driver 失敗: {type(e).__name__}: {e}")
            return ""
