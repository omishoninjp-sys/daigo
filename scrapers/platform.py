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


def _note_source(name: str) -> None:
    """把命中的 Source 名稱交給爬取監控。監控不可用時完全略過（fail-safe）。"""
    try:
        import scrape_monitor
        scrape_monitor.note_source(name)
    except Exception:
        pass


def _note_platform(platform_id: str) -> None:
    """把走到哪支 Platform 交給監控。timeout／例外路徑沒有 product，只能靠這個。"""
    try:
        import scrape_monitor
        scrape_monitor.note_platform(platform_id)
    except Exception:
        pass


def _note_error(error, where: str = "") -> None:
    """把 Source 內部被吞掉的例外交給監控（否則紀錄的 error_brief 會是空的）。"""
    try:
        import scrape_monitor
        scrape_monitor.note_error(error, where)
    except Exception:
        pass


def _note_http(status, body: str = "", final_url: str = "") -> None:
    """把 HTTP 回應交給監控（fail-safe）。403/429 靠這個才會被分類成 blocked。"""
    try:
        import scrape_monitor
        scrape_monitor.note_http(status, body, final_url)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# 失敗原因的分類（給 _note_error 用）
# ─────────────────────────────────────────────────────────────────────
# 🔴 為什麼要分類（2026-09-02）
#   Source 一律用 `return None` 表示失敗，Platform.fetch 的 except 攔不到，
#   所以網路層失敗**全部靠這裡自己 note 出去**。而只寫「httpx 失敗」等於沒說 ——
#   被擋 / 逾時 / 非 200 三種的處置完全不同：
#     被擋   → 機房 IP 或 TLS 指紋，要花錢買住宅代理才解得掉
#     逾時   → 先觀察；反覆出現才升級成「疑似被擋」
#     非 200 → 站改版或商品下架，是我們自己要改解析
#   分不出來就會像 2026-08 那次，把每一家 Shopify 商店的失敗都當成 blocked，
#   然後去解一個不存在的問題。
_NET_CONN_ERRORS = (
    "ConnectError", "ConnectionError", "RemoteProtocolError", "ProxyError",
    "NetworkError", "SSLError", "SSLZeroReturnError", "ReadError", "WriteError",
    "UnsupportedProtocol", "InvalidURL",
)


def net_error_brief(error) -> str:
    """網路層例外 → 帶分類的一句話。永遠不 raise。"""
    try:
        name = type(error).__name__
        msg = str(error).replace(chr(10), " ").strip()
        # ★ 逾時要獨立成一類：Akamai 依 TLS 指紋擋的時候，httpx 端看到的
        #   **只是 ReadTimeout** —— TCP 與 TLS 都握手成功，首位元組永遠不來。
        #   MUJI 就是這樣，看起來像「站很慢」，其實是擋。不要在這裡替它斷定
        #   是被擋（會誤判真的慢的站），但一定要記下來讓人看得到頻率。
        if "timeout" in name.lower() or "timeout" in msg.lower():
            return (f"逾時（{name}）：{msg}" if msg else f"逾時（{name}）")[:160]
        if name in _NET_CONN_ERRORS:
            return (f"連線失敗（{name}）：{msg}" if msg else f"連線失敗（{name}）")[:160]
        return f"{name}: {msg}"[:160]
    except Exception:
        return "網路層例外（分類失敗）"


def http_fail_brief(status, body: str = "", blocked=(401, 403, 429)) -> str:
    """非 200（或 200 但空回應）→ 帶分類的一句話。永遠不 raise。"""
    try:
        s = int(status)
    except Exception:
        return "非 200 回應（狀態碼不明）"
    if s in blocked:
        return f"被擋：HTTP {s}（機房 IP 或 TLS 指紋；設 PROXY_URL 可走住宅代理）"
    if s in (404, 410):
        return f"頁面不存在：HTTP {s}（商品下架或網址變了）"
    if s == 200 and not body:
        return "HTTP 200 但回應主體是空的（多半是被擋或轉址到空頁）"
    return f"非 200：HTTP {s}（站改版或暫時性錯誤）"


def missing_method_brief(method: str, what: str) -> str:
    """引擎上找不到某個方法 → 整支 Source 靜默跳過。

    ★ 這一類最危險，也最省錢修（一行）。方法被改名、Mixin 被拿掉、Platform
      被註冊到 Mixin 前面，這支 Source 就永遠回 None，log 裡一句話都沒有。
      MUJI 圖片消失六週、GU 抓不到商品，都是這個形狀。
      **訊息一定要寫出是哪個方法不見了**，只寫「退路不可用」查的人還是不知道去哪找。
    """
    return f"引擎沒有 {method}()，{what}整支跳過（方法被改名或 Mixin 被拿掉？）"


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
        _note_platform(self.id)
        ref = self.parse_url(url)
        last = None
        tried = ""
        for src in self.sources:
            tried = f"{src.__class__.__name__}({src.kind})"
            try:
                r = await src.get(url, ref, engine)
            except Exception as e:
                print(f"[{self.id}] Source {src.__class__.__name__} 失敗: {type(e).__name__}: {e}")
                # ★ 這裡以前只有 print。例外被吞掉、回傳一個空的 ProductInfo，
                #   上層看起來像「成功回傳」，紀錄的 error_brief 就是空的。
                _note_error(e, src.__class__.__name__)
                r = None
            if r and r.is_valid:
                r.platform_id = self.id
                _note_source(tried)
                return r
            if r:
                last = r
        # 全部失敗：仍要記下最後試到哪個 Source，不然只看得到「失敗」兩個字
        if tried:
            _note_source(tried)
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

        _note_platform(tag)
        fn = getattr(engine, method, None) or getattr(engine, "_scrape_with_playwright", None)
        if fn is None:
            raise RuntimeError(f"[legacy] 找不到爬取方法: {method}")

        product = await fn(url)
        _note_source(f"legacy:{method}")
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
