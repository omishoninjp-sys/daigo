"""
PAL CLOSET 爬蟲 Mixin
使用 httpx + BeautifulSoup
"""
import re
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


class PalClosetMixin:

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

            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                self._extract_og_tags(soup, product)

            qs = parse_qs(urlparse(url).query)
            brand_param = qs.get("b", [""])[0]
            if brand_param:
                for a in soup.select("ol a, nav a, [class*='breadcrumb'] a"):
                    if brand_param in a.get("href", "") and a.get_text(strip=True):
                        product.brand = a.get_text(strip=True)
                        break
                if not product.brand:
                    product.brand = brand_param

            self._extract_json_ld(soup, product)
            if not product.price_jpy:
                for pat in [r'"price"\s*:\s*"?([\d.]+)"?', r'[¥￥]([\d,]+)\s*(?:税込|円)']:
                    pm = re.search(pat, html)
                    if pm:
                        p = int(float(pm.group(1).replace(",", "")))
                        if 100 <= p <= 1000000:
                            product.price_jpy = p
                            break

            for img in soup.find_all("img", src=True):
                src = img["src"]
                if "contents.palcloset.jp" in src and not src.startswith("data:"):
                    product.image_url = src
                    break

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
