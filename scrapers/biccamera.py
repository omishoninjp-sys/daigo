"""
Biccamera.com 爬蟲 Mixin
URL 格式：https://www.biccamera.com/bc/item/{id}/

HTML 結構：
- 品牌：p[itemprop=manufacturer] a
- 標題：h1[itemprop=name]
- 價格：strong[itemprop=price][content]
- 主圖：img#PROD-CURRENT-IMG[src]
- 縮圖：.bcs_gallery ul li img → A01~A07
- 顏色：.bcs_variationSliderPc.bcs_color a.colorType
    .bcs_title = 顏色名、.bcs_text = 價格、.bcs_subText = 庫存
- 尺寸：.bcs_variationSliderPc.bcs_capacity a.capacityType
    div.bcs_title > div = 尺寸名、.bcs_text = 價格、.bcs_subText = 庫存
"""

import re
import time as _time
from bs4 import BeautifulSoup
from scrapers.base import ProductInfo, normalize_price


IN_STOCK_TEXTS = {"在庫あり", "店在庫有り"}


class BiccameraMixin:

    async def _scrape_biccamera(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        html, driver_ref = self._biccamera_fetch_with_driver(url)
        if not html or len(html) < 5000:
            print(f"[Biccamera] ❌ HTML 太短: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # 品牌
            brand_el = soup.select_one('p[itemprop="manufacturer"] a')
            if brand_el:
                product.brand = brand_el.get_text(strip=True)

            # 標題
            h1 = soup.select_one('h1[itemprop="name"]')
            if h1:
                product.title = h1.get_text(strip=True)

            # 價格（schema.org content 屬性最可靠）
            price_el = soup.select_one('strong[itemprop="price"][content]')
            if price_el:
                product.price_jpy = normalize_price(price_el.get("content", ""))
            if not product.price_jpy:
                # fallback：.bcs_price strong
                p_el = soup.select_one("td.bcs_price strong")
                if p_el:
                    product.price_jpy = normalize_price(
                        re.sub(r"[^\d]", "", p_el.get_text())
                    )

            # 主圖
            main_img = soup.select_one("img#PROD-CURRENT-IMG")
            if main_img:
                src = main_img.get("src", "")
                # 移除縮放參數，取原圖
                product.image_url = re.sub(r"\?.*$", "", src)

            # 額外圖片：縮圖列（A01~A07）
            seen = {product.image_url}
            for img in soup.select(".bcs_gallery ul li img"):
                src = img.get("src", "")
                # 去掉縮放參數
                clean = re.sub(r"\?.*$", "", src)
                if clean and clean not in seen and "noimage" not in clean:
                    seen.add(clean)
                    product.extra_images.append(clean)
                    if len(product.extra_images) >= 8:
                        break

            # Variants：顏色 × 尺寸
            colors = self._biccamera_parse_color(soup)
            sizes  = self._biccamera_parse_size(soup)

            if colors and sizes:
                # 顏色和尺寸都有 → cross product（同款不同色 × 同色不同尺寸）
                # biccamera 實際上同一頁面的 color 和 size 是分開維度
                for color_info in colors:
                    for size_info in sizes:
                        # 庫存：兩者都有庫存才算有庫存
                        in_stock = color_info["in_stock"] and size_info["in_stock"]
                        price = color_info["price"] or size_info["price"] or product.price_jpy
                        product.variants.append({
                            "color":    color_info["name"],
                            "size":     size_info["name"],
                            "price":    price,
                            "in_stock": in_stock,
                            "image":    color_info["image"],
                        })
            elif colors:
                for c in colors:
                    product.variants.append({
                        "color":    c["name"],
                        "size":     "",
                        "price":    c["price"] or product.price_jpy,
                        "in_stock": c["in_stock"],
                        "image":    c["image"],
                    })
            elif sizes:
                for s in sizes:
                    product.variants.append({
                        "color":    "",
                        "size":     s["name"],
                        "price":    s["price"] or product.price_jpy,
                        "in_stock": s["in_stock"],
                        "image":    "",
                    })

            if product.variants:
                product.in_stock = any(v["in_stock"] for v in product.variants)
                # 最低價為商品起始價
                prices = [v["price"] for v in product.variants if v.get("price")]
                if prices:
                    product.price_jpy = min(prices)

            print(
                f"[Biccamera] ✅ {product.title[:40]} / ¥{product.price_jpy} / "
                f"colors={len(colors)} sizes={len(sizes)} variants={len(product.variants)}"
            )

            # 用 Selenium driver 下載圖片（繞過防盜連結）
            product.image_url, product.extra_images = self._biccamera_download_images(
                driver_ref, product.image_url, product.extra_images
            )
            # 顏色圖片也下載
            for v in product.variants:
                if v.get("image"):
                    v["image"] = self._biccamera_img_b64(driver_ref, v["image"]) or v["image"]

        except Exception as e:
            import traceback
            print(f"[Biccamera] ❌ 解析失敗: {e}")
            print(traceback.format_exc())

        return product

    def _biccamera_parse_color(self, soup) -> list[dict]:
        results = []
        color_area = soup.select_one("div.bcs_variationSliderPc.bcs_color")
        if not color_area:
            return results
        for a in color_area.select("a.colorType"):
            name_el = a.select_one(".bcs_title")
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            price_el = a.select_one(".bcs_text")
            price_str = price_el.get_text(strip=True) if price_el else ""
            price = normalize_price(re.sub(r"[^\d]", "", price_str)) if price_str else None

            stock_el = a.select_one(".bcs_subText")
            stock_text = stock_el.get_text(strip=True) if stock_el else ""
            in_stock = any(t in stock_text for t in IN_STOCK_TEXTS) or "出荷" in stock_text

            # 顏色縮圖圖片
            img_el = a.select_one("figure img")
            img = ""
            if img_el:
                img = re.sub(r"\?.*$", "", img_el.get("src", ""))

            results.append({"name": name, "price": price, "in_stock": in_stock, "image": img})
        return results

    def _biccamera_parse_size(self, soup) -> list[dict]:
        results = []
        size_area = soup.select_one("div.bcs_variationSliderPc.bcs_capacity")
        if not size_area:
            return results
        for a in size_area.select("a.capacityType"):
            # 尺寸名在 div.bcs_title > div 裡
            title_div = a.select_one("div.bcs_title div")
            name = title_div.get_text(strip=True) if title_div else ""
            if not name:
                # fallback
                title_el = a.select_one("div.bcs_title")
                name = title_el.get_text(strip=True) if title_el else ""
            if not name:
                continue

            price_el = a.select_one(".bcs_text")
            price_str = price_el.get_text(strip=True) if price_el else ""
            price = normalize_price(re.sub(r"[^\d]", "", price_str)) if price_str else None

            stock_el = a.select_one(".bcs_subText")
            stock_text = stock_el.get_text(strip=True) if stock_el else ""
            in_stock = any(t in stock_text for t in IN_STOCK_TEXTS) or "出荷" in stock_text

            results.append({"name": name, "price": price, "in_stock": in_stock})
        return results

    def _biccamera_fetch(self, url: str) -> str:
        """Selenium UC driver 抓取"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return ""
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
                    for i in range(12):
                        _time.sleep(3)
                        try:
                            html = driver.page_source
                        except Exception:
                            break
                        if (i >= 1
                                and len(html) > 5000):
                            return html
                    return html
                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[Biccamera] fetch 失敗: {e}")
                    return ""
        return ""

    def _biccamera_fetch_with_driver(self, url: str):
        """回傳 (html, driver) tuple，讓後續可用 driver 下載圖片"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return "", None
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
                    for i in range(12):
                        _time.sleep(3)
                        try:
                            html = driver.page_source
                        except Exception:
                            break
                        if (i >= 1
                                and len(html) > 5000):
                            return html, driver
                    return html, driver
                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[Biccamera] fetch 失敗: {e}")
                    return "", None
        return "", None

    def _biccamera_img_b64(self, driver, url: str) -> str | None:
        """用 Selenium driver 的 JS fetch 下載圖片並轉 base64"""
        if not driver or not url:
            return None
        try:
            js = """
            return await new Promise((resolve) => {
                fetch(arguments[0])
                    .then(r => r.blob())
                    .then(blob => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    })
                    .catch(() => resolve(null));
            });
            """
            result = driver.execute_script(js, url)
            return result if result else None
        except Exception as e:
            print(f"[Biccamera] JS 圖片下載失敗: {e}")
            return None

    def _biccamera_download_images(self, driver, main_url: str, extra_urls: list) -> tuple[str, list]:
        """下載主圖和額外圖片，成功則回傳 base64 data URL，失敗保留原 URL"""
        if not driver:
            return main_url, extra_urls

        # 主圖
        new_main = main_url
        if main_url:
            b64 = self._biccamera_img_b64(driver, main_url)
            if b64:
                new_main = f"data:image/jpeg;base64,{b64}"

        # 額外圖片（最多 8 張）
        new_extras = []
        for url in extra_urls[:8]:
            b64 = self._biccamera_img_b64(driver, url)
            if b64:
                new_extras.append(f"data:image/jpeg;base64,{b64}")
            else:
                new_extras.append(url)

        return new_main, new_extras
