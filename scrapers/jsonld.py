"""
共用 JSON-LD Platform 基底（schema.org Product/offers）
========================================================
很多日本 EC 站（BookOff、SNIDEL/MASH USSE、以及其他）在伺服器 HTML 裡直接內嵌
schema.org 的 JSON-LD Product 區塊，價格/圖/品牌/庫存都在裡面，不需渲染 JS。
這支把「解析 JSON-LD → ProductInfo」與「httpx/Selenium 抓頁」抽成共用基底，
新站只要繼承 JsonLdPlatform、填 hosts 即可（見 platform_snidel.py）。

  parse_jsonld_product(html, url)   純解析（httpx 或 SeleniumBase page_source 都可餵）
  JsonLdHttpxSource                 httpx 抓頁（含 config.PROXY_URL）
  JsonLdSeleniumSource              httpx 被擋時退回引擎 UC driver
  JsonLdPlatform(Platform)          基底：sources=[httpx, selenium]，子類填 hosts

要點：
  · 只走 JSON-LD，不讀頁面文字裸數字（避開推薦商品/其他賣場的干擾數字）。
  · offers 容錯：dict / list / AggregateOffer；price 可為字串（"71000"）。
  · availability 非 InStock 視為缺貨；JSON-LD 無 availability 時預設有貨。
"""
import re
import json
from urllib.parse import urlparse

import httpx

from scrapers.base import ProductInfo
from scrapers.platform import Platform, Source

try:
    from config import PROXY_URL
except Exception:
    PROXY_URL = None


def _note_http(status, body=""):
    """回報 HTTP 狀態給爬取監控（fail-safe，監控壞掉不影響爬取）。"""
    try:
        import scrape_monitor
        scrape_monitor.note_http(status, body)
    except Exception:
        pass



_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


# ─────────────────────────────────────────────────────────────────────
# 解析 helpers
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


def parse_jsonld_product(html: str, url: str = "") -> ProductInfo | None:
    """從含 schema.org JSON-LD 的商品頁 HTML 解析 → ProductInfo；無 Product 或無價 → None。"""
    if not html:
        return None
    node = _find_product(html)
    if not node:
        return None

    offer = _pick_offer(node.get("offers"))
    price = _price_from_offer(offer)
    if not price:
        return None

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
# 通用 Source
# ─────────────────────────────────────────────────────────────────────
class JsonLdHttpxSource(Source):
    kind = "scraper"

    def __init__(self, parser=None, tag="JsonLd"):
        self.parser = parser or parse_jsonld_product
        self.tag = tag

    async def get(self, url, ref, engine):
        html = await self._fetch(url)
        if not html:
            return None
        product = self.parser(html, url)
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
                _note_http(resp.status_code, resp.text)
                print(f"[{self.tag}] {url} → {resp.status_code}, {len(resp.text)} bytes")
                if resp.status_code == 200 and resp.text:
                    return resp.text
                if resp.status_code in (401, 403):
                    print(f"[{self.tag}] ⚠️ 被擋（可能機房 IP）；有設 PROXY_URL 會自動走 proxy")
                return None
        except Exception as e:
            print(f"[{self.tag}] httpx 錯誤: {type(e).__name__}: {e}")
            return None


def _note_error(error, where=""):
    """把被吞掉的失敗交給監控（fail-safe：監控壞掉不影響爬取）。"""
    try:
        import scrape_monitor
        scrape_monitor.note_error(error, where)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# 選配：用瀏覽器把主圖抓成 base64
# ─────────────────────────────────────────────────────────────────────
# ★ 為什麼需要這個（2026-09-02，MUJI）：
#   www.muji.com 走 Akamai（DNS → www.muji.com.edgekey.net），**依 TLS 指紋
#   擋掉非瀏覽器的請求** —— httpx 與 curl 對整個網域（含首頁）都是 TCP+TLS
#   握手成功、首位元組永遠不來，而同一台機器的 Chrome UC 正常。
#   所以圖片 URL 原樣交給 Shopify 讓它伺服器端抓，一定失敗（線上 68% 的
#   MUJI 商品無圖）。唯一可行的是**在已經通過 Akamai 的瀏覽器 session 裡**
#   用 fetch() 把圖抓成 blob 再轉 base64，帶著同一組 cookie 與指紋。
#
# ★ 預設關閉。只有明確需要的平台才 image_b64=True —— 這是額外的瀏覽器
#   往返，沒有被擋的站不該付這個成本。
def _browser_image_b64(engine, image_url: str, tag: str):
    """在 Selenium session 內把圖抓成 base64。取不到回 None，絕不 raise。"""
    lock = getattr(engine, "_driver_lock", None)
    if lock is None:
        return None
    try:
        with lock:
            driver = getattr(engine, "_driver", None)
            if driver is None:
                print(f"[{tag}] 圖片 base64：driver 已關閉，略過")
                _note_error("圖片 base64 略過：driver 已關閉", tag)
                return None
            # ★ 必須同源。fetch() 之所以能過 Akamai 是因為帶著該網域的 cookie；
            #   driver 若已被別的請求導到其他站，這就變成跨來源請求，
            #   CORS 會擋掉讀取 blob。寧可放棄也不要抓到錯的東西。
            try:
                cur_host = (urlparse(driver.current_url).hostname or "").lower()
            except Exception:
                cur_host = ""
            img_host = (urlparse(image_url).hostname or "").lower()
            if not cur_host or cur_host != img_host:
                print(f"[{tag}] 圖片 base64：driver 已離開該網域"
                      f"（現在 {cur_host or '?'}，圖片 {img_host}），略過")
                _note_error(f"圖片 base64 略過：driver 已離開該網域（{cur_host or '?'}）", tag)
                return None
            b64 = driver.execute_script("""
                return await fetch(arguments[0])
                    .then(r => r.blob())
                    .then(b => new Promise((res, rej) => {
                        const fr = new FileReader();
                        fr.onload = () => res(fr.result.split(',')[1]);
                        fr.onerror = rej;
                        fr.readAsDataURL(b);
                    }));
            """, image_url)
            if b64 and len(b64) > 100:
                print(f"[{tag}] 主圖 base64 OK（瀏覽器，{len(b64) // 1024}KB）")
                return b64
            print(f"[{tag}] 圖片 base64：回傳空或過短")
            _note_error("圖片 base64 回傳空或過短", tag)
    except Exception as e:
        print(f"[{tag}] 圖片 base64 失敗: {type(e).__name__}: {e}")
        _note_error(e, f"{tag}/image_b64")
    return None


class JsonLdSeleniumSource(Source):
    kind = "scraper"

    def __init__(self, parser=None, tag="JsonLd", image_b64=False):
        self.parser = parser or parse_jsonld_product
        self.tag = tag
        # ★ 預設關閉。只有被 Akamai 之類依 TLS 指紋擋掉、圖片只能走瀏覽器的
        #   平台才打開（目前只有 MujiPlatform）。見上面 _browser_image_b64。
        self.image_b64 = image_b64

    async def get(self, url, ref, engine):
        fetch = getattr(engine, "_fetch_with_selenium", None)
        if fetch is None:
            return None
        try:
            html = fetch(url)  # 同步；引擎內有 _driver_lock，配合請求佇列序列化
        except Exception as e:
            print(f"[{self.tag}] Selenium 失敗: {type(e).__name__}: {e}")
            return None
        if not html:
            return None
        product = self.parser(html, url)
        if not (product and product.price_jpy):
            return None
        if self.image_b64 and product.image_url and not getattr(product, "image_base64", ""):
            b64 = _browser_image_b64(engine, product.image_url, self.tag)
            if b64:
                product.image_base64 = b64
        return product


# ─────────────────────────────────────────────────────────────────────
# 基底 Platform：子類只要填 id + hosts
# ─────────────────────────────────────────────────────────────────────
class JsonLdPlatform(Platform):
    hosts: tuple = ()      # 子類覆寫，例如 ("snidel.com",)
    sources = [JsonLdHttpxSource(), JsonLdSeleniumSource()]

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)
