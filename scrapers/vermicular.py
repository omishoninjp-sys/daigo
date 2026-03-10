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
            # fallback: <title> tag（格納 "商品名 | Vermicular"）
            if not product.title:
                title_tag = soup.find("title")
                if title_tag:
                    raw = title_tag.get_text(strip=True)
                    product.title = raw.split("|")[0].strip()

            # === 價格：span.MainContents_priceArea_price_number__bwKjr → "¥18,590" ===
            price_el = soup.find("span", class_=re.compile(r"priceArea_price_number"))
            if price_el:
                m = re.search(r'[\d,]+', price_el.get_text())
                if m:
                    product.price_jpy = int(m.group(0).replace(',', ''))

            # === 當前顏色名稱：p.MainContents_colorArea_header > span:last ===
            # HTML: <p><span>カラー</span><span>/</span><span>オーク</span></p>
            color_header = soup.find("p", class_=re.compile(r"colorArea_header"))
            current_color = ""
            if color_header:
                spans = color_header.find_all("span")
                if len(spans) >= 3:
                    current_color = spans[-1].get_text(strip=True)

            # === 當前尺寸：div.ChoiceBorderItem__current 裡的文字 ===
            current_size_el = soup.find("div", class_=re.compile(r"ChoiceBorderItem__current"))
            current_size = ""
            if current_size_el:
                inner = current_size_el.find("div")
                current_size = (inner or current_size_el).get_text(strip=True)
                # HTML encode 修正 EGG & TOAST
                current_size = current_size.replace("&amp;", "&")

            # === 圖片：img.MainImages_item_img（跳過 video 元素）===
            imgs = []
            seen = set()
            for img in soup.find_all("img", class_=re.compile(r"MainImages_item_img")):
                src = img.get("src", "")
                if src and src not in seen and "/static/photo/item/" in src:
                    seen.add(src)
                    imgs.append(src)
            # video poster 也加入
            for video in soup.find_all("video", class_=re.compile(r"MainImages_item_img")):
                poster = video.get("poster", "")
                if poster and poster not in seen:
                    seen.add(poster)
                    imgs.append(poster)

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:8]

            # === 顏色 swatch 圖（當前顏色） ===
            color_img = ""
            # 找被選中的（active）顏色 item
            for a_el in soup.find_all("a", class_=re.compile(r"colorArea_group_colorItem")):
                href = a_el.get("href", "")
                # 判斷是否為當前頁面的 URL（URL 末段相同）
                if url.rstrip("/").endswith(href.rstrip("/")):
                    img_el = a_el.find("img")
                    if img_el:
                        color_img = _abs(img_el.get("src", ""))
                    break
            # fallback：直接取第一個 color swatch
            if not color_img:
                first_swatch = soup.find("a", class_=re.compile(r"colorArea_group_colorItem"))
                if first_swatch:
                    img_el = first_swatch.find("img")
                    if img_el:
                        color_img = _abs(img_el.get("src", ""))

            # === 組合 variant（此頁只有一個） ===
            if current_color or current_size:
                product.variants.append({
                    "color":    current_color,
                    "size":     current_size,
                    "sku":      url.rstrip("/").rsplit("/", 1)[-1],  # e.g. "I00002783"
                    "price":    product.price_jpy or 0,
                    "in_stock": True,
                    "image":    color_img or product.image_url,
                })

            print(
                f"[Vermicular] ✅ {product.title} / ¥{product.price_jpy} / "
                f"color={current_color} size={current_size} / images={len(imgs)}"
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
