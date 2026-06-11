"""
ZOZOTOWN Platform —— Platform 介面的第一支實作（pilot）。

兩個 Source（依序試）：
  1. ZozoYahooSource  雅虎官方店 SSR（httpx，繞過 Akamai）        kind=partner
  2. ZozoLegacySource 退回舊版 zozo.jp 爬蟲（SeleniumBase UC）     kind=scraper

兩個 Source 都「委派」給現有 Scraper 引擎的既有方法（_zozo_via_yahoo /
_scrape_zozotown_legacy）——不重複邏輯、不動既有 zozo 程式。這就是「對上層透明、
底層可換多策略」的範本；日後要把邏輯整支搬進來、或加第三個 Source（如官方 API）
都只動這支檔案。
"""
import re
from urllib.parse import urlparse

from scrapers.base import ProductInfo
from scrapers.platform import Platform, Source


def _zozo_gid(url: str) -> str:
    """從 zozo.jp 或雅虎店連結抽 goods ID。"""
    m = re.search(r'/goods(?:-sale)?/(\d{4,})', url or "")
    if m:
        return m.group(1)
    m = re.search(r'/zozo/(\d{4,})\.html', url or "")
    if m:
        return m.group(1)
    return ""


class ZozoYahooSource(Source):
    """ZOZOTOWN 雅虎官方店（乾淨 SSR，無 Akamai）。"""
    kind = "partner"

    async def get(self, url, ref, engine):
        gid = ref or _zozo_gid(url)
        if not gid:
            return None
        product = ProductInfo(source_url=url, brand="")
        ok = await engine._zozo_via_yahoo(url, gid, product)
        if ok and product.is_valid:
            return product
        return product if product.price_jpy else None


class ZozoLegacySource(Source):
    """退回舊版 zozo.jp UC 爬蟲（雅虎店為 zozo.jp 子集，查無時補。）"""
    kind = "scraper"

    async def get(self, url, ref, engine):
        fn = getattr(engine, "_scrape_zozotown_legacy", None)
        if fn is None:
            return None
        try:
            return await fn(url)
        except Exception as e:
            print(f"[zozotown] legacy 失敗: {type(e).__name__}: {e}")
            return None


class ZozotownPlatform(Platform):
    id = "zozotown"
    sources = [ZozoYahooSource(), ZozoLegacySource()]

    def matches(self, url: str) -> bool:
        return "zozo" in (urlparse(url).hostname or "").lower()

    def parse_url(self, url: str) -> str:
        return _zozo_gid(url)

    # TODO(programmatic SEO)：search() 可走雅虎店搜尋 / zozo 分類端點，
    #   批量產品牌/分類落地頁。本輪先沿用預設（回 []），待 Rakuten/amiami 先示範。
