"""
GOYOUTATI — 即時訂單通知 API
部署在 Zeabur，供 Shopify 前端呼叫

環境變數：
  SHOPIFY_STORE_DOMAIN     e.g. goyoutati.myshopify.com
  SHOPIFY_ADMIN_TOKEN      shpca_xxxx（現有的舊版 token 直接用）
  SHOPIFY_COLLECTION_ID    （選填）鎖定某個系列 ID
  ALLOWED_ORIGIN           e.g. https://www.goyoutati.com
"""

import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

SHOPIFY_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
ADMIN_TOKEN    = os.environ["SHOPIFY_ADMIN_TOKEN"]
COLLECTION_ID  = os.getenv("SHOPIFY_COLLECTION_ID", "")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_data_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 120

REGION_MAP = {
    "Taipei": "台北市", "New Taipei": "新北市", "Taoyuan": "桃園市",
    "Taichung": "台中市", "Tainan": "台南市", "Kaohsiung": "高雄市",
    "Hsinchu": "新竹市", "Keelung": "基隆市", "Chiayi": "嘉義市",
    "Hualien": "花蓮縣", "Yilan": "宜蘭縣", "Pingtung": "屏東縣",
    "Singapore": "新加坡", "Kuala Lumpur": "吉隆坡",
    "Bangkok": "曼谷", "Hong Kong": "香港", "Macau": "澳門",
    "Tokyo": "東京", "Osaka": "大阪",
}

COUNTRY_FLAG = {
    "TW": "🇹🇼", "SG": "🇸🇬", "MY": "🇲🇾",
    "TH": "🇹🇭", "HK": "🇭🇰", "MO": "🇲🇴",
    "JP": "🇯🇵", "US": "🇺🇸",
}

def trim_title(title: str) -> str:
    """
    把 Shopify 商品名稱縮短成適合 ticker 顯示的長度。
    格式通常是「日本代購｜品牌 商品名 - 細節｜來源站」
    → 只取中間那段，最多 20 字
    """
    # 去掉「日本代購｜」前綴
    if "｜" in title:
        parts = title.split("｜")
        # 取第二段（品牌+商品名）
        core = parts[1] if len(parts) > 1 else parts[0]
    else:
        core = title
    # 去掉「 - 細節描述」後的部分
    if " - " in core:
        core = core.split(" - ")[0]
    # 超過 20 字截斷
    if len(core) > 20:
        core = core[:20] + "…"
    return core.strip()

def time_ago_zh(s: str) -> str:
    try:
        dt   = datetime.fromisoformat(s.replace("Z", "+00:00"))
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return "剛剛"
    if diff < 90:      return "剛剛"
    elif diff < 3600:  return f"{diff // 60} 分鐘前"
    elif diff < 86400: return f"{diff // 3600} 小時前"
    else:              return f"{diff // 86400} 天前"

HEADERS = {"X-Shopify-Access-Token": ADMIN_TOKEN}

async def fetch_real_orders() -> list[dict]:
    url = (
        f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/orders.json"
        "?status=any&limit=15&fields=id,created_at,billing_address,line_items"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []
    result = []
    for order in r.json().get("orders", []):
        addr     = order.get("billing_address") or {}
        city_zh  = REGION_MAP.get(addr.get("city", ""), addr.get("city", "")) or "台灣"
        flag     = COUNTRY_FLAG.get(addr.get("country_code", ""), "🌏")
        items    = order.get("line_items", [])
        if not items or not items[0].get("title"):
            continue
        result.append({
            "flag":    flag,
            "region":  city_zh,
            "product": trim_title(items[0]["title"]),
            "time":    time_ago_zh(order.get("created_at", "")),
        })
    return result

async def fetch_recent_products() -> list[dict]:
    if COLLECTION_ID:
        url = (
            f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/products.json"
            f"?collection_id={COLLECTION_ID}&limit=20"
            "&fields=id,title,created_at,status"
        )
    else:
        url = (
            f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/products.json"
            "?limit=20&fields=id,title,created_at,status"
        )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []

    now    = datetime.now(timezone.utc)
    cities = [
        ("台北市", "🇹🇼"), ("新北市", "🇹🇼"), ("台中市", "🇹🇼"),
        ("高雄市", "🇹🇼"), ("桃園市", "🇹🇼"), ("台南市", "🇹🇼"),
        ("新加坡", "🇸🇬"), ("吉隆坡", "🇲🇾"),
    ]
    result = []
    for i, p in enumerate(r.json().get("products", [])):
        if p.get("status") != "active":
            continue
        try:
            dt   = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            if (now - dt).total_seconds() > 86400:
                continue
        except Exception:
            continue
        city, flag = cities[i % len(cities)]
        result.append({
            "flag":    flag,
            "region":  city,
            "product": trim_title(p.get("title", "")),
            "time":    time_ago_zh(p["created_at"]),
        })
    return result

@app.get("/api/recent-orders")
async def recent_orders():
    now = time.time()
    if _data_cache["data"] and (now - _data_cache["ts"]) < CACHE_TTL:
        return {"orders": _data_cache["data"], "cached": True}

    orders   = await fetch_real_orders()
    products = await fetch_recent_products()

    combined  = []
    prod_iter = iter(products)
    for i, o in enumerate(orders):
        combined.append(o)
        if i % 2 == 1:
            try:
                combined.append(next(prod_iter))
            except StopIteration:
                pass
    combined.extend(prod_iter)

    final = combined[:12]
    _data_cache["data"] = final
    _data_cache["ts"]   = now
    return {"orders": final, "cached": False}

@app.get("/health")
async def health():
    return {"ok": True}
