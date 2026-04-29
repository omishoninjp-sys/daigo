"""
yoshidakaban.com (PORTER / 吉田カバン) scraper

關鍵策略：
1. 強制改寫到日文版 URL（去掉 /zh-CHT/, /zh-CN/, /en/, /ko/ 等語系前綴）
   → 避免抓到 TWD/HKD/USD 等外幣價格
2. 強制 Accept-Language: ja-JP + cookie，防止 CDN 自動依 IP 切語系
3. 移除 strikethrough/del 元素，避免抓到划掉的舊價（特價商品）
4. 優先抓「税込」附近的價格；找不到時 fallback 取所有 ¥xxx 的最小值
5. 商品名稱、圖片用 og:title / og:image meta tags

商品頁 URL 格式：
  https://www.yoshidakaban.com/product/{id}.html              ← 目標（日文）
  https://www.yoshidakaban.com/zh-CHT/product/{id}.html       ← 會顯示 TWD
  https://www.yoshidakaban.com/en/product/{id}.html           ← 會顯示 USD
  https://www.yoshidakaban.com/ko/product/{id}.html           ← 會顯示 KRW
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SCRAPER_NAME = "YoshidaKaban"
BASE_URL = "https://www.yoshidakaban.com"

# 語系前綴：path 開頭可能出現的語系代碼
_LANG_PREFIX_RE = re.compile(r"^/(zh-CHT|zh-CN|en|ko)(/|$)", re.IGNORECASE)


# ── URL 正規化 ──────────────────────────────────────────────────────────


def force_japanese_url(url: str) -> str:
    """
    把 yoshidakaban.com 的任何語系版本 URL 改寫成日文版（無前綴）。
    這是 scraper 內部最後一道保險；normalize_url() 應該已經先做過一次。

    >>> force_japanese_url('https://www.yoshidakaban.com/zh-CHT/product/113830.html')
    'https://www.yoshidakaban.com/product/113830.html'
    >>> force_japanese_url('https://www.yoshidakaban.com/en/product/113830.html')
    'https://www.yoshidakaban.com/product/113830.html'
    >>> force_japanese_url('https://www.yoshidakaban.com/product/113830.html')
    'https://www.yoshidakaban.com/product/113830.html'
    """
    parsed = urlparse(url)
    new_path = _LANG_PREFIX_RE.sub("/", parsed.path)
    return urlunparse(parsed._replace(path=new_path))


# ── 抽取輔助函式 ────────────────────────────────────────────────────────


def _strip_noise(soup: BeautifulSoup) -> None:
    """移除可能造成誤抓的劃線/舊價/script 元素。"""
    selectors = [
        # 劃線元素（特價商品的原價）
        "del", "s", "strike",
        ".price--old", ".old-price", ".price-was", ".price__compare",
        '[class*="strikethrough"]', '[class*="line-through"]',
        # 不會包含可見價格的元素
        "script", "style", "noscript",
    ]
    for sel in selectors:
        try:
            for tag in soup.select(sel):
                tag.decompose()
        except Exception:
            pass


def _to_int_yen(s: str) -> Optional[int]:
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
    """把相對 URL 補完成絕對 URL。"""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    return src


# ── 各欄位抽取 ──────────────────────────────────────────────────────────


def _extract_price(soup: BeautifulSoup) -> Optional[int]:
    """抓税込（含稅）價格。"""
    _strip_noise(soup)

    # 1. OG meta: product:price:amount
    og_price = soup.find("meta", attrs={"property": "product:price:amount"})
    if og_price and og_price.get("content"):
        v = _to_int_yen(og_price["content"])
        if v:
            logger.info(f"[{SCRAPER_NAME}] price from og meta: {v}")
            return v

    # 2. schema.org: itemprop="price"
    for tag in soup.find_all(attrs={"itemprop": "price"}):
        content = tag.get("content") or tag.get_text(strip=True)
        v = _to_int_yen(content)
        if v:
            logger.info(f"[{SCRAPER_NAME}] price from itemprop: {v}")
            return v

    # 3. 可見文字：優先「税込」附近的 ¥xxx
    text = soup.get_text(" ", strip=True)
    tax_inc_patterns = [
        r"¥\s*([\d,]+)\s*[\(（]\s*税込\s*[\)）]",  # ¥xxx (税込)
        r"¥\s*([\d,]+)\s*税込",                     # ¥xxx 税込
        r"税込\s*[:：]?\s*¥\s*([\d,]+)",            # 税込 ¥xxx
        r"税込価格\s*[:：]?\s*¥?\s*([\d,]+)",       # 税込価格: xxx
    ]
    tax_inc_prices: list[int] = []
    for pat in tax_inc_patterns:
        for m in re.finditer(pat, text):
            v = _to_int_yen(m.group(1))
            if v:
                tax_inc_prices.append(v)

    if tax_inc_prices:
        v = min(tax_inc_prices)
        logger.info(
            f"[{SCRAPER_NAME}] price from 税込 pattern: {v} "
            f"(candidates={tax_inc_prices})"
        )
        return v

    # 4. Fallback：所有 ¥xxx 取 min（已先移除劃線元素，所以這裡的 min
    #    應該就是現價；如果頁面同時顯示 税抜+税込，取 min 會選到税抜，
    #    這是已知 trade-off — Yoshida Kaban 通常只顯示税込所以實務 OK）
    all_prices: list[int] = []
    for m in re.finditer(r"¥\s*([\d,]+)", text):
        v = _to_int_yen(m.group(1))
        if v:
            all_prices.append(v)

    if all_prices:
        v = min(all_prices)
        logger.info(
            f"[{SCRAPER_NAME}] price fallback to min(¥): {v} "
            f"(candidates={all_prices})"
        )
        return v

    logger.warning(f"[{SCRAPER_NAME}] no price found")
    return None


def _extract_name(soup: BeautifulSoup) -> Optional[str]:
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


def _extract_image(soup: BeautifulSoup) -> Optional[str]:
    """主圖：og:image → 商品圖區。"""
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image and og_image.get("content"):
        return _absolute_url(og_image["content"])

    # Fallback selector（涵蓋常見命名）
    for sel in [
        ".item-image img", ".product-image img", ".detail-image img",
        ".main-image img", "img.product-photo", ".product__media img",
        "#mainImage", ".slick-slide img",
    ]:
        img = soup.select_one(sel)
        if img and img.get("src"):
            return _absolute_url(img["src"])

    return None


def _extract_sku(soup: BeautifulSoup) -> Optional[str]:
    """商品編號：XXX-XXXXX 格式（如 386-96175 / 381-26879）。"""
    # 優先從 og:title 找（最準）
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        m = re.search(r"(\d{3}-\d{5})", og_title["content"])
        if m:
            return m.group(1)

    # 退而求其次掃整頁
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(\d{3}-\d{5})", text)
    if m:
        return m.group(1)

    return None


def _extract_in_stock(soup: BeautifulSoup) -> bool:
    """庫存判斷：頁面有「売り切れ」/「品切れ」/「SOLD OUT」→ 缺貨。"""
    text = soup.get_text(" ", strip=True)
    out_keywords = ["売り切れ", "品切れ", "SOLD OUT", "在庫なし", "販売終了"]
    for kw in out_keywords:
        if kw in text:
            return False
    return True


# ── 主入口 ──────────────────────────────────────────────────────────────


def scrape(url: str) -> dict[str, Any]:
    """爬取 yoshidakaban 商品頁，回傳統一格式 dict。"""
    target_url = force_japanese_url(url)
    if target_url != url:
        logger.info(f"[{SCRAPER_NAME}] URL rewritten: {url} → {target_url}")

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
    # 強制日文 cookie（防止 IP 自動切語系）
    cookies = {
        "lang": "ja",
        "language": "ja",
        "locale": "ja-JP",
        "preferred_lang": "ja",
    }

    with httpx.Client(
        headers=headers,
        cookies=cookies,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(target_url)
        resp.raise_for_status()
        html = resp.text

    # 防禦性檢查：是否被重定向回非日文版
    final_url = str(resp.url)
    if _LANG_PREFIX_RE.match(urlparse(final_url).path):
        logger.warning(
            f"[{SCRAPER_NAME}] 被重定向到非日文版: {final_url}，價格可能為外幣"
        )

    soup = BeautifulSoup(html, "html.parser")

    name = _extract_name(soup)
    price = _extract_price(soup)
    image = _extract_image(soup)
    sku = _extract_sku(soup)
    in_stock = _extract_in_stock(soup)

    result = {
        "name": name,
        "price": price,
        "currency": "JPY",
        "image": image,
        "url": target_url,
        "sku": sku,
        "in_stock": in_stock,
        "source": "yoshidakaban",
    }
    logger.info(
        f"[{SCRAPER_NAME}] result: name={name!r}, price={price}, "
        f"sku={sku}, in_stock={in_stock}"
    )
    return result


# 對外別名（base.py 路由用 scrape 即可）
scrape_yoshidakaban = scrape


__all__ = [
    "scrape",
    "scrape_yoshidakaban",
    "force_japanese_url",
    "SCRAPER_NAME",
]


# ── CLI 測試 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    test_urls = sys.argv[1:] or [
        # 預設測試：用 zh-CHT 版測試會不會被改寫成日文版
        "https://www.yoshidakaban.com/zh-CHT/product/113324.html",
    ]
    for u in test_urls:
        print(f"\n=== {u} ===")
        try:
            print(json.dumps(scrape(u), ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"ERROR: {e}")
