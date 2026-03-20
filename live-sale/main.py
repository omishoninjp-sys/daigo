"""
GOYOUTATI — 即時訂單通知 API
部署在 Zeabur，供 Shopify 前端呼叫

環境變數：
  SHOPIFY_STORE_DOMAIN     e.g. goyoutati.myshopify.com
  SHOPIFY_ACCESS_TOKEN     shpca_xxxx
  SHOPIFY_COLLECTION_ID    產品通知鎖定系列（預設 449326186730）
  ALLOWED_ORIGIN           e.g. https://www.goyoutati.com
"""

import os
import random
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

SHOPIFY_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
ADMIN_TOKEN    = os.environ["SHOPIFY_ACCESS_TOKEN"]
COLLECTION_ID  = os.getenv("SHOPIFY_COLLECTION_ID", "449326186730")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_data_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 120  # 秒

# ── 城市對照（shipping_address 英文 → 中文）──
REGION_MAP = {
    "Taipei": "台北市", "New Taipei": "新北市", "Taoyuan": "桃園市",
    "Taichung": "台中市", "Tainan": "台南市", "Kaohsiung": "高雄市",
    "Hsinchu": "新竹市", "Keelung": "基隆市", "Chiayi": "嘉義市",
    "Hualien": "花蓮縣", "Yilan": "宜蘭縣", "Pingtung": "屏東縣",
    "Miaoli": "苗栗縣", "Changhua": "彰化縣", "Nantou": "南投縣",
    "Yunlin": "雲林縣", "Taitung": "台東縣", "Penghu": "澎湖縣",
    "Kinmen": "金門縣", "Lienchiang": "連江縣",
    "Hong Kong": "香港", "Kowloon": "九龍", "New Territories": "新界",
    "Singapore": "新加坡", "Kuala Lumpur": "吉隆坡",
    "Bangkok": "曼谷", "Macau": "澳門", "Tokyo": "東京", "Osaka": "大阪",
}

COUNTRY_FLAG = {
    "TW": "🇹🇼", "HK": "🇭🇰", "MO": "🇲🇴",
    "SG": "🇸🇬", "MY": "🇲🇾", "TH": "🇹🇭",
    "JP": "🇯🇵", "US": "🇺🇸",
}

# 產品通知城市池（台灣 + 香港）
PRODUCT_CITIES = [
    ("台北市", "🇹🇼"), ("新北市", "🇹🇼"), ("台中市", "🇹🇼"),
    ("高雄市", "🇹🇼"), ("桃園市", "🇹🇼"), ("台南市", "🇹🇼"),
    ("新竹市", "🇹🇼"), ("基隆市", "🇹🇼"), ("嘉義市", "🇹🇼"),
    ("花蓮縣", "🇹🇼"), ("宜蘭縣", "🇹🇼"), ("屏東縣", "🇹🇼"),
    ("香港",   "🇭🇰"),
]

HEADERS = {"X-Shopify-Access-Token": ADMIN_TOKEN}


def trim_title(title: str) -> str:
    """截短商品名稱，最多 20 字"""
    if "｜" in title:
        parts = title.split("｜")
        core = parts[1] if len(parts) > 1 else parts[0]
    else:
        core = title
    if " - " in core:
        core = core.split(" - ")[0]
    core = core.strip()
    if len(core) > 20:
        core = core[:20] + "…"
    return core


def mask_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    return name[0] + "XX"


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


async def fetch_real_orders() -> list[dict]:
    url = (
        f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/orders.json"
        "?status=any&limit=20"
        "&fields=id,created_at,shipping_address,billing_address"
        ",line_items,total_price,currency,customer"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
    print(f"[Orders] status={r.status_code}")
    if r.status_code != 200:
        print(f"[Orders] 錯誤: {r.text[:200]}")
        return []

    result = []
    for order in r.json().get("orders", []):
        addr         = order.get("shipping_address") or order.get("billing_address") or {}
        city_raw     = addr.get("city", "")
        country_code = addr.get("country_code", "TW")
        city_zh      = REGION_MAP.get(city_raw, city_raw).strip() or "台灣"
        flag         = COUNTRY_FLAG.get(country_code, "🌏")

        customer  = order.get("customer") or {}
        addr_name = addr.get("name", "")
        full_name = addr_name or (
            (customer.get("first_name", "") + customer.get("last_name", "")).strip()
        )
        masked = mask_name(full_name)

        items = order.get("line_items", [])
        if not items:
            continue
        product_title = trim_title(items[0].get("title", ""))
        if not product_title:
            continue
        if "系統費" in product_title:
            continue

        item_count = sum(i.get("quantity", 1) for i in items)

        try:
            amount = f"¥{int(float(order.get('total_price', 0))):,}"
        except Exception:
            amount = ""

        result.append({
            "flag":       flag,
            "region":     city_zh,
            "customer":   masked,
            "product":    product_title,
            "product_id": str(items[0].get("product_id", "")),
            "image":      "",
            "amount":     amount,
            "count":      item_count,
            "time":       time_ago_zh(order.get("created_at", "")),
            "_type":      "order",
        })
    print(f"[Orders] 取得 {len(result)} 筆訂單")
    return result


async def fetch_product_images(product_ids: list[str]) -> dict[str, str]:
    ids_param = ",".join(filter(None, product_ids[:20]))
    if not ids_param:
        return {}
    url = (
        f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/products.json"
        f"?ids={ids_param}&fields=id,images&limit=20"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
    if r.status_code != 200:
        return {}
    result = {}
    for p in r.json().get("products", []):
        imgs = p.get("images", [])
        if imgs:
            result[str(p["id"])] = imgs[0].get("src", "")
    return result


async def fetch_recent_products() -> list[dict]:
    """抓指定系列最近 30 天新上架的商品，城市隨機抽台灣 + 香港"""
    url = (
        f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/products.json"
        f"?collection_id={COLLECTION_ID}&limit=20"
        "&fields=id,title,created_at,status,images"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)

    print(f"[Products] status={r.status_code} collection={COLLECTION_ID}")
    if r.status_code != 200:
        print(f"[Products] 錯誤: {r.text[:200]}")
        return []

    products = r.json().get("products", [])
    print(f"[Products] 取得 {len(products)} 個商品（過濾前）")

    now    = datetime.now(timezone.utc)
    result = []
    for p in products:
        if p.get("status") != "active":
            continue
        try:
            dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            age_days = (now - dt).total_seconds() / 86400
            if age_days > 30:
                continue
        except Exception:
            continue

        city, flag = random.choice(PRODUCT_CITIES)
        imgs  = p.get("images", [])
        image = imgs[0].get("src", "") if imgs else ""

        result.append({
            "flag":     flag,
            "region":   city,
            "customer": "",
            "product":  trim_title(p.get("title", "")),
            "image":    image,
            "amount":   "",
            "count":    1,
            "time":     time_ago_zh(p["created_at"]),
            "_type":    "product",
        })

    print(f"[Products] 過濾後剩 {len(result)} 個商品")
    return result


@app.get("/api/recent-orders")
async def recent_orders():
    now_ts = time.time()

    if _data_cache["data"] and (now_ts - _data_cache["ts"]) < CACHE_TTL:
        pool = _data_cache["data"].copy()
        random.shuffle(pool)
        return {"orders": pool, "cached": True}

    orders   = await fetch_real_orders()
    products = await fetch_recent_products()

    product_ids = [o["product_id"] for o in orders if o.get("product_id")]
    if product_ids:
        img_map = await fetch_product_images(product_ids)
        for o in orders:
            if not o["image"]:
                o["image"] = img_map.get(o["product_id"], "")

    combined  = []
    prod_iter = iter(products)
    for i, o in enumerate(orders):
        combined.append(o)
        if i % 2 == 1:
            try:
                combined.append(next(prod_iter))
            except StopIteration:
                pass
    for p in prod_iter:
        combined.append(p)

    def clean(item: dict) -> dict:
        return {k: v for k, v in item.items() if not k.startswith("_") and k != "product_id"}

    final = [clean(x) for x in combined[:15]]
    _data_cache["data"] = final
    _data_cache["ts"]   = now_ts

    print(f"[API] 最終回傳 {len(final)} 筆資料")
    random.shuffle(final)
    return {"orders": final, "cached": False}


@app.get("/health")
async def health():
    return {"ok": True}
