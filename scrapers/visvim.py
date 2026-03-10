"""
visvim / WMV / ICT 爬蟲 Mixin
shop.visvim.tv

頁面結構（已從 HTML 驗證）：
  標題：h1.detail-texts-name
  價格：div.detail-texts-price → "￥48,400"
  色彩 variant：table.detail-shoppingbag-list-color（每個 table = 一個顏色）
    顏色名稱：th > a.carousel-link-item > span
    顏色大圖：th > a > img[data-thumb]（相對路徑，需補 https://shop.visvim.tv）
    尺寸：td.detail-shoppingbag-list-size-no（"-" → ONE SIZE）
    SKU：button[id^='variation_cart_button_'] 的 ID 後綴
    在庫：button.block-variation-add-cart--btn 存在 = 有庫存
  商品全圖（carousel）：div.carousel-item img[src]（相對路徑）
  備援價格：<meta property="etm:goods_detail"> JSON → price 欄位
"""
import json
import re
import time as _time

from scrapers.base import ProductInfo

BASE = "https://shop.visvim.tv"


def _abs(path: str) -> str:
    """相對路徑補全為絕對 URL"""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("//"):
        return "https:" + path
    return BASE + (path if path.startswith("/") else "/" + path)


class VisvimMixin:

    async def _scrape_visvim(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="visvim")

        html = await self._visvim_fetch_html(url)
        if not html:
            print(f"[visvim] ❌ 無法取得 HTML: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            h1 = soup.find("h1", class_="detail-texts-name")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                # fallback: hidden input
                hidden = soup.find("input", id="hidden_goods_name")
                if hidden:
                    product.title = hidden.get("value", "").strip()

            # === 價格：div.detail-texts-price → "￥48,400" ===
            price_div = soup.find("div", class_="detail-texts-price")
            if price_div:
                raw = price_div.get_text(strip=True)
                m = re.search(r'[\d,]+', raw)
                if m:
                    product.price_jpy = int(m.group(0).replace(',', ''))

            # 備援：<meta property="etm:goods_detail"> JSON
            if not product.price_jpy:
                meta_el = soup.find("meta", property="etm:goods_detail")
                if meta_el:
                    try:
                        meta_data = json.loads(meta_el.get("content", "{}"))
                        p = meta_data.get("price", "")
                        if p:
                            product.price_jpy = int(str(p).replace(',', ''))
                    except Exception:
                        pass

            # === 全圖清單（carousel）：div.carousel-item img[src] ===
            carousel_imgs = []
            seen_imgs = set()
            for item in soup.find_all("div", class_="carousel-item"):
                img = item.find("img")
                if not img:
                    continue
                src = _abs(img.get("src", ""))
                if src and "/img/goods/" in src and src not in seen_imgs:
                    seen_imgs.add(src)
                    carousel_imgs.append(src)

            # === 顏色 Variants：table.detail-shoppingbag-list-color ===
            color_tables = soup.find_all("table", class_="detail-shoppingbag-list-color")

            for tbl in color_tables:
                # 顏色名稱
                span = tbl.find("th").find("span") if tbl.find("th") else None
                color_name = span.get_text(strip=True) if span else ""

                # 大圖：data-thumb 屬性（L 尺寸圖）
                color_img_el = tbl.find("img", {"data-thumb": True})
                if color_img_el:
                    color_img = _abs(color_img_el.get("data-thumb", ""))
                else:
                    # fallback：img src
                    color_img_el2 = tbl.find("th").find("img") if tbl.find("th") else None
                    color_img = _abs(color_img_el2.get("src", "")) if color_img_el2 else ""

                # 尺寸 rows
                size_rows = tbl.find_all("tr")
                if not size_rows:
                    continue

                for row in size_rows:
                    size_td = row.find("td", class_="detail-shoppingbag-list-size-no")
                    if not size_td:
                        continue
                    size_raw = size_td.get_text(strip=True)
                    size = "ONE SIZE" if size_raw in ("-", "－", "") else size_raw

                    # SKU：button id="variation_cart_button_{sku}"
                    btn = row.find("button", id=re.compile(r"^variation_cart_button_"))
                    sku = ""
                    in_stock = False
                    if btn:
                        btn_id = btn.get("id", "")
                        sku = btn_id.replace("variation_cart_button_", "")
                        in_stock = True  # button 存在 = 可加入購物車

                    product.variants.append({
                        "color":    color_name,
                        "size":     size,
                        "sku":      sku,
                        "price":    product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image":    color_img,
                    })

            # === 設定主圖 / extra_images ===
            if product.variants:
                # 主圖 = 第一個有圖的 variant
                first_img = next(
                    (v["image"] for v in product.variants if v["image"]), ""
                )
                product.image_url = first_img or (carousel_imgs[0] if carousel_imgs else "")
                # extra_images：carousel 圖（前 8 張，排除主圖）
                product.extra_images = [
                    img for img in carousel_imgs if img != product.image_url
                ][:8]
            elif carousel_imgs:
                product.image_url = carousel_imgs[0]
                product.extra_images = carousel_imgs[1:8]

            print(
                f"[visvim] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / "
                f"images={1 + len(product.extra_images)}"
            )

        except Exception as e:
            import traceback
            print(f"[visvim] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    async def _visvim_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase UC driver 取得 JS 渲染後 HTML"""
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
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # 等價格 div 和 color table 都出現
                        if (i >= 1
                                and "detail-texts-price" in html
                                and "detail-shoppingbag-list-color" in html
                                and len(html) > 8000):
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 8000:
                        return html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[visvim] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
