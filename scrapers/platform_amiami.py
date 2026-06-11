"""
amiami (あみあみ) Platform —— 第二支官方 API Platform（接 ZOZO 的範本）。

兩個 Source（依序試）：
  1. RakutenApiSource(shop_code="amiami")  樂天 Ichiba 官方 API   kind=official_api
        —— 用共用 rakuten_api client，scode → 店內搜尋 → slug 比對。
  2. AmiamiUcSource                         amiami.jp UC 直爬 fallback  kind=scraper
        —— 委派 engine._amiami_scrape_jp（沿用 AmiamiMixin 的 driver 邏輯，不重寫）。
        補中古 -R / 未上架 / 樂天店未收的商品。

search(): 走樂天 amiami 店關鍵字搜尋 → 回 list[ProductInfo]，供 programmatic SEO 落地頁。

路由：只比對 amiami.jp host（與原 detect_platform 一致）。
  rakuten.co.jp/amiami 連結維持走原 Rakuten 路徑（RakutenMixin），本檔不影響。

備註：Rakuten API 無變體（amiami 多為單 SKU，可接受）。AmiamiMixin 仍保留在引擎，
  其樂天方法已被 rakuten_api.py 取代（閒置、無害），UC fallback 方法仍在用。
"""
import re
from urllib.parse import urlparse

from scrapers.base import ProductInfo
from scrapers.platform import Platform, Source
from scrapers.rakuten_api import find_by_code, search_items


def _amiami_code(url: str):
    m = re.search(r'rakuten\.co\.jp/amiami/([\w\-]+)', url or "", re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'[?&](?:g|s)code=([\w\-]+)', url or "", re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _amiami_clean_url(url: str) -> str:
    clean = (url or "").split("#")[0].strip()
    m = re.search(r'(https?://item\.rakuten\.co\.jp/amiami/[\w\-]+)', clean, re.IGNORECASE)
    if m:
        return m.group(1).rstrip("/") + "/"
    base_m = re.match(r'(https?://[^/]+/top/detail/detail)', clean, re.IGNORECASE)
    key_m = re.search(r'(g?s?code)=([\w\-]+)', clean, re.IGNORECASE)
    if base_m and key_m:
        return f"{base_m.group(1)}?{key_m.group(1)}={key_m.group(2)}"
    return clean


class RakutenApiSource(Source):
    kind = "official_api"

    def __init__(self, shop_code: str):
        self.shop_code = shop_code

    async def get(self, url, ref, engine):
        code = ref or _amiami_code(url)
        if not code:
            return None
        print(f"[Amiami] 商品代碼: {code} → 樂天 API（shop={self.shop_code}）")
        product = await find_by_code(code, self.shop_code)
        if product and product.is_valid:
            product.source_url = _amiami_clean_url(url)   # 顯示/快取用原站連結
            return product
        return None


class AmiamiUcSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        fn = getattr(engine, "_amiami_scrape_jp", None)
        if fn is None:
            return None
        clean = _amiami_clean_url(url)
        print(f"[Amiami] ↩️ 樂天查無，改用 amiami.jp 直爬 fallback")
        product = ProductInfo(source_url=clean)
        try:
            await fn(clean, product)
        except Exception as e:
            print(f"[Amiami] UC fallback 失敗: {type(e).__name__}: {e}")
            return None
        if product.is_valid:
            return product
        return product if product.price_jpy else None


class AmiamiPlatform(Platform):
    id = "amiami"
    sources = [RakutenApiSource("amiami"), AmiamiUcSource()]

    def matches(self, url: str) -> bool:
        return "amiami.jp" in (urlparse(url).hostname or "").lower()

    def parse_url(self, url: str):
        return _amiami_code(url)

    async def search(self, query: str, engine=None, **kw) -> list:
        """programmatic SEO 入口：amiami 店關鍵字搜尋 → list[ProductInfo]。"""
        hits = int(kw.get("hits", 30))
        available_only = bool(kw.get("available_only", False))
        products = await search_items(query, shop_code="amiami",
                                      hits=hits, available_only=available_only)
        for p in products:
            p.platform_id = self.id
        return products
