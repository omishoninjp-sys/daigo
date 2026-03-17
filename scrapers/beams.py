"""
BEAMS 爬蟲 Mixin
使用 httpx + BeautifulSoup，Chrome fallback
"""
import re
import time as _time

import httpx
from bs4 import BeautifulSoup

from config import USER_AGENT
from scrapers.base import ProductInfo
from scrapers.driver import VALID_SIZES

# 預約・取り寄せ也算有庫存，只有「在庫なし」才算缺貨
_OUT_OF_STOCK = {"在庫なし"}
_STOCK_PAT = r'(在庫あり|在庫なし|残りわずか|残り\d+点|取り寄せ|予約受付中|予約|入荷次第発送)'


class BeamsMixin:

    async def _scrape_beams(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.beams.co.jp/",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Cache-Control": "max-age=0",
                "Connection": "keep-alive",
            }

            html = None
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        html = resp.text
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
                pass

            if not html:
                try:
                    html = await self._beams_chrome_fallback(url)
                    if not html:
                        return product
                except Exception:
                    return product

            soup = BeautifulSoup(html, "html.parser")
            url_path = url.rstrip("/").split("/item/")[-1] if "/item/" in url else ""

            t = soup.find("title")
            if t:
                txt = t.get_text(strip=True)
                txt = re.split(r'通販[｜|]', txt)[0].strip()
                txt = re.sub(r'（[^）]*）\s*$', '', txt).strip()
                txt = re.sub(r'（[ァ-ヶー\s・]+）', ' ', txt).strip()
                txt = re.sub(r'\s+', ' ', txt)
                if txt:
                    product.title = txt

            if not product.title and url_path:
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if url_path in href and a.get_text(strip=True):
                        candidate = a.get_text(strip=True)
                        if len(candidate) > 3 and len(candidate) < 200:
                            product.title = candidate
                            break

            for a in soup.find_all("a"):
                href = a.get("href", "")
                if re.match(r'^/[a-z]+$', href) and a.get_text(strip=True):
                    brand = a.get_text(strip=True)
                    if brand and "BEAMS" in brand.upper() and len(brand) < 40:
                        product.brand = brand
                        break
            if not product.brand:
                product.brand = "BEAMS"

            page_text = soup.get_text(" ", strip=True)
            for pat in [r'[￥¥]\s*([\d,]+)\s*[（(]税込', r'[￥¥]\s*([\d,]+)']:
                pm = re.search(pat, page_text)
                if pm:
                    try:
                        p = int(pm.group(1).replace(",", ""))
                        if 100 < p < 500000:
                            product.price_jpy = p
                            break
                    except:
                        pass

            item_id_match = re.search(r'/(\d{10,})/?$', url.split('?')[0].rstrip("/"))
            item_id = item_id_match.group(1) if item_id_match else ""

            images = []
            img_by_filename = {}

            for img in soup.find_all("img"):
                for attr in ["data-original", "src", "data-src", "data-lazy"]:
                    src = img.get(attr, "")
                    if not src or "cdn.beams.co.jp/img/goods" not in src:
                        continue
                    if src.startswith("//"):
                        src = "https:" + src
                    if item_id and item_id not in src:
                        continue
                    filename = src.split("/")[-1]
                    if not re.match(r'\d+_[CD]_\d+\.jpg', filename):
                        continue

                    def _size_priority(u):
                        if "/O/" in u: return 4
                        if "/L/" in u: return 3
                        if "/S1/" in u: return 1
                        if "/S2/" in u: return 0
                        return 2

                    if filename not in img_by_filename or _size_priority(src) > _size_priority(img_by_filename[filename]):
                        img_by_filename[filename] = src

            images = list(img_by_filename.values())

            if images and all("/S1/" in img for img in images) and item_id:
                images = [img.replace("/S1/", "/O/") for img in images]

            if not images and item_id:
                base = f"https://cdn.beams.co.jp/img/goods/{item_id}/O/{item_id}"
                images = [f"{base}_C_1.jpg", f"{base}_C_2.jpg"]

            if images:
                color_imgs = sorted([i for i in images if "_C_" in i], key=lambda x: x.split("/")[-1])
                detail_imgs = sorted([i for i in images if "_D_" in i], key=lambda x: x.split("/")[-1])

                if color_imgs:
                    product.image_url = color_imgs[0]
                    product.extra_images = color_imgs[1:] + detail_imgs[:3]
                elif detail_imgs:
                    product.image_url = detail_imgs[0]
                    product.extra_images = detail_imgs[1:4]
                else:
                    product.image_url = images[0]
                    product.extra_images = images[1:4]

            colors = []

            for h4 in soup.find_all("h4"):
                text = h4.get_text(strip=True)
                if text and len(text) < 40 and re.match(r'^[A-Za-z0-9/\s\-\.]+$', text) and any(c.isupper() for c in text):
                    colors.append(text)

            # 全局 size -> in_stock 對應表
            # 只有「在庫なし」才算缺貨，予約受付中・取り寄せ・入荷次第発送 都算有庫存
            size_stock_map = {}
            for size, stock in re.findall(
                r'([A-Z0-9][A-Z0-9.]*)／' + _STOCK_PAT,
                page_text
            ):
                if size in VALID_SIZES and size not in size_stock_map:
                    size_stock_map[size] = stock not in _OUT_OF_STOCK
                    if stock not in _OUT_OF_STOCK and stock != "在庫あり":
                        print(f"[BEAMS] 特殊庫存狀態: {size} → {stock}（視為有庫存）")

            sizes = list(size_stock_map.keys())

            if colors or sizes:
                if not colors:
                    colors = [""]
                if not sizes:
                    sizes = [""]

                for color in colors:
                    color_section = re.search(
                        re.escape(color) + r'(.+?)(?:' + '|'.join(re.escape(c) for c in colors if c != color) + r'|店舗在庫|$)',
                        page_text, re.DOTALL
                    ) if color else None

                    section_text = color_section.group(1) if color_section else page_text

                    for size in sizes:
                        stock_match = re.search(
                            re.escape(size) + r'／' + _STOCK_PAT,
                            section_text
                        )
                        if stock_match:
                            in_stock = stock_match.group(1) not in _OUT_OF_STOCK
                        else:
                            in_stock = size_stock_map.get(size, False)

                        color_img = ""
                        if color:
                            idx = colors.index(color)
                            c_imgs = [i for i in images if "_C_" in i]
                            if idx < len(c_imgs):
                                color_img = c_imgs[idx]

                        label_parts = [p for p in [color, size] if p]
                        variant = {
                            "color": color,
                            "size": size,
                            "sku": f"beams-{'-'.join(label_parts)}" if label_parts else "beams",
                            "price": product.price_jpy or 0,
                            "in_stock": in_stock,
                            "image": color_img,
                        }
                        product.variants.append(variant)

        except Exception as e:
            print(f"[BEAMS] ❌ 錯誤: {type(e).__name__}: {e}")

        return product

    async def _beams_chrome_fallback(self, url: str) -> str | None:
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
                for i in range(6):
                    _time.sleep(2)
                    try:
                        html = driver.page_source
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            session_dead = True
                            break
                        continue

                    has_data = (
                        'cdn.beams.co.jp' in html or
                        '税込' in html or
                        'beams.co.jp' in html
                    )

                    if i >= 1 and has_data and len(html) > 10000:
                        try:
                            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 3);")
                            _time.sleep(1)
                            driver.execute_script("window.scrollTo(0, 0);")
                            _time.sleep(1)
                            html = driver.page_source
                        except:
                            pass
                        return html

                if session_dead:
                    self._driver = None
                    self._create_driver()
                    continue

                if html and len(html) > 10000:
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
