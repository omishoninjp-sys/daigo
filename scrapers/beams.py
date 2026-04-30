"""
BEAMS 爬蟲 Mixin
使用 httpx + BeautifulSoup，Chrome fallback

價格抓取防呆（2026-04 強化）：
- BEAMS 商品頁有「関連するアイテム」推薦商品區塊（位於 DOM 主商品價格之前）
- 推薦商品價格格式 ¥xxx（無税込）會干擾，導致主商品價格 ￥xxx（税込）抓錯
- 修法：
  1. 優先抓 JSON-LD offers.price
  2. 移除所有指向「其他商品」的 <li>（用當前 item_id 比對 href）
  3. 移除「関連するアイテム」「他のレーベル」「スタイリング」等 section
  4. 強化 (税込) 正則容錯
  5. fallback 改成「找最接近頁尾的價格」+ item_id 旁邊的價格
"""
import json
import re
import time as _time

import httpx
from bs4 import BeautifulSoup

from config import USER_AGENT
from scrapers.base import ProductInfo
from scrapers.driver import VALID_SIZES

# 預約・取り寄せも在庫あり扱い、「在庫なし」だけ缺貨
_OUT_OF_STOCK = {"在庫なし"}
_STOCK_PAT = r'(在庫あり|在庫なし|残りわずか|残り\d+点|取り寄せ|予約受付中|予約|入荷次第発送)'

# 推薦商品/非主商品區塊的標題關鍵字
_NOISE_SECTION_KEYWORDS = [
    "関連するアイテム",
    "他のレーベル",
    "他のレーベルの",
    "スタイリング・フォトログ",
    "スタイリング・",
    "もっと見る",
    "売り上げランキング",
    "BEAMSをフォロー",
    "最近見たアイテム",
    "おすすめのアイテム",
]


class BeamsMixin:

    async def _scrape_beams(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            # zh-CHT / zh-CN 版轉成日文版，確保價格格式是「税込」
            url = re.sub(r'/zh-CHT/', '/', url)
            url = re.sub(r'/zh-CN/', '/', url, flags=re.IGNORECASE)

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

            # ── 先取出 item_id（用於後面價格抓取與移除推薦商品）
            item_id_match = re.search(r'/(\d{10,})/?$', url.split('?')[0].rstrip("/"))
            item_id = item_id_match.group(1) if item_id_match else ""

            # ── 標題
            t = soup.find("title")
            if t:
                txt = t.get_text(strip=True)
                txt = re.split(r'通販[｜|]', txt)[0].strip()
                txt = re.sub(r'(?:[（(])[^）)]*(?:[）)])\s*$', '', txt).strip()
                txt = re.sub(r'(?:[（(])[ァ-ヶー\s・]+(?:[）)])', ' ', txt).strip()
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

            # ── 品牌（在 noise 移除之前先抓，否則 BEAMS link 會被誤刪）
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if re.match(r'^/[a-z]+$', href) and a.get_text(strip=True):
                    brand = a.get_text(strip=True)
                    if brand and "BEAMS" in brand.upper() and len(brand) < 40:
                        product.brand = brand
                        break
            if not product.brand:
                product.brand = "BEAMS"

            # ── 圖片（先抓，等等清推薦商品 li 會把推薦商品圖也清掉，但主商品圖在 popup/main 區塊不受影響）
            images = self._beams_collect_images(soup, item_id)

            # ── ★ 價格抓取主邏輯 ★
            product.price_jpy = self._beams_extract_price(soup, html, item_id)

            # ── 圖片整理（保留原邏輯）
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

            # ── 變體（顏色 / 尺寸 / 庫存）
            # 重新 get_text，因為價格抓取階段移除了推薦商品 li
            page_text = soup.get_text(" ", strip=True)

            colors = []
            for h4 in soup.find_all("h4"):
                text = h4.get_text(strip=True)
                if text and len(text) < 40 and re.match(r'^[A-Za-z0-9/\s\-\.]+$', text) and any(c.isupper() for c in text):
                    colors.append(text)

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

            page_explicitly_sold_out = bool(re.search(r'在庫なし|sold\s*out|SOLD\s*OUT', page_text))

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
                        elif size == "":
                            in_stock = not page_explicitly_sold_out
                            print(f"[BEAMS] 無 size 商品庫存判斷: {'售完' if not in_stock else '有庫存'}")
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

            print(f"[BEAMS] ✓ title={product.title!r}, price={product.price_jpy}, brand={product.brand}, variants={len(product.variants)}")

        except Exception as e:
            print(f"[BEAMS] ❌ 錯誤: {type(e).__name__}: {e}")

        return product

    # ─────────────────────────────────────────────────────────────────
    # ★ 價格抓取核心邏輯 ★
    # ─────────────────────────────────────────────────────────────────
    def _beams_extract_price(self, soup: BeautifulSoup, html: str, item_id: str) -> int | None:
        """
        BEAMS 價格抓取（多層 fallback）：
        1. JSON-LD offers.price
        2. <meta property="product:price:amount">
        3. [itemprop="price"]
        4. 移除推薦商品/non-product 區塊後，找 (税込) 附近的價格
        5. 用 item_id / 商品番号 周圍的價格定位
        """
        # ── 1. JSON-LD ──
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    offers = item.get("offers")
                    if isinstance(offers, dict):
                        p = offers.get("price") or offers.get("lowPrice")
                        v = self._beams_to_int(p)
                        if v:
                            print(f"[BEAMS] price from JSON-LD: {v}")
                            return v
                    elif isinstance(offers, list):
                        for off in offers:
                            if isinstance(off, dict):
                                v = self._beams_to_int(off.get("price"))
                                if v:
                                    print(f"[BEAMS] price from JSON-LD (list): {v}")
                                    return v
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue

        # ── 2. <meta property="product:price:amount"> ──
        for meta in soup.find_all("meta", attrs={"property": re.compile(r"price|amount", re.I)}):
            content = meta.get("content")
            v = self._beams_to_int(content)
            if v:
                print(f"[BEAMS] price from meta: {v}")
                return v

        # ── 3. [itemprop="price"] ──
        for tag in soup.find_all(attrs={"itemprop": "price"}):
            content = tag.get("content") or tag.get_text(strip=True)
            v = self._beams_to_int(content)
            if v:
                print(f"[BEAMS] price from itemprop: {v}")
                return v

        # ── 預處理：移除干擾元素 ──
        # 4a. 移除刪除線（劃掉的舊價）
        for tag in soup.find_all(["del", "s", "strike"]):
            tag.decompose()

        # 4b. 移除「指向其他商品」的 li/div（推薦商品）
        if item_id:
            removed_count = 0
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                # 是商品連結，但不是當前商品
                if "/item/beams/" in href and item_id not in href:
                    # 找最近的 li / 容器
                    container = a.find_parent("li") or a.find_parent("div", class_=re.compile(r"item|product", re.I))
                    if container:
                        container.decompose()
                        removed_count += 1
                    else:
                        # 沒有明確容器就只移除 a 本身
                        a.decompose()
                        removed_count += 1
            if removed_count:
                print(f"[BEAMS] 移除推薦商品連結: {removed_count} 個")

        # 4c. 移除已知 noise 標題的整個 section
        for tag in soup.find_all(["section", "div", "ul", "aside", "nav"]):
            text_start = tag.get_text(" ", strip=True)[:80]
            if any(kw in text_start for kw in _NOISE_SECTION_KEYWORDS):
                tag.decompose()

        page_text = soup.get_text(" ", strip=True)

        # ── 4. 找 (税込) 附近的價格（強化容錯）──
        tax_patterns = [
            # ￥xxx（税込）/ ¥xxx (税込) - 標準格式
            r'[￥¥]\s*([\d,]+)\s*[（(]\s*(?:税込|含稅)\s*[）)]',
            # ￥xxx 税込（無括號）
            r'[￥¥]\s*([\d,]+)\s*(?:税込|含稅)',
            # 税込 ￥xxx
            r'(?:税込|含稅)\s*[:：]?\s*[¥￥]\s*([\d,]+)',
            # 税込価格 ￥xxx
            r'税込価格\s*[:：]?\s*[¥￥]?\s*([\d,]+)',
        ]
        candidates = []
        for pat in tax_patterns:
            for m in re.finditer(pat, page_text):
                v = self._beams_to_int(m.group(1))
                if v and 100 < v < 500_000:
                    candidates.append(v)
            if candidates:
                # 一找到就停（按優先順序）
                break

        if candidates:
            # 同一 pattern 多筆 → 取 min（特賣價 ≤ 原價）
            v = min(candidates)
            print(f"[BEAMS] price from 税込 pattern: {v} (candidates={candidates})")
            return v

        # ── 5. fallback：用「商品番号」附近的價格定位 ──
        # 主商品旁邊一定會出現「商品番号：xx-xx-xxxx-xxx」
        if item_id:
            # 把 item_id 拆成商品番号格式：11050260060 → 11-05-0260-060
            sku_formatted = f"{item_id[0:2]}-{item_id[2:4]}-{item_id[4:8]}-{item_id[8:11]}" if len(item_id) >= 11 else item_id
            # 在商品番号前後 200 字內找價格
            for sku_pattern in [sku_formatted, item_id]:
                for m in re.finditer(re.escape(sku_pattern), page_text):
                    start = max(0, m.start() - 200)
                    end = min(len(page_text), m.end() + 200)
                    nearby = page_text[start:end]
                    price_m = re.search(r'[¥￥]\s*([\d,]+)', nearby)
                    if price_m:
                        v = self._beams_to_int(price_m.group(1))
                        if v and 100 < v < 500_000:
                            print(f"[BEAMS] price near 商品番号 {sku_pattern}: {v}")
                            return v

        # ── 6. 最後的 fallback：取 page_text 中所有 ¥xxx 的最後一個（主商品通常在頁尾）──
        all_yen = re.findall(r'[￥¥]\s*([\d,]+)', page_text)
        valid_prices = []
        for raw in all_yen:
            v = self._beams_to_int(raw)
            if v and 100 < v < 500_000:
                valid_prices.append(v)
        if valid_prices:
            # 取最後一個（通常是主商品在 popup 或頁尾的價格）
            v = valid_prices[-1]
            print(f"[BEAMS] price fallback last(¥): {v} (all={valid_prices})")
            return v

        print(f"[BEAMS] ⚠️ 無法抓到價格")
        return None

    @staticmethod
    def _beams_to_int(value) -> int | None:
        """'20,900' / '20900' / 20900 → 20900"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip()
            m = re.search(r'([\d,]+)', s)
            if not m:
                return None
            try:
                v = int(m.group(1).replace(",", ""))
            except ValueError:
                return None
        if 100 <= v <= 10_000_000:
            return v
        return None

    @staticmethod
    def _beams_collect_images(soup: BeautifulSoup, item_id: str) -> list[str]:
        """收集商品圖片（保留原邏輯）"""
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

        return list(img_by_filename.values())

    # ─────────────────────────────────────────────────────────────────
    # Chrome fallback（不變）
    # ─────────────────────────────────────────────────────────────────
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
