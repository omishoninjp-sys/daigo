"""
Supreme Japan (jp.supreme.com) 爬蟲 Mixin
jp.supreme.com 是 Shopify 店面，但 .json API 被封鎖。
策略：SeleniumBase UC driver 載頁後，從 JS 內嵌的 Shopify product data 抓資料。
"""
import re
import json
import time as _time

from scrapers.base import ProductInfo


class SupremeMixin:

    async def _scrape_supreme(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Supreme")

        html, product_json = await self._supreme_fetch(url)
        if not html and not product_json:
            print(f"[Supreme] ❌ 無法取得頁面: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 從 JS product JSON 取基本資料（標題、價格、尺寸）===
            if product_json:
                self._parse_supreme_from_json(product, product_json)

            # === 從 HTML thumbnail 補充所有顏色和圖片 ===
            # 結構：<li> 包含同一顏色的所有圖片 button
            # title="view Find God Football Jersey - Red (image 1 of 3)"
            color_imgs: dict[str, list[str]] = {}  # color -> [img1, img2, ...]

            thumb_list = soup.find("ul", attrs={"data-testid": "product-image-thumbnails"})
            if thumb_list:
                for li in thumb_list.find_all("li", recursive=False):
                    for btn in li.find_all("button"):
                        title_attr = btn.get("title", "")
                        # 解析 "view {product} - {Color} (image X of Y)"
                        m_color = re.search(r' - ([^(]+)\s*\(image', title_attr)
                        m_img   = re.search(r'\(image (\d+) of', title_attr)
                        if not m_color:
                            continue
                        color_name = m_color.group(1).strip()
                        img_el = btn.find("img")
                        if not img_el:
                            continue
                        src = img_el.get("src", "")
                        if not src:
                            continue
                        # 補全 protocol
                        if src.startswith("//"):
                            src = "https:" + src
                        # 去掉縮圖後綴 _90x / _480x 等，取原圖
                        src = re.sub(r'_\d+x(\.\w+)(\?|$)', r'\1\2', src)

                        color_imgs.setdefault(color_name, [])
                        if src not in color_imgs[color_name]:
                            color_imgs[color_name].append(src)

            print(f"[Supreme] 顏色圖片: { {c: len(imgs) for c, imgs in color_imgs.items()} }")

            # === 尺寸從 select 取（比 JSON 可靠）===
            sizes = []
            size_select = soup.find("select", attrs={"name": "size"})
            if size_select:
                for opt in size_select.find_all("option"):
                    val = opt.get_text(strip=True)
                    if val and val != "-- size --":
                        sizes.append(val)

            # === 重建 variants（顏色 × 尺寸）===
            if color_imgs and sizes:
                product.variants = []
                all_imgs = []
                color_first_img: dict[str, str] = {}

                for color, imgs in color_imgs.items():
                    color_first_img[color] = imgs[0] if imgs else product.image_url
                    for img in imgs:
                        if img not in all_imgs:
                            all_imgs.append(img)

                # 主圖用第一個顏色的第一張
                if all_imgs:
                    product.image_url = all_imgs[0]
                    product.extra_images = all_imgs[1:8]

                for color, imgs in color_imgs.items():
                    img_src = color_first_img.get(color, product.image_url)
                    for size in sizes:
                        product.variants.append({
                            "color": color,
                            "size":  size,
                            "sku":   f"sp-{color}-{size}".lower().replace(" ", "-"),
                            "price": product.price_jpy or 0,
                            "in_stock": True,
                            "image": img_src,
                        })

            elif not product_json:
                self._parse_supreme_from_html(product, html)

            # === 主圖 base64（Supreme CDN 需要 Referer）===
            if product.image_url:
                try:
                    import httpx, base64
                    async with httpx.AsyncClient(timeout=15) as client:
                        r = await client.get(
                            product.image_url,
                            headers={
                                "Referer": "https://jp.supreme.com/",
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            },
                            follow_redirects=True,
                        )
                        if r.status_code == 200:
                            product.image_base64 = base64.b64encode(r.content).decode()
                            print(f"[Supreme] ✅ 圖片 base64 ({len(product.image_base64)} chars)")
                        else:
                            print(f"[Supreme] ⚠️ 圖片下載失敗 {r.status_code}")
                except Exception as e:
                    print(f"[Supreme] ⚠️ 圖片 base64 失敗: {e}")

            print(
                f"[Supreme] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / images={1 + len(product.extra_images)}"
            )

        except Exception as e:
            import traceback
            print(f"[Supreme] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

            print(
                f"[Supreme] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / images={1 + len(product.extra_images)}"
            )

        except Exception as e:
            import traceback
            print(f"[Supreme] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    # ------------------------------------------------------------------ #

    def _parse_supreme_from_json(self, product: ProductInfo, pj: dict):
        """從 Shopify product JSON 物件解析"""
        product.title = pj.get("title", "")

        # 價格：Shopify 有時是整數日圓，有時是「分」單位（÷100）
        raw_variants = pj.get("variants", [])
        if raw_variants:
            price_raw = raw_variants[0].get("price", 0)
            try:
                price_val = int(float(str(price_raw)))
                # Shopify JS 注入的價格有時是「分」單位（cents）
                # 判斷：日本商品正常範圍 500〜500000 日圓
                # 若超過 500000，很可能是 cents → ÷ 100
                if price_val > 500000:
                    price_val = price_val // 100
                product.price_jpy = price_val
            except ValueError:
                pass

        # 圖片：image_id → src
        images = pj.get("images", [])
        img_id_to_src: dict[int, str] = {}
        img_srcs = []
        for img_obj in images:
            src = img_obj.get("src", "")
            if src:
                # protocol-relative URL の補完
                if src.startswith("//"):
                    src = "https:" + src
                img_id_to_src[img_obj.get("id", 0)] = src
                img_srcs.append(src)
                print(f"[Supreme] 圖片 URL: {src[:80]}")

        if img_srcs:
            product.image_url = img_srcs[0]
            product.extra_images = img_srcs[1:8]

        # color → 第一張對應圖片（透過 variant_ids 反查）
        color_img_map: dict[str, str] = {}
        for img_obj in images:
            raw_src = img_obj.get("src", "")
            fixed_src = ("https:" + raw_src) if raw_src.startswith("//") else raw_src
            for vid in img_obj.get("variant_ids", []):
                for v in raw_variants:
                    if v.get("id") == vid:
                        color = v.get("option1", "")
                        if color and color not in color_img_map:
                            color_img_map[color] = fixed_src

        # variants
        for v in raw_variants:
            color = v.get("option1", "") or ""
            size  = v.get("option2", "") or ""
            sku   = v.get("sku", f"sp-{color}-{size}".lower())
            img_src = (
                img_id_to_src.get(v.get("image_id", 0)) or
                color_img_map.get(color) or
                product.image_url
            )
            in_stock = v.get("available", True)
            product.variants.append({
                "color": color,
                "size":  size,
                "sku":   sku,
                "price": product.price_jpy or 0,
                "in_stock": in_stock,
                "image": img_src,
            })

    def _parse_supreme_from_html(self, product: ProductInfo, html: str):
        """Fallback HTML 解析（BeautifulSoup）"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # 標題
        for sel in [("h1", {}), ("h1", {"class": re.compile(r"product")})]:
            el = soup.find(sel[0], sel[1])
            if el and el.get_text(strip=True):
                product.title = el.get_text(strip=True)
                break

        # 價格
        for cls in [re.compile(r"price"), re.compile(r"Price")]:
            el = soup.find(class_=cls)
            if el:
                m = re.search(r'[¥￥]([\d,]+)', el.get_text())
                if m:
                    val = int(m.group(1).replace(',', ''))
                    if val >= 1000:
                        product.price_jpy = val
                        break

        # 圖片
        imgs = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if "cdn.shopify.com" in src and "/products/" in src and src not in imgs:
                imgs.append(src)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:8]

        # 尺寸（select / radio）
        sizes = []
        seen = set()
        for el in soup.find_all(["option", "input"]):
            val = el.get("value", "").strip()
            if val and val.upper() not in seen:
                size_pat = re.compile(r'^(XXS|XS|S|M|L|XL|2XL|3XL|ONE\s*SIZE|\d{2,3})$', re.I)
                if size_pat.match(val):
                    seen.add(val.upper())
                    sizes.append(val)

        for size in sizes:
            product.variants.append({
                "color": "",
                "size": size,
                "sku": f"sp-{size}".lower(),
                "price": product.price_jpy or 0,
                "in_stock": True,
                "image": product.image_url,
            })

    # ------------------------------------------------------------------ #

    async def _supreme_fetch(self, url: str):
        """
        SeleniumBase UC driver 載頁，
        同時嘗試用 JS 抽取 Shopify product JSON。
        回傳 (html, product_json_dict)
        """
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None, None

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
                    product_json = None
                    session_dead = False

                    for i in range(10):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        if i < 2 or len(html) < 5000:
                            continue

                        # 嘗試從 JS 抓 Shopify product data
                        try:
                            pj = driver.execute_script("""
                                // 方法1：ShopifyAnalytics
                                try {
                                    var p = window.ShopifyAnalytics &&
                                            window.ShopifyAnalytics.meta &&
                                            window.ShopifyAnalytics.meta.product;
                                    if (p && p.title) return p;
                                } catch(e) {}

                                // 方法2：__st (Shopify tracking)
                                try {
                                    var st = window.__st;
                                    if (st && st.p && st.p.title) return st.p;
                                } catch(e) {}

                                // 方法3：meta[type=application/json] 裡的 product JSON
                                try {
                                    var scripts = document.querySelectorAll(
                                        'script[type="application/json"]'
                                    );
                                    for (var s of scripts) {
                                        try {
                                            var d = JSON.parse(s.textContent);
                                            if (d && d.product && d.product.title) return d.product;
                                            if (d && d.title && d.variants) return d;
                                        } catch(e) {}
                                    }
                                } catch(e) {}

                                // 方法4：window.meta
                                try {
                                    if (window.meta && window.meta.product) return window.meta.product;
                                } catch(e) {}

                                return null;
                            """)
                            if pj and isinstance(pj, dict) and pj.get("title"):
                                product_json = pj
                                print(f"[Supreme] ✅ 取得 product JSON: {pj.get('title')}")
                                return html, product_json
                        except Exception:
                            pass

                        # 沒抓到 JSON 但頁面有商品資訊也接受
                        if '¥' in html and len(html) > 10000:
                            return html, None

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 5000:
                        return html, None

                    return None, None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[Supreme] fetch 失敗 attempt={attempt}: {e}")
                    return None, None

        return None, None
