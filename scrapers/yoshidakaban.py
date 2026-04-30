"""
yoshidakaban.com (PORTER / 吉田カバン / LUGGAGE LABEL) scraper

關鍵策略：
1. URL 進入 _scrape_yoshidakaban 之前，base.normalize_url() 已經把
   /zh-CHT/ /zh-CN/ /en/ /ko/ 等語系前綴去掉，但這裡再做一次保險。
2. 強制 Accept-Language: ja-JP + cookie 防止 CDN 自動切語系。
3. 移除 strikethrough/del 元素，避免抓到劃掉的舊價（特價商品）。
4. 優先抓「税込」附近的價格；找不到才 fallback 取 ¥xxx 的最小值。

商品頁 URL 格式：
  https://www.yoshidakaban.com/product/{id}.html              ← 目標（日文，¥）
  https://www.yoshidakaban.com/zh-CHT/product/{id}.html       ← 會顯示 TWD
  https://www.yoshidakaban.com/en/product/{id}.html           ← 會顯示 USD
  https://www.yoshidakaban.com/ko/product/{id}.html           ← 會顯示 KRW
"""
import re
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


BASE_URL = "https://www.yoshidakaban.com"
_LANG_PREFIX_RE = re.compile(r"^/(zh-CHT|zh-CN|en|ko)(/|$)", re.IGNORECASE)


def _force_japanese_url(url: str) -> str:
    """把任何語系版本 URL 改寫成日文版（無前綴）。base.normalize_url 已做過，這裡是雙保險。"""
    parsed = urlparse(url)
    new_path = _LANG_PREFIX_RE.sub("/", parsed.path)
    return urlunparse(parsed._replace(path=new_path))


def _to_int_yen(s: str) -> int | None:
    """'¥42,900' / '42,900' / '42900' → 42900；超出合理範圍回傳 None"""
    if not s:
        return None
    m = re.search(r"([\d,]+)", s)
    if not m:
        return None
    try:
        v = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if 100 <= v <= 10_000_000:
        return v
    return None


def _absolute_url(src: str) -> str:
    """補完相對 URL。"""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    return src


class YoshidaKabanMixin:
    """吉田カバン (PORTER / LUGGAGE LABEL) 商品爬取 Mixin"""

    async def _scrape_yoshidakaban(self, url: str) -> ProductInfo:
        target_url = _force_japanese_url(url)
        if target_url != url:
            print(f"[YoshidaKaban] URL 改寫: {url} → {target_url}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        # 強制日文 cookie，防 IP-based 自動切語系
        cookies = {
            "lang": "ja",
            "language": "ja",
            "locale": "ja-JP",
            "preferred_lang": "ja",
        }

        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=cookies,
                timeout=20.0,
                follow_redirects=True,
            ) as client:
                resp = await client.get(target_url)
                resp.raise_for_status()
                html = resp.text
                final_url = str(resp.url)
        except Exception as e:
            print(f"[YoshidaKaban] ❌ HTTP 錯誤: {e}")
            raise

        # 防禦性檢查：是否被重定向回非日文版
        if _LANG_PREFIX_RE.match(urlparse(final_url).path):
            print(f"[YoshidaKaban] ⚠️ 被重定向到非日文版: {final_url}，價格可能為外幣")

        soup = BeautifulSoup(html, "html.parser")

        # 解析各欄位
        title = self._yk_extract_title(soup)
        price = self._yk_extract_price(soup)
        image = self._yk_extract_image(soup)
        in_stock = self._yk_extract_in_stock(soup)
        brand = self._yk_extract_brand(title or "")

        print(
            f"[YoshidaKaban] ✓ title={title!r}, price={price}, "
            f"brand={brand}, in_stock={in_stock}"
        )

        return ProductInfo(
            title=title or "",
            price_jpy=price,
            image_url=image or "",
            source_url=target_url,
            brand=brand,
            currency="JPY",
            in_stock=in_stock,
        )

    # ── 私有 helper（用 _yk_ 前綴避免和其他 Mixin 衝突）──────────────

    @staticmethod
    def _yk_strip_noise(soup: BeautifulSoup) -> None:
        """移除可能造成誤抓的劃線/script 元素。"""
        selectors = [
            "del", "s", "strike",
            ".price--old", ".old-price", ".price-was", ".price__compare",
            '[class*="strikethrough"]', '[class*="line-through"]',
            "script", "style", "noscript",
        ]
        for sel in selectors:
            try:
                for tag in soup.select(sel):
                    tag.decompose()
            except Exception:
                pass

    @staticmethod
    def _yk_extract_title(soup: BeautifulSoup) -> str | None:
        """商品名稱：og:title → h1 → <title>"""
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
            # 去掉「| 吉田カバンホームページ | YOSHIDA & Co.」尾巴
            title = re.split(r"\s*[\|｜]\s*吉田", title)[0]
            title = re.split(r"\s*[\|｜]\s*YOSHIDA", title, flags=re.IGNORECASE)[0]
            title = title.strip()
            if title:
                return title

        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text:
                return text

        title_tag = soup.find("title")
        if title_tag:
            text = re.split(r"\s*[\|｜]\s*", title_tag.get_text(strip=True))[0]
            if text:
                return text

        return None

    def _yk_extract_price(self, soup: BeautifulSoup) -> int | None:
        """抓税込（含稅）價格。"""
        # 先移除劃線元素
        self._yk_strip_noise(soup)

        # 1. OG meta: product:price:amount
        og_price = soup.find("meta", attrs={"property": "product:price:amount"})
        if og_price and og_price.get("content"):
            v = _to_int_yen(og_price["content"])
            if v:
                print(f"[YoshidaKaban] price from og meta: {v}")
                return v

        # 2. schema.org: itemprop="price"
        for tag in soup.find_all(attrs={"itemprop": "price"}):
            content = tag.get("content") or tag.get_text(strip=True)
            v = _to_int_yen(content)
            if v:
                print(f"[YoshidaKaban] price from itemprop: {v}")
                return v

        # 3. 可見文字：優先「税込」附近的 ¥xxx
        text = soup.get_text(" ", strip=True)
        tax_inc_patterns = [
            r"¥\s*([\d,]+)\s*[\(（]\s*税込\s*[\)）]",   # ¥xxx (税込)
            r"¥\s*([\d,]+)\s*税込",                      # ¥xxx 税込
            r"税込\s*[:：]?\s*¥\s*([\d,]+)",             # 税込 ¥xxx
            r"税込価格\s*[:：]?\s*¥?\s*([\d,]+)",        # 税込価格: xxx
        ]
        tax_inc_prices: list[int] = []
        for pat in tax_inc_patterns:
            for m in re.finditer(pat, text):
                v = _to_int_yen(m.group(1))
                if v:
                    tax_inc_prices.append(v)

        if tax_inc_prices:
            v = min(tax_inc_prices)
            print(f"[YoshidaKaban] price from 税込 pattern: {v} (candidates={tax_inc_prices})")
            return v

        # 4. Fallback：所有 ¥xxx 取 min
        all_prices: list[int] = []
        for m in re.finditer(r"¥\s*([\d,]+)", text):
            v = _to_int_yen(m.group(1))
            if v:
                all_prices.append(v)

        if all_prices:
            v = min(all_prices)
            print(f"[YoshidaKaban] price fallback to min(¥): {v} (candidates={all_prices})")
            return v

        print(f"[YoshidaKaban] ⚠️ no price found")
        return None

    @staticmethod
    def _yk_extract_image(soup: BeautifulSoup) -> str | None:
        """主圖：og:image → 商品圖區。"""
        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            return _absolute_url(og_image["content"])

        for sel in [
            ".item-image img", ".product-image img", ".detail-image img",
            ".main-image img", "img.product-photo", ".product__media img",
            "#mainImage", ".slick-slide img",
        ]:
            img = soup.select_one(sel)
            if img and img.get("src"):
                return _absolute_url(img["src"])

        return None

    @staticmethod
    def _yk_extract_in_stock(soup: BeautifulSoup) -> bool:
        """頁面有「売り切れ」/「品切れ」/「SOLD OUT」→ 缺貨。"""
        text = soup.get_text(" ", strip=True)
        for kw in ["売り切れ", "品切れ", "SOLD OUT", "在庫なし", "販売終了"]:
            if kw in text:
                return False
        return True

    @staticmethod
    def _yk_extract_brand(title: str) -> str:
        """從商品標題推斷品牌：PORTER / POTR / LUGGAGE LABEL → 統一回 PORTER。"""
        upper = title.upper()
        if "LUGGAGE LABEL" in upper:
            return "LUGGAGE LABEL"
        if "POTR" in upper:
            return "POTR"
        if "PORTER" in upper:
            return "PORTER"
        return "YOSHIDA"
