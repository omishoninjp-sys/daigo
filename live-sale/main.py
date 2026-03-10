"""
GOYOUTATI — 即時訂單通知 API v3
環境變數：
  SHOPIFY_STORE_DOMAIN     e.g. goyoutati.myshopify.com
  SHOPIFY_ACCESS_TOKEN     shpca_xxxx
  SHOPIFY_COLLECTION_ID    449326186730
  ALLOWED_ORIGIN           *

v3 改動：
  - cache 儲存完整大池子，每次 request 動態隨機取樣 8 筆
  - product 通知城市改為 per-request 分配，刷新後城市不同
  - 同一個 request 內不重複城市，更真實
"""

import os
import time
import random
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

SHOPIFY_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
ADMIN_TOKEN    = os.environ["SHOPIFY_ACCESS_TOKEN"]
COLLECTION_ID  = os.getenv("SHOPIFY_COLLECTION_ID", "")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# 大池子 cache：存所有原始資料，每次 request 動態取樣
_data_cache: dict = {"orders": None, "products": None, "ts": 0}
CACHE_TTL   = 120   # 2 分鐘重新抓 Shopify
SERVE_COUNT = 8     # 每次回傳幾筆（讓客人每次刷到不同組合）

HEADERS = {"X-Shopify-Access-Token": ADMIN_TOKEN}

# ── 城市對照（真實 shipping_address.city 英文 → 中文）──
REGION_MAP = {
    # 台灣直轄市
    "Taipei": "台北市", "Taipei City": "台北市",
    "New Taipei": "新北市", "New Taipei City": "新北市",
    "Taoyuan": "桃園市", "Taoyuan City": "桃園市",
    "Taichung": "台中市", "Taichung City": "台中市",
    "Tainan": "台南市", "Tainan City": "台南市",
    "Kaohsiung": "高雄市", "Kaohsiung City": "高雄市",
    # 台灣縣市
    "Hsinchu": "新竹市", "Hsinchu City": "新竹市",
    "Hsinchu County": "新竹縣",
    "Keelung": "基隆市",
    "Chiayi": "嘉義市", "Chiayi City": "嘉義市", "Chiayi County": "嘉義縣",
    "Miaoli": "苗栗縣", "Miaoli County": "苗栗縣",
    "Changhua": "彰化縣", "Changhua County": "彰化縣",
    "Nantou": "南投縣", "Nantou County": "南投縣",
    "Yunlin": "雲林縣", "Yunlin County": "雲林縣",
    "Pingtung": "屏東縣", "Pingtung County": "屏東縣",
    "Taitung": "台東縣", "Taitung County": "台東縣",
    "Hualien": "花蓮縣", "Hualien County": "花蓮縣",
    "Yilan": "宜蘭縣", "Yilan County": "宜蘭縣",
    "Penghu": "澎湖縣",
    "Kinmen": "金門縣",
    "Lienchiang": "連江縣",
    # 香港
    "Hong Kong": "香港",
    "Kowloon": "九龍",
    "New Territories": "新界",
    "Central": "香港中環",
    "Wan Chai": "灣仔",
    "Mong Kok": "旺角",
    "Tsim Sha Tsui": "尖沙咀",
    "Sha Tin": "沙田",
    # 其他地區
    "Singapore": "新加坡",
    "Kuala Lumpur": "吉隆坡",
    "Bangkok": "曼谷",
    "Tokyo": "東京", "Osaka": "大阪",
    "Macau": "澳門",
}

COUNTRY_FLAG = {
    "TW": "🇹🇼", "HK": "🇭🇰", "MO": "🇲🇴",
    "SG": "🇸🇬", "MY": "🇲🇾", "TH": "🇹🇭",
    "JP": "🇯🇵", "US": "🇺🇸", "GB": "🇬🇧",
}

# product 通知用的城市池（台灣 + 香港），per-request 隨機抽
TW_HK_CITIES = [
    ("台北市", "🇹🇼"), ("新北市", "🇹🇼"), ("台中市", "🇹🇼"),
    ("高雄市", "🇹🇼"), ("桃園市", "🇹🇼"), ("台南市", "🇹🇼"),
    ("新竹市", "🇹🇼"), ("基隆市", "🇹🇼"), ("嘉義市", "🇹🇼"),
    ("彰化縣", "🇹🇼"), ("花蓮縣", "🇹🇼"), ("宜蘭縣", "🇹🇼"),
    ("屏東縣", "🇹🇼"), ("苗栗縣", "🇹🇼"), ("雲林縣", "🇹🇼"),
    ("香港",   "🇭🇰"), ("九龍",   "🇭🇰"), ("新界",   "🇭🇰"),
]


def resolve_city(addr: dict) -> tuple[str, str]:
    """從 address dict 取得（中文城市, 國旗）"""
    country_code = addr.get("country_code", "TW")
    flag = COUNTRY_FLAG.get(country_code, "🌏")
    city_raw = addr.get("city", "") or ""
    city_zh = REGION_MAP.get(city_raw.strip(), city_raw.strip()) or "台灣"
    return city_zh, flag


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


def trim_title(title: str) -> str:
    if "｜" in title:
        parts = title.split("｜")
        core = parts[1] if len(parts) > 1 else parts[0]
    else:
        core = title
    if " - " in core:
        core = core.split(" - ")[0]
    if len(core) > 20:
        core = core[:20] + "…"
    return core.strip()


async def fetch_real_orders() -> list[dict]:
    """抓真實訂單，取 shipping_address（真實運送城市）"""
    url = (
        f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/orders.json"
        "?status=any&limit=30"
        "&fields=id,created_at,shipping_address,billing_address,line_items,total_price,currency"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []

    result = []
    for order in r.json().get("orders", []):
        addr = order.get("shipping_address") or order.get("billing_address") or {}
        city_zh, flag = resolve_city(addr)

        items = order.get("line_items", [])
        if not items:
            continue

        # 商品圖片：取第一件有圖的
        image_url = None
        for item in items:
            if item.get("image") and item["image"].get("src"):
                image_url = item["image"]["src"]
                break

        # 訂單金額
        total    = order.get("total_price", "")
        currency = order.get("currency", "TWD")
        try:
            amt_float = float(total)
            if currency == "JPY":
                amount = f"¥{int(amt_float):,}"
            elif currency == "TWD":
                amount = f"NT$ {int(amt_float):,}"
            else:
                amount = f"{currency} {amt_float:.0f}"
        except Exception:
            amount = ""

        # 訂單件數
        qty = sum(i.get("quantity", 1) for i in items)

        result.append({
            "_is_product": False,
            "flag":    flag,
            "region":  city_zh,
            "product": trim_title(items[0].get("title", "")),
            "time":    time_ago_zh(order.get("created_at", "")),
            "image":   image_url,
            "amount":  amount,
            "qty":     qty,
        })
    return result


async def fetch_recent_products() -> list[dict]:
    """
    抓指定系列的最近商品（24小時內）。
    城市欄位故意留空，由 serve 時 per-request 動態分配，
    讓同一個訪客刷新後看到不同城市。
    """
    if COLLECTION_ID:
        url = (
            f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/products.json"
            f"?collection_id={COLLECTION_ID}&limit=20"
            "&fields=id,title,created_at,status,images,variants"
        )
    else:
        url = (
            f"https://{SHOPIFY_DOMAIN}/admin/api/2024-01/products.json"
            "?limit=20&fields=id,title,created_at,status,images,variants"
        )

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []

    now = datetime.now(timezone.utc)
    result = []

    for p in r.json().get("products", []):
        if p.get("status") != "active":
            continue
        try:
            dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            if (now - dt).total_seconds() > 86400:
                continue
        except Exception:
            continue

        images    = p.get("images", [])
        image_url = images[0]["src"] if images else None

        variants = p.get("variants", [])
        try:
            price  = min(float(v.get("price", 0)) for v in variants if v.get("price"))
            amount = f"NT$ {int(price):,}" if price else ""
        except Exception:
            amount = ""

        result.append({
            "_is_product": True,   # serve 時看這個旗標分配城市
            "product": trim_title(p.get("title", "")),
            "time":    time_ago_zh(p["created_at"]),
            "image":   image_url,
            "amount":  amount,
            "qty":     1,
        })
    return result


def assign_cities_no_repeat(items: list[dict]) -> list[dict]:
    """
    對 _is_product=True 的項目，從城市池隨機抽取不重複城市。
    確保同一批結果裡不會出現兩個「台北市」等重複城市。
    """
    # 先複製城市池並洗牌，保證這批不重複
    city_pool = TW_HK_CITIES.copy()
    random.shuffle(city_pool)
    city_iter = iter(city_pool)

    result = []
    for item in items:
        item = item.copy()
        if item.get("_is_product"):
            try:
                city, flag = next(city_iter)
            except StopIteration:
                # 城市池用完就重新洗一次
                city_pool = TW_HK_CITIES.copy()
                random.shuffle(city_pool)
                city_iter = iter(city_pool)
                city, flag = next(city_iter)
            item["flag"]   = flag
            item["region"] = city
        item.pop("_is_product", None)
        result.append(item)
    return result


@app.get("/api/recent-orders")
async def recent_orders():
    now = time.time()

    # 若 cache 過期，重新從 Shopify 抓
    if not _data_cache["orders"] or (now - _data_cache["ts"]) >= CACHE_TTL:
        orders   = await fetch_real_orders()
        products = await fetch_recent_products()
        _data_cache["orders"]   = orders
        _data_cache["products"] = products
        _data_cache["ts"]       = now

    orders   = _data_cache["orders"]   or []
    products = _data_cache["products"] or []

    # ── 每次 request 動態組合，讓同一個訪客刷新看到不同人 ──
    # 1. 隨機從訂單池取最多 20 筆
    order_pool = random.sample(orders, min(len(orders), 20))
    # 2. 隨機從商品池取最多 8 筆
    prod_pool  = random.sample(products, min(len(products), 8))

    # 3. 混合：每 2 筆訂單穿插 1 筆商品通知
    combined  = []
    prod_iter = iter(prod_pool)
    for i, o in enumerate(order_pool):
        combined.append(o)
        if i % 2 == 1:
            try:
                combined.append(next(prod_iter))
            except StopIteration:
                pass
    combined.extend(prod_iter)

    # 4. 再洗牌一次，限制 SERVE_COUNT 筆
    random.shuffle(combined)
    selected = combined[:SERVE_COUNT]

    # 5. 對 product 項目 per-request 分配不重複城市
    final = assign_cities_no_repeat(selected)

    return {"orders": final, "cached": (now - _data_cache["ts"]) < CACHE_TTL}


@app.get("/health")
async def health():
    return {"ok": True}
