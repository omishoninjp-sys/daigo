"""
商品資訊爬取模組

抓取策略（依優先順序）：
1. JSON-LD 結構化資料（最精確）
2. Open Graph meta tags（大部分網站都有）
3. 網站專用解析器（Amazon JP, 樂天等）
4. 通用 HTML 解析（fallback）
"""
import re
import json
from urllib.parse import urlparse
from dataclasses import dataclass, asdict

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT


@dataclass
class ProductInfo:
    """爬取到的商品資訊"""
    title: str = ""
    price_jpy: int | None = None
    image_url: str = ""
    description: str = ""
    source_url: str = ""
    brand: str = ""
    currency: str = "JPY"
    extra_images: list = None
    variants: list = None  # 如果有尺寸/顏色等選項

    def __post_init__(self):
        if self.extra_images is None:
            self.extra_images = []
        if self.variants is None:
            self.variants = []

    def to_dict(self):
        return asdict(self)

    @property
    def is_valid(self) -> bool:
        return bool(self.title and self.price_jpy and self.price_jpy > 0)


class Scraper:
    """通用商品爬取器"""

    def __init__(self):
        self.headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8,zh-TW;q=0.7",
        }
        # 各網站專用解析器
        self.site_parsers = {
            "amazon.co.jp": self._parse_amazon_jp,
            "www.amazon.co.jp": self._parse_amazon_jp,
            "item.rakuten.co.jp": self._parse_rakuten,
            "www.rakuten.co.jp": self._parse_rakuten,
            "zozo.jp": self._parse_zozo,
            "www.zozo.jp": self._parse_zozo,
        }

    async def scrape(self, url: str) -> ProductInfo:
        """
        主入口：根據 URL 抓取商品資訊
        """
        product = ProductInfo(source_url=url)

        try:
            html = await self._fetch(url)
            soup = BeautifulSoup(html, "html.parser")

            # Step 1: 嘗試 JSON-LD
            self._extract_json_ld(soup, product)

            # Step 2: 嘗試 OG tags
            self._extract_og_tags(soup, product)

            # Step 3: 網站專用解析器
            domain = urlparse(url).hostname or ""
            if domain in self.site_parsers:
                self.site_parsers[domain](soup, product)

            # Step 4: 通用 fallback
            if not product.title:
                self._extract_generic(soup, product)

            # 價格後處理：確保是整數日幣
            if product.price_jpy:
                product.price_jpy = self._normalize_price(product.price_jpy)

            # 圖片 URL 處理
            if product.image_url and not product.image_url.startswith("http"):
                base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                product.image_url = base + product.image_url

        except Exception as e:
            print(f"[Scraper] 爬取失敗 {url}: {e}")
            raise

        return product

    async def _fetch(self, url: str) -> str:
        """取得網頁 HTML"""
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=SCRAPE_TIMEOUT,
        ) as client:
            resp = await client.get(url, headers=self.headers)
            resp.raise_for_status()
            return resp.text

    # ================================================================
    # 解析策略
    # ================================================================

    def _extract_json_ld(self, soup: BeautifulSoup, product: ProductInfo):
        """從 JSON-LD 結構化資料抓取（最可靠）"""
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string or "")

                # 可能是陣列
                if isinstance(data, list):
                    data = next(
                        (d for d in data if d.get("@type") in ("Product", "IndividualProduct")),
                        data[0] if data else {},
                    )

                if data.get("@type") not in ("Product", "IndividualProduct"):
                    # 嘗試找巢狀的 Product
                    if "@graph" in data:
                        for item in data["@graph"]:
                            if item.get("@type") == "Product":
                                data = item
                                break
                    else:
                        continue

                if not product.title and data.get("name"):
                    product.title = data["name"]

                if not product.image_url and data.get("image"):
                    img = data["image"]
                    if isinstance(img, list):
                        product.image_url = img[0] if img else ""
                    elif isinstance(img, dict):
                        product.image_url = img.get("url", "")
                    else:
                        product.image_url = str(img)

                if not product.brand and data.get("brand"):
                    brand = data["brand"]
                    if isinstance(brand, dict):
                        product.brand = brand.get("name", "")
                    else:
                        product.brand = str(brand)

                if not product.description and data.get("description"):
                    product.description = data["description"][:500]

                # 價格
                if not product.price_jpy:
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        product.price_jpy = self._normalize_price(price)
                        product.currency = offers.get("priceCurrency", "JPY")

            except (json.JSONDecodeError, StopIteration):
                continue

    def _extract_og_tags(self, soup: BeautifulSoup, product: ProductInfo):
        """從 Open Graph meta tags 抓取"""
        og_map = {}
        for meta in soup.find_all("meta", property=True):
            og_map[meta["property"]] = meta.get("content", "")

        if not product.title:
            product.title = og_map.get("og:title", "")

        if not product.image_url:
            product.image_url = og_map.get("og:image", "")

        if not product.description:
            product.description = og_map.get("og:description", "")[:500]

        # 有些網站會用 product:price:amount
        if not product.price_jpy:
            price_str = og_map.get("product:price:amount", "")
            if price_str:
                product.price_jpy = self._normalize_price(price_str)

    def _extract_generic(self, soup: BeautifulSoup, product: ProductInfo):
        """通用 fallback 解析"""
        # 標題
        if not product.title:
            title_tag = soup.find("title")
            if title_tag:
                product.title = title_tag.get_text(strip=True)

        # 圖片：找最大的 product image
        if not product.image_url:
            for img in soup.find_all("img"):
                src = img.get("src", "")
                alt = (img.get("alt", "") or "").lower()
                if any(kw in alt for kw in ["product", "商品", "item"]) and src:
                    product.image_url = src
                    break
            # 還是沒有就取第一張大圖
            if not product.image_url:
                for img in soup.find_all("img", src=True):
                    src = img["src"]
                    if not any(skip in src.lower() for skip in ["logo", "icon", "banner", "sprite"]):
                        product.image_url = src
                        break

        # 價格：尋找日幣價格模式
        if not product.price_jpy:
            product.price_jpy = self._find_price_in_html(soup)

    def _find_price_in_html(self, soup: BeautifulSoup) -> int | None:
        """從 HTML 中尋找日幣價格"""
        # 常見的價格 CSS class / id
        price_selectors = [
            '[class*="price"]',
            '[class*="Price"]',
            '[id*="price"]',
            '[class*="amount"]',
            '[data-price]',
        ]

        for selector in price_selectors:
            elements = soup.select(selector)
            for el in elements:
                text = el.get_text(strip=True)
                price = self._extract_jpy_price(text)
                if price and price > 0:
                    return price

                # data-price 屬性
                data_price = el.get("data-price")
                if data_price:
                    return self._normalize_price(data_price)

        # 最後手段：在整個頁面找 ¥ 或 円 模式
        body_text = soup.get_text()
        prices = re.findall(r'[¥￥][\s]*([0-9,]+)', body_text)
        prices += re.findall(r'([0-9,]+)\s*円', body_text)

        if prices:
            # 取最常出現的價格（通常是正確售價）
            from collections import Counter
            normalized = [self._normalize_price(p) for p in prices]
            normalized = [p for p in normalized if p and 100 <= p <= 9999999]
            if normalized:
                most_common = Counter(normalized).most_common(1)
                return most_common[0][0]

        return None

    # ================================================================
    # 網站專用解析器
    # ================================================================

    def _parse_amazon_jp(self, soup: BeautifulSoup, product: ProductInfo):
        """Amazon.co.jp 專用"""
        # 標題
        if not product.title:
            title_el = soup.find(id="productTitle")
            if title_el:
                product.title = title_el.get_text(strip=True)

        # 價格
        if not product.price_jpy:
            # 新版 Amazon 價格結構
            price_el = soup.select_one(".a-price .a-offscreen")
            if price_el:
                product.price_jpy = self._extract_jpy_price(price_el.get_text())

            # 舊版
            if not product.price_jpy:
                price_el = soup.find(id="priceblock_ourprice") or soup.find(id="priceblock_dealprice")
                if price_el:
                    product.price_jpy = self._extract_jpy_price(price_el.get_text())

        # 圖片
        if not product.image_url:
            img_el = soup.find(id="landingImage") or soup.find(id="imgBlkFront")
            if img_el:
                # 優先取 data-old-hires（高解析）
                product.image_url = img_el.get("data-old-hires") or img_el.get("src", "")

        # 品牌
        if not product.brand:
            brand_el = soup.find(id="bylineInfo")
            if brand_el:
                product.brand = brand_el.get_text(strip=True).replace("ブランド: ", "").replace("のストアを表示", "")

    def _parse_rakuten(self, soup: BeautifulSoup, product: ProductInfo):
        """樂天市場專用"""
        if not product.title:
            title_el = soup.find("span", class_="item_name") or soup.find("h1")
            if title_el:
                product.title = title_el.get_text(strip=True)

        if not product.price_jpy:
            price_el = soup.select_one(".price2, .item_price, [class*='price']")
            if price_el:
                product.price_jpy = self._extract_jpy_price(price_el.get_text())

    def _parse_zozo(self, soup: BeautifulSoup, product: ProductInfo):
        """ZOZOTOWN 專用"""
        if not product.price_jpy:
            price_el = soup.select_one("[class*='price']")
            if price_el:
                product.price_jpy = self._extract_jpy_price(price_el.get_text())

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _extract_jpy_price(text: str) -> int | None:
        """從文字中提取日幣價格"""
        if not text:
            return None
        # 移除空白和特殊字元
        text = text.strip()
        # 找數字（可能有逗號）
        match = re.search(r'([0-9][0-9,]*)', text.replace('¥', '').replace('￥', '').replace('円', '').replace('税込', ''))
        if match:
            return int(match.group(1).replace(',', ''))
        return None

    @staticmethod
    def _normalize_price(price) -> int | None:
        """統一價格格式為整數"""
        if isinstance(price, (int, float)):
            return int(price)
        if isinstance(price, str):
            cleaned = re.sub(r'[^0-9.]', '', price)
            if cleaned:
                return int(float(cleaned))
        return None
