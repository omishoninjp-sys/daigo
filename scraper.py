"""
商品資訊爬取模組（Playwright 無頭瀏覽器版）
"""
import re
import json
import asyncio
from urllib.parse import urlparse
from dataclasses import dataclass, asdict
from collections import Counter

from bs4 import BeautifulSoup
from config import SCRAPE_TIMEOUT, USER_AGENT

_browser = None
_browser_lock = asyncio.Lock()


async def get_browser():
    global _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--single-process",
                ],
            )
        return _browser


@dataclass
class ProductInfo:
    title: str = ""
    price_jpy: int | None = None
    image_url: str = ""
    description: str = ""
    source_url: str = ""
    brand: str = ""
    currency: str = "JPY"
    extra_images: list = None
    variants: list = None

    def __post_init__(self):
        if self.extra_images is None:
            self.extra_images = []
        if self.variants is None:
            self.variants = []

    def to_dict(self):
        return asdict(self)

    @property
    def is_valid(self):
        return bool(self.title and self.price_jpy and self.price_jpy > 0)


class Scraper:
    def __init__(self):
        self.site_parsers = {
            "amazon.co.jp": self._parse_amazon_jp,
            "www.amazon.co.jp": self._parse_amazon_jp,
            "item.rakuten.co.jp": self._parse_rakuten,
            "www.rakuten.co.jp": self._parse_rakuten,
            "zozo.jp": self._parse_zozo,
            "www.zozo.jp": self._parse_zozo,
        }

    async def scrape(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            html = await self._fetch(url)
            soup = BeautifulSoup(html, "html.parser")

            for step_name, step_fn in [
                ("JSON-LD", lambda: self._extract_json_ld(soup, product)),
                ("OG tags", lambda: self._extract_og_tags(soup, product)),
                ("Site parser", lambda: self._run_site_parser(url, soup, product)),
                ("Generic", lambda: self._extract_generic(soup, product) if not product.title or not product.price_jpy else None),
            ]:
                try:
                    step_fn()
                except Exception as e:
                    print(f"[Scraper] {step_name} failed: {e}")

            if product.price_jpy:
                product.price_jpy = self._normalize_price(product.price_jpy)
                # 合理價格範圍：¥100 ~ ¥1,000,000（超過的通常是抓錯）
                if product.price_jpy and (product.price_jpy < 100 or product.price_jpy > 1000000):
                    print(f"[Scraper] 價格不合理 ¥{product.price_jpy}，重置")
                    product.price_jpy = None
            if product.image_url and not product.image_url.startswith("http"):
                base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                product.image_url = base + product.image_url

        except Exception as e:
            print(f"[Scraper] Failed {url}: {e}")
            raise
        return product

    def _run_site_parser(self, url, soup, product):
        domain = urlparse(url).hostname or ""
        if domain in self.site_parsers:
            self.site_parsers[domain](soup, product)

    async def _fetch(self, url: str) -> str:
        """用 Playwright 載入頁面"""
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="ja-JP",
            extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()
        try:
            # 封鎖 media 和 font 加速載入
            await page.route("**/*", lambda route: (
                route.abort()
                if route.request.resource_type in ("media", "font")
                else route.continue_()
            ))
            await page.goto(url, wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT * 1000)
            await page.wait_for_timeout(2000)
            return await page.content()
        finally:
            await page.close()
            await context.close()

    # === Extractors ===

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
                        product.currency = offers.get("priceCurrency", "JPY")
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
        """從 HTML 找日幣價格，優先抓「税込」價格"""
        text = soup.get_text()

        # 優先：找「X,XXX円（税込）」格式
        tax_prices = re.findall(r'([0-9,]+)\s*円\s*[（\(]?\s*税込', text)
        if tax_prices:
            p = self._normalize_price(tax_prices[0])
            if p and 100 <= p <= 1000000:
                return p

        # 其次：找 price class 裡的價格
        for sel in ['[class*="price"]', '[class*="Price"]', '[id*="price"]', '[data-price]']:
            for el in soup.select(sel):
                p = self._extract_jpy_price(el.get_text(strip=True))
                if p and 100 <= p <= 1000000:
                    return p
                dp = el.get("data-price")
                if dp:
                    v = self._normalize_price(dp)
                    if v and 100 <= v <= 1000000:
                        return v

        # 最後：找 ¥ 或 円 模式，取最常出現的合理價格
        prices = re.findall(r'[¥￥]\s*([0-9,]+)', text)
        prices += re.findall(r'([0-9,]+)\s*円', text)
        if prices:
            normalized = [self._normalize_price(p) for p in prices]
            normalized = [p for p in normalized if p and 100 <= p <= 1000000]
            if normalized:
                return Counter(normalized).most_common(1)[0][0]
        return None

    # === Site Parsers ===

    def _parse_amazon_jp(self, soup, product):
        if not product.title:
            el = soup.find(id="productTitle")
            if el:
                product.title = el.get_text(strip=True)
        if not product.price_jpy:
            el = soup.select_one(".a-price .a-offscreen")
            if el:
                product.price_jpy = self._extract_jpy_price(el.get_text())
            if not product.price_jpy:
                el = soup.find(id="priceblock_ourprice") or soup.find(id="priceblock_dealprice")
                if el:
                    product.price_jpy = self._extract_jpy_price(el.get_text())
        if not product.image_url:
            el = soup.find(id="landingImage") or soup.find(id="imgBlkFront")
            if el:
                product.image_url = el.get("data-old-hires") or el.get("src", "")
        if not product.brand:
            el = soup.find(id="bylineInfo")
            if el:
                product.brand = el.get_text(strip=True).replace("ブランド: ", "").replace("のストアを表示", "")

    def _parse_rakuten(self, soup, product):
        if not product.title:
            el = soup.find("span", class_="item_name") or soup.find("h1")
            if el:
                product.title = el.get_text(strip=True)
        if not product.price_jpy:
            el = soup.select_one(".price2, .item_price, [class*='price']")
            if el:
                product.price_jpy = self._extract_jpy_price(el.get_text())

    def _parse_zozo(self, soup, product):
        if not product.title:
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
        if not product.price_jpy:
            text = soup.get_text()
            tax = re.findall(r'[¥￥]([0-9,]+)(?:税込|（税込）|\(税込\))', text)
            if tax:
                product.price_jpy = self._normalize_price(tax[0])
            if not product.price_jpy:
                all_p = re.findall(r'[¥￥]([0-9,]+)', text)
                valid = [self._normalize_price(p) for p in all_p]
                valid = [p for p in valid if p and 100 <= p <= 999999]
                if valid:
                    product.price_jpy = Counter(valid).most_common(1)[0][0]
        if not product.image_url:
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "imgz.jp" in src and ("_d_" in src or "_b_" in src):
                    product.image_url = src
                    break
        if not product.brand:
            el = soup.select_one('a[href*="/brand/"]')
            if el:
                product.brand = el.get_text(strip=True)

    # === Utils ===

    @staticmethod
    def _extract_jpy_price(text):
        if not text:
            return None
        cleaned = text.replace('¥', '').replace('￥', '').replace('円', '').replace('税込', '').strip()
        m = re.search(r'([0-9][0-9,]*)', cleaned)
        return int(m.group(1).replace(',', '')) if m else None

    @staticmethod
    def _normalize_price(price):
        if isinstance(price, (int, float)):
            return int(price)
        if isinstance(price, str):
            cleaned = re.sub(r'[^0-9.]', '', price)
            return int(float(cleaned)) if cleaned else None
        return None
