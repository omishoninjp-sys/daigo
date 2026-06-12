"""
共用 Yahoo!購物 商品検索 API (v3) client
========================================
給 ZOZO 搜尋用（ZOZO 在 Yahoo!購物的官方店，賣家代碼 zozo）。
模子與 scrapers/rakuten_api.py 一致：search_items(keyword, seller_id=...) → list[ProductInfo]。

商品検索是公開讀取 API，只需 appid（Client ID），不用 access token / OAuth / 綁店家。
  端點：https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch
  認證：appid = Yahoo Client ID（アプリケーションID）
  限流：約 1 query/秒（只在「按搜尋」時打一次，足夠）

環境變數（Zeabur）：
  YAHOO_APP_ID   Yahoo Client ID（アプリケーションID）   ← 必填

注意：依 Yahoo 開發者規範，前台顯示結果需標示來源（クレジット表示，已在前台頁尾加）。
"""
import os
import re
import asyncio

import httpx

from scrapers.base import ProductInfo

_ENDPOINT = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
_HTTP_TIMEOUT = 20.0
_MIN_PRICE = 50
_MAX_PRICE = 5_000_000


def has_credentials() -> bool:
    return bool(os.environ.get("YAHOO_APP_ID", "").strip())


def _appid() -> str:
    return os.environ.get("YAHOO_APP_ID", "").strip()


async def _call(extra_params: dict) -> dict | None:
    """呼叫 Yahoo 商品検索 API。回 dict；失敗回 None。429/5xx 自動重試。"""
    appid = _appid()
    if not appid:
        print("[Yahoo] ⚠️ 缺 YAHOO_APP_ID")
        return None

    params = {"appid": appid, **extra_params}
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) goyoutati-daigo/1.0",
    }

    last_status, last_text = None, ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(_ENDPOINT, params=params, headers=headers)
        except Exception as e:
            print(f"[Yahoo] ❌ API 連線失敗: {type(e).__name__}: {e}")
            return None

        last_status, last_text = resp.status_code, resp.text[:200]

        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as e:
                print(f"[Yahoo] ❌ 回傳非 JSON: {e} | {resp.text[:200]}")
                return None
        if resp.status_code in (429, 500, 502, 503):
            print(f"[Yahoo] ⏳ {resp.status_code}（attempt {attempt + 1}/3）等 1.2s 重試… {resp.text[:120]}")
            await asyncio.sleep(1.2)
            continue
        print(f"[Yahoo] ❌ API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    print(f"[Yahoo] ❌ 重試後仍失敗（HTTP {last_status}）：{last_text}")
    return None


# ─────────────────────────────────────────────────────────────────────
# 解析 helpers
# ─────────────────────────────────────────────────────────────────────
def _items(data: dict) -> list:
    """v3 回傳 hits 陣列。"""
    hits = data.get("hits")
    return hits if isinstance(hits, list) else []


def _to_int(value):
    if value is None:
        return None
    s = re.sub(r'[^0-9]', '', str(value))
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if _MIN_PRICE <= v <= _MAX_PRICE else None


def _image_of(hit: dict) -> str:
    image = hit.get("image")
    if isinstance(image, dict):
        return (image.get("medium") or image.get("small") or "").strip()
    if isinstance(image, str):
        return image.strip()
    return ""


def _brand_of(hit: dict) -> str:
    brand = hit.get("brand")
    if isinstance(brand, dict):
        return (brand.get("name") or "").strip()
    return ""


def _item_to_product(hit: dict) -> ProductInfo:
    p = ProductInfo()

    name = str(hit.get("name") or "").strip()
    if name:
        p.title = name

    price = _to_int(hit.get("price"))
    if price:
        p.price_jpy = price

    desc = str(hit.get("description") or hit.get("headLine") or "").strip()
    if desc:
        p.description = desc[:1500]

    p.in_stock = bool(hit.get("inStock", True))

    img = _image_of(hit)
    if img:
        p.image_url = img

    brand = _brand_of(hit)
    if brand:
        p.brand = brand

    url = str(hit.get("url") or "").strip()
    if url:
        p.source_url = url  # 例：https://store.shopping.yahoo.co.jp/zozo/xxxx.html

    return p


# ─────────────────────────────────────────────────────────────────────
# 公開 API
# ─────────────────────────────────────────────────────────────────────
async def search_items(keyword: str, seller_id: str = None, hits: int = 30,
                       start: int = 1, **extra) -> list:
    """
    關鍵字搜尋 → list[ProductInfo]（只回 is_valid 的）。
    seller_id="zozo" 鎖 ZOZO 的 Yahoo 店。
    Yahoo 用 start（1-based 位移）翻頁，非 page；results 為單頁筆數（上限 50）。
    可額外傳 sort、genre_category_id 等 Yahoo 參數（**extra 直通）。
    """
    if not keyword:
        return []
    params = {
        "query": keyword,
        "results": max(1, min(int(hits), 50)),
        "start": max(1, int(start)),
    }
    if seller_id:
        params["seller_id"] = seller_id
    params.update(extra)

    data = await _call(params)
    if data is None:
        return []

    out = []
    for hit in _items(data):
        try:
            p = _item_to_product(hit)
            if p.is_valid:
                out.append(p)
        except Exception:
            continue
    return out
