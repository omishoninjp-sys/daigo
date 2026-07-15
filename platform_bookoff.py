"""
BOOKOFF 公式オンラインストア Platform（shopping.bookoff.co.jp）v1
================================================================
對齊 ZozotownPlatform：真 Platform、SSR 自含、不需 Selenium。
BOOKOFF 商品頁在伺服器 HTML 裡就內嵌 schema.org 的 JSON-LD（Product/offers），
價格、庫存、狀態、圖片全在裡面，httpx 直抓即可。

  BookoffJsonLdSource    httpx 抓頁 + 解析 JSON-LD             kind=scraper
     · 沿用共用 config.PROXY_URL：若有設 proxy 則走 proxy。
  BookoffSeleniumSource  httpx 被擋時退回引擎 UC driver 抓頁    kind=scraper
     · 委派 engine._fetch_with_selenium（uc_open_with_reconnect），
       再用同一個 parse_bookoff 解析 page_source。
     · BOOKOFF 會切斷機房 IP 的 httpx 直連（RemoteProtocolError），
       真瀏覽器 UC 有機會通過（不保證，機房 IP 仍可能被 Akamai 擋）。

要點：
  · 頁面上有推薦商品的數字（2,420 / 1,800 / ¥5,170…）——一律只走 JSON-LD，
    不讀頁面文字裡的裸數字，才不會抓錯。
  · 二手品：單件庫存、無變體；offers.availability 非 InStock 一律視為缺貨，
    避免對售罄品開單。
  · 抓取 / 解析分離：parse_bookoff(html, url) 是純函式；若日後 httpx 被擋，
    可改用 SeleniumBase 的 driver.page_source 餵給它，零改動。

新增這支後，只需在 scrapers/__init__.py 註冊（見檔尾說明）；base.py 不用動。
"""
import re
import json
from urllib.parse import urlparse

import httpx

from scrapers.base import ProductInfo
from scrapers.platform import Platform, Source

try:
    from config import PROXY_URL
except Exception:  # 測試 / 尚未設定時
    PROXY_URL = None


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
# 商品識別碼（選配，僅供 log）：/used/0017127470、/goods/…、/new/…
_ID_RE = re.compile(r"/(?:used|goods|new|ec/[a-z0-9]+)?/?(\d{6,})", re.I)


# ─────────────────────────────────────────────────────────────────────
# JSON-LD 解析 helpers
# ─────────────────────────────────────────────────────────────────────
def _iter_jsonld(html: str):
    for m in _LD_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:
                continue
        pool = data if isinstance(data, list) else [data]
        for node in pool:
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                for g in node["@graph"]:
                    if isinstance(g, dict):
                        yield g
            elif isinstance(node, dict):
                yield node


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return "Product" in types


def _find_product(html: str):
    for node in _iter_jsonld(html):
        if _is_product(node):
            return node
    return None


def _pick_offer(offers):
    """offers 可能是 dict、list 或 AggregateOffer。優先取 InStock 的那筆。"""
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        cand = [o for o in offers if isinstance(o, dict)]
        instock = [o for o in cand
                   if str(o.get("availability", "")).lower().endswith("instock")]
        pool = instock or cand
        return pool[0] if pool else None
    return None


def _to_int(value):
    if value is None:
        return None
    s = re.sub(r"[^0-9]", "", str(value))
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _price_from_offer(offer: dict):
    if not isinstance(offer, dict):
        return None
    for k in ("price", "lowPrice", "highPrice"):
        v = _to_int(offer.get(k))
        if v:
            return v
    ps = offer.get("priceSpecification")
    if isinstance(ps, dict):
        v = _to_int(ps.get("price"))
        if v:
            return v
    if isinstance(ps, list):
        for one in ps:
            if isinstance(one, dict):
                v = _to_int(one.get("price"))
                if v:
                    return v
    return None


def _first_image(node: dict) -> str:
    img = node.get("image")
    if isinstance(img, list) and img:
        first = img[0]
        if isinstance(first, dict):
            return str(first.get("url") or first.get("contentUrl") or "").strip()
        return str(first).strip()
    if isinstance(img, dict):
        return str(img.get("url") or img.get("contentUrl") or "").strip()
    if isinstance(img, str):
        return img.strip()
    return ""


def _condition_label(offer: dict, node: dict) -> str:
    cond = str((offer or {}).get("itemCondition") or node.get("itemCondition") or "").lower()
    if "used" in cond or "refurb" in cond:
        return "中古品"
    if "new" in cond:
        return "新品"
    return ""


# ─────────────────────────────────────────────────────────────────────
# 純解析（不碰網路）—— httpx 或 SeleniumBase page_source 都可餵進來
# ─────────────────────────────────────────────────────────────────────
def parse_bookoff(html: str, url: str = "") -> ProductInfo | None:
    if not html:
        return None
    node = _find_product(html)
    if not node:
        return None

    offer = _pick_offer(node.get("offers"))
    price = _price_from_offer(offer)
    if not price:
        return None  # 沒價格不硬給，回 None 讓上層退手動表單

    p = ProductInfo(source_url=(url or str(node.get("url") or "").strip()))
    p.price_jpy = price

    name = str(node.get("name") or "").strip()
    if name:
        p.title = name

    img = _first_image(node)
    if img:
        p.image_url = img

    brand = node.get("brand")
    if isinstance(brand, dict) and brand.get("name"):
        p.brand = str(brand["name"]).strip()
    elif isinstance(brand, str) and brand.strip():
        p.brand = brand.strip()

    avail = str((offer or {}).get("availability", "")).lower()
    p.in_stock = (("instock" in avail) or ("preorder" in avail)) if avail else True

    cond = _condition_label(offer, node)
    if cond:
        p.description = cond

    return p


# ─────────────────────────────────────────────────────────────────────
# Source：httpx 抓頁（含 proxy fallback）
# ─────────────────────────────────────────────────────────────────────
class BookoffJsonLdSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        html = await self._fetch(url)
        if not html:
            return None
        product = parse_bookoff(html, url)
        # 有價格才回；否則 None 讓上層走手動表單
        return product if (product and product.price_jpy) else None

    async def _fetch(self, url: str):
        headers = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        }
        proxy_arg = PROXY_URL if PROXY_URL else None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:
                resp = await client.get(url.strip(), headers=headers)
                print(f"[BookOff] {url} → {resp.status_code}, {len(resp.text)} bytes")
                if resp.status_code == 200 and resp.text:
                    return resp.text
                if resp.status_code in (401, 403):
                    print("[BookOff] ⚠️ 被擋（可能機房 IP）；已設 PROXY_URL 會自動走 proxy")
                return None
        except Exception as e:
            print(f"[BookOff] httpx 錯誤: {type(e).__name__}: {e}")
            return None


# ─────────────────────────────────────────────────────────────────────
# Source（退路）：引擎 UC driver 抓頁（httpx 被擋時）
# ─────────────────────────────────────────────────────────────────────
class BookoffSeleniumSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        fetch = getattr(engine, "_fetch_with_selenium", None)
        if fetch is None:
            return None  # 沒有引擎（例如純解析測試）→ 交回上層
        try:
            # 比照 generic._scrape_with_playwright：_fetch_with_selenium 為同步、
            # 內部有 _driver_lock，配合請求佇列序列化，直接呼叫即可。
            html = fetch(url)
        except Exception as e:
            print(f"[BookOff] Selenium 失敗: {type(e).__name__}: {e}")
            return None
        if not html:
            return None
        product = parse_bookoff(html, url)
        return product if (product and product.price_jpy) else None


# ─────────────────────────────────────────────────────────────────────
# Platform
# ─────────────────────────────────────────────────────────────────────
class BookoffPlatform(Platform):
    id = "bookoff"
    sources = [BookoffJsonLdSource(), BookoffSeleniumSource()]

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "bookoff.co.jp" or host.endswith(".bookoff.co.jp")

    def parse_url(self, url: str) -> str:
        m = _ID_RE.search(url or "")
        return m.group(1) if m else url

    # TODO(programmatic SEO)：search() 可接 BOOKOFF 站內搜尋端點，批量產分類落地頁。


# ─────────────────────────────────────────────────────────────────────
# 註冊方式（scrapers/__init__.py）：
#
#   from scrapers.platform_bookoff import BookoffPlatform
#   ...
#   register(ZozotownPlatform())
#   register(AmiamiPlatform())
#   register(BookoffPlatform())        # ← 加這行（LegacyPlatform 之前）
#   register(LegacyPlatform())
#
# base.py 不用改（真 Platform 用自己的 matches()；BOOKOFF 也不在 BLOCKED_DOMAINS）。
# ─────────────────────────────────────────────────────────────────────
