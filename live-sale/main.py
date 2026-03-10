"""
GOYOUTATI — 即時訂單通知 API
部署在 Zeabur，供 Shopify 前端呼叫

環境變數需要設定：
  SHOPIFY_STORE_DOMAIN   e.g. your-store.myshopify.com
  SHOPIFY_ADMIN_TOKEN    Admin API access token (read_orders scope)
  ALLOWED_ORIGIN         e.g. https://www.goyoutati.com
"""

import os
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

SHOPIFY_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
SHOPIFY_TOKEN  = os.environ["SHOPIFY_ADMIN_TOKEN"]
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN, "https://cdn.shopify.com"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── 簡易記憶體快取（避免打爆 Shopify rate limit）──
_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 120  # 2 分鐘


def time_ago_zh(created_at_str: str) -> str:
    """把 ISO 時間字串轉成中文時間差，例如 '3 分鐘前'"""
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return "剛剛"

    if diff < 90:
        return "剛剛"
    elif diff < 3600:
        return f"{diff // 60} 分鐘前"
    elif diff < 86400:
        return f"{diff // 3600} 小時前"
    else:
        return f"{diff // 86400} 天前"


REGION_MAP = {
    # 台灣常見城市
    "Taipei":      "台北市",
    "New Taipei":  "新北市",
    "Taoyuan":     "桃園市",
    "Taichung":    "台中市",
    "Tainan":      "台南市",
    "Kaohsiung":   "高雄市",
    "Hsinchu":     "新竹市",
    "Keelung":     "基隆市",
    "Chiayi":      "嘉義市",
    "Hualien":     "花蓮縣",
    "Yilan":       "宜蘭縣",
    "Pingtung":    "屏東縣",
    # 海外
    "Singapore":   "新加坡",
    "Kuala Lumpur": "吉隆坡",
    "Bangkok":     "曼谷",
    "Hong Kong":   "香港",
    "Macau":       "澳門",
    "Tokyo":       "東京",
    "Osaka":       "大阪",
}

COUNTRY_FLAG = {
    "TW": "🇹🇼",
    "SG": "🇸🇬",
    "MY": "🇲🇾",
    "TH": "🇹🇭",
    "HK": "🇭🇰",
    "MO": "🇲🇴",
    "JP": "🇯🇵",
    "US": "🇺🇸",
}


async def fetch_orders_from_shopify() -> list[dict]:
    url = (
        f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/orders.json"
        "?status=any&limit=20&fields=id,created_at,billing_address,line_items"
    )
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Shopify API error")
    
    orders = r.json().get("orders", [])
    result = []

    for order in orders:
        addr = order.get("billing_address") or {}
        city_raw    = addr.get("city", "")
        country_code = addr.get("country_code", "")

        city_zh = REGION_MAP.get(city_raw, city_raw) or "台灣"
        flag    = COUNTRY_FLAG.get(country_code, "🌏")

        line_items = order.get("line_items", [])
        if not line_items:
            continue
        product_title = line_items[0].get("title", "")
        if not product_title:
            continue

        result.append({
            "flag":    flag,
            "region":  city_zh,
            "product": product_title,
            "time":    time_ago_zh(order.get("created_at", "")),
        })

    return result[:10]  # 最多回傳 10 筆


@app.get("/api/recent-orders")
async def recent_orders():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return {"orders": _cache["data"], "cached": True}

    data = await fetch_orders_from_shopify()
    _cache["data"] = data
    _cache["ts"]   = now
    return {"orders": data, "cached": False}


@app.get("/health")
async def health():
    return {"ok": True}
