"""
共用 Rakuten Ichiba Item Search API client
==========================================
自 scrapers/amiami.py 的已驗證樂天邏輯抽出（認證 / 重試 / 解析一致），給：
  - amiami Platform（platform_amiami.py）的官方 API Source
  - programmatic SEO 的 search_items()
共用，行為與線上版一致。

v1.1：search_items 加 AND→OR 退路——樂天 keyword 多字預設 AND（全字都要中），
  多字查無時自動改 orFlag=1（OR）再試一次，避免「櫃子 黑色」這種多字查無。

限制（重要）：Rakuten API 無變體 / SKU、availability 只有 0/1。
  → 適合 amiami（單 SKU）。rakuten.co.jp 一般站需要變體，維持原 RakutenMixin 爬蟲，不走這支。

環境變數（Zeabur）：
  RAKUTEN_APP_ID      樂天 Application ID（UUID）   ← 必填
  RAKUTEN_ACCESS_KEY  樂天 Access Key（pk_...）     ← 必填
  RAKUTEN_REFERER     預設 https://goyoutati.com/   ← 須對應 App 後台 Allowed websites
"""
import os
import re
import asyncio

import httpx

from scrapers.base import ProductInfo

_ENDPOINT = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
_DEFAULT_REFERER = "https://goyoutati.com/"
_HTTP_TIMEOUT = 20.0
_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


def has_credentials() -> bool:
    return bool(os.environ.get("RAKUTEN_APP_ID", "").strip()
                and os.environ.get("RAKUTEN_ACCESS_KEY", "").strip())


def _creds():
    app_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
    referer = (os.environ.get("RAKUTEN_REFERER", "").strip() or _DEFAULT_REFERER)
    return app_id, access_key, referer


async def _call(extra_params: dict) -> dict | None:
    """呼叫樂天 Ichiba Item Search API。回 dict；失敗回 None。403/429 自動重試。"""
    app_id, access_key, referer = _creds()
    if not app_id or not access_key:
        print("[Rakuten] ⚠️ 缺 RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY")
        return None

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "formatVersion": 2,
        **extra_params,
    }
    origin = re.sub(r'(https?://[^/]+).*', r'\1', referer)
    headers = {
        "Referer": referer,
        "Origin": origin,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) goyoutati-daigo/1.0",
    }

    last_status, last_text = None, ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(_ENDPOINT, params=params, headers=headers)
        except Exception as e:
            print(f"[Rakuten] ❌ API 連線失敗: {type(e).__name__}: {e}")
            return None

        last_status, last_text = resp.status_code, resp.text[:200]

        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as e:
                print(f"[Rakuten] ❌ 回傳非 JSON: {e} | {resp.text[:200]}")
                return None
        if resp.status_code == 404:
            print("[Rakuten] ⚠️ 404 not_found")
            return {"Items": []}
        if resp.status_code == 400 and ("itemCode" in resp.text or "wrong_parameter" in resp.text):
            print(f"[Rakuten] ⚠️ 400 wrong_parameter（視為查無）：{resp.text[:150]}")
            return {"Items": []}
        if resp.status_code in (403, 429):
            print(f"[Rakuten] ⏳ {resp.status_code}（attempt {attempt + 1}/3）等 1.5s 重試… {resp.text[:120]}")
            await asyncio.sleep(1.5)
            continue
        print(f"[Rakuten] ❌ API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    if last_status == 403:
        print("[Rakuten] ❌ 403 重試後仍失敗。檢查 Allowed websites 是否含 'goyoutati.com'。"
              f" 回應：{last_text}")
    else:
        print(f"[Rakuten] ❌ 重試後仍失敗（HTTP {last_status}）：{last_text}")
    return None


# ─────────────────────────────────────────────────────────────────────
# 解析 helpers（自 amiami.py 沿用）
# ─────────────────────────────────────────────────────────────────────
def _items(data: dict) -> list:
    items = data.get("Items")
    if items is None:
        items = data.get("items")
    out = []
    for entry in (items or []):
        if isinstance(entry, dict):
            inner = entry.get("Item") or entry.get("item")
            out.append(inner if isinstance(inner, dict) else entry)
    return out


def _to_int(value):
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("，", "").replace("¥", "").replace("円", "")
    if not s:
        return None
    try:
        v = int(float(s))
    except (ValueError, TypeError):
        return None
    return v if _MIN_PRICE <= v <= _MAX_PRICE else None


def _clean_title(name: str) -> str:
    t = (name or "").strip()
    t = re.sub(r'(\s*《[^》]*》\s*)+$', '', t).strip()
    t = re.sub(r'\s*\[[^\]]+\]\s*$', '', t).strip()
    return t or (name or "").strip()


def _brand_from_title(name: str) -> str:
    brackets = re.findall(r'\[([^\]]+)\]', name or "")
    if brackets:
        b = brackets[-1].strip()
        if b and not re.fullmatch(r'\d+', b):
            return b
    return ""


def _extract_images(item: dict) -> list:
    raw = item.get("mediumImageUrls") or item.get("smallImageUrls") or []
    out, seen = [], set()
    for entry in raw:
        if isinstance(entry, dict):
            u = entry.get("imageUrl") or entry.get("url") or ""
        else:
            u = str(entry or "")
        u = u.strip()
        if not u:
            continue
        u = re.split(r'\?_ex=\d+x\d+', u)[0]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _item_to_product(item: dict) -> ProductInfo:
    p = ProductInfo()
    raw_name = str(item.get("itemName") or "").strip()
    if raw_name:
        p.title = _clean_title(raw_name)
        p.brand = _brand_from_title(raw_name)

    price = _to_int(item.get("itemPrice"))
    if price:
        p.price_jpy = price

    caption = str(item.get("itemCaption") or "").strip()
    if caption:
        p.description = caption[:1500]

    avail = item.get("availability")
    p.in_stock = (avail == 1 or avail == "1")

    imgs = _extract_images(item)
    if imgs:
        p.image_url = imgs[0]
        p.extra_images = imgs[1:10]

    url = str(item.get("itemUrl") or "").strip()
    if url:
        p.source_url = url
    return p


def _slug_of(item: dict, shop_code: str) -> str:
    m = re.search(rf'/{re.escape(shop_code)}/([\w\-]+)', str(item.get("itemUrl") or ""))
    return m.group(1).lower() if m else ""


# ─────────────────────────────────────────────────────────────────────
# 公開 API
# ─────────────────────────────────────────────────────────────────────
async def find_by_code(code: str, shop_code: str) -> ProductInfo | None:
    """店內關鍵字搜 scode，再以 itemUrl slug 比對；命中回 ProductInfo，否則 None。"""
    if not code:
        return None
    data = await _call({
        "shopCode": shop_code,
        "keyword": code,
        "availability": 0,
        "hits": 10,
    })
    if data is None:
        return None

    items = _items(data)
    if not items:
        return None

    target = code.lower()
    chosen = None
    for it in items:
        if _slug_of(it, shop_code) == target:
            chosen = it
            break
    if not chosen:
        for it in items:
            if _slug_of(it, shop_code).startswith(target):
                chosen = it
                break
    if not chosen:
        return None

    try:
        return _item_to_product(chosen)
    except Exception as e:
        print(f"[Rakuten] ❌ 解析錯誤: {type(e).__name__}: {e}")
        return None


async def _do_search(params: dict) -> list:
    data = await _call(params)
    if data is None:
        return []
    out = []
    for it in _items(data):
        try:
            p = _item_to_product(it)
            if p.is_valid:
                out.append(p)
        except Exception:
            continue
    return out


async def search_items(keyword: str, shop_code: str = None, hits: int = 30,
                       available_only: bool = False, or_fallback: bool = True, **extra) -> list:
    """
    關鍵字搜尋 → list[ProductInfo]（只回 is_valid 的）。
    多字預設 AND；AND 查無且為多字時，自動改 orFlag=1（OR）再試一次。
    可額外傳 page、genreId、sort、NGKeyword 等樂天參數（**extra 直通）。
    """
    if not keyword:
        return []
    base = {
        "keyword": keyword,
        "availability": 1 if available_only else 0,
        "hits": max(1, min(int(hits), 30)),   # 樂天單頁上限 30
    }
    if shop_code:
        base["shopCode"] = shop_code
    base.update(extra)

    out = await _do_search(base)

    # 多字 AND 查無 → 改 OR 再試一次（寧可多給點結果）
    if not out and or_fallback and len(str(keyword).split()) > 1:
        print(f"[Rakuten] AND 查無，改 OR 再試：{keyword!r}")
        out = await _do_search({**base, "orFlag": 1})

    return out
