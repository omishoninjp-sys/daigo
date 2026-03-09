"""
GRL / grail.bz 爬蟲 Mixin
結構：ul.list-item-addcart > li
  每個 li = 一個顏色，包含：
  - img[id^="color_img_"] → 顏色圖片
  - p.txt-info             → 顏色名稱
  - select.size-select > option → 尺寸（"M/在庫あり" or "M/在庫なし"）
  - .btn-buy.add-cart-button 存在 → 在庫あり
"""
import re
import time as _time

from scrapers.base import ProductInfo


class GrailMixin:

    async def _scrape_grail(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="GRL")

        html = await self._grail_fetch_html(url)
        if not html:
            print(f"[GRL] ❌ 無法取得 HTML: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            for sel in [
                ("h1", {"class": re.compile(r"item-name|product-name|title", re.I)}),
                ("h1", {}),
                ("h2", {"class": re.compile(r"item-name|product-name", re.I)}),
            ]:
                el = soup.find(sel[0], sel[1])
                if el and el.get_text(strip=True):
                    product.title = el.get_text(strip=True)
                    break

            # === 價格 ===
            for cls in [
                re.compile(r"price", re.I),
                re.compile(r"item-price", re.I),
            ]:
                el = soup.find(class_=cls)
                if el:
                    m = re.search(r'[¥￥]([\d,]+)', el.get_text())
                    if m:
                        val = int(m.group(1).replace(',', ''))
                        if val >= 100:
                            product.price_jpy = val
                            break

            # Fallback 價格：掃全文找 ¥XXX
            if not product.price_jpy:
                for text in soup.stripped_strings:
                    m = re.match(r'^[¥￥]\s*([1-9][\d,]{1,})$', text.strip())
                    if m:
                        val = int(m.group(1).replace(',', ''))
                        if 100 <= val <= 50000:
                            product.price_jpy = val
                            break

            # === 主圖（商品頁大圖）===
            main_imgs = []
            # 常見主圖 selector
            for img_sel in [
                {"id": re.compile(r"main.?image|main.?img", re.I)},
                {"class": re.compile(r"main.?image|main.?img|item.?image", re.I)},
            ]:
                container = soup.find(attrs=img_sel)
                if container:
                    for img in container.find_all("img"):
                        src = img.get("src", "")
                        if src and "cdn.grail.bz" in src and src not in main_imgs:
                            main_imgs.append(src)

            # Fallback：從 cdn.grail.bz/images/goods/ 找大圖（/t/ 路徑轉換為原圖）
            if not main_imgs:
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if "cdn.grail.bz/images/goods" in src:
                        src = src.replace("/images/goods/t/", "/images/goods/")
                        if src not in main_imgs:
                            main_imgs.append(src)

            if main_imgs:
                product.image_url = main_imgs[0]
                product.extra_images = main_imgs[1:8]

            # === Variants：從 ul.list-item-addcart 解析 ===
            cart_list = soup.find("ul", class_=re.compile(r"list-item-addcart", re.I))
            if cart_list:
                for li in cart_list.find_all("li", recursive=False):
                    # 顏色圖片
                    color_img_el = li.find("img", id=re.compile(r"color_img_"))
                    color_img = color_img_el.get("src", "") if color_img_el else ""
                    # /t/ はサムネイルパス → 除去して原寸大取得
                    # 例: /images/goods/t/ac1909/ac1909_col_11.jpg
                    #  → /images/goods/ac1909/ac1909_col_11.jpg
                    if color_img:
                        color_img = color_img.replace("/images/goods/t/", "/images/goods/")
                    # 顏色名稱
                    color_name_el = li.find("p", class_="txt-info")
                    color_name = color_name_el.get_text(strip=True) if color_name_el else ""

                    # 在庫確認：有 .add-cart-button 就是在庫あり
                    has_stock_btn = li.find(class_=re.compile(r"add-cart-button"))

                    # 尺寸 select
                    size_select = li.find("select", class_="size-select")
                    if size_select:
                        for opt in size_select.find_all("option"):
                            opt_text = opt.get_text(strip=True)  # "M/在庫あり"
                            # 解析尺寸部分
                            size_part = opt_text.split("/")[0].strip()
                            in_stock = "在庫あり" in opt_text

                            sku = opt.get("value", f"grl-{color_name}-{size_part}".lower())
                            img_src = color_img if color_img else product.image_url

                            product.variants.append({
                                "color": color_name,
                                "size":  size_part,
                                "sku":   str(sku),
                                "price": product.price_jpy or 0,
                                "in_stock": in_stock,
                                "image": img_src,
                            })
                    else:
                        # 沒有 select，直接記顏色
                        in_stock = bool(has_stock_btn)
                        product.variants.append({
                            "color": color_name,
                            "size":  "",
                            "sku":   f"grl-{color_name}".lower().replace(" ", "-"),
                            "price": product.price_jpy or 0,
                            "in_stock": in_stock,
                            "image": color_img or product.image_url,
                        })

                # 主圖用第一個有圖的 variant 的顏色圖（如果主圖沒抓到）
                if not product.image_url and product.variants:
                    for v in product.variants:
                        if v.get("image"):
                            product.image_url = v["image"]
                            break

            print(
                f"[GRL] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants"
            )

        except Exception as e:
            import traceback
            print(f"[GRL] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    async def _grail_fetch_html(self, url: str) -> str | None:
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
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        if i >= 1 and "list-item-addcart" in html and len(html) > 5000:
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
                    print(f"[GRL] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
