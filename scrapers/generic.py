"""
通用爬蟲 Mixin
- Playwright / httpx 通用抓取
- JSON-LD、OG tag、generic 解析器
"""
import re
import json
from collections import Counter
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo, normalize_price


class GenericMixin:

    # ============================================================
    # 通用 - httpx（其他日本網站）
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
    def _extract_json_ld(self, soup, product: ProductInfo):
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
                        p = normalize_price(price)
                        if p and 100 <= p <= 1000000:
                            product.price_jpy = p
            except (json.JSONDecodeError, StopIteration):
                continue

    def _extract_og_tags(self, soup, product: ProductInfo):
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
                product.price_jpy = normalize_price(p)

    def _extract_generic(self, soup, product: ProductInfo):
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

    def _find_price_in_html(self, soup) -> int | None:
        text = soup.get_text()
        tax_prices = re.findall(r'([0-9,]+)\s*円\s*[（\(]?\s*税込', text)
        if tax_prices:
            p = normalize_price(tax_prices[0])
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
            normalized = [normalize_price(p) for p in prices]
            normalized = [p for p in normalized if p and 100 <= p <= 1000000]
            if normalized:
                return Counter(normalized).most_common(1)[0][0]
        return None
