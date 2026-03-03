"""
商品資訊爬取模組 v3.2
- Amazon.co.jp: requests + BeautifulSoup（快速、穩定）
- Uniqlo JP: 內部 API（超快、不需瀏覽器）
- MUJI JP: HTML + 內部 API（不需瀏覽器）
- ZOZOTOWN: undetected-chromedriver（繞過 Akamai）
- 其他網站: Playwright 無頭瀏覽器
"""
import re
import json
import asyncio
from urllib.parse import urlparse
from dataclasses import dataclass, asdict, field
from collections import Counter

import httpx
from bs4 import BeautifulSoup
from config import SCRAPE_TIMEOUT, USER_AGENT, ZOZO_SCRAPER_URL, PROXY_URL

# ============ ProductInfo ============

@dataclass
class ProductInfo:
    title: str = ""
    price_jpy: int | None = None
    image_url: str = ""
    description: str = ""
    source_url: str = ""
    brand: str = ""
    currency: str = "JPY"
    extra_images: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    image_base64: str = ""  # 當 Shopify 無法直接下載圖片時，用 base64 上傳
    is_adult: bool = False   # 成人商品標記

    def to_dict(self):
        d = asdict(self)
        d.pop("image_base64", None)  # 不回傳 base64 到前端（太大）
        return d

    @property
    def is_valid(self):
        return bool(self.title and self.price_jpy and self.price_jpy > 0)


# ============ Platform Detection ============

def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "zozo" in host:
        return "zozotown"
    if "amazon.co.jp" in host or "amazon.jp" in host or "amzn.asia" in host or "amzn.to" in host:
        return "amazon"
    if "uniqlo.com" in host:
        return "uniqlo"
    if "muji.com" in host:
        return "muji"
    if "beams.co.jp" in host:
        return "beams"
    if "rakuten.co.jp" in host:
        return "rakuten"
    return "generic"


# ============ Scraper ============

class Scraper:
    def __init__(self):
        import threading
        self._driver = None
        self._driver_lock = threading.Lock()
        self._driver_use_count = 0
        self._driver_max_uses = 50  # 每 50 次請求重建 driver
        self._proxy_verified = False

    def get_driver_status(self) -> dict:
        return {
            "alive": self._driver is not None,
            "use_count": self._driver_use_count,
            "max_uses": self._driver_max_uses,
        }

    def _create_driver(self):
        """建立或重建 Chrome driver"""
        import os, time as _time
        try:
            from seleniumbase import Driver
        except ImportError:
            print("[Driver] seleniumbase 未安裝")
            return None

        # 先關閉舊的
        if self._driver:
            try:
                self._driver.quit()
            except:
                pass
            self._driver = None

        proxy_arg = None
        if PROXY_URL:
            from urllib.parse import urlparse as _urlparse
            _pp = _urlparse(PROXY_URL)
            proxy_arg = f"{_pp.hostname}:{_pp.port}"
            print(f"[Driver] 建立 Chrome UC + proxy: {proxy_arg}")
        else:
            print(f"[Driver] 建立 Chrome UC（無 proxy）")

        self._driver = Driver(
            uc=True,
            headless=False,
            proxy=proxy_arg,
            locale_code='ja',
            chromium_arg='--lang=ja-JP,--disable-component-update,--disable-background-networking,--disable-sync,--no-first-run,--no-sandbox,--disable-dev-shm-usage',
        )
        self._driver_use_count = 0
        self._proxy_verified = False
        print(f"[Driver] ✅ Chrome 已啟動")
        return self._driver

    def _ensure_driver(self):
        """確保 driver 存活，必要時重建"""
        need_recreate = False

        if self._driver is None:
            need_recreate = True
        elif self._driver_use_count >= self._driver_max_uses:
            print(f"[Driver] 已使用 {self._driver_use_count} 次，重建中...")
            need_recreate = True
        else:
            # 測試 driver 是否還活著
            try:
                _ = self._driver.title
            except:
                print(f"[Driver] Chrome 已斷線，重建中...")
                need_recreate = True

        if need_recreate:
            self._create_driver()

        return self._driver

    # 成人商品關鍵字（日文 + 中文 + 英文）
    _ADULT_KEYWORDS = [
        # 日文
        "オナホ", "オナニー", "バイブ", "ローター", "アダルト",
        "大人のおもちゃ", "性具", "ラブグッズ", "コンドーム",
        "潤滑", "ローション", "電動マッサージ", "アダルトグッズ",
        "セクシーランジェリー", "セクシー下着", "ボディストッキング",
        "SM", "拘束", "エッチ", "18禁", "R-18", "R18",
        # 英文
        "masturbat", "vibrator", "dildo", "adult toy", "sex toy",
        "fleshlight", "onahole", "tenga", "lube ", "lubricant",
        "bondage", "fetish",
    ]

    def _detect_adult(self, product: ProductInfo) -> bool:
        """偵測是否為成人商品"""
        text = f"{product.title} {product.description} {product.source_url}".lower()
        for kw in self._ADULT_KEYWORDS:
            if kw.lower() in text:
                print(f"[Adult] ⚠️ 偵測到成人商品關鍵字: '{kw}'")
                return True
        return False

    async def scrape(self, url: str) -> ProductInfo:
        platform = detect_platform(url)

        if platform == "zozotown":
            product = await self._scrape_zozotown(url)
        elif platform == "amazon":
            product = await self._scrape_amazon(url)
        elif platform == "uniqlo":
            product = await self._scrape_uniqlo(url)
        elif platform == "muji":
            product = await self._scrape_muji(url)
        elif platform == "beams":
            product = await self._scrape_beams(url)
        else:
            product = await self._scrape_with_playwright(url)

        # 成人商品偵測
        if product.title and self._detect_adult(product):
            product.is_adult = True

        return product

    # ============================================================
    # Amazon.co.jp - requests（不需要瀏覽器，速度快）
    # ============================================================
    async def _scrape_amazon(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        try:
            # 短連結展開
            if "amzn.asia" in url or "amzn.to" in url:
                async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
                    resp = await c.head(url)
                    url = str(resp.url)
                    product.source_url = url

            # 驗證 ASIN
            am = re.search(r'/(?:dp|gp/product|gp/aw/d|ASIN)/([A-Z0-9]{10})', url)
            if not am:
                return product  # 不是商品頁

            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
                "Referer": "https://www.amazon.co.jp/",
                "Upgrade-Insecure-Requests": "1",
            }

            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=True, cookies={
                "session-id": "355-0769823-1641625",  # 假 session
                "i18n-prefs": "JPY",
                "lc-acbjp": "ja_JP",
                "sp-cdn": '"L5Z9:JP"',
            }) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    print(f"[Amazon] HTTP {resp.status_code}")
                    return product
                if "captcha" in str(resp.url).lower():
                    print(f"[Amazon] CAPTCHA 偵測到")
                    return product
                html = resp.text
                final_url = str(resp.url)

                # === 年齡確認頁面（成人商品） ===
                html_lower = html.lower()
                url_lower = final_url.lower()
                age_gate_indicators = [
                    "black_curtain", "age_verification", "年齢確認",
                    "18歳以上", "アダルト", "over18", "adult-verification",
                ]
                is_age_gate = any(ind in html_lower or ind in url_lower for ind in age_gate_indicators)
                # 也檢查：沒有 productTitle 但有確認類按鈕
                if not is_age_gate and "productTitle" not in html and ("はい" in html or "確認" in html):
                    is_age_gate = True

                if is_age_gate:
                    print(f"[Amazon] 偵測到年齡確認頁面 (url: {final_url[:80]})")
                    product.is_adult = True  # age gate 本身就是成人指標
                    age_soup = BeautifulSoup(html, "html.parser")

                    # 方法 1: 找確認表單 (form POST)
                    age_form = age_soup.find("form")
                    if age_form:
                        action = age_form.get("action", "")
                        if not action.startswith("http"):
                            action = f"https://www.amazon.co.jp{action}"
                        # 收集隱藏欄位
                        form_data = {}
                        for inp in age_form.find_all("input"):
                            name = inp.get("name")
                            if name:
                                form_data[name] = inp.get("value", "")
                        try:
                            resp2 = await client.post(action, data=form_data, headers=headers)
                            if resp2.status_code == 200:
                                html = resp2.text
                                print(f"[Amazon] 年齡確認 POST 成功")
                        except:
                            pass

                    # 方法 2: 找確認連結 (GET redirect)
                    if "productTitle" not in html:
                        for a in age_soup.find_all("a", href=True):
                            href = a.get("href", "")
                            text = a.get_text(strip=True)
                            if ("はい" in text or "18" in text or "yes" in text.lower()) and href:
                                if not href.startswith("http"):
                                    href = f"https://www.amazon.co.jp{href}"
                                try:
                                    resp3 = await client.get(href, headers=headers)
                                    if resp3.status_code == 200:
                                        html = resp3.text
                                        print(f"[Amazon] 年齡確認 GET 成功")
                                except:
                                    pass
                                break

                    # 方法 3: 直接用 ASIN 短連結重試（帶 session cookies）
                    asin = am.group(1)
                    if "productTitle" not in html:
                        try:
                            resp4 = await client.get(
                                f"https://www.amazon.co.jp/dp/{asin}",
                                headers=headers,
                            )
                            if resp4.status_code == 200 and "productTitle" in resp4.text:
                                html = resp4.text
                                print(f"[Amazon] 重試 dp/{asin} 成功")
                        except:
                            pass

                    if "productTitle" not in html:
                        print(f"[Amazon] ⚠️ 年齡確認繞過失敗，HTML 前200字: {html[:200]}")

            soup = BeautifulSoup(html, "html.parser")

            # 登入頁檢查
            if soup.find("form", {"name": "signIn"}) or soup.select_one("#ap_email"):
                return product

            # 標題
            el = soup.select_one("#productTitle")
            if el:
                product.title = el.get_text(strip=True)
            if not product.title:
                t = soup.find("title")
                if t:
                    txt = t.get_text(strip=True)
                    if "サインイン" not in txt and "Sign" not in txt:
                        product.title = txt

            # 品牌
            el = soup.select_one("#bylineInfo") or soup.select_one(".po-brand .po-break-word")
            if el:
                b = el.get_text(strip=True)
                b = re.sub(r'^(ブランド[：:]\s*|Brand[：:]\s*|Visit the |のストアを表示)', '', b)
                product.brand = re.sub(r'\s*(Store|ストア)$', '', b).strip()

            # 價格
            for sel in [
                "#corePrice_feature_div .a-offscreen",
                "span.a-price span.a-offscreen",
                ".a-price .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
            ]:
                el = soup.select_one(sel)
                if el:
                    pm = re.search(r'[\d,]+', el.get_text(strip=True).replace('￥', '').replace('¥', ''))
                    if pm:
                        product.price_jpy = int(pm.group().replace(',', ''))
                        break

            # 圖片
            hi = re.findall(r'"hiRes"\s*:\s*"(https?://[^"]+)"', html)
            if hi:
                all_imgs = list(dict.fromkeys(hi))[:10]
                if all_imgs:
                    product.image_url = all_imgs[0]
                    product.extra_images = all_imgs[1:]
            else:
                el = soup.select_one("#landingImage")
                if el:
                    src = el.get("data-old-hires") or el.get("src", "")
                    if src:
                        product.image_url = src
                for img in soup.select("#altImages img"):
                    src = img.get("src", "")
                    if src and "sprite" not in src and "grey-pixel" not in src:
                        lg = re.sub(r'\._[^.]*_\.', '.', src)
                        if lg != product.image_url and lg not in product.extra_images:
                            product.extra_images.append(lg)

            # 說明
            bullets = soup.select("#feature-bullets li span.a-list-item")
            if bullets:
                product.description = "\n".join(
                    [b.get_text(strip=True) for b in bullets if len(b.get_text(strip=True)) > 2]
                )[:500]

            print(f"[Amazon] ✅ {product.title[:40]} / ¥{product.price_jpy:,}" if product.price_jpy else f"[Amazon] ⚠️ 價格未找到")

        except Exception as e:
            print(f"[Amazon] ❌ 錯誤: {e}")

        return product

    # ============================================================
    # Uniqlo JP - 內部 API + HTML 解析（不需瀏覽器）
    # ============================================================
    async def _scrape_uniqlo(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="UNIQLO")

        # 從 URL 提取商品代碼：/products/E484664-000/00
        m = re.search(r'/products/(E?\d[\w-]+)', url)
        if not m:
            print(f"[Uniqlo] ❌ 無法從 URL 提取商品代碼: {url}")
            return product

        product_code = m.group(1)  # e.g. "E484664-000"
        product_id = re.sub(r'[^0-9]', '', product_code.split('-')[0])  # "484664"

        # 從 URL 提取 colorDisplayCode
        color_from_url = ""
        cm = re.search(r'colorDisplayCode=(\w+)', url)
        if cm:
            color_from_url = cm.group(1)

        print(f"[Uniqlo] 商品代碼: {product_code} (ID: {product_id}, color: {color_from_url})")

        # 瀏覽器 headers（模擬真實請求）
        browser_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:

            # === Step 1: 抓 HTML 頁面（取得 cookies 和內嵌資料）===
            cookies = {}
            html_text = ""
            try:
                print(f"[Uniqlo] Step 1: 抓 HTML 頁面...")
                resp = await client.get(url, headers=browser_headers)
                html_text = resp.text
                cookies = dict(resp.cookies)
                print(f"[Uniqlo] HTML: {resp.status_code}, {len(html_text)} bytes, cookies: {list(cookies.keys())[:5]}")

                # 解析 HTML（標題、圖片）
                self._parse_uniqlo_html(html_text, product_id, product)
            except Exception as e:
                print(f"[Uniqlo] HTML 抓取錯誤: {type(e).__name__}: {e}")

            # === Step 2: 從 HTML 找內嵌 JSON 資料 ===
            embedded_found = False
            if html_text:
                embedded_found = self._parse_uniqlo_embedded_json(html_text, product_code, product_id, product)
                if embedded_found and product.price_jpy and product.variants:
                    print(f"[Uniqlo] ✅ 內嵌 JSON 解析成功: {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
                    return product

            # === Step 3: 呼叫內部 Commerce API ===
            api_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
                "Referer": url,
                "Origin": "https://www.uniqlo.com",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "X-Requested-With": "XMLHttpRequest",
            }

            # 嘗試多個 API 路徑
            api_urls = [
                f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products?productIds={product_code}&withPrices=true&withStocks=true&withColors=true&withSizes=true&httpFailure=true",
                f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products?productIds={product_id}&withPrices=true&withStocks=true&withColors=true&withSizes=true",
                f"https://www.uniqlo.com/jp/api/commerce/v3/ja/products?productIds={product_code}",
            ]

            for api_url in api_urls:
                try:
                    print(f"[Uniqlo] Step 3: API {api_url[:90]}...")
                    resp = await client.get(api_url, headers=api_headers, cookies=cookies)
                    print(f"[Uniqlo] API response: {resp.status_code}, {len(resp.text)} bytes")

                    if resp.status_code == 200:
                        api_data = resp.json()

                        # 印出 top-level keys 方便 debug
                        print(f"[Uniqlo] API keys: {list(api_data.keys())[:8]}")

                        product = self._parse_uniqlo_api(api_data, product_code, product_id, product)
                        if product.price_jpy and product.variants:
                            print(f"[Uniqlo] ✅ API 完整解析: {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
                            return product
                        elif product.price_jpy:
                            print(f"[Uniqlo] API 取得價格 ¥{product.price_jpy:,} 但無 variants，繼續 fallback")
                            break  # 有價格了，跳出 API loop 去 Step 4 建 variants
                        else:
                            print(f"[Uniqlo] API 回傳但未找到價格")
                    elif resp.status_code == 403:
                        print(f"[Uniqlo] API 403 Forbidden - 可能被擋")
                    elif resp.status_code == 404:
                        print(f"[Uniqlo] API 404 - 路徑不對")
                    else:
                        # 印出 response body 前 200 字元
                        print(f"[Uniqlo] API {resp.status_code}: {resp.text[:200]}")

                except Exception as e:
                    print(f"[Uniqlo] API 錯誤: {type(e).__name__}: {e}")

            # === Step 4: Fallback - 至少用 HTML 資料 + URL 參數建構基本 variants ===
            if product.title and not product.variants:
                print(f"[Uniqlo] Step 4: 用 HTML 資料建構基本 variants")
                product = self._build_uniqlo_fallback_variants(product, product_id, color_from_url, html_text)

        if product.title:
            print(f"[Uniqlo] 最終結果: {product.title[:40]} / ¥{product.price_jpy or '?'} / {len(product.variants)} variants")
        else:
            print(f"[Uniqlo] ⚠️ 未取得資料")

        return product

    def _parse_uniqlo_embedded_json(self, html: str, product_code: str, product_id: str, product: ProductInfo) -> bool:
        """從 HTML 內嵌的 script 標籤找商品 JSON"""
        soup = BeautifulSoup(html, "html.parser")

        for script in soup.find_all("script"):
            text = script.string or ""
            if not text or len(text) < 100:
                continue

            # 方法 A: __NEXT_DATA__
            if "__NEXT_DATA__" in text or "window.__NEXT_DATA__" in text:
                try:
                    jm = re.search(r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</)', text, re.DOTALL)
                    if jm:
                        next_data = json.loads(jm.group(1))
                        props = next_data.get("props", {}).get("pageProps", {})
                        if props:
                            print(f"[Uniqlo] 找到 __NEXT_DATA__: keys={list(props.keys())[:5]}")
                            # 嘗試找商品資料
                            for key in ["product", "productDetail", "data", "initialData"]:
                                if key in props:
                                    self._parse_uniqlo_api({"result": {"items": {product_code: props[key]}}}, product_code, product_id, product)
                                    if product.price_jpy:
                                        return True
                except Exception as e:
                    print(f"[Uniqlo] __NEXT_DATA__ 解析錯誤: {e}")

            # 方法 B: window.__INITIAL_STATE__ 或其他全域變數
            for pattern in [r'__INITIAL_STATE__\s*=\s*({.+?})\s*;',
                           r'window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*;',
                           r'window\.PRODUCT_DATA\s*=\s*({.+?})\s*;']:
                try:
                    sm = re.search(pattern, text, re.DOTALL)
                    if sm:
                        state = json.loads(sm.group(1))
                        print(f"[Uniqlo] 找到全域狀態: keys={list(state.keys())[:5]}")
                        self._parse_uniqlo_api(state, product_code, product_id, product)
                        if product.price_jpy:
                            return True
                except:
                    pass

            # 方法 C: 直接找包含商品 ID 和價格的 JSON 片段
            if product_id in text and ("price" in text.lower() or "prices" in text.lower()):
                # 嘗試提取完整的 JSON 物件
                for jm in re.finditer(r'\{[^{}]*"' + re.escape(product_id) + r'"[^{}]*\}', text):
                    try:
                        chunk = json.loads(jm.group())
                        if "price" in str(chunk).lower():
                            self._parse_uniqlo_api(chunk, product_code, product_id, product)
                            if product.price_jpy:
                                return True
                    except:
                        pass

        return False

    def _parse_uniqlo_api(self, data: dict, product_code: str, product_id: str, product: ProductInfo) -> ProductInfo:
        """解析 Uniqlo 內部 API 回傳的 JSON"""
        # 嘗試找 items/products 字典
        items = {}
        if "result" in data:
            result = data["result"]
            items = result.get("items", {}) or result.get("products", {}) or {}
            # 有些 API 版本 items 是 list
            if isinstance(items, list):
                items = {str(i.get("productId", i.get("id", idx))): i for idx, i in enumerate(items) if isinstance(i, dict)}
        elif "items" in data:
            items = data["items"]
        elif "products" in data:
            items = data["products"]

        # 找到商品資料
        prod = items.get(product_code) or items.get(product_id)
        if not prod:
            # 嘗試部分匹配
            for k, v in items.items():
                if product_id in str(k):
                    prod = v
                    break
        if not prod and items:
            prod = next(iter(items.values()))
        if not prod:
            if "name" in data or "productName" in data or "productId" in data:
                prod = data

        if not prod:
            print(f"[Uniqlo] API 回傳中找不到商品: keys={list(data.keys())[:5]}")
            return product

        # ---- 商品名稱 ----
        name = prod.get("name") or prod.get("productName") or prod.get("title") or ""
        if name:
            product.title = name

        # ---- 價格（嘗試所有可能的結構）----
        price = self._extract_uniqlo_price(prod)
        if price and price > 0:
            product.price_jpy = price

        # ---- 圖片 ----
        images = prod.get("images", {}) or {}
        img_urls = []

        # images 可能是 dict（main/sub 結構）或 list
        if isinstance(images, dict):
            for img_key in ["main", "sub", "chip", "swatch"]:
                img_list = images.get(img_key, []) or []
                if isinstance(img_list, dict):
                    img_list = list(img_list.values())
                for img in img_list:
                    u = ""
                    if isinstance(img, str):
                        u = img
                    elif isinstance(img, dict):
                        u = img.get("url") or img.get("image") or img.get("src") or ""
                    if u and u not in img_urls:
                        img_urls.append(u)
        elif isinstance(images, list):
            for img in images:
                u = img.get("url", "") if isinstance(img, dict) else str(img)
                if u:
                    img_urls.append(u)

        # Fallback 圖片
        if not img_urls:
            img_urls.append(f"https://image.uniqlo.com/UQ/ST3/jp/imagesgoods/{product_id}/item/jpgoods_69_{product_id}_3x4.jpg?width=600")

        if img_urls and not product.image_url:
            product.image_url = img_urls[0]
        if len(img_urls) > 1 and not product.extra_images:
            product.extra_images = img_urls[1:9]

        # ---- 顏色和尺寸 → Variants ----
        colors = prod.get("colors", {}) or {}
        sizes = prod.get("sizes", {}) or {}
        l2s = prod.get("l2s", []) or prod.get("stocks", []) or []

        variants = []

        # 結構化 colors + sizes
        if isinstance(colors, dict) and isinstance(sizes, dict) and colors and sizes:
            for color_code, color_info in colors.items():
                color_name = ""
                color_img = ""
                if isinstance(color_info, dict):
                    color_name = color_info.get("displayColorName") or color_info.get("name") or color_code
                    # 圖片
                    ci = color_info.get("image")
                    if isinstance(ci, dict):
                        color_img = ci.get("url") or ci.get("src") or ""
                    elif isinstance(ci, str):
                        color_img = ci
                    if not color_img:
                        color_img = f"https://image.uniqlo.com/UQ/ST3/AsianCommon/imagesgoods/{product_id}/chip/goods_{color_code}_{product_id}_chip.jpg"
                else:
                    color_name = str(color_info)

                for size_code, size_info in sizes.items():
                    size_name = ""
                    if isinstance(size_info, dict):
                        size_name = size_info.get("displaySizeName") or size_info.get("name") or size_code
                    else:
                        size_name = str(size_info)

                    # 從 l2s 找庫存
                    in_stock = True
                    sku = f"{product_id}-{color_code}-{size_code}"
                    if isinstance(l2s, list):
                        for s in l2s:
                            if isinstance(s, dict):
                                sc = str(s.get("color", {}).get("displayCode", "")) if isinstance(s.get("color"), dict) else str(s.get("colorCode", ""))
                                ss = str(s.get("size", {}).get("displayCode", "")) if isinstance(s.get("size"), dict) else str(s.get("sizeCode", ""))
                                if sc == color_code and ss == size_code:
                                    in_stock = s.get("stock", {}).get("statusCode", "") != "OUT_OF_STOCK" if isinstance(s.get("stock"), dict) else True
                                    sku = s.get("l2Id", sku)
                                    break

                    variants.append({
                        "color": color_name,
                        "size": size_name,
                        "sku": sku,
                        "price": product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image": color_img,
                    })

        # l2s 直接建構 variants（如果上面沒成功）
        if not variants and isinstance(l2s, list) and l2s:
            for s in l2s:
                if not isinstance(s, dict):
                    continue
                color = ""
                size = ""
                if isinstance(s.get("color"), dict):
                    color = s["color"].get("displayColorName", "") or s["color"].get("name", "")
                else:
                    color = s.get("colorDisplayName", "") or s.get("color", "")
                if isinstance(s.get("size"), dict):
                    size = s["size"].get("displaySizeName", "") or s["size"].get("name", "")
                else:
                    size = s.get("sizeDisplayName", "") or s.get("size", "")

                in_stock = True
                if isinstance(s.get("stock"), dict):
                    in_stock = s["stock"].get("statusCode", "") != "OUT_OF_STOCK"

                variants.append({
                    "color": color,
                    "size": size,
                    "sku": s.get("l2Id", "") or s.get("sku", ""),
                    "price": product.price_jpy or 0,
                    "in_stock": in_stock,
                    "image": "",
                })

        if variants:
            product.variants = variants

        return product

    def _extract_uniqlo_price(self, prod: dict) -> int | None:
        """從 Uniqlo 商品資料中提取價格（嘗試所有可能的結構）"""
        # 直接欄位
        for key in ["minPrice", "price", "retailPrice", "salePrice", "originPrice"]:
            v = prod.get(key)
            if v and isinstance(v, (int, float)) and v > 0:
                return int(v)

        # prices 結構
        prices = prod.get("prices") or prod.get("price") or {}
        if isinstance(prices, (int, float)) and prices > 0:
            return int(prices)

        if isinstance(prices, dict):
            # { "base": { "value": 5990 }, "promo": { "value": 5990 } }
            for sub_key in ["promo", "base", "current", "sale", "original"]:
                sub = prices.get(sub_key)
                if isinstance(sub, dict):
                    v = sub.get("value") or sub.get("price") or sub.get("amount")
                    if v and float(v) > 0:
                        return int(float(v))
                elif isinstance(sub, (int, float)) and sub > 0:
                    return int(sub)

            # 直接在 prices 裡
            v = prices.get("value") or prices.get("price") or prices.get("amount")
            if v and float(v) > 0:
                return int(float(v))

        return None

    def _build_uniqlo_fallback_variants(self, product: ProductInfo, product_id: str, color_from_url: str, html: str) -> ProductInfo:
        """當 API 全失敗時，從 HTML 文字提取尺寸建構基本 variants"""
        # 從 HTML 中找到顯示的尺寸
        sizes_found = []
        size_pattern = r'\b(XS|S|M|L|XL|XXL|3XL|4XL)\b'
        soup = BeautifulSoup(html, "html.parser")

        # 找有 size 相關文字的區塊
        text = soup.get_text(" ", strip=True)

        # Uniqlo 固定的常見尺寸
        # 從 HTML 文字抓：「サイズ: 男女兼用 M  XS S M L XL XXL 3XL」
        size_section = re.search(r'サイズ[：:]\s*(?:男女兼用|レディス|メンズ)?\s*\w+\s+((?:(?:XS|S|M|L|XL|XXL|3XL|4XL)\s*)+)', text)
        if size_section:
            sizes_found = re.findall(size_pattern, size_section.group(1))

        if not sizes_found:
            # fallback: 抓所有獨立尺寸標記
            all_sizes = re.findall(size_pattern, text)
            # 去重保持順序
            seen = set()
            for s in all_sizes:
                if s not in seen:
                    seen.add(s)
                    sizes_found.append(s)

        # 顏色
        color_name = ""
        color_match = re.search(r'カラー[：:]\s*(\d+)\s+(\w+)', text)
        if color_match:
            color_name = f"{color_match.group(1)} {color_match.group(2)}"  # "69 NAVY"
        elif color_from_url:
            color_name = color_from_url

        if not sizes_found:
            sizes_found = ["XS", "S", "M", "L", "XL", "XXL", "3XL"]
            print(f"[Uniqlo] 使用預設尺寸: {sizes_found}")
        else:
            print(f"[Uniqlo] 從 HTML 找到尺寸: {sizes_found}")

        color_img = f"https://image.uniqlo.com/UQ/ST3/AsianCommon/imagesgoods/{product_id}/chip/goods_{color_from_url}_{product_id}_chip.jpg" if color_from_url else ""

        variants = []
        for size in sizes_found:
            variants.append({
                "color": color_name,
                "size": size,
                "sku": f"{product_id}-{color_from_url}-{size}",
                "price": product.price_jpy or 0,
                "in_stock": True,
                "image": color_img,
            })

        product.variants = variants
        return product

    def _parse_uniqlo_html(self, html: str, product_id: str, product: ProductInfo) -> ProductInfo:
        """從 HTML 解析 Uniqlo 商品基本資訊"""
        soup = BeautifulSoup(html, "html.parser")

        # 標題
        og_title = soup.find("meta", property="og:title")
        if og_title:
            product.title = og_title.get("content", "").replace("| ユニクロ", "").strip()
        if not product.title:
            title_tag = soup.find("title")
            if title_tag:
                t = title_tag.get_text()
                product.title = t.split("|")[0].strip() if "|" in t else t.strip()

        # OG 圖片
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            product.image_url = og_img["content"]

        # 從頁面找所有商品圖片
        extra = []
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "image.uniqlo.com" in src and product_id in src and src not in extra:
                extra.append(src)
        if extra and not product.image_url:
            product.image_url = extra[0]
        product.extra_images = [u for u in extra if u != product.image_url][:8]

        # 價格：從 ld+json
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = next((x for x in ld if x.get("@type") == "Product"), ld[0] if ld else {})
                if ld.get("@type") == "Product":
                    offers = ld.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price")
                    if price:
                        product.price_jpy = int(float(price))
                        print(f"[Uniqlo] ld+json 找到價格: ¥{product.price_jpy}")
            except:
                pass

        # 價格 fallback：從文字找 ¥（SPA 通常抓不到）
        if not product.price_jpy:
            text = soup.get_text()
            pm = re.search(r'[¥￥]([\d,]+)', text)
            if pm:
                product.price_jpy = int(pm.group(1).replace(",", ""))

        return product

    # ============================================================
    # MUJI JP - HTML 解析 + 內部 API（不需瀏覽器）
    # ============================================================
    async def _scrape_muji(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="無印良品")

        # 從 URL 提取 JAN code：/detail/4548076445289
        m = re.search(r'/detail/(\d{10,14})', url)
        if not m:
            print(f"[MUJI] ❌ 無法從 URL 提取商品代碼: {url}")
            return product

        jan_code = m.group(1)  # e.g. "4548076445289"
        print(f"[MUJI] JAN: {jan_code}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

        # MUJI 封雲端 IP，需走 proxy
        proxy_arg = PROXY_URL if PROXY_URL else None
        if proxy_arg:
            print(f"[MUJI] 使用 proxy: {proxy_arg[:30]}...")
        else:
            print(f"[MUJI] ⚠️ 無 proxy，MUJI 可能 timeout")

        async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:

            # === Step 1: 抓 HTML 頁面 ===
            html_text = ""
            cookies = {}
            try:
                print(f"[MUJI] Step 1: 抓 HTML...")
                resp = await client.get(url, headers=headers)
                html_text = resp.text
                cookies = dict(resp.cookies)
                print(f"[MUJI] HTML: {resp.status_code}, {len(html_text)} bytes")
            except Exception as e:
                print(f"[MUJI] HTML 錯誤: {type(e).__name__}: {e}")

            # === 從 HTML 提取基本資訊 ===
            if html_text:
                soup = BeautifulSoup(html_text, "html.parser")

                # 標題：從 <title>
                title_tag = soup.find("title")
                if title_tag:
                    t = title_tag.get_text()
                    product.title = t.replace("| 無印良品", "").replace("|無印良品", "").strip()
                    print(f"[MUJI] 標題: {product.title[:50]}")

                # OG 標題（可能更乾淨）
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    t = og_title["content"].replace("| 無印良品", "").strip()
                    if t:
                        product.title = t

                # OG 圖片
                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get("content"):
                    product.image_url = og_img["content"]
                    print(f"[MUJI] OG image: {product.image_url[:60]}")

                # 已知的 MUJI 圖片 URL 模式
                if not product.image_url:
                    product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

                # 額外圖片：從 HTML 找
                extra = []
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if "muji.com" in src and jan_code in src and src not in extra:
                        extra.append(src)
                product.extra_images = [u for u in extra if u != product.image_url][:8]

                # 從 ld+json 找價格
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(script.string or "")
                        if isinstance(ld, list):
                            ld = next((x for x in ld if x.get("@type") == "Product"), ld[0] if ld else {})
                        if ld.get("@type") == "Product":
                            offers = ld.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            price = offers.get("price")
                            if price:
                                product.price_jpy = int(float(price))
                                print(f"[MUJI] ld+json 價格: ¥{product.price_jpy}")
                    except:
                        pass

                # 從內嵌 script 找商品 JSON
                self._parse_muji_embedded_json(soup, jan_code, product)

                if product.price_jpy and product.title:
                    print(f"[MUJI] ✅ HTML 解析完成: {product.title[:40]} / ¥{product.price_jpy:,}")
                    return product

            # === Step 2: 嘗試 MUJI 內部 API ===
            api_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja-JP,ja;q=0.9",
                "Referer": url,
                "Origin": "https://www.muji.com",
                "X-Requested-With": "XMLHttpRequest",
            }

            # 嘗試多個可能的 API 路徑
            api_urls = [
                f"https://www.muji.com/jp/api/store/cmdty/detail/{jan_code}",
                f"https://www.muji.com/jp/api/store/v1/cmdty/{jan_code}",
                f"https://www.muji.com/jp/api/product/{jan_code}",
                f"https://www.muji.com/jp/api/v1/product/{jan_code}",
                f"https://www.muji.com/jp/store/api/cmdty/detail/{jan_code}",
            ]

            for api_url in api_urls:
                try:
                    print(f"[MUJI] Step 2: API {api_url[:80]}...")
                    resp = await client.get(api_url, headers=api_headers, cookies=cookies)
                    print(f"[MUJI] API response: {resp.status_code}, {len(resp.text)} bytes")

                    if resp.status_code == 200:
                        try:
                            api_data = resp.json()
                            print(f"[MUJI] ✅ API JSON keys: {list(api_data.keys())[:8]}")
                            self._parse_muji_api(api_data, jan_code, product)
                            if product.price_jpy:
                                print(f"[MUJI] ✅ API 價格: ¥{product.price_jpy:,}")
                                break
                        except:
                            # 可能不是 JSON
                            if len(resp.text) < 500:
                                print(f"[MUJI] 非 JSON: {resp.text[:200]}")
                    elif resp.status_code != 404:
                        print(f"[MUJI] API {resp.status_code}: {resp.text[:200]}")

                except Exception as e:
                    print(f"[MUJI] API 錯誤: {type(e).__name__}: {e}")

        # === Step 3: httpx 全部失敗，用 Chrome UC fallback ===
        if not product.price_jpy:
            print(f"[MUJI] httpx 全部 timeout，嘗試 Chrome UC...")
            try:
                success = await self._muji_chrome_fallback(url, product, jan_code)
                if success:
                    print(f"[MUJI] ✅ Chrome UC 成功")
            except Exception as e:
                print(f"[MUJI] Chrome UC 錯誤: {type(e).__name__}: {e}")

        # === 最後保底：確保有圖片 ===
        if not product.image_url:
            product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

        if product.title:
            print(f"[MUJI] 最終: {product.title[:40]} / ¥{product.price_jpy or '?'} / {len(product.variants)} variants")
        else:
            print(f"[MUJI] ⚠️ 未取得資料")

        return product

    def _parse_muji_embedded_json(self, soup, jan_code: str, product: ProductInfo):
        """從 MUJI HTML 內嵌 script 找商品資料"""
        for script in soup.find_all("script"):
            text = script.string or ""
            if not text or len(text) < 50:
                continue

            # __NEXT_DATA__
            if "__NEXT_DATA__" in text:
                try:
                    jm = re.search(r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</)', text, re.DOTALL)
                    if jm:
                        data = json.loads(jm.group(1))
                        props = data.get("props", {}).get("pageProps", {})
                        print(f"[MUJI] __NEXT_DATA__ pageProps keys: {list(props.keys())[:8]}")
                        self._parse_muji_api(props, jan_code, product)
                        if product.price_jpy:
                            return
                except Exception as e:
                    print(f"[MUJI] __NEXT_DATA__ 錯誤: {e}")

            # window.__INITIAL_STATE__ 或類似
            for pat in [r'__INITIAL_STATE__\s*=\s*({.+?})\s*;',
                       r'window\.PRODUCT\s*=\s*({.+?})\s*;',
                       r'window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*;']:
                try:
                    sm = re.search(pat, text, re.DOTALL)
                    if sm:
                        data = json.loads(sm.group(1))
                        print(f"[MUJI] 全域狀態 keys: {list(data.keys())[:8]}")
                        self._parse_muji_api(data, jan_code, product)
                        if product.price_jpy:
                            return
                except:
                    pass

            # 直接找價格 pattern（"price":XXXX 或 "salePrice":XXXX）
            if jan_code in text and ('"price"' in text or '"salePrice"' in text):
                price_m = re.search(r'"(?:sale)?[Pp]rice"\s*:\s*(\d{3,6})', text)
                if price_m and not product.price_jpy:
                    product.price_jpy = int(price_m.group(1))
                    print(f"[MUJI] script 內找到價格: ¥{product.price_jpy}")

    def _parse_muji_api(self, data: dict, jan_code: str, product: ProductInfo):
        """解析 MUJI API 或內嵌 JSON"""
        # 嘗試常見的 data 結構
        prod = None

        # 直接是商品
        if "janCode" in data or "commodityCode" in data:
            prod = data

        # 巢狀結構
        for key in ["product", "cmdty", "detail", "commodity", "data", "item"]:
            if key in data and isinstance(data[key], dict):
                prod = data[key]
                break

        if not prod:
            return

        # 名稱
        name = prod.get("name") or prod.get("commodityName") or prod.get("productName") or ""
        if name and not product.title:
            product.title = name

        # 價格
        for pk in ["price", "salePrice", "sellingPrice", "retailPrice", "displayPrice", "priceIncTax", "priceExcTax"]:
            v = prod.get(pk)
            if v and isinstance(v, (int, float)) and v > 0:
                product.price_jpy = int(v)
                break

        if not product.price_jpy:
            prices = prod.get("prices", {}) or prod.get("price", {})
            if isinstance(prices, dict):
                for pk in ["selling", "retail", "sale", "current", "value"]:
                    v = prices.get(pk)
                    if v and isinstance(v, (int, float)):
                        product.price_jpy = int(v)
                        break

        # 圖片
        images = prod.get("images", []) or prod.get("imageList", []) or []
        if isinstance(images, list):
            for img in images:
                u = img.get("url", "") if isinstance(img, dict) else str(img)
                if u and "muji.com" in u:
                    if not product.image_url:
                        product.image_url = u
                    elif u != product.image_url and len(product.extra_images) < 8:
                        product.extra_images.append(u)

        # 尺寸/顏色
        variants_data = prod.get("skuList", []) or prod.get("variants", []) or prod.get("sizes", []) or prod.get("colors", []) or []
        if isinstance(variants_data, list):
            for v in variants_data:
                if not isinstance(v, dict):
                    continue
                variant = {
                    "color": v.get("colorName", "") or v.get("color", ""),
                    "size": v.get("sizeName", "") or v.get("size", ""),
                    "sku": v.get("janCode", "") or v.get("sku", "") or v.get("skuCode", ""),
                    "price": product.price_jpy or 0,
                    "in_stock": v.get("stockStatus", "") != "OUT_OF_STOCK",
                    "image": "",
                }
                if variant["color"] or variant["size"]:
                    product.variants.append(variant)

    # ============================================================
    # MUJI Chrome UC fallback
    # ============================================================
    async def _muji_chrome_fallback(self, url: str, product: ProductInfo, jan_code: str) -> bool:
        """用 Chrome UC 載入 MUJI 頁面，直接填入 product 的所有欄位"""
        import time as _time, base64

        with self._driver_lock:
          for attempt in range(2):  # 最多重試 1 次
            try:
                driver = self._ensure_driver()
                if not driver:
                    print(f"[MUJI] Chrome driver 無法建立")
                    return False

                self._driver_use_count += 1

                # 清理 tab + cookies
                try:
                    handles = driver.window_handles
                    if len(handles) > 1:
                        for h in handles[1:]:
                            driver.switch_to.window(h)
                            driver.close()
                        driver.switch_to.window(handles[0])
                    driver.delete_all_cookies()
                except:
                    pass

                print(f"[MUJI] Chrome UC 載入 (attempt {attempt+1}): {url}")
                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception as e:
                    err_name = type(e).__name__
                    print(f"[MUJI] uc_open: {err_name}: {e}")
                    if "InvalidSession" in err_name or "invalid session" in str(e).lower():
                        print(f"[MUJI] Session 已死，重建 driver...")
                        self._driver = None
                        self._create_driver()
                        continue  # 重試
                    # 其他錯誤繼續嘗試等待

                # 等待渲染
                html = ""
                session_dead = False
                for i in range(8):
                    _time.sleep(2)
                    try:
                        html = driver.page_source
                        title = driver.title
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            session_dead = True
                            break
                        continue

                    has_data = (
                        'application/ld+json' in html or
                        '無印良品' in html or
                        'og:title' in html or
                        '税込' in html or
                        '円' in html
                    )

                    if i >= 1 and has_data:
                        print(f"[MUJI] Chrome 頁面就緒 ({i+1}次, {len(html)} bytes, title: {title[:40]})")
                        break

                if session_dead:
                    print(f"[MUJI] Session 死了，重建 driver...")
                    self._driver = None
                    self._create_driver()
                    continue  # 重試

                if not html or len(html) < 5000:
                    print(f"[MUJI] Chrome 頁面載入失敗 ({len(html)} bytes)")
                    return False

                # === 解析 HTML ===
                soup = BeautifulSoup(html, "html.parser")

                # 標題
                if not product.title:
                    og_title = soup.find("meta", property="og:title")
                    if og_title and og_title.get("content"):
                        product.title = og_title["content"].replace("| 無印良品", "").strip()
                    else:
                        title_tag = soup.find("title")
                        if title_tag:
                            product.title = title_tag.get_text().replace("| 無印良品", "").replace("|無印良品", "").strip()

                # 從 ld+json 找價格
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(script.string or "")
                        if isinstance(ld, list):
                            ld = next((x for x in ld if x.get("@type") == "Product"), ld[0] if ld else {})
                        if ld.get("@type") == "Product":
                            offers = ld.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            price = offers.get("price")
                            if price:
                                product.price_jpy = int(float(price))
                                print(f"[MUJI] Chrome ld+json 價格: ¥{product.price_jpy}")
                    except:
                        pass

                # 內嵌 JSON
                self._parse_muji_embedded_json(soup, jan_code, product)

                # 從渲染後的 HTML 文字找價格
                if not product.price_jpy:
                    page_text = soup.get_text(" ", strip=True)
                    for pat in [r'¥\s*([\d,]+)', r'([\d,]+)\s*円', r'([\d,]+)\s*[（(]税込']:
                        pm = re.search(pat, page_text)
                        if pm:
                            try:
                                p = int(pm.group(1).replace(",", ""))
                                if 50 < p < 500000:
                                    product.price_jpy = p
                                    print(f"[MUJI] Chrome 文字價格: ¥{p:,}")
                                    break
                            except:
                                pass

                # === 圖片：用 JS 在 Chrome 內下載為 base64 ===
                og_img = soup.find("meta", property="og:image")
                img_url = og_img["content"] if og_img and og_img.get("content") else f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"
                product.image_url = img_url

                try:
                    print(f"[MUJI] Chrome 下載圖片: {img_url[:60]}...")
                    b64 = driver.execute_script("""
                        return await fetch(arguments[0])
                            .then(r => r.blob())
                            .then(b => new Promise((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onload = () => resolve(reader.result.split(',')[1]);
                                reader.onerror = reject;
                                reader.readAsDataURL(b);
                            }));
                    """, img_url)
                    if b64 and len(b64) > 100:
                        product.image_base64 = b64
                        print(f"[MUJI] ✅ 圖片 base64: {len(b64)} chars")
                except Exception as e:
                    print(f"[MUJI] 圖片下載失敗: {type(e).__name__}: {e}")
                    # 嘗試同步方式
                    try:
                        b64 = driver.execute_script("""
                            var xhr = new XMLHttpRequest();
                            xhr.open('GET', arguments[0], false);
                            xhr.responseType = 'arraybuffer';
                            xhr.send();
                            if (xhr.status === 200) {
                                var bytes = new Uint8Array(xhr.response);
                                var binary = '';
                                for (var i = 0; i < bytes.length; i++) {
                                    binary += String.fromCharCode(bytes[i]);
                                }
                                return btoa(binary);
                            }
                            return '';
                        """, img_url)
                        if b64 and len(b64) > 100:
                            product.image_base64 = b64
                            print(f"[MUJI] ✅ 圖片 base64 (sync): {len(b64)} chars")
                    except Exception as e2:
                        print(f"[MUJI] 圖片 sync 也失敗: {type(e2).__name__}: {e2}")

                # === 額外圖片 ===
                extra = []
                for img_tag in soup.find_all("img"):
                    src = img_tag.get("src", "")
                    if "muji.com" in src and jan_code in src and src != img_url and src not in extra:
                        extra.append(src)
                product.extra_images = extra[:8]

                # === Variants：從渲染後的 DOM 找尺寸/顏色 ===
                self._extract_muji_variants_from_html(driver, soup, product, jan_code)

                return bool(product.price_jpy)

            except Exception as e:
                err_name = type(e).__name__
                print(f"[MUJI] Chrome 錯誤 (attempt {attempt+1}): {err_name}: {e}")
                if "InvalidSession" in err_name and attempt == 0:
                    print(f"[MUJI] 重建 driver 後重試...")
                    self._driver = None
                    self._create_driver()
                    continue  # 重試
                return False
          # for loop 結束（兩次都失敗）
          return False

    # 合法尺寸白名單（用來過濾 DOM 抓到的按鈕文字）
    _VALID_SIZES = {
        "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL",
        "F", "フリー", "FREE",
        # 數字尺寸（褲子/鞋子等）
        *[str(n) for n in range(19, 32)],  # 19-31 (鞋)
        *[str(n) for n in range(55, 120, 5)],  # 55-115 (腰圍)
    }

    def _extract_muji_variants_from_html(self, driver, soup, product: ProductInfo, jan_code: str):
        """從 MUJI 渲染後的 HTML 提取尺寸/顏色 variants"""
        try:
            # === 用白名單過濾，避免抓到「カートに入れる」等按鈕 ===
            valid_sizes_js = json.dumps(list(self._VALID_SIZES))

            variants_js = driver.execute_script(f"""
                var validSizes = new Set({valid_sizes_js});
                var results = {{sizes: [], colors: []}};

                // 找所有按鈕/連結文字，用白名單過濾出尺寸
                var allBtns = document.querySelectorAll(
                    '[class*="size"] button, [class*="size"] a, ' +
                    '[class*="Size"] button, [class*="Size"] a, ' +
                    '.cmdty-size-list button, .cmdty-size-list a'
                );
                allBtns.forEach(function(el) {{
                    var text = el.textContent.trim();
                    if (validSizes.has(text) && !results.sizes.includes(text)) {{
                        results.sizes.push(text);
                    }}
                }});

                // 找顏色：MUJI 用色塊按鈕，通常有 aria-label 或 title 包含顏色名
                var colorEls = document.querySelectorAll(
                    '[class*="color"] button, [class*="Color"] button, ' +
                    '.cmdty-color-list button, [aria-label*="カラー"]'
                );
                colorEls.forEach(function(el) {{
                    var text = (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent).trim();
                    // 過濾掉太長或包含動作文字的
                    if (text && text.length < 15 && !text.includes('カート') && 
                        !text.includes('閉じる') && !text.includes('確認') &&
                        !results.colors.includes(text)) {{
                        results.colors.push(text);
                    }}
                }});

                // 從 select 元素
                document.querySelectorAll('select').forEach(function(sel) {{
                    var label = (sel.getAttribute('aria-label') || sel.name || '').toLowerCase();
                    sel.querySelectorAll('option').forEach(function(opt) {{
                        var val = opt.textContent.trim();
                        if (!val || val.includes('選択') || val.includes('選んで')) return;
                        if (label.includes('size') || label.includes('サイズ')) {{
                            if (validSizes.has(val) && !results.sizes.includes(val)) results.sizes.push(val);
                        }} else if (label.includes('color') || label.includes('カラー')) {{
                            if (!results.colors.includes(val)) results.colors.push(val);
                        }}
                    }});
                }});

                return results;
            """)

            sizes = variants_js.get("sizes", []) if variants_js else []
            colors = variants_js.get("colors", []) if variants_js else []

            print(f"[MUJI] JS variants: sizes={sizes}, colors={colors}")

            # fallback: 從 HTML 文字找尺寸 pattern
            if not sizes:
                page_text = soup.get_text(" ", strip=True)
                size_section = re.search(r'サイズ[：:\s]*(?:[\w・]+\s*)?((?:(?:XS|S|M|L|XL|XXL|3XL|4XL|F|フリー)\s*[/／・]?\s*)+)', page_text)
                if size_section:
                    sizes = re.findall(r'\b(XS|S|M|L|XL|XXL|3XL|4XL|F|フリー)\b', size_section.group(1))
                    sizes = list(dict.fromkeys(sizes))  # 去重保序
                    print(f"[MUJI] HTML text sizes: {sizes}")

            # 建構 variants
            if sizes or colors:
                if not colors:
                    colors = [""]
                if not sizes:
                    sizes = [""]

                for color in colors:
                    for size in sizes:
                        label_parts = [p for p in [color, size] if p]
                        variant = {
                            "color": color,
                            "size": size,
                            "sku": f"{jan_code}-{'-'.join(label_parts)}" if label_parts else jan_code,
                            "price": product.price_jpy or 0,
                            "in_stock": True,
                            "image": "",
                        }
                        product.variants.append(variant)
                print(f"[MUJI] ✅ 建構 {len(product.variants)} variants")
            else:
                print(f"[MUJI] 未找到尺寸/顏色選項（可能是無尺寸商品）")

        except Exception as e:
            print(f"[MUJI] variant 提取錯誤: {type(e).__name__}: {e}")

    # ============================================================
    # BEAMS - httpx + BeautifulSoup（SSR，不需要 JS）
    # ============================================================
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
            for attempt in range(3):
                # attempt 0: 直連; attempt 1: proxy; attempt 2: proxy + 更長 timeout
                use_proxy = attempt >= 1 and PROXY_URL
                try:
                    timeout_val = 30.0 if attempt == 0 else 60.0
                    client_kwargs = {
                        "timeout": httpx.Timeout(timeout_val, connect=15.0),
                        "follow_redirects": True,
                    }
                    if use_proxy:
                        client_kwargs["proxy"] = PROXY_URL
                        print(f"[BEAMS] attempt {attempt+1}: 使用 proxy ({PROXY_URL[:30]}...)")
                    else:
                        print(f"[BEAMS] attempt {attempt+1}: 直連 (timeout={timeout_val}s)")

                    async with httpx.AsyncClient(**client_kwargs) as client:
                        resp = await client.get(url, headers=headers)
                        if resp.status_code != 200:
                            print(f"[BEAMS] HTTP {resp.status_code} (attempt {attempt+1})")
                            if attempt < 2:
                                await asyncio.sleep(2)
                                continue
                            return product
                        html = resp.text
                        mode = "proxy" if use_proxy else "直連"
                        print(f"[BEAMS] 頁面取得成功 ({mode}, {len(html)} bytes, attempt {attempt+1})")
                        break
                except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError) as te:
                    print(f"[BEAMS] {type(te).__name__} (attempt {attempt+1}/3, {'proxy' if use_proxy else '直連'})")
                    if attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    raise

            if not html:
                # httpx 全部失敗，用 Chrome UC fallback
                if PROXY_URL:
                    print(f"[BEAMS] httpx 全部 timeout，嘗試 Chrome UC...")
                    try:
                        html = await self._beams_chrome_fallback(url)
                        if html:
                            print(f"[BEAMS] ✅ Chrome UC 成功 ({len(html)} bytes)")
                        else:
                            print(f"[BEAMS] ❌ Chrome UC 也失敗")
                            return product
                    except Exception as e:
                        print(f"[BEAMS] Chrome UC 錯誤: {type(e).__name__}: {e}")
                        return product
                else:
                    print(f"[BEAMS] ❌ 無法取得頁面（無 proxy 可用）")
                    return product

            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            # 麵包屑最後一個 or 頁面內的商品名
            breadcrumbs = soup.select("a")
            for a in reversed(breadcrumbs):
                href = a.get("href", "")
                if "/item/" in href and a.get_text(strip=True):
                    product.title = a.get_text(strip=True)
                    break

            if not product.title:
                t = soup.find("title")
                if t:
                    txt = t.get_text(strip=True)
                    # "BEAMS HEART（ビームス ハート）フィルム ライト フーディー..."
                    # 取括號後的部分
                    m = re.search(r'[）\)]\s*(.+?)(?:（|通販)', txt)
                    if m:
                        product.title = m.group(1).strip()
                    else:
                        product.title = txt.split("通販")[0].strip()

            # === 品牌 ===
            # 找 label link
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if re.match(r'^/[a-z]+$', href) and a.get_text(strip=True):
                    brand = a.get_text(strip=True)
                    if brand and "BEAMS" in brand.upper() and len(brand) < 40:
                        product.brand = brand
                        break
            if not product.brand:
                product.brand = "BEAMS"

            # === 價格 ===
            page_text = soup.get_text(" ", strip=True)
            # ￥10,780（税込）or ¥10,780
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

            # === 圖片 ===
            # cdn.beams.co.jp/img/goods/{id}/S1/{id}_C_1.jpg (color image)
            # cdn.beams.co.jp/img/goods/{id}/S1/{id}_D_N.jpg (detail images)
            images = []
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "cdn.beams.co.jp/img/goods" in src:
                    if src.startswith("//"):
                        src = "https:" + src
                    if src not in images:
                        images.append(src)

            if images:
                # 優先用 _C_ (color) 圖片當主圖
                color_imgs = [i for i in images if "_C_" in i]
                detail_imgs = [i for i in images if "_D_" in i]

                if color_imgs:
                    product.image_url = color_imgs[0]
                    # 額外圖片：其他 color + 前幾張 detail
                    product.extra_images = (color_imgs[1:] + detail_imgs[:5])
                elif detail_imgs:
                    product.image_url = detail_imgs[0]
                    product.extra_images = detail_imgs[1:6]
                else:
                    product.image_url = images[0]
                    product.extra_images = images[1:6]

            # === Variants（顏色 × 尺寸） ===
            colors = []
            sizes = []

            # 找顏色：h4 標籤 "BLACK", "NAVY" 等
            for h4 in soup.find_all("h4"):
                text = h4.get_text(strip=True)
                if text and len(text) < 20 and re.match(r'^[A-Z\s]+$', text):
                    colors.append(text)

            # 找尺寸：從 "S／在庫あり", "M／在庫あり" 等
            size_stock_pattern = re.findall(r'([A-Z0-9]+)／(在庫あり|在庫なし|残りわずか)', page_text)
            seen_sizes = set()
            for size, stock in size_stock_pattern:
                if size in self._VALID_SIZES and size not in seen_sizes:
                    sizes.append(size)
                    seen_sizes.add(size)

            if colors or sizes:
                if not colors:
                    colors = [""]
                if not sizes:
                    sizes = [""]

                # 從文字裡建構 variant + 庫存
                for color in colors:
                    # 找這個顏色下面的 size+stock
                    color_section = re.search(
                        re.escape(color) + r'(.+?)(?:' + '|'.join(re.escape(c) for c in colors if c != color) + r'|店舗在庫|$)',
                        page_text, re.DOTALL
                    ) if color else None

                    section_text = color_section.group(1) if color_section else page_text

                    for size in sizes:
                        stock_match = re.search(re.escape(size) + r'／(在庫あり|在庫なし|残りわずか)', section_text)
                        in_stock = True
                        if stock_match:
                            in_stock = stock_match.group(1) != "在庫なし"

                        # 找顏色對應的圖片
                        color_img = ""
                        if color:
                            for img_url in images:
                                if "_C_" in img_url:
                                    # 第一張 _C_ 給第一個顏色，第二張給第二個
                                    idx = colors.index(color)
                                    c_imgs = [i for i in images if "_C_" in i]
                                    if idx < len(c_imgs):
                                        color_img = c_imgs[idx]
                                    break

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

                print(f"[BEAMS] ✅ {len(product.variants)} variants (colors={colors}, sizes={sizes})")
            else:
                print(f"[BEAMS] 未找到 variants")

            # === 說明 ===
            desc_parts = []
            for text_block in page_text.split("■"):
                if text_block.startswith("デザイン") or text_block.startswith("コーディネート") or text_block.startswith("素材"):
                    desc_parts.append("■" + text_block[:200])
            if desc_parts:
                product.description = "\n".join(desc_parts)[:500]

            if product.price_jpy:
                print(f"[BEAMS] ✅ {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
            else:
                print(f"[BEAMS] ⚠️ 價格未找到")

        except Exception as e:
            print(f"[BEAMS] ❌ 錯誤: {type(e).__name__}: {e}")

        return product

    # ============================================================
    # BEAMS Chrome UC fallback
    # ============================================================
    async def _beams_chrome_fallback(self, url: str) -> str | None:
        """用 Chrome UC 載入 BEAMS 頁面，回傳 HTML"""
        import time as _time

        with self._driver_lock:
          for attempt in range(2):
            try:
                driver = self._ensure_driver()
                if not driver:
                    print(f"[BEAMS] Chrome driver 無法建立")
                    return None

                self._driver_use_count += 1

                # 清理
                try:
                    handles = driver.window_handles
                    if len(handles) > 1:
                        for h in handles[1:]:
                            driver.switch_to.window(h)
                            driver.close()
                        driver.switch_to.window(handles[0])
                    driver.delete_all_cookies()
                except:
                    pass

                print(f"[BEAMS] Chrome UC 載入 (attempt {attempt+1}): {url[:80]}")
                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception as e:
                    err_name = type(e).__name__
                    print(f"[BEAMS] uc_open: {err_name}: {e}")
                    if "InvalidSession" in err_name or "invalid session" in str(e).lower():
                        print(f"[BEAMS] Session 已死，重建 driver...")
                        self._driver = None
                        self._create_driver()
                        continue

                # 等待渲染
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

                    # BEAMS 頁面特徵
                    has_data = (
                        'cdn.beams.co.jp' in html or
                        '税込' in html or
                        'beams.co.jp' in html
                    )

                    if i >= 1 and has_data and len(html) > 10000:
                        print(f"[BEAMS] Chrome 頁面就緒 ({i+1}次, {len(html)} bytes)")
                        return html

                if session_dead:
                    print(f"[BEAMS] Session 死了，重建 driver...")
                    self._driver = None
                    self._create_driver()
                    continue

                if html and len(html) > 10000:
                    return html

                print(f"[BEAMS] Chrome 頁面載入失敗 ({len(html)} bytes)")
                return None

            except Exception as e:
                err_name = type(e).__name__
                print(f"[BEAMS] Chrome 錯誤: {err_name}: {e}")
                if "InvalidSession" in err_name and attempt == 0:
                    self._driver = None
                    self._create_driver()
                    continue
                return None

        return None

    # ============================================================
    # ZOZOTOWN - SeleniumBase UC + xvfb（繞過 Akamai）
    # ============================================================
    async def _scrape_zozotown(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        # 方法 1: SeleniumBase UC + xvfb + proxy（IP 白名單）
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_zozo_uc, url
            )
            if data and data.get("title"):
                product.title = data.get("title", "")
                product.price_jpy = data.get("price", 0) or None
                product.brand = data.get("brand", "")
                product.description = data.get("description", "")[:500]
                images = data.get("images", [])
                if images:
                    product.image_url = images[0]
                    product.extra_images = images[1:9]
                # variants
                variants = data.get("variants", [])
                if variants:
                    product.variants = variants
                print(f"[ZOZO] ✅ {product.title[:40]} / ¥{product.price_jpy:,} / {len(variants)} variants" if product.price_jpy else f"[ZOZO] ✅ {product.title[:40]}")
            else:
                print("[ZOZO] ⚠️ 未取得資料")
        except Exception as e:
            print(f"[ZOZO] ❌ 錯誤: {e}")

        # 方法 2: 外部 product-fetcher（如有設定）
        if not product.title and ZOZO_SCRAPER_URL:
            result = await self._scrape_zozo_via_proxy(url)
            if result and result.title:
                return result

        return product

    def _fetch_zozo_uc(self, url: str) -> dict | None:
        """
        用常駐 SeleniumBase UC mode Chrome 爬 ZOZOTOWN
        - 重用 driver，不每次建立/關閉
        - threading.Lock 防止並發
        """
        import os, time as _time

        with self._driver_lock:
            try:
                driver = self._ensure_driver()
                if not driver:
                    return None

                # 首次驗證 proxy
                if not self._proxy_verified and PROXY_URL:
                    try:
                        driver.get('http://httpbin.org/ip')
                        _time.sleep(1)
                        src = driver.page_source
                        if '103.230' in src:
                            print(f"[ZOZO] ✅ proxy 正常 (IP: 103.230.9.105)")
                        else:
                            print(f"[ZOZO] proxy IP: {src[:100]}")
                        self._proxy_verified = True
                    except Exception as e:
                        print(f"[ZOZO] proxy 測試: {type(e).__name__}")

                # 清理：關閉多餘 tab，清 cookies
                try:
                    handles = driver.window_handles
                    if len(handles) > 1:
                        for h in handles[1:]:
                            driver.switch_to.window(h)
                            driver.close()
                        driver.switch_to.window(handles[0])
                    driver.delete_all_cookies()
                except:
                    pass

                self._driver_use_count += 1

                # 用 uc_open_with_reconnect 載入（反偵測核心）
                print(f"[ZOZO] 載入: {url}")
                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception as e:
                    print(f"[ZOZO] uc_open: {type(e).__name__}: {e}")

                # 等待頁面渲染
                for i in range(12):
                    _time.sleep(3 if i < 3 else 2)
                    try:
                        html = driver.page_source
                        title = driver.title
                    except:
                        continue

                    has_data = ('application/ld+json' in html or
                               '__NEXT_DATA__' in html or
                               'og:title' in html)

                    if i < 2:
                        print(f"[ZOZO] 嘗試 {i+1}: {len(html)} bytes | title={title[:60]} | data={has_data}")
                        if len(html) < 500:
                            print(f"[ZOZO] HTML: {html[:300]}")
                    else:
                        print(f"[ZOZO] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                    if has_data:
                        result = driver.execute_script(r"""
                            var r = {title:'', brand:'', price:0, price_text:'',
                                     original_price:0, original_price_text:'', discount:'',
                                     images:[], description:'', item_id:'', in_stock:true,
                                     variants:[], variant_debug:''};

                            var m = location.pathname.match(/\/goods(?:-sale)?\/(\d+)/);
                            if (m) r.item_id = m[1];

                            // === ld+json ===
                            document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                                try {
                                    var d = JSON.parse(s.textContent);
                                    if (Array.isArray(d)) d = d.find(function(i){return i['@type']==='Product'}) || d[0];
                                    if (d && d['@type'] === 'Product') {
                                        r.title = d.name || '';
                                        var b = d.brand || '';
                                        r.brand = (typeof b === 'object') ? (b.name || '') : String(b);
                                        r.description = d.description || '';
                                        var img = d.image || [];
                                        if (typeof img === 'string') img = [img];
                                        r.images = img.filter(function(i){return typeof i === 'string' && i.indexOf('c.imgz.jp') !== -1}).slice(0,15);
                                        var offers = d.offers || {};
                                        if (Array.isArray(offers)) {
                                            // 多個 offers = 多個 variant
                                            offers.forEach(function(o) {
                                                if (o.price) {
                                                    if (!r.price) {
                                                        r.price = parseInt(o.price);
                                                        r.price_text = '\u00a5' + r.price.toLocaleString();
                                                    }
                                                }
                                            });
                                            offers = offers[0] || {};
                                        } else {
                                            if (offers.price) {
                                                r.price = parseInt(offers.price);
                                                r.price_text = '\u00a5' + r.price.toLocaleString();
                                            }
                                        }
                                        if (offers.availability && offers.availability.indexOf('OutOfStock') !== -1) r.in_stock = false;
                                    }
                                } catch(e) {}
                            });

                            // === __NEXT_DATA__ (variants) ===
                            var nd = document.getElementById('__NEXT_DATA__');
                            if (nd) {
                                try {
                                    var ndata = JSON.parse(nd.textContent);
                                    var props = ndata.props && ndata.props.pageProps ? ndata.props.pageProps : {};

                                    // 嘗試找 product 資料
                                    var prod = props.product || props.goods || props.item ||
                                              (props.initialState && props.initialState.product) || {};

                                    if (!r.title && prod.name) r.title = prod.name;
                                    if (!r.brand && prod.brandName) r.brand = prod.brandName;
                                    if (!r.price && prod.price) {
                                        r.price = parseInt(prod.price);
                                        r.price_text = '\u00a5' + r.price.toLocaleString();
                                    }
                                    if (r.images.length === 0 && prod.images) {
                                        r.images = prod.images.map(function(i){return i.url || i}).slice(0,15);
                                    }

                                    // 找 variants/skus/items
                                    var items = prod.items || prod.skus || prod.variants ||
                                               prod.colorSizes || prod.detail && prod.detail.items || [];

                                    if (items.length > 0) {
                                        items.forEach(function(item) {
                                            var v = {
                                                color: item.colorName || item.color || item.colorLabel || '',
                                                size: item.sizeName || item.size || item.sizeLabel || '',
                                                sku: item.skuId || item.id || item.sku || '',
                                                price: item.price ? parseInt(item.price) : r.price,
                                                in_stock: item.soldout !== true && item.inStock !== false,
                                                image: item.imageUrl || item.image || ''
                                            };
                                            if (v.color || v.size) r.variants.push(v);
                                        });
                                    }

                                    // Debug: 列出 pageProps 的 top-level keys
                                    r.variant_debug = 'pageProps keys: ' + Object.keys(props).join(',');
                                    if (prod && typeof prod === 'object') {
                                        r.variant_debug += ' | prod keys: ' + Object.keys(prod).join(',');
                                    }
                                } catch(e) {
                                    r.variant_debug = 'NEXT_DATA error: ' + e.message;
                                }
                            }

                            // === DOM: variant extraction from ZOZO DT/DD structure ===
                            if (r.variants.length === 0) {
                                // ZOZO 結構: <dl> → <dt>顏色名</dt><dd>含 ul>li 尺寸列表</dd>
                                var dts = document.querySelectorAll('dt.p-goods-information-action__term');

                                dts.forEach(function(dt) {
                                    var colorName = dt.textContent.trim();
                                    var dd = dt.nextElementSibling; // 對應的 <dd>
                                    if (!dd) return;

                                    // 顏色縮圖
                                    // 顏色縮圖 - 在 DL（DT的父元素）裡面找 img
                                    var dlParent = dt.parentElement; // DL.p-goods-information-action
                                    var thumbImg = null;
                                    if (dlParent) {
                                        thumbImg = dlParent.querySelector('img[src*="imgz.jp"], img[src*="zozo"]');
                                    }
                                    if (!thumbImg && dd) {
                                        thumbImg = dd.querySelector('img');
                                    }
                                    var colorImage = '';
                                    if (thumbImg) {
                                        colorImage = thumbImg.src || thumbImg.getAttribute('data-src') || '';
                                        // 把縮圖 URL 換成較大尺寸
                                        if (colorImage.indexOf('_35.') !== -1) {
                                            colorImage = colorImage.replace(/_35\./, '_500.');
                                        }
                                    }

                                    // 該顏色下的所有尺寸
                                    var sizeItems = dd.querySelectorAll('li.p-goods-add-cart-list__item');
                                    sizeItems.forEach(function(li) {
                                        var fullText = li.textContent.replace(/\s+/g, ' ').trim();

                                        // 尺寸: "M / 在庫あり" → M
                                        var sizeMatch = fullText.match(/^\s*([A-Z0-9SMLXF]+(?:\s*[\-~]\s*[A-Z0-9SMLXF]+)?)\s*[\/／]/);
                                        if (!sizeMatch) sizeMatch = fullText.match(/^\s*(フリー|FREE|F|ONE\s*SIZE|ワンサイズ|\d+(?:cm)?)\s*[\/／]/i);
                                        var size = sizeMatch ? sizeMatch[1].trim() : '';
                                        if (!size) return;

                                        // 庫存
                                        var inStock = fullText.indexOf('在庫あり') !== -1;
                                        var soldOut = fullText.indexOf('SOLD') !== -1;

                                        // SKU: form hidden input
                                        var sku = '';
                                        var form = li.querySelector('form');
                                        if (form) {
                                            form.querySelectorAll('input[type="hidden"]').forEach(function(inp) {
                                                var n = (inp.name || '').toLowerCase();
                                                if (n === 'did' || n === 'sid' || n === 'detail_id' || n === 'gid') sku = inp.value || '';
                                            });
                                            if (!sku && form.action) {
                                                var dm = form.action.match(/[?&]did=(\d+)/);
                                                if (dm) sku = dm[1];
                                            }
                                        }

                                        r.variants.push({
                                            color: colorName,
                                            size: size,
                                            sku: sku,
                                            price: r.price,
                                            in_stock: inStock && !soldOut,
                                            image: colorImage
                                        });
                                    });
                                });

                                // 如果沒有 DT/DD 結構（單色商品），fallback 到 li 直接抓
                                if (r.variants.length === 0) {
                                    var items = document.querySelectorAll('li.p-goods-add-cart-list__item');
                                    items.forEach(function(li) {
                                        var fullText = li.textContent.replace(/\s+/g, ' ').trim();
                                        var sizeMatch = fullText.match(/^\s*([A-Z0-9SMLXF]+(?:\s*[\-~]\s*[A-Z0-9SMLXF]+)?)\s*[\/／]/);
                                        if (!sizeMatch) sizeMatch = fullText.match(/^\s*(フリー|FREE|F|ONE\s*SIZE|ワンサイズ|\d+(?:cm)?)\s*[\/／]/i);
                                        var size = sizeMatch ? sizeMatch[1].trim() : '';
                                        if (!size) return;

                                        var inStock = fullText.indexOf('在庫あり') !== -1;
                                        var soldOut = fullText.indexOf('SOLD') !== -1;
                                        var sku = '';
                                        var form = li.querySelector('form');
                                        if (form) {
                                            form.querySelectorAll('input[type="hidden"]').forEach(function(inp) {
                                                var n = (inp.name || '').toLowerCase();
                                                if (n === 'did' || n === 'sid' || n === 'detail_id' || n === 'gid') sku = inp.value || '';
                                            });
                                        }

                                        r.variants.push({
                                            color: '',
                                            size: size,
                                            sku: sku,
                                            price: r.price,
                                            in_stock: inStock && !soldOut,
                                            image: ''
                                        });
                                    });
                                }

                                var dtTexts=[]; dts.forEach(function(dt,i){dtTexts.push(dt.textContent.trim().substring(0,20));}); 
                                // 全面搜尋顏色圖片位置
                                var colorImgDebug = '';
                                // 1. 找所有含 color/thumb/swatch 的 class
                                var colorEls = document.querySelectorAll('[class*="color"] img, [class*="thumb"] img, [class*="swatch"] img, [class*="Color"] img, [class*="Thumb"] img');
                                colorImgDebug += 'colorEls:' + colorEls.length;
                                if (colorEls.length > 0) { colorImgDebug += '(' + colorEls[0].src.substring(0, 80) + ')'; }
                                // 2. 找 DT 的父元素有沒有圖片
                                if (dts.length > 0) {
                                    var parent = dts[0].parentElement;
                                    if (parent) {
                                        var parentImgs = parent.querySelectorAll('img');
                                        colorImgDebug += ' | parent_imgs:' + parentImgs.length;
                                        // 往上再找一層
                                        var grandparent = parent.parentElement;
                                        if (grandparent) {
                                            var gpImgs = grandparent.querySelectorAll('img');
                                            colorImgDebug += ' | gp_imgs:' + gpImgs.length;
                                            if (gpImgs.length > 0) { colorImgDebug += '(' + gpImgs[0].src.substring(0, 80) + ')'; }
                                        }
                                    }
                                }
                                // 3. 找 button 裡的 img（可能是顏色按鈕）
                                var btnImgs = document.querySelectorAll('button img[src*="imgz.jp"], button img[src*="zozo"]');
                                colorImgDebug += ' | btn_imgs:' + btnImgs.length;
                                if (btnImgs.length > 0) { colorImgDebug += '(' + btnImgs[0].src.substring(0, 80) + ')'; }
                                // 4. 找所有小圖（可能是色票）
                                var smallImgs = document.querySelectorAll('img[width], img[class*="small"], img[class*="chip"]');
                                colorImgDebug += ' | small_imgs:' + smallImgs.length;
                                // 5. dump DT 附近的 HTML 結構
                                if (dts.length > 0) {
                                    var dtParent = dts[0].closest('dl') || dts[0].parentElement;
                                    if (dtParent && dtParent.parentElement) {
                                        var sibHtml = '';
                                        var sibs = dtParent.parentElement.children;
                                        for (var si = 0; si < Math.min(sibs.length, 5); si++) {
                                            sibHtml += sibs[si].tagName + '.' + (sibs[si].className || '').substring(0, 40) + ' ';
                                        }
                                        colorImgDebug += ' | siblings:' + sibHtml.trim();
                                    }
                                }
                                r.variant_debug += ' | dts:' + dts.length + '(' + dtTexts.join(',') + ') | ' + colorImgDebug + ' | parsed:' + r.variants.length;

                                // Dump first form for debugging sku
                                var firstForm = document.querySelector('li.p-goods-add-cart-list__item form');
                                if (firstForm) {
                                    var formInfo = 'action:' + (firstForm.action||'').substring(0, 60);
                                    firstForm.querySelectorAll('input').forEach(function(inp) {
                                        formInfo += ' | ' + (inp.name||inp.type) + '=' + (inp.value||'').substring(0, 30);
                                    });
                                    r.variant_debug += ' | form: ' + formInfo;
                                }

                                // Dedup: 同色同尺寸只留一個
                                var seen = {};
                                var unique = [];
                                r.variants.forEach(function(v) {
                                    var key = v.color.replace(/s+/g,'') + '|' + v.size.replace(/s+/g,'');
                                    if (!seen[key]) {
                                        seen[key] = true;
                                        unique.push(v);
                                    }
                                });
                                r.variants = unique;
                            }

                            // === OG fallback ===
                            if (!r.title) {
                                var og = document.querySelector('meta[property="og:title"]');
                                if (og) r.title = og.content.replace(/\s*[-|]\s*ZOZOTOWN.*$/, '');
                            }
                            if (r.images.length === 0) {
                                var ogImg = document.querySelector('meta[property="og:image"]');
                                if (ogImg && ogImg.content) r.images.push(ogImg.content);
                            }

                            if (!r.price) {
                                document.querySelectorAll('[class*="price"], [class*="Price"]').forEach(function(el) {
                                    if (!r.price) {
                                        var pm = el.textContent.match(/[\u00a5\uffe5]([\d,]+)/);
                                        if (pm) { r.price = parseInt(pm[1].replace(/,/g,'')); r.price_text = '\u00a5' + r.price.toLocaleString(); }
                                    }
                                });
                            }

                            var seen = {};
                            r.images.forEach(function(u){ seen[u] = true; });
                            document.querySelectorAll('img[src*="c.imgz.jp"], img[data-src*="c.imgz.jp"]').forEach(function(img) {
                                var src = img.src || img.getAttribute('data-src') || '';
                                if (src && !seen[src] && img.naturalWidth > 50) {
                                    r.images.push(src);
                                    seen[src] = true;
                                }
                            });
                            r.images = r.images.slice(0, 20);

                            return r;
                        """)
                        # 印 variant debug 資訊
                        if result:
                            vd = result.get('variant_debug', '')
                            vs = result.get('variants', [])
                            print(f"[ZOZO] variant_debug: {vd}")
                            print(f"[ZOZO] variants: {len(vs)} 個")
                            for v in vs[:6]:
                                print(f"  - {v.get('color','')} / {v.get('size','')} | stock={v.get('in_stock')} | sku={v.get('sku','')} | img={v.get('image','')[:60]}")
                        return result

                    if 'access denied' in (title or '').lower() and i >= 2:
                        print("[ZOZO] 被 Akamai 擋住")
                        break

                print("[ZOZO] ⚠️ 未取得資料")

            except Exception as e:
                print(f"[ZOZO] SeleniumBase 錯誤: {e}")
                import traceback; traceback.print_exc()
                # driver 可能壞了，標記重建
                try:
                    self._driver.quit()
                except:
                    pass
                self._driver = None

            return None

    async def _scrape_zozo_via_proxy(self, url: str) -> ProductInfo | None:
        """備用：代理到外部 product-fetcher"""
        product = ProductInfo(source_url=url)
        try:
            print(f"[ZOZO] 代理到 {ZOZO_SCRAPER_URL}")
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{ZOZO_SCRAPER_URL.rstrip('/')}/api/fetch",
                    json={"url": url},
                )
                data = resp.json()

            if data.get("error"):
                print(f"[ZOZO] 外部爬蟲錯誤: {data['error']}")
                return None

            product.title = data.get("title", "")
            product.price_jpy = data.get("price", 0) or None
            product.brand = data.get("brand", "")
            product.description = data.get("description", "")[:500]
            images = data.get("images", [])
            if images:
                product.image_url = images[0]
                product.extra_images = images[1:9]
            return product if product.title else None

        except Exception as e:
            print(f"[ZOZO] 外部爬蟲連線失敗: {e}")
            return None

    # ============================================================
    # 通用 - Playwright（其他日本網站）
    # ============================================================
    async def _scrape_with_playwright(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            html = await self._fetch_playwright(url)
            soup = BeautifulSoup(html, "html.parser")

            # JSON-LD
            self._extract_json_ld(soup, product)
            # OG tags
            self._extract_og_tags(soup, product)
            # Generic HTML
            if not product.title or not product.price_jpy:
                self._extract_generic(soup, product)

            # 價格合理性檢查
            if product.price_jpy and (product.price_jpy < 100 or product.price_jpy > 1000000):
                print(f"[Generic] ⚠️ 價格不合理 ¥{product.price_jpy}，重置")
                product.price_jpy = None

            # 相對 URL 修正
            if product.image_url and not product.image_url.startswith("http"):
                base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                product.image_url = base + product.image_url

        except Exception as e:
            print(f"[Generic] ❌ 錯誤: {e}")

        return product

    async def _fetch_playwright(self, url: str) -> str:
        """通用網頁抓取（用 httpx，大部分網站不需要 JS 渲染）"""
        async with httpx.AsyncClient(
            timeout=SCRAPE_TIMEOUT,
            follow_redirects=True,
            headers={
                'User-Agent': USER_AGENT,
                'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            },
        ) as client:
            resp = await client.get(url)
            return resp.text

    # ============================================================
    # Extractors（通用解析器）
    # ============================================================
    def _extract_json_ld(self, soup, product):
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") in ("Product", "IndividualProduct")), data[0] if data else {})
                if data.get("@type") not in ("Product", "IndividualProduct"):
                    if "@graph" in data:
                        for item in data["@graph"]:
                            if item.get("@type") == "Product":
                                data = item
                                break
                    else:
                        continue

                if not product.title:
                    product.title = data.get("name", "")
                if not product.image_url and data.get("image"):
                    img = data["image"]
                    product.image_url = img[0] if isinstance(img, list) else (img.get("url", "") if isinstance(img, dict) else str(img))
                if not product.brand and data.get("brand"):
                    b = data["brand"]
                    product.brand = b.get("name", "") if isinstance(b, dict) else str(b)
                if not product.description:
                    product.description = (data.get("description") or "")[:500]
                if not product.price_jpy:
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        p = self._normalize_price(price)
                        if p and 100 <= p <= 1000000:
                            product.price_jpy = p
            except (json.JSONDecodeError, StopIteration):
                continue

    def _extract_og_tags(self, soup, product):
        og = {}
        for meta in soup.find_all("meta", property=True):
            og[meta["property"]] = meta.get("content", "")
        if not product.title:
            product.title = og.get("og:title", "")
        if not product.image_url:
            product.image_url = og.get("og:image", "")
        if not product.description:
            product.description = og.get("og:description", "")[:500]
        if not product.price_jpy:
            p = og.get("product:price:amount", "")
            if p:
                product.price_jpy = self._normalize_price(p)

    def _extract_generic(self, soup, product):
        if not product.title:
            t = soup.find("title")
            if t:
                product.title = t.get_text(strip=True)
        if not product.image_url:
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if not any(s in src.lower() for s in ["logo", "icon", "banner", "sprite", "blank"]):
                    product.image_url = src
                    break
        if not product.price_jpy:
            product.price_jpy = self._find_price_in_html(soup)

    def _find_price_in_html(self, soup):
        text = soup.get_text()
        # 優先：税込價格
        tax_prices = re.findall(r'([0-9,]+)\s*円\s*[（\(]?\s*税込', text)
        if tax_prices:
            p = self._normalize_price(tax_prices[0])
            if p and 100 <= p <= 1000000:
                return p
        # price class
        for sel in ['[class*="price"]', '[class*="Price"]', '[id*="price"]']:
            for el in soup.select(sel):
                m = re.search(r'[¥￥]?\s*([\d,]+)', el.get_text(strip=True))
                if m:
                    p = int(m.group(1).replace(',', ''))
                    if 100 <= p <= 1000000:
                        return p
        # ¥ 或 円
        prices = re.findall(r'[¥￥]\s*([0-9,]+)', text)
        prices += re.findall(r'([0-9,]+)\s*円', text)
        if prices:
            normalized = [self._normalize_price(p) for p in prices]
            normalized = [p for p in normalized if p and 100 <= p <= 1000000]
            if normalized:
                return Counter(normalized).most_common(1)[0][0]
        return None

    @staticmethod
    def _normalize_price(price):
        if isinstance(price, (int, float)):
            return int(price)
        if isinstance(price, str):
            cleaned = re.sub(r'[^0-9.]', '', price)
            return int(float(cleaned)) if cleaned else None
        return None
