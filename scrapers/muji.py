"""
MUJI JP 爬蟲 Mixin（v2 — 2026 站台改版後）
www.muji.com

平台特性（2026 改版）：
- 已從舊 Next.js Pages Router 改為 App Router + React Server Components。
- 舊的 `__NEXT_DATA__` / `__INITIAL_STATE__` / `window.PRODUCT` 全部消失，
  改為 RSC flight payload：`self.__next_f.push([1,"..."])`（多段，需串接解碼）。
- 商品頁仍為 SSR（搜尋引擎可索引），故主路徑用 httpx 取 HTML，**不需開瀏覽器**。

資料來源優先序：
1. RSC payload 的 product node（最完整）：
     "product":{"name":..,"price":{"basic":2290},"janCode":..,"colorName":..,"sizeName":..}
     "productImages":[{"media":{"src":".._org.jpg"}}]
     "colorVariations":[...]  "sizeVariations":[...]
2. JSON-LD offers.price（價格備援，改版後仍存在）
3. SeleniumBase UC（僅在 httpx 被 Cloudflare 擋時退守；rendered DOM 一樣含 RSC payload）

註：變體（colorVariations / sizeVariations）的「項目欄位」以本單品（空陣列）無法 100% 驗證，
    解析採防禦式：只在抓到 janCode 或 color/size 標籤時才產生變體，缺資料時安全退回空陣列。
    若要鎖定服飾變體欄位，請另丟一個 MUJI 服飾 URL 校正。
"""
import re
import json
import asyncio

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT, PROXY_URL
from scrapers.base import ProductInfo


_MIN_PRICE = 50
_MAX_PRICE = 1_000_000


class MujiMixin:

    async def _scrape_muji(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="無印良品")

        jan_code = self._muji_extract_jan(url)
        if not jan_code:
            print(f"[MUJI] ❌ 無法從 URL 提取 JAN: {url}")
            return product
        print(f"[MUJI] JAN: {jan_code}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }
        proxy_arg = PROXY_URL if PROXY_URL else None

        # ── 主路徑：httpx 取 SSR HTML ──
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:
            try:
                resp = await client.get(url, headers=headers)
                print(f"[MUJI] HTML: {resp.status_code}, {len(resp.text)} bytes")
                if resp.status_code == 200 and resp.text:
                    self._muji_parse_html(resp.text, product, jan_code)
            except Exception as e:
                print(f"[MUJI] httpx 錯誤: {type(e).__name__}: {e}")

        # ── 退守：httpx 沒拿到價格（被擋 / 非 200）才開瀏覽器 ──
        if not product.price_jpy:
            print(f"[MUJI] httpx 未取得價格，退守 SeleniumBase")
            try:
                await self._muji_chrome_fallback(url, product, jan_code)
            except Exception as e:
                print(f"[MUJI] Chrome UC 錯誤: {type(e).__name__}: {e}")

        # ── 圖片保底 ──
        if not product.image_url:
            product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

        if product.is_valid:
            print(
                f"[MUJI] ✅ {product.title[:50]!r} | ¥{product.price_jpy:,} | "
                f"images={1 + len(product.extra_images)} | variants={len(product.variants)}"
            )
        else:
            print(f"[MUJI] ⚠️ 部分資料缺失 | title={product.title[:30]!r} | price={product.price_jpy}")

        return product

    # ─────────────────────────────────────────────────────────────────
    # URL → JAN
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _muji_extract_jan(url: str) -> str:
        # /detail/4550583831981  或  /detail/<slug>/4550583831981
        m = re.search(r'/detail/(?:[^/?#]+/)?(\d{10,14})', url)
        if m:
            return m.group(1)
        # 保底：URL 內最後一段 13 碼數字（JAN）
        cands = re.findall(r'\d{13}', url)
        return cands[-1] if cands else ""

    # ─────────────────────────────────────────────────────────────────
    # 解析（RSC 優先 → JSON-LD 備援）
    # ─────────────────────────────────────────────────────────────────
    def _muji_parse_html(self, html: str, product: ProductInfo, jan_code: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")

        # 1) RSC flight payload
        big = self._muji_decode_rsc(html)
        if big:
            node = self._muji_find_product_node(big)
            if node:
                self._muji_apply_product_node(node, product)
            self._muji_apply_images(big, product)
            self._muji_apply_variants(big, product)

        # 2) JSON-LD（價格備援 + 描述）
        self._muji_apply_jsonld(soup, product)

        # 3) 標題保底：og:title / <title>
        if not product.title:
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                product.title = re.sub(r'\s*\|\s*無印良品\s*$', '', og["content"]).strip()
            else:
                tt = soup.find("title")
                if tt:
                    product.title = re.sub(r'\s*\|\s*無印良品\s*$', '', tt.get_text()).strip()

        return bool(product.price_jpy)

    # ── RSC 解碼 ──
    @staticmethod
    def _muji_decode_rsc(html: str) -> str:
        chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
        big = ""
        for c in chunks:
            try:
                big += json.loads('"' + c + '"')
            except Exception:
                pass
        return big

    @staticmethod
    def _muji_balanced_from(text: str, i: int):
        """從 text[i]（'{' 或 '['）起，抽出平衡括號的 JSON 子字串並 parse。"""
        if i >= len(text) or text[i] not in "{[":
            return None
        open_ch = text[i]
        close_ch = "}" if open_ch == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < len(text):
            ch = text[j]
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j + 1])
                        except Exception:
                            return None
            j += 1
        return None

    def _muji_find_product_node(self, big: str) -> dict | None:
        """找 "product":{...} 且含 janCode + price 的節點。"""
        for mm in re.finditer(r'"product"\s*:\s*\{', big):
            obj = self._muji_balanced_from(big, mm.end() - 1)
            if isinstance(obj, dict) and obj.get("janCode") and obj.get("price") is not None:
                return obj
        return None

    def _muji_extract_after(self, big: str, key: str):
        """找 "key": 之後緊接的平衡 {..} 或 [..]。"""
        i = big.find('"' + key + '"')
        if i == -1:
            return None
        c = big.find(":", i)
        if c == -1:
            return None
        c += 1
        while c < len(big) and big[c] in " \n\r\t":
            c += 1
        return self._muji_balanced_from(big, c)

    def _muji_apply_product_node(self, node: dict, product: ProductInfo) -> None:
        name = (node.get("name") or "").strip()
        if name:
            product.title = name

        price = node.get("price")
        if isinstance(price, dict):
            price = price.get("basic")
        v = self._muji_to_int(price)
        if v:
            product.price_jpy = v

    def _muji_apply_images(self, big: str, product: ProductInfo) -> None:
        imgs = self._muji_extract_after(big, "productImages")
        if not isinstance(imgs, list):
            return
        urls = []
        for it in imgs:
            if isinstance(it, dict):
                media = it.get("media") or {}
                src = media.get("src") if isinstance(media, dict) else ""
                if src and src not in urls:
                    urls.append(src)
        if urls:
            product.image_url = urls[0]
            product.extra_images = urls[1:9]

    def _muji_apply_variants(self, big: str, product: ProductInfo) -> None:
        """
        防禦式解析 colorVariations / sizeVariations。
        每項可能是同商品線的兄弟商品（各自 janCode + colorName/sizeName）。
        只在抓到 janCode 或 color/size 標籤時才產生變體；欄位不符就安全略過。
        """
        seen = set()
        for key in ("colorVariations", "sizeVariations"):
            arr = self._muji_extract_after(big, key)
            if not isinstance(arr, list):
                continue
            for v in arr:
                if not isinstance(v, dict):
                    continue
                jan = (v.get("janCode") or v.get("sku") or "").strip()
                color = (v.get("colorName") or v.get("color") or "").strip()
                size = (v.get("sizeName") or v.get("size") or "").strip()
                if not (jan or color or size):
                    continue
                dedup = jan or f"{color}|{size}"
                if dedup in seen:
                    continue
                seen.add(dedup)

                vp = v.get("price")
                if isinstance(vp, dict):
                    vp = vp.get("basic")
                vprice = self._muji_to_int(vp) or product.price_jpy or 0

                stock = v.get("stockStatus")
                in_stock = (stock != "OUT_OF_STOCK") if stock else True

                media = v.get("media") if isinstance(v.get("media"), dict) else {}
                product.variants.append({
                    "color": color,
                    "size": size,
                    "sku": jan or dedup,
                    "price": vprice,
                    "in_stock": in_stock,
                    "image": media.get("src", ""),
                })

        if product.variants:
            print(f"[MUJI] 變體 {len(product.variants)} 筆（自 RSC colorVariations/sizeVariations）")

    def _muji_apply_jsonld(self, soup: BeautifulSoup, product: ProductInfo) -> None:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(s.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(ld, list):
                ld = next((x for x in ld if isinstance(x, dict) and x.get("@type") == "Product"),
                          ld[0] if ld else {})
            if not isinstance(ld, dict) or ld.get("@type") != "Product":
                continue

            if not product.title and ld.get("name"):
                product.title = str(ld["name"]).strip()

            if not product.description and ld.get("description"):
                product.description = str(ld["description"]).strip()[:1500]

            if not product.price_jpy:
                offers = ld.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    v = self._muji_to_int(offers.get("price"))
                    if v:
                        product.price_jpy = v

            # 庫存
            offers = ld.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                avail = str(offers.get("availability", "")).lower()
                if "outofstock" in avail or "soldout" in avail:
                    product.in_stock = False
            return

    @staticmethod
    def _muji_to_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        s = str(value).strip().replace(",", "").replace("¥", "").replace("円", "")
        s = re.sub(r'\.\d+$', '', s)
        s = re.sub(r'[^0-9]', '', s)
        if not s:
            return None
        try:
            v = int(s)
        except ValueError:
            return None
        return v if _MIN_PRICE <= v <= _MAX_PRICE else None

    # ─────────────────────────────────────────────────────────────────
    # 退守：SeleniumBase UC（rendered DOM 一樣含 RSC payload）
    # ─────────────────────────────────────────────────────────────────
    async def _muji_chrome_fallback(self, url: str, product: ProductInfo, jan_code: str) -> bool:
        import time as _time

        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return False
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
                        has_data = ("__next_f" in html or "application/ld+json" in html
                                    or "ProductPrice" in html)
                        if i >= 1 and has_data:
                            break

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue
                    if not html or len(html) < 5000:
                        return False

                    self._muji_parse_html(html, product, jan_code)
                    return bool(product.price_jpy)

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[MUJI] Chrome fallback 失敗: {type(e).__name__}: {e}")
                    return False
            return False
