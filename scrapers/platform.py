"""
GOYOUTATI daigo —— Platform 介面層 (v1, 2026-06)

對齊轉型藍圖：
  維度一  每個「來源」= 一個 Platform；底下掛多個 Source（official_api / partner /
          scraper），對上層透明。新增來源只要寫一個 Platform，不動路由、不動上層。
  結論#1  search()：日後給 programmatic SEO 批量產生品牌/分類落地頁
          （Rakuten / amiami 等有官方搜尋 API 的來源先實作）。
  #2量測  platform_id：每筆 ProductInfo 標來源，貫穿訂單/Shopify，
          用來算「哪個來源真的有營收」。
  別大爆炸 LegacyPlatform 把現有 45 支 Mixin 原樣接進來、零行為變更；
          再一支一支抽成真正的 Platform（ZOZOTOWN 為第一支）。
"""
from abc import ABC, abstractmethod

from scrapers.base import ProductInfo, detect_platform


class Source(ABC):
    """
    單一取得策略。kind ∈ {official_api, partner, scraper}。
    get() 回 ProductInfo（可能 invalid）或 None（此策略完全取不到，換下一條）。
    """
    kind: str = "scraper"

    @abstractmethod
    async def get(self, url: str, ref, engine) -> ProductInfo | None:
        ...


class Platform(ABC):
    """
    一個來源（店/站）。
      matches(url)    路由（取代 detect_platform 的巨大 if/elif）
      parse_url(url)  抽出識別碼（scode / goods id …），給 source 用
      fetch(url)      依序試 sources，對上層透明（預設實作）
      search(query)   選配：programmatic SEO 用
    """
    id: str = ""
    sources: list = []

    @abstractmethod
    def matches(self, url: str) -> bool:
        ...

    def parse_url(self, url: str):
        """抽識別碼；預設回原 url。"""
        return url

    async def fetch(self, url: str, engine) -> ProductInfo:
        """預設：依序試 self.sources，第一個 is_valid 即回；皆失敗回最後的部分結果。"""
        ref = self.parse_url(url)
        last = None
        for src in self.sources:
            try:
                r = await src.get(url, ref, engine)
            except Exception as e:
                print(f"[{self.id}] Source {src.__class__.__name__} 失敗: {type(e).__name__}: {e}")
                r = None
            if r and r.is_valid:
                r.platform_id = self.id
                return r
            if r:
                last = r
        out = last or ProductInfo(source_url=url)
        out.platform_id = self.id
        return out

    async def search(self, query: str, engine=None, **kw) -> list:
        """選配：給 programmatic SEO 批量落地頁用。預設未實作。"""
        return []


class LegacyPlatform(Platform):
    """
    遷移催化劑：尚未抽成 Platform 的來源，原樣導向現有 Scraper Mixin 方法。
    路由沿用 detect_platform；方法名 = '_scrape_' + 平台字串。
    特例：generic 且 oakley.com → _scrape_oakley；其餘 generic → _scrape_with_playwright。
    註冊在最後當 catch-all。
    """
    id = "legacy"

    def matches(self, url: str) -> bool:
        return True

    async def fetch(self, url: str, engine) -> ProductInfo:
        plat = detect_platform(url)
        if plat == "generic":
            is_oakley = "oakley.com" in (url or "")
            method = "_scrape_oakley" if is_oakley else "_scrape_with_playwright"
            tag = "oakley" if is_oakley else "generic"
        else:
            method = "_scrape_" + plat
            tag = plat

        fn = getattr(engine, method, None) or getattr(engine, "_scrape_with_playwright", None)
        if fn is None:
            raise RuntimeError(f"[legacy] 找不到爬取方法: {method}")

        product = await fn(url)
        if not getattr(product, "platform_id", ""):
            product.platform_id = tag
        return product


# ───────────────────────── registry / dispatch ─────────────────────────
REGISTRY: list = []


def register(platform: Platform) -> Platform:
    """註冊一個 Platform。真 Platform 先註冊，LegacyPlatform 最後（catch-all）。"""
    REGISTRY.append(platform)
    return platform


def get_platform(url: str) -> Platform:
    """回傳第一個 matches 的 Platform；沒有則回最後一個（應為 LegacyPlatform）。"""
    for p in REGISTRY:
        try:
            if p.matches(url):
                return p
        except Exception:
            continue
    return REGISTRY[-1] if REGISTRY else None
