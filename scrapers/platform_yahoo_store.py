"""
Yahoo!ショッピング 商店街（store.shopping.yahoo.co.jp）Platform
================================================================
接 ZOZO / amiami 的範本，第六支真 Platform。原本這些網址落在 detect_platform 的
"generic" → LegacyPlatform → _scrape_with_playwright（無變體、無逐變體庫存、要開瀏覽器）。

商品頁是 Next.js SSR，httpx 直接拿得到完整 HTML，資料在 <script id="__NEXT_DATA__">：
  props.pageProps.item
    ├ name / applicablePrice（税込實售價）/ regularPrice / bargainPrice
    ├ brandName（"ブランド登録なし" 視為無品牌）/ sellerManagedItemId / janCode
    ├ stock.isAvailable                      整件庫存
    ├ individualItemOptionList[]             選項軸（isSizeOption 標出哪一軸是尺寸）
    ├ individualItemList[]                   逐 SKU：optionList / price / stock.isAvailable / image
    └ images.mainImage / detailImageList / itemImageList（firstOptionChoiceName → 該顏色的圖）
  props.pageProps.seller.id                  賣家代碼（Lib 圖網址要用）

四個 Source（依序試）：
  1. YahooStoreHttpxSource   httpx SSR + __NEXT_DATA__            kind=scraper
        —— 唯一拿得到「變體 + 逐變體庫存 + 逐顏色圖」的來源，故排第一。
  2. YahooStoreApiSource     Yahoo!購物 商品検索 API v3            kind=official_api
        —— 走共用 scrapers/yahoo_api.py（seller_id + 商品代碼搜尋，再比對網址）。
           排第二而非第一的理由：商品検索是「關鍵字搜尋」，沒有 itemLookup 這種
           以商品代碼精準取單品的端點，且回傳無變體；只當 httpx 被擋時的救援。
           需要環境變數 YAHOO_APP_ID，沒設就自動跳過。
  3. YahooStoreSeleniumSource  引擎 UC driver 抓頁，同一支 parser  kind=scraper
  4. YahooStoreGenericSource   最後退回 engine._scrape_with_playwright（＝原本 generic
        的行為），保證分類頁/改版頁不會比接手前更差。              kind=scraper

圖片網址（實測 i/n 是最大張；i/l > i/g > i/d 依序縮小）：
  type=Item → https://item-shopping.c.yimg.jp/i/n/{id}
  type=Lib  → https://shopping.c.yimg.jp/lib/{seller}/{id}

價格：applicablePrice 為 **税込實售價**（與 <meta product:price:amount> 一致，
  含セール価格）；taxExcludedApplicablePrice 是税拔，不要拿。

ZOZO 的雅虎店（store.shopping.yahoo.co.jp/zozo/…）由 ZozotownPlatform 處理
  （註冊順序在前，本平台的 matches() 另外明確排除 seller=zozo）。

註冊（scrapers/__init__.py）：
  from scrapers.platform_yahoo_store import YahooStorePlatform
  register(YahooStorePlatform())          # LegacyPlatform 之前
"""
import re
import json
import html as _html
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import PROXY_URL
from scrapers.base import ProductInfo
from scrapers.jsonld import parse_jsonld_product
from scrapers.platform import Platform, Source
from scrapers.yahoo_api import search_items as _api_search, has_credentials as _api_ready


_MIN_PRICE = 50
_MAX_PRICE = 2_000_000

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_STORE_URL = "https://store.shopping.yahoo.co.jp/{seller}/{code}.html"
_ITEM_IMG = "https://item-shopping.c.yimg.jp/i/n/{iid}"
_LIB_IMG = "https://shopping.c.yimg.jp/lib/{seller}/{iid}"

# brandName 這幾個值等同「沒有品牌」，不要寫進 ProductInfo.brand
_NO_BRAND = ("ブランド登録なし", "ブランド登録無し", "ノーブランド", "ノーブランド品")

_MAX_EXTRA_IMAGES = 8

# store.shopping.yahoo.co.jp/<seller>/<code>.html
_RE_STORE_ITEM = re.compile(
    r'store\.shopping\.yahoo\.co\.jp/([\w\-]+)/([\w\-.]+)\.html(?:[?#]|$)', re.I)
# PayPayモール 舊格式 paypaymall.yahoo.co.jp/store/<seller>/item/<code>
_RE_PAYPAY_ITEM = re.compile(
    r'paypaymall\.yahoo\.co\.jp/store/([\w\-]+)/item/([\w\-.]+?)/?(?:[?#]|$)', re.I)

_HOSTS = ("store.shopping.yahoo.co.jp", "paypaymall.yahoo.co.jp")


# ─────────────────────────────────────────────────────────────────────
# URL helpers
# ─────────────────────────────────────────────────────────────────────
def parse_store_ref(url: str):
    """商品網址 → (seller, code)；不是商品頁 → None。

    注意：Yahoo 商店街的「分類頁」也是 /<seller>/<xxx>.html，光看網址無法和商品頁
    區分。故本函式只負責抽代碼，真假由後續 parser 判定（抓不到價格就往下一個 Source）。
    """
    u = (url or "").split("#")[0]
    for rx in (_RE_STORE_ITEM, _RE_PAYPAY_ITEM):
        m = rx.search(u)
        if m:
            return m.group(1), m.group(2)
    return None


def canonical_url(seller: str, code: str) -> str:
    """統一成 store.shopping 的商品網址（去追蹤參數、PayPayモール 也轉過來）。"""
    if not seller or not code:
        return ""
    return _STORE_URL.format(seller=seller, code=code)


# ─────────────────────────────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────────────────────────────
def _to_int(value):
    """數字/字串 → int（帶合理價格區間檢查）。

    dict / list 一律拒收：本函式是拿 str(value) 抽數字的，把 dict 丟進來會把裡面
    每個數字黏成一串（{"applicablePrice":4950,"immediatePrice":4620} → 49504620），
    運氣好超出區間變 None、運氣不好（990/890 → 990890）就變成一個「看起來正常」
    的假價。價格欄位若是巢狀結構，請走 _sku_price()。
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    s = re.sub(r'[^0-9]', '', str(value))
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if _MIN_PRICE <= v <= _MAX_PRICE else None


def _sku_price(raw):
    """individualItemList[].price → int（取不到回 None）。

    實測有三種形態：
      None                                          該 SKU 與主商品同價（多數頁面）
      {"applicablePrice": N, "immediatePrice": M}   N＝該 SKU 的正常售價
      N                                             純數字（少數頁面）

    immediatePrice（今すぐ買える価格＝即時折扣後）不採用：主商品價取的是
    applicablePrice，變體若改用 immediatePrice 會變成主／變體兩套基準，
    上架後價差會亂掉。兩者一律取 applicablePrice。
    """
    if isinstance(raw, dict):
        for key in ("applicablePrice", "price"):
            v = _to_int(raw.get(key))
            if v:
                return v
        return None
    return _to_int(raw)


def _strip_html(s) -> str:
    """caption / catchCopy 可能夾 HTML；轉純文字給 description 用。"""
    if not s:
        return ""
    txt = re.sub(r'<br\s*/?>', " ", str(s), flags=re.I)
    txt = re.sub(r'<[^>]+>', " ", txt)
    txt = _html.unescape(txt)
    return re.sub(r'\s+', " ", txt).strip()


def _clean_size(s: str) -> str:
    """「サイズXS」→ XS、「[M]」→ M、「L（メンズ：…）」→ L。"""
    s = re.split(r'[（(]', s or "")[0].strip()
    s = re.sub(r'^\[|\]$', '', s).strip()
    s = re.sub(r'^サイズ\s*(?=\S)', '', s).strip()
    return s


def _clean_title(t: str) -> str:
    """og:title「商品名 : 店名 - 通販 - Yahoo!ショッピング」→ 商品名。"""
    t = (t or "").strip()
    t = re.split(r'\s*[-－]\s*通販\s*[-－]\s*Yahoo', t)[0].strip()
    if " : " in t:                      # 店名固定接在最後一個 " : " 之後
        t = t.rsplit(" : ", 1)[0].strip()
    return t


def _image_url(img, seller: str) -> str:
    """images 節點 → 絕對網址。type=Lib 的圖放在賣家的 lib 目錄下。"""
    if not isinstance(img, dict):
        return ""
    iid = str(img.get("id") or "").strip()
    if not iid:
        return ""
    if str(img.get("type") or "").lower() == "lib":
        return _LIB_IMG.format(seller=seller, iid=iid) if seller else ""
    return _ITEM_IMG.format(iid=iid)


# ─────────────────────────────────────────────────────────────────────
# __NEXT_DATA__ 解析
# ─────────────────────────────────────────────────────────────────────
def _next_data(html_text: str):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _page_item(data):
    try:
        item = data["props"]["pageProps"]["item"]
    except Exception:
        item = None
    if isinstance(item, dict) and item:
        return item
    # 部分頁面（例：ZOZO 雅虎店）把 item 放在 react-query 的 dehydratedState 裡
    try:
        for q in data["props"]["pageProps"]["dehydratedState"]["queries"]:
            it = (((q or {}).get("state") or {}).get("data") or {}).get("itemData", {}).get("item")
            if isinstance(it, dict) and it:
                return it
    except Exception:
        pass
    return None


def _page_seller(data, fallback: str = "") -> str:
    try:
        sid = str(data["props"]["pageProps"]["seller"]["id"] or "").strip()
        if sid:
            return sid
    except Exception:
        pass
    return fallback


def _variants_from_item(item: dict, seller: str):
    """individualItemList → variants（只留有庫存的）。

    回傳 (variants, found, any_available)：
      found=False         → 這頁沒有 SKU 清單，呼叫端當單品處理。
      any_available=False → 有清單但全部缺貨 → 整件標為缺貨。
    只剩單一顏色且單一尺寸時回 []（視為單品，不建變體選單），與 ZOZO 一致。
    """
    ilist = item.get("individualItemList") or []
    if not ilist:
        return [], False, False

    # 哪一軸是尺寸：由 individualItemOptionList 的 isSizeOption 決定（比猜名字可靠）
    size_axes = {
        (a.get("name") or "").strip()
        for a in (item.get("individualItemOptionList") or [])
        if a.get("isSizeOption")
    }

    # 第一軸（通常是顏色）的代表圖：itemImageList 的 firstOptionChoiceName
    img_by_choice = {}
    for im in ((item.get("images") or {}).get("itemImageList") or []):
        choice = str((im or {}).get("firstOptionChoiceName") or "").strip()
        if choice and choice not in img_by_choice:
            u = _image_url(im, seller)
            if u:
                img_by_choice[choice] = u

    variants = []
    any_available = False
    skipped = 0
    for it in ilist:
        stock = it.get("stock") or {}
        if not stock.get("isAvailable"):
            skipped += 1
            continue                      # ← 缺貨變體：直接不放進去
        any_available = True

        color = size = ""
        for o in (it.get("optionList") or []):
            nm = (o.get("name") or "").strip()
            cv = (o.get("choiceName") or "").strip()
            if not cv:
                continue
            if nm in size_axes or nm == "サイズ":
                if not size:
                    size = _clean_size(cv)
            elif nm == "カラー":
                if not color:
                    color = cv
            elif not color:               # 非標準軸名 → 第一軸當顏色槽
                color = cv
            elif not size:                # 第二軸當尺寸槽
                size = _clean_size(cv)

        raw_price = it.get("price")
        price = _sku_price(raw_price)
        if raw_price is not None and price is None:
            # 有給價卻讀不出來 → 這支 SKU 會被當成同主價，寧可吵一聲也別靜靜賣錯
            print(f"[YahooStore] ⚠️ SKU {it.get('skuId')!r} 價格無法解析，退用主商品價："
                  f"{raw_price!r}")

        variants.append({
            "color": color,
            "size": size,
            "sku": str(it.get("skuId") or "") or "-".join(p for p in (color, size) if p),
            "price": price or 0,   # 0 → shopify 用主商品價（該 SKU 與主商品同價）
            "in_stock": True,
            "image": _image_url(it.get("image"), seller) or img_by_choice.get(color, ""),
        })

    if skipped:
        print(f"[YahooStore] 缺貨變體已排除 {skipped} 個（只保留有庫存）")

    distinct_colors = {v["color"] for v in variants if v["color"]}
    distinct_sizes = {v["size"] for v in variants if v["size"]}
    if len(distinct_colors) <= 1 and len(distinct_sizes) <= 1:
        return [], True, any_available

    return variants, True, any_available


def _images_from_item(item: dict, seller: str):
    """(主圖, 額外圖)。額外圖走 detailImageList → itemImageList，去重、去主圖。"""
    images = item.get("images") or {}
    main = _image_url(images.get("mainImage"), seller)
    if not main and item.get("mainImageId"):
        main = _ITEM_IMG.format(iid=str(item["mainImageId"]).strip())

    extra = []
    for key in ("detailImageList", "itemImageList"):
        for im in (images.get(key) or []):
            u = _image_url(im, seller)
            if u and u != main and u not in extra:
                extra.append(u)
            if len(extra) >= _MAX_EXTRA_IMAGES:
                return main, extra
    return main, extra


def _description_from_item(item: dict) -> str:
    bits = []
    if item.get("isUsed"):
        cond = _strip_html(item.get("usedConditionText"))
        bits.append(f"中古品：{cond}" if cond else "中古品")
    if item.get("isPreOrder"):
        bits.append("預約商品")
    catch = _strip_html(item.get("catchCopy"))
    if catch:
        bits.append(catch)
    for spec in (item.get("specList") or [])[:4]:
        name = str((spec or {}).get("name") or "").strip()
        vals = [str((v or {}).get("name") or "").strip()
                for v in ((spec or {}).get("valueList") or [])]
        vals = [v for v in vals if v][:4]
        if name and vals:
            bits.append(f"{name}：{'・'.join(vals)}")
    jan = str(item.get("janCode") or "").strip()
    if jan:
        bits.append(f"JAN：{jan}")
    code = str(item.get("sellerManagedItemId") or "").strip()
    if code:
        bits.append(f"品番：{code}")
    return "｜".join(bits)[:1500]


def _from_next_data(item: dict, url: str, seller: str):
    price = (_to_int(item.get("applicablePrice"))
             or _to_int(item.get("bargainPrice"))
             or _to_int(item.get("regularPrice")))
    if not price:
        return None

    p = ProductInfo(source_url=url or str(item.get("url") or "").strip())
    p.price_jpy = price

    title = str(item.get("name") or "").strip()
    if title:
        p.title = title

    brand = str(item.get("brandName") or "").strip()
    if brand and brand not in _NO_BRAND:
        p.brand = brand

    p.description = _description_from_item(item)

    main, extra = _images_from_item(item, seller)
    if main:
        p.image_url = main
    p.extra_images = extra

    p.in_stock = bool((item.get("stock") or {}).get("isAvailable", True))

    variants, found, any_available = _variants_from_item(item, seller)
    p.variants = variants
    if found and not any_available:
        p.in_stock = False            # 整件全部缺貨

    return p


# ─────────────────────────────────────────────────────────────────────
# meta 退路（__NEXT_DATA__ 與 JSON-LD 都失效時）
# ─────────────────────────────────────────────────────────────────────
def _from_meta(html_text: str, url: str):
    soup = BeautifulSoup(html_text, "html.parser")

    def meta(prop=None, name=None):
        if prop:
            el = soup.find("meta", attrs={"property": prop})
            if el and el.get("content"):
                return el["content"].strip()
        if name:
            el = soup.find("meta", attrs={"name": name})
            if el and el.get("content"):
                return el["content"].strip()
        return ""

    price = _to_int(meta(prop="product:price:amount") or meta(name="product:price:amount"))
    if not price:
        return None

    p = ProductInfo(source_url=url)
    p.price_jpy = price

    title = _clean_title(meta(prop="og:title"))
    if title:
        p.title = title

    img = meta(prop="og:image")
    if img:
        p.image_url = img

    p.description = _strip_html(meta(prop="og:description"))[:1500]

    extra = []
    for tag in soup.find_all("img"):
        src = tag.get("src") or ""
        if "yimg.jp" in src and src != p.image_url and src not in extra:
            extra.append(src)
        if len(extra) >= _MAX_EXTRA_IMAGES:
            break
    p.extra_images = extra

    return p


def parse_store_page(html_text: str, url: str = "", seller: str = ""):
    """Yahoo 商店街商品頁 HTML → ProductInfo（httpx 或 Selenium 的 page_source 都可餵）。"""
    if not html_text:
        return None

    data = _next_data(html_text)
    if data:
        item = _page_item(data)
        if item:
            p = _from_next_data(item, url, _page_seller(data, seller))
            if p and p.price_jpy:
                return p

    # 退路 1：schema.org JSON-LD Product（Yahoo 商品頁固定內嵌一塊）
    p = parse_jsonld_product(html_text, url)
    if p and p.price_jpy:
        p.title = _clean_title(p.title) or p.title
        return p

    # 退路 2：og / product meta
    return _from_meta(html_text, url)


# ─────────────────────────────────────────────────────────────────────
# Source 1：httpx SSR
# ─────────────────────────────────────────────────────────────────────
class YahooStoreHttpxSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        seller, code = ref if ref else (parse_store_ref(url) or ("", ""))
        target = canonical_url(seller, code) or (url or "").split("#")[0]
        html_text = await self._fetch(target)
        if not html_text:
            return None
        product = parse_store_page(html_text, target, seller)
        if not product or not product.price_jpy:
            return None
        if product.is_valid:
            print(f"[YahooStore] ✅ {product.title[:50]!r} | ¥{product.price_jpy:,} | "
                  f"brand={product.brand!r} | variants={len(product.variants)} | "
                  f"images={1 + len(product.extra_images)} | in_stock={product.in_stock}")
        return product

    async def _fetch(self, url: str):
        headers = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        }
        proxy_arg = PROXY_URL if PROXY_URL else None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:
                resp = await client.get(url, headers=headers)
                print(f"[YahooStore] {url} → {resp.status_code}, {len(resp.text)} bytes")
                if resp.status_code == 200 and resp.text:
                    return resp.text
                if resp.status_code in (401, 403, 429):
                    print("[YahooStore] ⚠️ 被擋（可能機房 IP）；有設 PROXY_URL 會自動走 proxy")
                return None
        except Exception as e:
            print(f"[YahooStore] httpx 錯誤: {type(e).__name__}: {e}")
            return None


# ─────────────────────────────────────────────────────────────────────
# Source 2：Yahoo!購物 商品検索 API v3（救援用；無變體）
# ─────────────────────────────────────────────────────────────────────
class YahooStoreApiSource(Source):
    kind = "official_api"

    async def get(self, url, ref, engine):
        seller, code = ref if ref else (parse_store_ref(url) or ("", ""))
        if not code:
            return None
        if not _api_ready():
            print("[YahooStore] ⏭️ 未設 YAHOO_APP_ID，跳過官方 API")
            return None
        print(f"[YahooStore] ↩️ 改用官方 API 搜尋（seller={seller!r}, code={code!r}）")
        try:
            hits = await _api_search(code, seller_id=seller or None, hits=30)
        except Exception as e:
            print(f"[YahooStore] API 失敗: {type(e).__name__}: {e}")
            return None
        for p in hits:
            hit_ref = parse_store_ref(p.source_url)
            if hit_ref and hit_ref[1].lower() == code.lower():
                p.source_url = canonical_url(seller or hit_ref[0], code)
                print(f"[YahooStore] ✅ API 命中 {p.title[:40]!r} | ¥{p.price_jpy:,}")
                return p
        print(f"[YahooStore] API 搜尋 {len(hits)} 筆，無網址相符的商品")
        return None


# ─────────────────────────────────────────────────────────────────────
# Source 3：引擎 UC driver（httpx 被擋時）
# ─────────────────────────────────────────────────────────────────────
class YahooStoreSeleniumSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        fetch = getattr(engine, "_fetch_with_selenium", None)
        if fetch is None:
            return None
        seller, code = ref if ref else (parse_store_ref(url) or ("", ""))
        target = canonical_url(seller, code) or (url or "").split("#")[0]
        try:
            html_text = fetch(target)   # 同步；引擎內有 _driver_lock，配合請求佇列序列化
        except Exception as e:
            print(f"[YahooStore] Selenium 失敗: {type(e).__name__}: {e}")
            return None
        if not html_text:
            return None
        product = parse_store_page(html_text, target, seller)
        return product if (product and product.price_jpy) else None


# ─────────────────────────────────────────────────────────────────────
# Source 4：最後退路 —— 原本 generic 的 Playwright 爬蟲（行為不比接手前差）
# ─────────────────────────────────────────────────────────────────────
class YahooStoreGenericSource(Source):
    kind = "scraper"

    async def get(self, url, ref, engine):
        fn = getattr(engine, "_scrape_with_playwright", None)
        if fn is None:
            return None
        print("[YahooStore] ↩️ 全部失敗，退回 generic Playwright")
        try:
            return await fn(url)
        except Exception as e:
            print(f"[YahooStore] generic fallback 失敗: {type(e).__name__}: {e}")
            return None


# ─────────────────────────────────────────────────────────────────────
# Platform
# ─────────────────────────────────────────────────────────────────────
class YahooStorePlatform(Platform):
    id = "yahoo_store"
    sources = [
        YahooStoreHttpxSource(),
        YahooStoreApiSource(),
        YahooStoreSeleniumSource(),
        YahooStoreGenericSource(),
    ]

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in _HOSTS):
            return False
        ref = parse_store_ref(url)
        # ZOZO 的雅虎店由 ZozotownPlatform 接（它註冊在前，這裡再擋一次以免順序被改動）
        if ref and ref[0].lower() == "zozo":
            return False
        return True

    def parse_url(self, url: str):
        return parse_store_ref(url)

    async def search(self, query: str, engine=None, **kw) -> list:
        """programmatic SEO 入口：Yahoo 商品検索 API → list[ProductInfo]。
        seller_id="xxx" 可鎖單一店家；不給就是全站關鍵字搜尋。"""
        if not _api_ready():
            print("[YahooStore] ⚠️ 未設 YAHOO_APP_ID，search() 回空")
            return []
        products = await _api_search(
            query,
            seller_id=kw.get("seller_id"),
            hits=int(kw.get("hits", 30)),
            start=int(kw.get("start", 1)),
        )
        for p in products:
            p.platform_id = self.id
        return products
