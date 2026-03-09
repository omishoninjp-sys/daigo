"""
NEIGHBORHOOD 爬蟲 Mixin
neighborhood.jp 需要 JS 渲染，使用現有 SeleniumBase Chrome driver
"""
import re
import json
import time as _time

from scrapers.base import ProductInfo


class NeighborhoodMixin:

    async def _scrape_neighborhood(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            html = await self._neighborhood_fetch_html(url)
            if not html:
                print(f"[NEIGHBORHOOD] ❌ 無法取得 HTML")
                return product

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題 ──────────────────────────────────────
            h1 = soup.find("h1", class_="product-detail-inner-title")
            if h1:
                product.title = h1.get_text(strip=True)

            # ── 品牌 ──────────────────────────────────────
            vendor = soup.find("p", class_="product-detail-inner-vendor")
            product.brand = vendor.get_text(strip=True) if vendor else "NEIGHBORHOOD"

            # ── 價格 ──────────────────────────────────────
            price_el = soup.find("span", class_=re.compile(r'product-price'))
            if price_el:
                price_text = price_el.get_text(strip=True)
                m = re.search(r'[\d,]+', price_text.replace('¥', '').replace('￥', ''))
                if m:
                    try:
                        product.price_jpy = int(m.group(0).replace(',', ''))
                    except ValueError:
                        pass

            # ── product.json：圖片 + variants + 顏色圖片對應 ──
            imgs = []
            pj_data = None
            try:
                import httpx
                handle = url.rstrip("/").split("/")[-1].split("?")[0]
                json_url = f"https://www.neighborhood.jp/products/{handle}.json"
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(json_url, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200:
                        pj_data = r.json().get("product", {})
                        for img_obj in pj_data.get("images", []):
                            src = img_obj.get("src", "")
                            if src and src not in imgs:
                                imgs.append(src)
            except Exception as e:
                print(f"[NEIGHBORHOOD] product.json 失敗: {e}")

            # Fallback：從 HTML <img> 抓
            if not imgs:
                for img in soup.find_all("img"):
                    src = (img.get("src") or img.get("data-src") or "")
                    if "cdn.shopify.com" in src and "/products/" in src:
                        src = re.sub(r'_\d+x\d*(\.\w+)$', r'\1', src)
                        if src not in imgs:
                            imgs.append(src)

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:8]

            # ── 從 product.json 直接建 variants + color_img_map ──
            if pj_data:
                # image_id -> src
                img_id_to_src = {
                    img_obj["id"]: img_obj["src"]
                    for img_obj in pj_data.get("images", [])
                    if img_obj.get("id") and img_obj.get("src")
                }
                # variant_id -> color (option1)
                vid_to_color = {
                    v["id"]: v.get("option1", "")
                    for v in pj_data.get("variants", [])
                }
                # color -> 第一張對應圖片 src（從 images[].variant_ids 反查）
                color_img_map: dict[str, str] = {}
                for img_obj in pj_data.get("images", []):
                    for vid in img_obj.get("variant_ids", []):
                        color = vid_to_color.get(vid, "")
                        if color and color not in color_img_map:
                            color_img_map[color] = img_obj["src"]

                # 庫存：用 HTML 的 qua JSON，沒有就預設 True
                stock_map = {}
                for script in soup.find_all("script", type="application/json"):
                    raw = (script.string or "").strip()
                    if not raw.startswith("["):
                        continue
                    try:
                        items = json.loads(raw)
                        if isinstance(items, list) and items and "qua" in items[0]:
                            for item in items:
                                name = item.get("name", "")
                                try:
                                    stock_map[name] = int(item.get("qua", "0")) > 0
                                except (ValueError, TypeError):
                                    stock_map[name] = False
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue

                # 直接從 variants 建立，不需 HTML 解析顏色/尺寸
                for v in pj_data.get("variants", []):
                    color = v.get("option1", "")
                    size  = v.get("option2", "")
                    sku   = v.get("sku", f"nh-{color}-{size}".lower())
                    key   = f"{color} {size}".strip()
                    img_src = color_img_map.get(color) or product.image_url
                    # variant 自身的 image_id 優先
                    vid_img = img_id_to_src.get(v.get("image_id", 0))
                    if vid_img:
                        img_src = vid_img
                    product.variants.append({
                        "color": color,
                        "size":  size,
                        "sku":   sku,
                        "price": product.price_jpy or 0,
                        "in_stock": stock_map.get(key, True),
                        "image": img_src,
                    })
            else:
                # Fallback：HTML 解析（舊邏輯）
                stock_map = {}
                colors, sizes = [], []
                color_div = soup.find("div", id="colorOptions")
                if color_div:
                    for inp in color_div.find_all("input", type="radio"):
                        val = inp.get("value", "").strip()
                        if val and val not in colors:
                            colors.append(val)
                size_div = soup.find("div", id="sizeOptions")
                if size_div:
                    for inp in size_div.find_all("input", type="radio"):
                        val = inp.get("value", "").strip()
                        if val and val not in sizes:
                            sizes.append(val)
                if colors or sizes:
                    if not colors: colors = [""]
                    if not sizes:  sizes  = [""]
                    for color in colors:
                        for size in sizes:
                            product.variants.append({
                                "color": color, "size": size,
                                "sku": f"nh-{color}-{size}".lower().replace(" ", "-"),
                                "price": product.price_jpy or 0,
                                "in_stock": False,
                                "image": product.image_url,
                            })

            print(f"[NEIGHBORHOOD] ✅ {product.title} / ¥{product.price_jpy} / {len(product.variants)} variants")

        except Exception as e:
            print(f"[NEIGHBORHOOD] ❌ {type(e).__name__}: {e}")

        return product

    async def _neighborhood_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase Chrome driver 取得 JS 渲染後的 HTML"""
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
                        err_name = type(e).__name__
                        if "InvalidSession" in err_name or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    html = ""
                    session_dead = False
                    for i in range(8):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # 等待 JS 渲染完成（等到庫存 JSON 出現）
                        if i >= 1 and 'product-detail-inner-title' in html and len(html) > 5000:
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 5000:
                        return html

                    return None

                except Exception as e:
                    err_name = type(e).__name__
                    if "InvalidSession" in err_name and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None

        return None
