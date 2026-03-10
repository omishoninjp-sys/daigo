"""
Vermicular 官方 EC (shop.vermicular.jp) 爬蟲 Mixin
使用 SeleniumBase UC driver（Next.js SSR，需等 JS 渲染）

頁面結構（已從 HTML 驗證）：
  標題：h1（React rendered），fallback: <title> 解析
  價格：span.MainContents_priceArea_price_number__bwKjr → "¥18,590"
  當前顏色名稱：p.MainContents_colorArea_header__RMG1G > span:nth(2) → "オーク"
  當前尺寸：div.ChoiceBorderItem__current > div → "EGG & TOAST"
  圖片：img.MainImages_item_img__Q7Eqs[src]（完整 URL，含影片 poster 略過）
  顏色圖片：a.MainContents_colorArea_group_colorItem__O4ga1 > div > img → color swatch
  注意：各顏色 / 各尺寸皆為獨立 URL，每頁只有一個 variant
"""
import re
import time as _time

from scrapers.base import ProductInfo

BASE = "https://shop.vermicular.jp"


def _abs(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return BASE + (path if path.startswith("/") else "/" + path)


class VermicularMixin:

    async def _scrape_vermicular(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Vermicular")

        html = await self._vermicular_fetch_html(url)
        if not html:
            print(f"[Vermicular] ❌ 無法取得 HTML: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                title_tag = soup.find("title")
                if title_tag:
                    raw = title_tag.get_text(strip=True)
                    product.title = raw.split("|")[0].strip()

            # === 價格 ===
            price_el = soup.find("span", class_=re.compile(r"priceArea_price_number"))
            if price_el:
                m = re.search(r'[\d,]+', price_el.get_text())
                if m:
                    product.price_jpy = int(m.group(0).replace(',', ''))

            # === 當前尺寸 ===
            current_size_el = soup.find("div", class_=re.compile(r"ChoiceBorderItem__current"))
            current_size = ""
            if current_size_el:
                inner = current_size_el.find("div")
                current_size = (inner or current_size_el).get_text(strip=True).replace("&amp;", "&")

            # === 圖片（主圖 + sub）===
            imgs = []
            seen = set()
            for img in soup.find_all("img", class_=re.compile(r"MainImages_item_img")):
                src = img.get("src", "")
                if src and src not in seen and "/static/photo/item/" in src:
                    seen.add(src)
                    imgs.append(src)
            for video in soup.find_all("video", class_=re.compile(r"MainImages_item_img")):
                poster = video.get("poster", "")
                if poster and poster not in seen:
                    seen.add(poster)
                    imgs.append(poster)

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:8]

            # === 收集所有顏色 URL ===
            # a.MainContents_colorArea_group_colorItem__O4ga1[href]
            color_urls = []
            seen_hrefs = set()
            for a_el in soup.find_all("a", class_=re.compile(r"colorArea_group_colorItem")):
                href = a_el.get("href", "")
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    full = "https://shop.vermicular.jp" + href if href.startswith("/") else href
                    color_urls.append(full)

            print(f"[Vermicular] 發現 {len(color_urls)} 個顏色 URL: {color_urls}")

            # === 爬各顏色頁取得 color + swatch 圖 ===
            async def _scrape_color_page(color_url: str) -> dict | None:
                """回傳 {"color", "size", "sku", "image"} 或 None"""
                try:
                    c_html = await self._vermicular_fetch_html(color_url)
                    if not c_html:
                        return None
                    c_soup = BeautifulSoup(c_html, "html.parser")

                    # 顏色名
                    c_header = c_soup.find("p", class_=re.compile(r"colorArea_header"))
                    color_name = ""
                    if c_header:
                        spans = c_header.find_all("span")
                        if len(spans) >= 3:
                            color_name = spans[-1].get_text(strip=True)

                    # swatch 圖（當前被選中的 colorItem）
                    swatch_img = ""
                    for a in c_soup.find_all("a", class_=re.compile(r"colorArea_group_colorItem")):
                        href = a.get("href", "")
                        if color_url.rstrip("/").endswith(href.rstrip("/")):
                            img_el = a.find("img")
                            if img_el:
                                src = img_el.get("src", "")
                                swatch_img = _abs(src)
                            break

                    # 尺寸
                    size_el = c_soup.find("div", class_=re.compile(r"ChoiceBorderItem__current"))
                    size = ""
                    if size_el:
                        inner = size_el.find("div")
                        size = (inner or size_el).get_text(strip=True).replace("&amp;", "&")

                    sku = color_url.rstrip("/").rsplit("/", 1)[-1]
                    return {
                        "color": color_name,
                        "size": size or current_size,
                        "sku": sku,
                        "image": swatch_img,
                    }
                except Exception as e:
                    print(f"[Vermicular] 顏色頁爬取失敗 {color_url}: {e}")
                    return None

            # 如果有多個顏色 URL，逐一爬取
            if len(color_urls) > 1:
                for c_url in color_urls:
                    v = await _scrape_color_page(c_url)
                    if v:
                        product.variants.append({
                            "color":    v["color"],
                            "size":     v["size"],
                            "sku":      v["sku"],
                            "price":    product.price_jpy or 0,
                            "in_stock": True,
                            "image":    v["image"],
                        })
            else:
                # 只有一個顏色，用當前頁資料
                color_header = soup.find("p", class_=re.compile(r"colorArea_header"))
                current_color = ""
                if color_header:
                    spans = color_header.find_all("span")
                    if len(spans) >= 3:
                        current_color = spans[-1].get_text(strip=True)

                color_img = ""
                for a_el in soup.find_all("a", class_=re.compile(r"colorArea_group_colorItem")):
                    href = a_el.get("href", "")
                    if url.rstrip("/").endswith(href.rstrip("/")):
                        img_el = a_el.find("img")
                        if img_el:
                            color_img = _abs(img_el.get("src", ""))
                        break

                product.variants.append({
                    "color":    current_color,
                    "size":     current_size,
                    "sku":      url.rstrip("/").rsplit("/", 1)[-1],
                    "price":    product.price_jpy or 0,
                    "in_stock": True,
                    "image":    color_img or product.image_url,
                })

            print(
                f"[Vermicular] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / images={len(imgs)}"
            )

        except Exception as e:
            import traceback
            print(f"[Vermicular] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    async def _vermicular_fetch_html(self, url: str) -> str | None:
        """SeleniumBase UC driver 取得 Next.js 渲染後 HTML"""
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
                    for i in range(10):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # 等待 React hydrate：價格 span + 顏色 header 都出現
                        if (i >= 1
                                and "priceArea_price_number" in html
                                and "colorArea_header" in html
                                and len(html) > 10000):
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 10000:
                        return html
                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[Vermicular] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
