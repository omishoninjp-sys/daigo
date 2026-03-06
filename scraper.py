"""
商品資訊爬取模組 v3.3
- Amazon.co.jp: requests + BeautifulSoup（快速、穩定）
- Uniqlo JP: 內部 API（超快、不需瀏覽器）
- MUJI JP: HTML + 內部 API（不需瀏覽器）
- ZOZOTOWN: undetected-chromedriver（繞過 Akamai）
- にじさんじ: httpx + BeautifulSoup（SSR，不需瀏覽器）
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
    if "nijisanji.jp" in host:
        return "nijisanji"
    if "palcloset.jp" in host:
        return "palcloset"
    if "rakuten.co.jp" in host:
        return "rakuten"
    # Shopify 日本商店
    if "nanouniverse" in host or "store.nanouniverse.jp" in host:
        return "shopify_jp"
    # Mercari
    if "mercari.com" in host or "jp.mercari.com" in host:
        return "mercari"
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
        url = self._normalize_url(url)   
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
        elif platform == "nijisanji":
            product = await self._scrape_nijisanji(url)
        elif platform == "palcloset":
            product = await self._scrape_palcloset(url)
        elif platform == "shopify_jp":
            product = await self._scrape_shopify_jp(url)
        elif platform == "mercari":
            product = await self._scrape_mercari(url)
        elif "oakley.com" in url:
            product = await self._scrape_oakley(url)
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
                _asin_pattern = r'/(?:dp|gp/product|gp/aw/d|ASIN)/([A-Z0-9]{10})'
                _desktop_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
                    resp = await c.get(url, headers={"User-Agent": _desktop_ua})
                    # 掃全部 redirect 歷史 + 最終 URL，只要找到 ASIN 就停
                    all_urls = [str(r.url) for r in resp.history] + [str(resp.url)]
                    print(f"[Amazon] redirect chain: {all_urls}")
                    found_asin = None
                    for _u in all_urls:
                        _m = re.search(_asin_pattern, _u)
                        if _m:
                            found_asin = _m.group(1)
                            break
                if found_asin:
                    url = f"https://www.amazon.co.jp/dp/{found_asin}"
                    print(f"[Amazon] 短連結展開 → {url}")
                else:
                    url = str(resp.url)
                    print(f"[Amazon] 短連結展開 (無法提取 ASIN): {url}")
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
                # 如果頁面已有商品標題，就不是 age gate（避免誤判）
                if is_age_gate and "productTitle" in html:
                    is_age_gate = False
                    print(f"[Amazon] age gate 誤判排除（productTitle 存在）")

                if is_age_gate:
                    print(f"[Amazon] 偵測到年齡確認頁面 (url: {final_url[:80]})")
                    product.is_adult = True
                    asin = am.group(1)

                    # age gate 繞過後，直接重新 GET 正確的 ASIN URL
                    # 不跟隨「はい」redirect，避免跑到搜尋結果或其他商品
                    try:
                        resp_retry = await client.get(
                            f"https://www.amazon.co.jp/dp/{asin}",
                            headers=headers,
                        )
                        if resp_retry.status_code == 200 and "productTitle" in resp_retry.text:
                            html = resp_retry.text
                            print(f"[Amazon] 直接重取 dp/{asin} 成功")
                        else:
                            # 嘗試帶 mature-content cookie 繞過
                            mature_cookies = {
                                "session-id": "355-0769823-1641625",
                                "i18n-prefs": "JPY",
                                "lc-acbjp": "ja_JP",
                                "sp-cdn": '"L5Z9:JP"',
                                "mature-content-preference": "1",
                            }
                            resp_mature = await client.get(
                                f"https://www.amazon.co.jp/dp/{asin}",
                                headers=headers,
                                cookies=mature_cookies,
                            )
                            if resp_mature.status_code == 200 and "productTitle" in resp_mature.text:
                                html = resp_mature.text
                                print(f"[Amazon] mature cookie 繞過成功")
                            else:
                                print(f"[Amazon] ⚠️ age gate 繞過失敗")
                    except Exception as e:
                        print(f"[Amazon] age gate 繞過錯誤: {e}")

            soup = BeautifulSoup(html, "html.parser")

            if soup.find("form", {"name": "signIn"}) or soup.select_one("#ap_email"):
                return product

            el = soup.select_one("#productTitle")
            if el:
                product.title = el.get_text(strip=True)
            if not product.title:
                t = soup.find("title")
                if t:
                    txt = t.get_text(strip=True)
                    if "サインイン" not in txt and "Sign" not in txt:
                        product.title = txt

            el = soup.select_one("#bylineInfo") or soup.select_one(".po-brand .po-break-word")
            if el:
                b = el.get_text(strip=True)
                b = re.sub(r'^(ブランド[：:]\s*|Brand[：:]\s*|Visit the |のストアを表示)', '', b)
                product.brand = re.sub(r'\s*(Store|ストア)$', '', b).strip()

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
    # にじさんじオフィシャルストア（Salesforce Commerce Cloud）
    # SSR 頁面，httpx 即可，不需要 Chrome
    # ============================================================
    async def _scrape_nijisanji(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="にじさんじ")

        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
            }

            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    print(f"[Nijisanji] HTTP {resp.status_code}")
                    return product
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                og = soup.find("meta", property="og:title")
                if og:
                    product.title = og.get("content", "").replace("｜にじさんじオフィシャルストア", "").strip()

            # === 圖片 ===
            base = "https://shop.nijisanji.jp"
            imgs = []
            seen_imgs = set()
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if "nijisanji-master-catalog" in src and ("physical" in src or "digital" in src):
                    if not src.startswith("http"):
                        src = base + src
                    if src not in seen_imgs:
                        seen_imgs.add(src)
                        imgs.append(src)

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:9]

            # === Variants（商品選択モーダル内のリスト） ===
            variants = []
            min_price = None

            for li in soup.find_all("li"):
                text = li.get_text(" ", strip=True)
                # 找包含價格的 li
                price_m = re.search(r'[¥￥]([\d,]+)\s*税込', text)
                if not price_m:
                    price_m = re.search(r'([\d,]+)\s*税込', text)
                if not price_m:
                    continue

                price = int(price_m.group(1).replace(",", ""))
                if price < 100 or price > 500000:
                    continue

                # 商品名：去掉價格和多餘標記
                name = text
                name = re.sub(r'[¥￥][\d,]+\s*税込', '', name).strip()
                name = re.sub(r'[\d,]+\s*税込', '', name).strip()
                name = re.sub(r'\+\s*まもなく(終了|販売)', '', name).strip()
                name = re.sub(r'まもなく(終了|販売)', '', name).strip()
                name = re.sub(r'\s+', ' ', name).strip()

                if len(name) < 3:
                    continue
                if any(skip in name for skip in ["カート", "ログイン", "お気に入り", "ページ", "TOP", "閉じる", "選択してください"]):
                    continue

                if min_price is None or price < min_price:
                    min_price = price

                variants.append({
                    "color": "",
                    "size": name,
                    "sku": "",
                    "price": price,
                    "in_stock": "在庫なし" not in text and "売り切れ" not in text,
                    "image": product.image_url,
                })

            # 重複排除
            seen_v = set()
            unique_variants = []
            for v in variants:
                key = f"{v['size']}|{v['price']}"
                if key not in seen_v:
                    seen_v.add(key)
                    unique_variants.append(v)
            product.variants = unique_variants

            # === 價格 ===
            if min_price:
                product.price_jpy = min_price
            else:
                for pat in [r'[¥￥]([\d,]+)\s*税込', r'[¥￥]([\d,]+)']:
                    pm = re.search(pat, html)
                    if pm:
                        p = int(pm.group(1).replace(",", ""))
                        if 100 < p < 500000:
                            product.price_jpy = p
                            break

            # === 說明 ===
            for section_title in ["商品説明", "商品仕様"]:
                tag = soup.find(lambda t: t.name and t.get_text(strip=True) == section_title)
                if tag:
                    next_el = tag.find_next_sibling()
                    if next_el:
                        product.description = next_el.get_text(" ", strip=True)[:500]
                        break

            print(f"[Nijisanji] ✅ {product.title[:40]} / ¥{product.price_jpy} / {len(product.variants)} variants")

        except Exception as e:
            print(f"[Nijisanji] ❌ 錯誤: {type(e).__name__}: {e}")

        return product
    # ============================================================
    # PAL CLOSET
    # ============================================================
    async def _scrape_palcloset(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ja,en-US;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Referer": "https://www.palcloset.jp/",
            }
            async with httpx.AsyncClient(follow_redirects=True, timeout=SCRAPE_TIMEOUT, headers=headers) as client:
                resp = await client.get(url)
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # 標題
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                self._extract_og_tags(soup, product)

            # 品牌（URL の b= パラメータ or breadcrumb）
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(url).query)
            brand_param = qs.get("b", [""])[0]
            if brand_param:
                for a in soup.select("ol a, nav a, [class*='breadcrumb'] a"):
                    if brand_param in a.get("href", "") and a.get_text(strip=True):
                        product.brand = a.get_text(strip=True)
                        break
                if not product.brand:
                    product.brand = brand_param

            # 価格（JSON-LD 優先）
            self._extract_json_ld(soup, product)
            if not product.price_jpy:
                for pat in [r'"price"\s*:\s*"?([\d.]+)"?', r'[¥￥]([\d,]+)\s*(?:税込|円)']:
                    pm = re.search(pat, html)
                    if pm:
                        p = int(float(pm.group(1).replace(",", "")))
                        if 100 <= p <= 1000000:
                            product.price_jpy = p
                            break

            # 主画像
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if "contents.palcloset.jp" in src and not src.startswith("data:"):
                    product.image_url = src
                    break

            # カラー variants（cbk_sku_wrapper から抽出）
            seen_colors: set = set()
            variants = []

            for wrapper in soup.find_all('div', class_='cbk_sku_wrapper'):
                color_tag = wrapper.find('p', class_='cart_pic__desc__color')
                img_tag = wrapper.find('div', class_='cart_pic').find('img') if wrapper.find('div', class_='cart_pic') else None
                
                if not color_tag:
                    continue
                color = color_tag.get_text(strip=True).replace('カラー：', '')
                img_url = img_tag['src'] if img_tag and img_tag.get('src') else ''
                
                if not color or color in seen_colors:
                    continue
                seen_colors.add(color)
                variants.append({
                    "color": color,
                    "size": "",
                    "sku": color,
                    "price": product.price_jpy or 0,
                    "in_stock": True,
                    "image": img_url,
                })

            product.variants = variants
            product.extra_images = [v["image"] for v in variants if v["image"] and v["image"] != product.image_url][:8]
            print(f"[PalCloset] ✅ {product.title[:40] if product.title else '?'} / ¥{product.price_jpy} / {len(variants)} colors: {[v['color'] for v in variants]}")
        except Exception as e:
            print(f"[PalCloset] ❌ {type(e).__name__}: {e}")
        return product

    # ============================================================
    # Uniqlo JP - 內部 API + HTML 解析（不需瀏覽器）
    # ============================================================
    async def _scrape_uniqlo(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="UNIQLO")

        m = re.search(r'/products/(E?\d[\w-]+)', url)
        if not m:
            print(f"[Uniqlo] ❌ 無法從 URL 提取商品代碼: {url}")
            return product

        product_code = m.group(1)
        product_id = re.sub(r'[^0-9]', '', product_code.split('-')[0])

        color_from_url = ""
        cm = re.search(r'colorDisplayCode=(\w+)', url)
        if cm:
            color_from_url = cm.group(1)

        print(f"[Uniqlo] 商品代碼: {product_code} (ID: {product_id}, color: {color_from_url})")

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

            cookies = {}
            html_text = ""
            try:
                print(f"[Uniqlo] Step 1: 抓 HTML 頁面...")
                resp = await client.get(url, headers=browser_headers)
                html_text = resp.text
                cookies = dict(resp.cookies)
                print(f"[Uniqlo] HTML: {resp.status_code}, {len(html_text)} bytes, cookies: {list(cookies.keys())[:5]}")
                self._parse_uniqlo_html(html_text, product_id, product)
            except Exception as e:
                print(f"[Uniqlo] HTML 抓取錯誤: {type(e).__name__}: {e}")

            embedded_found = False
            if html_text:
                embedded_found = self._parse_uniqlo_embedded_json(html_text, product_code, product_id, product)
                if embedded_found and product.price_jpy and product.variants:
                    print(f"[Uniqlo] ✅ 內嵌 JSON 解析成功: {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
                    return product

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
                        print(f"[Uniqlo] API keys: {list(api_data.keys())[:8]}")
                        product = self._parse_uniqlo_api(api_data, product_code, product_id, product)
                        if product.price_jpy and product.variants:
                            print(f"[Uniqlo] ✅ API 完整解析: {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
                            return product
                        elif product.price_jpy:
                            print(f"[Uniqlo] API 取得價格 ¥{product.price_jpy:,} 但無 variants，繼續 fallback")
                            break
                        else:
                            print(f"[Uniqlo] API 回傳但未找到價格")
                    elif resp.status_code == 403:
                        print(f"[Uniqlo] API 403 Forbidden")
                    elif resp.status_code == 404:
                        print(f"[Uniqlo] API 404")
                    else:
                        print(f"[Uniqlo] API {resp.status_code}: {resp.text[:200]}")

                except Exception as e:
                    print(f"[Uniqlo] API 錯誤: {type(e).__name__}: {e}")

            if product.title and not product.variants:
                print(f"[Uniqlo] Step 4: 用 HTML 資料建構基本 variants")
                product = self._build_uniqlo_fallback_variants(product, product_id, color_from_url, html_text)

        if product.title:
            print(f"[Uniqlo] 最終結果: {product.title[:40]} / ¥{product.price_jpy or '?'} / {len(product.variants)} variants")
        else:
            print(f"[Uniqlo] ⚠️ 未取得資料")

        return product

    def _parse_uniqlo_embedded_json(self, html: str, product_code: str, product_id: str, product: ProductInfo) -> bool:
        soup = BeautifulSoup(html, "html.parser")

        for script in soup.find_all("script"):
            text = script.string or ""
            if not text or len(text) < 100:
                continue

            if "__NEXT_DATA__" in text or "window.__NEXT_DATA__" in text:
                try:
                    jm = re.search(r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</)', text, re.DOTALL)
                    if jm:
                        next_data = json.loads(jm.group(1))
                        props = next_data.get("props", {}).get("pageProps", {})
                        if props:
                            print(f"[Uniqlo] 找到 __NEXT_DATA__: keys={list(props.keys())[:5]}")
                            for key in ["product", "productDetail", "data", "initialData"]:
                                if key in props:
                                    self._parse_uniqlo_api({"result": {"items": {product_code: props[key]}}}, product_code, product_id, product)
                                    if product.price_jpy:
                                        return True
                except Exception as e:
                    print(f"[Uniqlo] __NEXT_DATA__ 解析錯誤: {e}")

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

            if product_id in text and ("price" in text.lower() or "prices" in text.lower()):
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
        items = {}
        if "result" in data:
            result = data["result"]
            items = result.get("items", {}) or result.get("products", {}) or {}
            if isinstance(items, list):
                items = {str(i.get("productId", i.get("id", idx))): i for idx, i in enumerate(items) if isinstance(i, dict)}
        elif "items" in data:
            items = data["items"]
        elif "products" in data:
            items = data["products"]

        prod = items.get(product_code) or items.get(product_id)
        if not prod:
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

        name = prod.get("name") or prod.get("productName") or prod.get("title") or ""
        if name:
            product.title = name

        price = self._extract_uniqlo_price(prod)
        if price and price > 0:
            product.price_jpy = price

        images = prod.get("images", {}) or {}
        img_urls = []

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

        if not img_urls:
            img_urls.append(f"https://image.uniqlo.com/UQ/ST3/jp/imagesgoods/{product_id}/item/jpgoods_69_{product_id}_3x4.jpg?width=600")

        if img_urls and not product.image_url:
            product.image_url = img_urls[0]
        if len(img_urls) > 1 and not product.extra_images:
            product.extra_images = img_urls[1:9]

        colors = prod.get("colors", {}) or {}
        sizes = prod.get("sizes", {}) or {}
        l2s = prod.get("l2s", []) or prod.get("stocks", []) or []

        variants = []

        if isinstance(colors, dict) and isinstance(sizes, dict) and colors and sizes:
            for color_code, color_info in colors.items():
                color_name = ""
                color_img = ""
                if isinstance(color_info, dict):
                    color_name = color_info.get("displayColorName") or color_info.get("name") or color_code
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
        for key in ["minPrice", "price", "retailPrice", "salePrice", "originPrice"]:
            v = prod.get(key)
            if v and isinstance(v, (int, float)) and v > 0:
                return int(v)

        prices = prod.get("prices") or prod.get("price") or {}
        if isinstance(prices, (int, float)) and prices > 0:
            return int(prices)

        if isinstance(prices, dict):
            for sub_key in ["promo", "base", "current", "sale", "original"]:
                sub = prices.get(sub_key)
                if isinstance(sub, dict):
                    v = sub.get("value") or sub.get("price") or sub.get("amount")
                    if v and float(v) > 0:
                        return int(float(v))
                elif isinstance(sub, (int, float)) and sub > 0:
                    return int(sub)

            v = prices.get("value") or prices.get("price") or prices.get("amount")
            if v and float(v) > 0:
                return int(float(v))

        return None

    def _build_uniqlo_fallback_variants(self, product: ProductInfo, product_id: str, color_from_url: str, html: str) -> ProductInfo:
        sizes_found = []
        size_pattern = r'\b(XS|S|M|L|XL|XXL|3XL|4XL)\b'
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        size_section = re.search(r'サイズ[：:]\s*(?:男女兼用|レディス|メンズ)?\s*\w+\s+((?:(?:XS|S|M|L|XL|XXL|3XL|4XL)\s*)+)', text)
        if size_section:
            sizes_found = re.findall(size_pattern, size_section.group(1))

        if not sizes_found:
            all_sizes = re.findall(size_pattern, text)
            seen = set()
            for s in all_sizes:
                if s not in seen:
                    seen.add(s)
                    sizes_found.append(s)

        color_name = ""
        color_match = re.search(r'カラー[：:]\s*(\d+)\s+(\w+)', text)
        if color_match:
            color_name = f"{color_match.group(1)} {color_match.group(2)}"
        elif color_from_url:
            color_name = color_from_url

        if not sizes_found:
            sizes_found = ["XS", "S", "M", "L", "XL", "XXL", "3XL"]

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
        soup = BeautifulSoup(html, "html.parser")

        og_title = soup.find("meta", property="og:title")
        if og_title:
            product.title = og_title.get("content", "").replace("| ユニクロ", "").strip()
        if not product.title:
            title_tag = soup.find("title")
            if title_tag:
                t = title_tag.get_text()
                product.title = t.split("|")[0].strip() if "|" in t else t.strip()

        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            product.image_url = og_img["content"]

        extra = []
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "image.uniqlo.com" in src and product_id in src and src not in extra:
                extra.append(src)
        if extra and not product.image_url:
            product.image_url = extra[0]
        product.extra_images = [u for u in extra if u != product.image_url][:8]

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
            except:
                pass

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

        m = re.search(r'/detail/(\d{10,14})', url)
        if not m:
            print(f"[MUJI] ❌ 無法從 URL 提取商品代碼: {url}")
            return product

        jan_code = m.group(1)
        print(f"[MUJI] JAN: {jan_code}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

        proxy_arg = PROXY_URL if PROXY_URL else None

        async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:

            html_text = ""
            cookies = {}
            try:
                resp = await client.get(url, headers=headers)
                html_text = resp.text
                cookies = dict(resp.cookies)
                print(f"[MUJI] HTML: {resp.status_code}, {len(html_text)} bytes")
            except Exception as e:
                print(f"[MUJI] HTML 錯誤: {type(e).__name__}: {e}")

            if html_text:
                soup = BeautifulSoup(html_text, "html.parser")

                title_tag = soup.find("title")
                if title_tag:
                    t = title_tag.get_text()
                    product.title = t.replace("| 無印良品", "").replace("|無印良品", "").strip()

                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    t = og_title["content"].replace("| 無印良品", "").strip()
                    if t:
                        product.title = t

                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get("content"):
                    product.image_url = og_img["content"]

                if not product.image_url:
                    product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

                extra = []
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if "muji.com" in src and jan_code in src and src not in extra:
                        extra.append(src)
                product.extra_images = [u for u in extra if u != product.image_url][:8]

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
                    except:
                        pass

                self._parse_muji_embedded_json(soup, jan_code, product)

                if product.price_jpy and product.title:
                    return product

            api_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja-JP,ja;q=0.9",
                "Referer": url,
                "Origin": "https://www.muji.com",
                "X-Requested-With": "XMLHttpRequest",
            }

            api_urls = [
                f"https://www.muji.com/jp/api/store/cmdty/detail/{jan_code}",
                f"https://www.muji.com/jp/api/store/v1/cmdty/{jan_code}",
                f"https://www.muji.com/jp/api/product/{jan_code}",
                f"https://www.muji.com/jp/api/v1/product/{jan_code}",
                f"https://www.muji.com/jp/store/api/cmdty/detail/{jan_code}",
            ]

            for api_url in api_urls:
                try:
                    resp = await client.get(api_url, headers=api_headers, cookies=cookies)
                    if resp.status_code == 200:
                        try:
                            api_data = resp.json()
                            self._parse_muji_api(api_data, jan_code, product)
                            if product.price_jpy:
                                break
                        except:
                            pass
                except Exception as e:
                    print(f"[MUJI] API 錯誤: {type(e).__name__}: {e}")

        if not product.price_jpy:
            try:
                success = await self._muji_chrome_fallback(url, product, jan_code)
            except Exception as e:
                print(f"[MUJI] Chrome UC 錯誤: {type(e).__name__}: {e}")

        if not product.image_url:
            product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

        return product

    def _parse_muji_embedded_json(self, soup, jan_code: str, product: ProductInfo):
        for script in soup.find_all("script"):
            text = script.string or ""
            if not text or len(text) < 50:
                continue

            if "__NEXT_DATA__" in text:
                try:
                    jm = re.search(r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</)', text, re.DOTALL)
                    if jm:
                        data = json.loads(jm.group(1))
                        props = data.get("props", {}).get("pageProps", {})
                        self._parse_muji_api(props, jan_code, product)
                        if product.price_jpy:
                            return
                except Exception as e:
                    print(f"[MUJI] __NEXT_DATA__ 錯誤: {e}")

            for pat in [r'__INITIAL_STATE__\s*=\s*({.+?})\s*;',
                       r'window\.PRODUCT\s*=\s*({.+?})\s*;',
                       r'window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*;']:
                try:
                    sm = re.search(pat, text, re.DOTALL)
                    if sm:
                        data = json.loads(sm.group(1))
                        self._parse_muji_api(data, jan_code, product)
                        if product.price_jpy:
                            return
                except:
                    pass

            if jan_code in text and ('"price"' in text or '"salePrice"' in text):
                price_m = re.search(r'"(?:sale)?[Pp]rice"\s*:\s*(\d{3,6})', text)
                if price_m and not product.price_jpy:
                    product.price_jpy = int(price_m.group(1))

    def _parse_muji_api(self, data: dict, jan_code: str, product: ProductInfo):
        prod = None

        if "janCode" in data or "commodityCode" in data:
            prod = data

        for key in ["product", "cmdty", "detail", "commodity", "data", "item"]:
            if key in data and isinstance(data[key], dict):
                prod = data[key]
                break

        if not prod:
            return

        name = prod.get("name") or prod.get("commodityName") or prod.get("productName") or ""
        if name and not product.title:
            product.title = name

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

        images = prod.get("images", []) or prod.get("imageList", []) or []
        if isinstance(images, list):
            for img in images:
                u = img.get("url", "") if isinstance(img, dict) else str(img)
                if u and "muji.com" in u:
                    if not product.image_url:
                        product.image_url = u
                    elif u != product.image_url and len(product.extra_images) < 8:
                        product.extra_images.append(u)

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

    async def _muji_chrome_fallback(self, url: str, product: ProductInfo, jan_code: str) -> bool:
        import time as _time, base64

        with self._driver_lock:
          for attempt in range(2):
            try:
                driver = self._ensure_driver()
                if not driver:
                    return False

                self._driver_use_count += 1

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
                        break

                if session_dead:
                    self._driver = None
                    self._create_driver()
                    continue

                if not html or len(html) < 5000:
                    return False

                soup = BeautifulSoup(html, "html.parser")

                if not product.title:
                    og_title = soup.find("meta", property="og:title")
                    if og_title and og_title.get("content"):
                        product.title = og_title["content"].replace("| 無印良品", "").strip()
                    else:
                        title_tag = soup.find("title")
                        if title_tag:
                            product.title = title_tag.get_text().replace("| 無印良品", "").replace("|無印良品", "").strip()

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
                    except:
                        pass

                self._parse_muji_embedded_json(soup, jan_code, product)

                if not product.price_jpy:
                    page_text = soup.get_text(" ", strip=True)
                    for pat in [r'¥\s*([\d,]+)', r'([\d,]+)\s*円', r'([\d,]+)\s*[（(]税込']:
                        pm = re.search(pat, page_text)
                        if pm:
                            try:
                                p = int(pm.group(1).replace(",", ""))
                                if 50 < p < 500000:
                                    product.price_jpy = p
                                    break
                            except:
                                pass

                og_img = soup.find("meta", property="og:image")
                img_url = og_img["content"] if og_img and og_img.get("content") else f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"
                product.image_url = img_url

                try:
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
                except Exception as e:
                    print(f"[MUJI] 圖片下載失敗: {type(e).__name__}: {e}")

                self._extract_muji_variants_from_html(driver, soup, product, jan_code)

                return bool(product.price_jpy)

            except Exception as e:
                err_name = type(e).__name__
                if "InvalidSession" in err_name and attempt == 0:
                    self._driver = None
                    self._create_driver()
                    continue
                return False
          return False

    _VALID_SIZES = {
        "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL",
        "F", "フリー", "FREE",
        *[str(n) for n in range(19, 32)],
        *[str(n) for n in range(55, 120, 5)],
    }

    def _extract_muji_variants_from_html(self, driver, soup, product: ProductInfo, jan_code: str):
        try:
            valid_sizes_js = json.dumps(list(self._VALID_SIZES))

            variants_js = driver.execute_script(f"""
                var validSizes = new Set({valid_sizes_js});
                var results = {{sizes: [], colors: []}};

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

                var colorEls = document.querySelectorAll(
                    '[class*="color"] button, [class*="Color"] button, ' +
                    '.cmdty-color-list button, [aria-label*="カラー"]'
                );
                colorEls.forEach(function(el) {{
                    var text = (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent).trim();
                    if (text && text.length < 15 && !text.includes('カート') &&
                        !text.includes('閉じる') && !text.includes('確認') &&
                        !results.colors.includes(text)) {{
                        results.colors.push(text);
                    }}
                }});

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

            if not sizes:
                page_text = soup.get_text(" ", strip=True)
                size_section = re.search(r'サイズ[：:\s]*(?:[\w・]+\s*)?((?:(?:XS|S|M|L|XL|XXL|3XL|4XL|F|フリー)\s*[/／・]?\s*)+)', page_text)
                if size_section:
                    sizes = re.findall(r'\b(XS|S|M|L|XL|XXL|3XL|4XL|F|フリー)\b', size_section.group(1))
                    sizes = list(dict.fromkeys(sizes))

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

        except Exception as e:
            print(f"[MUJI] variant 提取錯誤: {type(e).__name__}: {e}")

    # ============================================================
    # BEAMS - httpx + BeautifulSoup
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
                except Exception as e:
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

            item_id_match = re.search(r'/(\d{10,})/?$', url.rstrip("/"))
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
            sizes = []

            for h4 in soup.find_all("h4"):
                text = h4.get_text(strip=True)
                if text and len(text) < 20 and re.match(r'^[A-Z\s]+$', text):
                    colors.append(text)

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

                for color in colors:
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
        import time as _time

        with self._driver_lock:
          for attempt in range(2):
            try:
                driver = self._ensure_driver()
                if not driver:
                    return None

                self._driver_use_count += 1

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

    # ============================================================
    # ZOZOTOWN - SeleniumBase UC + xvfb（繞過 Akamai）
    # ============================================================
    async def _scrape_zozotown(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

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
                variants = data.get("variants", [])
                if variants:
                    product.variants = variants
            else:
                print("[ZOZO] ⚠️ 未取得資料")
        except Exception as e:
            print(f"[ZOZO] ❌ 錯誤: {e}")

        if not product.title and ZOZO_SCRAPER_URL:
            result = await self._scrape_zozo_via_proxy(url)
            if result and result.title:
                return result

        return product

    def _fetch_zozo_uc(self, url: str) -> dict | None:
        import os, time as _time

        with self._driver_lock:
            try:
                driver = self._ensure_driver()
                if not driver:
                    return None

                if not self._proxy_verified and PROXY_URL:
                    try:
                        driver.get('http://httpbin.org/ip')
                        _time.sleep(1)
                        src = driver.page_source
                        print(f"[ZOZO] proxy IP: {src[:100]}")
                        self._proxy_verified = True
                    except Exception as e:
                        print(f"[ZOZO] proxy 測試: {type(e).__name__}")

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

                print(f"[ZOZO] 載入: {url}")
                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception as e:
                    print(f"[ZOZO] uc_open: {type(e).__name__}: {e}")

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

                    print(f"[ZOZO] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                    if has_data:
                        result = driver.execute_script(r"""
                            var r = {title:'', brand:'', price:0, price_text:'',
                                     original_price:0, original_price_text:'', discount:'',
                                     images:[], description:'', item_id:'', in_stock:true,
                                     variants:[], variant_debug:''};

                            var m = location.pathname.match(/\/goods(?:-sale)?\/(\d+)/);
                            if (m) r.item_id = m[1];

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
                                            offers.forEach(function(o) {
                                                if (o.price && !r.price) {
                                                    r.price = parseInt(o.price);
                                                    r.price_text = '\u00a5' + r.price.toLocaleString();
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

                            var nd = document.getElementById('__NEXT_DATA__');
                            if (nd) {
                                try {
                                    var ndata = JSON.parse(nd.textContent);
                                    var props = ndata.props && ndata.props.pageProps ? ndata.props.pageProps : {};
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

                                    r.variant_debug = 'pageProps keys: ' + Object.keys(props).join(',');
                                } catch(e) {
                                    r.variant_debug = 'NEXT_DATA error: ' + e.message;
                                }
                            }

                            if (r.variants.length === 0) {
                                var dts = document.querySelectorAll('dt.p-goods-information-action__term');

                                dts.forEach(function(dt) {
                                    var colorName = dt.textContent.trim();
                                    var dd = dt.nextElementSibling;
                                    if (!dd) return;

                                    var dlParent = dt.parentElement;
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
                                        if (colorImage.indexOf('_35.') !== -1) {
                                            colorImage = colorImage.replace(/_35\./, '_500.');
                                        }
                                    }

                                    var sizeItems = dd.querySelectorAll('li.p-goods-add-cart-list__item');
                                    sizeItems.forEach(function(li) {
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
                                            color: colorName,
                                            size: size,
                                            sku: sku,
                                            price: r.price,
                                            in_stock: inStock && !soldOut,
                                            image: colorImage
                                        });
                                    });
                                });

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
                        if result:
                            vd = result.get('variant_debug', '')
                            vs = result.get('variants', [])
                            print(f"[ZOZO] variant_debug: {vd}")
                            print(f"[ZOZO] variants: {len(vs)} 個")
                        return result

                    if 'access denied' in (title or '').lower() and i >= 2:
                        print("[ZOZO] 被 Akamai 擋住")
                        break

                print("[ZOZO] ⚠️ 未取得資料")

            except Exception as e:
                print(f"[ZOZO] SeleniumBase 錯誤: {e}")
                try:
                    self._driver.quit()
                except:
                    pass
                self._driver = None

            return None

    async def _scrape_zozo_via_proxy(self, url: str) -> ProductInfo | None:
        product = ProductInfo(source_url=url)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{ZOZO_SCRAPER_URL.rstrip('/')}/api/fetch",
                    json={"url": url},
                )
                data = resp.json()

            if data.get("error"):
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
    # Mercari
    # ============================================================
    async def _scrape_mercari(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        m = re.search(r'/item/(m\d+)', url)
        item_id = m.group(1) if m else ""

        try:
            html = None
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(15.0, connect=8.0),
                    follow_redirects=True,
                    headers={
                        'User-Agent': USER_AGENT,
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'ja,en-US;q=0.9',
                    },
                ) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        html = resp.text
                        if self._extract_mercari_next_data(html, product):
                            return product
                        soup = BeautifulSoup(html, "html.parser")
                        self._extract_json_ld(soup, product)
                        self._extract_og_tags(soup, product)
                        if product.title and product.price_jpy:
                            return product
            except Exception as e:
                print(f"[Mercari] httpx 失敗: {type(e).__name__}: {e}")

            chrome_data = await self._mercari_chrome_extract(url)

            if chrome_data:
                if chrome_data.get("title"):
                    product.title = chrome_data["title"]
                if chrome_data.get("price"):
                    product.price_jpy = self._normalize_price(chrome_data["price"])
                if chrome_data.get("image"):
                    product.image_url = chrome_data["image"]
                if chrome_data.get("extra_images"):
                    product.extra_images = chrome_data["extra_images"][:4]
                if chrome_data.get("description"):
                    product.description = chrome_data["description"][:500]
                if chrome_data.get("brand"):
                    product.brand = chrome_data["brand"]

                condition = chrome_data.get("condition", "")
                if condition and product.description:
                    product.description = f"【{condition}】\n{product.description}"
                elif condition:
                    product.description = f"【{condition}】"

        except Exception as e:
            print(f"[Mercari] ❌ 錯誤: {type(e).__name__}: {e}")

        return product

    def _extract_mercari_next_data(self, html: str, product: ProductInfo) -> bool:
        try:
            m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if not m:
                return False

            data = json.loads(m.group(1))

            def find_item(obj, depth=0):
                if depth > 8 or not isinstance(obj, dict):
                    return None
                if obj.get("name") and obj.get("price") and (obj.get("photos") or obj.get("thumbnails") or obj.get("imageUrls")):
                    return obj
                if obj.get("item") and isinstance(obj["item"], dict):
                    return find_item(obj["item"], depth + 1)
                for v in obj.values():
                    if isinstance(v, dict):
                        result = find_item(v, depth + 1)
                        if result:
                            return result
                    elif isinstance(v, list):
                        for item in v[:5]:
                            if isinstance(item, dict):
                                result = find_item(item, depth + 1)
                                if result:
                                    return result
                return None

            item = find_item(data)
            if not item:
                return False

            product.title = item.get("name", "") or item.get("productName", "")
            price = item.get("price", 0)
            if price:
                product.price_jpy = self._normalize_price(price)
            product.description = (item.get("description", "") or "")[:500]

            photos = item.get("photos") or item.get("thumbnails") or item.get("imageUrls") or []
            if isinstance(photos, list) and photos:
                if isinstance(photos[0], dict):
                    urls = [p.get("url") or p.get("imageUrl") or p.get("src", "") for p in photos]
                else:
                    urls = [str(p) for p in photos]
                urls = [u for u in urls if u]
                if urls:
                    product.image_url = urls[0]
                    product.extra_images = urls[1:5]

            brand = item.get("brand", {})
            if isinstance(brand, dict):
                product.brand = brand.get("name", "")
            elif isinstance(brand, str):
                product.brand = brand

            return bool(product.title and product.price_jpy)

        except (json.JSONDecodeError, KeyError, TypeError):
            return False

    async def _mercari_chrome_extract(self, url: str) -> dict | None:
        import time as _time

        with self._driver_lock:
          for attempt in range(2):
            try:
                driver = self._ensure_driver()
                if not driver:
                    return None

                self._driver_use_count += 1

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

                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=8)
                except Exception as e:
                    err_name = type(e).__name__
                    if "InvalidSession" in err_name or "invalid session" in str(e).lower():
                        self._driver = None
                        self._create_driver()
                        continue

                result = None
                for i in range(8):
                    _time.sleep(2)
                    try:
                        result = driver.execute_script("""
                            var r = {title:'', price:'', image:'', extra_images:[],
                                     description:'', brand:'', condition:'', debug:''};

                            try {
                                var nd = document.getElementById('__NEXT_DATA__');
                                if (nd) {
                                    var data = JSON.parse(nd.textContent);
                                    function findItem(obj, depth) {
                                        if (depth > 8 || !obj || typeof obj !== 'object') return null;
                                        if (obj.name && obj.price && (obj.photos || obj.thumbnails || obj.imageUrls)) return obj;
                                        if (obj.item && typeof obj.item === 'object') {
                                            var found = findItem(obj.item, depth+1);
                                            if (found) return found;
                                        }
                                        for (var k in obj) {
                                            if (typeof obj[k] === 'object') {
                                                var found = findItem(obj[k], depth+1);
                                                if (found) return found;
                                            }
                                        }
                                        return null;
                                    }
                                    var item = findItem(data, 0);
                                    if (item) {
                                        r.title = item.name || item.productName || '';
                                        r.price = String(item.price || '');
                                        r.description = (item.description || '').substring(0, 500);
                                        var brand = item.brand;
                                        if (brand && typeof brand === 'object') r.brand = brand.name || '';
                                        else if (typeof brand === 'string') r.brand = brand;
                                        var photos = item.photos || item.thumbnails || item.imageUrls || [];
                                        if (photos.length > 0) {
                                            var urls = photos.map(function(p) {
                                                return (typeof p === 'object') ? (p.url || p.imageUrl || p.src || '') : String(p);
                                            }).filter(function(u) { return u; });
                                            if (urls.length > 0) {
                                                r.image = urls[0];
                                                r.extra_images = urls.slice(1, 5);
                                            }
                                        }
                                        r.debug += 'next_data_ok';
                                    }
                                }
                            } catch(e) { r.debug += 'next_err:' + e.message + ' '; }

                            if (!r.title) {
                                var h1 = document.querySelector('h1');
                                if (h1) r.title = h1.textContent.trim();
                                if (!r.title) {
                                    var og = document.querySelector('meta[property="og:title"]');
                                    if (og) r.title = og.content || '';
                                }
                            }

                            if (!r.price) {
                                var priceEls = document.querySelectorAll('[class*="price"], [class*="Price"], [data-testid*="price"]');
                                for (var i = 0; i < priceEls.length; i++) {
                                    var txt = priceEls[i].textContent;
                                    var m = txt.match(/[¥￥]\\s*([\\d,]+)/);
                                    if (m) { r.price = m[1].replace(/,/g, ''); break; }
                                }
                            }

                            if (!r.image) {
                                var ogImg = document.querySelector('meta[property="og:image"]');
                                if (ogImg && ogImg.content) r.image = ogImg.content;
                                if (!r.image) {
                                    var imgs = document.querySelectorAll('img[src*="static.mercdn.net"]');
                                    var goodImgs = [];
                                    for (var i = 0; i < imgs.length; i++) {
                                        var src = imgs[i].src || imgs[i].dataset.src || '';
                                        if (src && src.indexOf('thumb') === -1 && src.indexOf('icon') === -1) {
                                            goodImgs.push(src);
                                        }
                                    }
                                    if (goodImgs.length > 0) {
                                        r.image = goodImgs[0];
                                        r.extra_images = goodImgs.slice(1, 5);
                                    }
                                }
                            }

                            return r;
                        """)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            self._driver = None
                            self._create_driver()
                            break
                        continue

                    if result and result.get("title") and result.get("price"):
                        return result

                if result and (result.get("title") or result.get("price")):
                    return result

                return None

            except Exception as e:
                if attempt == 0:
                    self._driver = None
                    self._create_driver()
                    continue
                return None

        return None

    # ============================================================
    # Shopify 日本商店
    # ============================================================
    async def _scrape_shopify_jp(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.hostname}"

        path_parts = parsed.path.strip("/").split("/")
        if "products" in path_parts:
            idx = path_parts.index("products")
            handle = path_parts[idx + 1] if idx + 1 < len(path_parts) else ""
        else:
            handle = ""

        if not handle:
            return await self._scrape_with_playwright(url)

        json_url = f"{base_url}/products/{handle}.json"
        print(f"[Shopify] 嘗試 JSON API: {json_url}")

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={
                    'User-Agent': USER_AGENT,
                    'Accept': 'application/json',
                    'Accept-Language': 'ja,en-US;q=0.9',
                },
            ) as client:
                resp = await client.get(json_url)

                if resp.status_code == 200:
                    data = resp.json()
                    prod = data.get("product", {})

                    product.title = prod.get("title", "")
                    product.brand = prod.get("vendor", "")
                    product.description = (prod.get("body_html") or "")[:500]
                    if product.description:
                        product.description = re.sub(r'<[^>]+>', '', product.description).strip()

                    images = prod.get("images", [])
                    if images:
                        product.image_url = images[0].get("src", "")
                        product.extra_images = [img.get("src", "") for img in images[1:5] if img.get("src")]

                    variants = prod.get("variants", [])
                    if variants:
                        first_price = variants[0].get("price", "")
                        if first_price:
                            product.price_jpy = self._normalize_price(first_price)

                        options = prod.get("options", [])
                        image_id_map = {}
                        for img in images:
                            image_id_map[img.get("id")] = img.get("src", "")

                        color_image_seen = {}

                        for v in variants:
                            option1 = v.get("option1", "") or ""
                            option2 = v.get("option2", "") or ""
                            option3 = v.get("option3", "") or ""
                            available = v.get("available", True)

                            variant_info = {"color": "", "size": "", "in_stock": available, "image": ""}

                            for opt in options:
                                opt_name = (opt.get("name", "") or "").lower()
                                opt_pos = opt.get("position", 0)
                                val = ""
                                if opt_pos == 1: val = option1
                                elif opt_pos == 2: val = option2
                                elif opt_pos == 3: val = option3

                                if any(k in opt_name for k in ["色", "color", "カラー", "colour"]):
                                    variant_info["color"] = val
                                elif any(k in opt_name for k in ["サイズ", "size", "寸"]):
                                    variant_info["size"] = val
                                elif not variant_info["color"]:
                                    variant_info["color"] = val

                            title = v.get("title", "")
                            if not variant_info["color"] and not variant_info["size"] and title:
                                if re.match(r'^[XSML0-9]+$', title.upper().strip()):
                                    variant_info["size"] = title
                                else:
                                    variant_info["color"] = title

                            v_image_id = v.get("image_id")
                            featured = v.get("featured_image", {}) or {}

                            img_src = ""
                            if v_image_id and v_image_id in image_id_map:
                                img_src = image_id_map[v_image_id]
                            elif featured and featured.get("src"):
                                img_src = featured["src"]

                            color = variant_info["color"]
                            if img_src and color and color not in color_image_seen:
                                color_image_seen[color] = img_src

                            if color and color in color_image_seen:
                                variant_info["image"] = color_image_seen[color]
                            elif img_src:
                                variant_info["image"] = img_src

                            product.variants.append(variant_info)

                    print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy}" if product.price_jpy else f"[Shopify] ✅ {product.title[:40]}")
                    return product
        except Exception as e:
            print(f"[Shopify] JSON API 失敗: {type(e).__name__}: {e}")

        return await self._scrape_with_playwright(url)

    # ============================================================
    # Oakley JP - SeleniumBase UC（SFCC JS rendered）
    # ============================================================
    async def _scrape_oakley(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Oakley")

        data = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_oakley_uc, url
        )

        if data:
            product.title = data.get("title", "")
            product.price_jpy = data.get("price") or None
            product.description = data.get("description", "")[:500]
            images = data.get("images", [])
            if images:
                product.image_url = images[0]
                product.extra_images = images[1:9]
            product.variants = data.get("variants", [])

        print(f"[Oakley] {'✅' if product.title else '⚠️'} {product.title[:40] if product.title else '未取得'} / ¥{product.price_jpy} / {len(product.variants)} variants")
        return product

    def _fetch_oakley_uc(self, url: str) -> dict | None:
        import time as _time

        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1

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

                    try:
                        driver.uc_open_with_reconnect(url, reconnect_time=6)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            self._driver = None
                            self._create_driver()
                            continue

                    for i in range(10):
                        _time.sleep(3)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                break
                            continue

                        has_data = (
                            'og:title' in html or
                            'application/ld+json' in html or
                            'addToCart' in html or
                            'productName' in html
                        )
                        print(f"[Oakley] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                        if i >= 1 and has_data:
                            break

                    result = driver.execute_script("""
                        var r = {title:'', price:0, images:[], description:'', variants:[]};

                        // Step 1: 標題從 h1（最準確，就是當前商品）
                        var h1 = document.querySelector('h1');
                        if (h1) r.title = h1.textContent.trim();

                        // Step 2: OG title fallback
                        if (!r.title) {
                            var og = document.querySelector('meta[property="og:title"]');
                            if (og) r.title = (og.content || '').replace(/\s*\|.*$/, '').trim();
                        }

                        // Step 3: 價格 - 找 h1 附近最近的價格元素（最準確）
                        (function() {
                            // 方法A: 從 h1 往上找父元素，在其中尋找價格
                            var h1el = document.querySelector('h1');
                            if (h1el) {
                                var parent = h1el.parentElement;
                                for (var depth = 0; depth < 8; depth++) {
                                    if (!parent) break;
                                    var txt = parent.textContent;
                                    var allPrices = txt.match(/[\u00a5\uffe5]([\d,]{3,7})(?:\s*(?:\(税込\)|税込))?/g);
                                    if (allPrices && allPrices.length > 0) {
                                        // 取第一個合理價格（100〜2000000）
                                        for (var i = 0; i < allPrices.length; i++) {
                                            var numMatch = allPrices[i].match(/([\d,]+)/);
                                            if (numMatch) {
                                                var p = parseInt(numMatch[1].replace(/,/g,''));
                                                if (p >= 100 && p <= 2000000) {
                                                    r.price = p;
                                                    break;
                                                }
                                            }
                                        }
                                        if (r.price) break;
                                    }
                                    parent = parent.parentElement;
                                }
                            }

                            // 方法B: 掃 main/article 內所有含 ¥ 的元素，取 h1 距離最近的
                            if (!r.price) {
                                var mainEl = document.querySelector('main') || document.querySelector('article') || document.body;
                                var allEls = mainEl.querySelectorAll('*');
                                var bestEl = null, bestDist = 999999;
                                var h1rect = h1el ? h1el.getBoundingClientRect() : null;
                                for (var i = 0; i < allEls.length; i++) {
                                    var el = allEls[i];
                                    if (el.children.length > 0) continue; // 只要葉節點
                                    var t = el.textContent.trim();
                                    var pm = t.match(/^[\u00a5\uffe5]([\d,]{3,7})/);
                                    if (!pm) continue;
                                    var p = parseInt(pm[1].replace(/,/g,''));
                                    if (p < 100 || p > 2000000) continue;
                                    if (h1rect) {
                                        var rect = el.getBoundingClientRect();
                                        var dist = Math.abs(rect.top - h1rect.bottom);
                                        if (dist < bestDist) { bestDist = dist; bestEl = el; r.price = p; }
                                    } else {
                                        r.price = p; break;
                                    }
                                }
                            }
                        })();

                        // Step 4: JSON-LD（補圖片和描述，價格只在 DOM 沒找到時才用）
                        var currentPath = location.pathname;
                        var bestLd = null;
                        document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                            try {
                                var d = JSON.parse(s.textContent);
                                if (Array.isArray(d)) {
                                    d = d.find(function(i){ return i['@type'] === 'Product'; }) || null;
                                }
                                if (!d || d['@type'] !== 'Product') return;
                                var ldUrl = (d.url || d['@id'] || '');
                                if (!bestLd) {
                                    bestLd = d;
                                } else if (ldUrl && ldUrl.indexOf(currentPath) !== -1) {
                                    bestLd = d;
                                }
                            } catch(e) {}
                        });
                        if (bestLd) {
                            if (!r.title) r.title = bestLd.name || '';
                            if (!r.price && bestLd.offers) {
                                var offers = bestLd.offers;
                                if (Array.isArray(offers)) offers = offers[0];
                                if (offers && offers.price) r.price = parseInt(offers.price);
                            }
                            if (bestLd.image) {
                                var imgs = Array.isArray(bestLd.image) ? bestLd.image : [bestLd.image];
                                imgs.forEach(function(i){ if (typeof i === 'string') r.images.push(i); });
                            }
                            r.description = bestLd.description || '';
                        }

                        // Step 5: OG image fallback
                        if (r.images.length === 0) {
                            var ogImg = document.querySelector('meta[property="og:image"]');
                            if (ogImg && ogImg.content) r.images.push(ogImg.content);
                        }

                        // Variants - 精準抓 color / size，避免抓到導覽列按鈕
                        var colorVariants = [];
                        var sizeVariants = [];

                        // 顏色 swatch（只找有 aria-label 且在商品區塊內的按鈕）
                        var colorSelectors = [
                            '[data-testid*="color"] button',
                            '[data-testid*="swatch"] button',
                            '[class*="ColorSwatch"] button',
                            '[class*="colorSwatch"] button',
                            '[class*="color-swatch"] button',
                            '[class*="SwatchWrapper"] button',
                            '[class*="swatchWrapper"] button',
                        ];
                        colorSelectors.forEach(function(sel) {
                            document.querySelectorAll(sel).forEach(function(btn) {
                                var label = (btn.getAttribute('aria-label') || btn.getAttribute('title') || '').trim();
                                if (!label) return;
                                if (label.length > 60) return;
                                // 過濾掉導覽用文字
                                var skip = ['カート', 'ログイン', '検索', 'メニュー', 'Close', 'Back', 'Next', 'Prev', 'Add to'];
                                for (var i = 0; i < skip.length; i++) { if (label.indexOf(skip[i]) !== -1) return; }
                                var alreadyAdded = colorVariants.some(function(v){ return v.label === label; });
                                if (!alreadyAdded) {
                                    var unavailable = btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('disabled') || btn.classList.contains('unavailable') || btn.classList.contains('out-of-stock');
                                    colorVariants.push({label: label, available: !unavailable});
                                }
                            });
                        });

                        // 尺寸
                        var sizeSelectors = [
                            '[data-testid*="size"] button',
                            '[class*="SizeOption"] button',
                            '[class*="sizeOption"] button',
                            '[class*="size-option"] button',
                            '[class*="SizeButton"] button',
                            '[class*="sizeButton"] button',
                        ];
                        sizeSelectors.forEach(function(sel) {
                            document.querySelectorAll(sel).forEach(function(btn) {
                                var label = (btn.getAttribute('aria-label') || btn.textContent || '').trim();
                                if (!label || label.length > 20) return;
                                var alreadyAdded = sizeVariants.some(function(v){ return v.label === label; });
                                if (!alreadyAdded) {
                                    var unavailable = btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('disabled');
                                    sizeVariants.push({label: label, available: !unavailable});
                                }
                            });
                        });

                        // 組合 color x size
                        if (colorVariants.length > 0 || sizeVariants.length > 0) {
                            var colors = colorVariants.length > 0 ? colorVariants : [{label: '', available: true}];
                            var sizes = sizeVariants.length > 0 ? sizeVariants : [{label: '', available: true}];
                            colors.forEach(function(c) {
                                sizes.forEach(function(s) {
                                    r.variants.push({
                                        color: c.label,
                                        size: s.label,
                                        sku: c.label + (s.label ? '-' + s.label : ''),
                                        price: r.price,
                                        in_stock: c.available && s.available,
                                        image: ''
                                    });
                                });
                            });
                        }

                        // Product images from img tags
                        if (r.images.length < 3) {
                            document.querySelectorAll('img').forEach(function(img) {
                                var src = img.src || img.getAttribute('data-src') || '';
                                if (src && src.indexOf('oakley.com') !== -1 && src.indexOf('product') !== -1) {
                                    if (r.images.indexOf(src) === -1) r.images.push(src);
                                }
                            });
                        }

                        return r;
                    """)

                    if result:
                        return result

                    return None

                except Exception as e:
                    print(f"[Oakley] SeleniumBase 錯誤: {type(e).__name__}: {e}")
                    if attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None

        return None

    # ============================================================
    # 通用 - Playwright（其他日本網站）
    # ============================================================
    async def _scrape_with_playwright(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            html = await self._fetch_playwright(url)

            if 'Shopify.shop' in html or '"shopify"' in html.lower() or 'cdn.shopify.com' in html:
                shopify_product = await self._scrape_shopify_jp(url)
                if shopify_product.title and shopify_product.variants:
                    return shopify_product

            soup = BeautifulSoup(html, "html.parser")

            self._extract_json_ld(soup, product)
            self._extract_og_tags(soup, product)
            if not product.title or not product.price_jpy:
                self._extract_generic(soup, product)

            if product.price_jpy and (product.price_jpy < 100 or product.price_jpy > 1000000):
                product.price_jpy = None

            if product.image_url and not product.image_url.startswith("http"):
                base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                product.image_url = base + product.image_url

        except Exception as e:
            print(f"[Generic] ❌ 錯誤: {e}")

        return product

    async def _fetch_playwright(self, url: str) -> str:
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
        tax_prices = re.findall(r'([0-9,]+)\s*円\s*[（\(]?\s*税込', text)
        if tax_prices:
            p = self._normalize_price(tax_prices[0])
            if p and 100 <= p <= 1000000:
                return p
        for sel in ['[class*="price"]', '[class*="Price"]', '[id*="price"]']:
            for el in soup.select(sel):
                m = re.search(r'[¥￥]?\s*([\d,]+)', el.get_text(strip=True))
                if m:
                    p = int(m.group(1).replace(',', ''))
                    if 100 <= p <= 1000000:
                        return p
        prices = re.findall(r'[¥￥]\s*([0-9,]+)', text)
        prices += re.findall(r'([0-9,]+)\s*円', text)
        if prices:
            normalized = [self._normalize_price(p) for p in prices]
            normalized = [p for p in normalized if p and 100 <= p <= 1000000]
            if normalized:
                return Counter(normalized).most_common(1)[0][0]
        return None

    @staticmethod
    def _normalize_url(url: str) -> str:
        import re as _re
        shopserve_m = _re.match(r'(https?://[^/]+)/smp/item/(.+)', url)
        if shopserve_m:
            normalized = f"{shopserve_m.group(1)}/SHOP/{shopserve_m.group(2)}"
            print(f"[Normalize] ShopServe 手機版 → PC 版: {url} → {normalized}")
            return normalized
        return url

    @staticmethod
    def _normalize_price(price):
        if isinstance(price, (int, float)):
            return int(price)
        if isinstance(price, str):
            cleaned = re.sub(r'[^0-9.]', '', price)
            return int(float(cleaned)) if cleaned else None
        return None
