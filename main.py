"""
GOYOUTATI DAIGO 代購系統 API v3.3
- 快取 scrape 結果（30 分鐘）
- 常駐 Chrome 實例
- SEO 最佳化標題（ChatGPT 翻譯）
- 併發限制 + 排隊機制 + 超時保護
"""
import time
import asyncio
import traceback
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import (
    API_SECRET_KEY, ALLOWED_ORIGINS, ZOZO_SCRAPER_URL, DAIGO_COLLECTION_ID,
    CACHE_TTL, MAX_CONCURRENT_SCRAPES, SCRAPE_QUEUE_TIMEOUT,
)
from scraper import Scraper, ProductInfo
from pricing import calculate_selling_price, get_jpy_to_twd_rate
from shopify_client import ShopifyClient
from seo_title import generate_seo_title

print(f"[Config] DAIGO_COLLECTION_ID = '{DAIGO_COLLECTION_ID}'")
print(f"[Config] CACHE_TTL = {CACHE_TTL}s, MAX_CONCURRENT = {MAX_CONCURRENT_SCRAPES}, QUEUE_TIMEOUT = {SCRAPE_QUEUE_TIMEOUT}s")

app = FastAPI(title="GOYOUTATI DAIGO API", version="3.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

scraper = Scraper()
shopify = ShopifyClient()

# === 併發控制 ===
_scrape_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)
_queue_count = 0       # 目前排隊中的請求數
_active_count = 0      # 目前正在爬取的請求數
_queue_lock = asyncio.Lock()


async def _increment_queue():
    global _queue_count
    async with _queue_lock:
        _queue_count += 1
        pos = _queue_count + _active_count
    return pos


async def _queue_to_active():
    global _queue_count, _active_count
    async with _queue_lock:
        _queue_count -= 1
        _active_count += 1


async def _decrement_active():
    global _active_count
    async with _queue_lock:
        _active_count -= 1


# === 快取 ===
_scrape_cache: dict[str, tuple[ProductInfo, float]] = {}


def cache_get(url: str) -> ProductInfo | None:
    if url in _scrape_cache:
        product, ts = _scrape_cache[url]
        if time.time() - ts < CACHE_TTL:
            print(f"[Cache] ✅ 命中快取: {url[:60]}")
            return product
        else:
            del _scrape_cache[url]
    return None


def cache_set(url: str, product: ProductInfo):
    _scrape_cache[url] = (product, time.time())
    # 清理過期
    now = time.time()
    expired = [k for k, (_, ts) in _scrape_cache.items() if now - ts > CACHE_TTL]
    for k in expired:
        del _scrape_cache[k]


async def verify_api_key(x_api_key: str = Header(default="")):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# === 帶併發控制的爬取 ===

async def scrape_with_queue(url: str) -> ProductInfo:
    """
    帶排隊 + 超時的爬取
    - 先檢查快取
    - 用 Semaphore 限制同時爬取數量
    - 排隊超過 SCRAPE_QUEUE_TIMEOUT 秒就放棄
    """
    # 1. 先查快取（不需要排隊）
    cached = cache_get(url)
    if cached:
        return cached

    # 2. 進入排隊
    position = await _increment_queue()
    print(f"[Queue] 📋 新請求加入排隊 (位置 #{position}): {url[:60]}")

    try:
        # 3. 等待 Semaphore（帶超時）
        try:
            await asyncio.wait_for(
                _scrape_semaphore.acquire(),
                timeout=SCRAPE_QUEUE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[Queue] ⏰ 排隊超時 ({SCRAPE_QUEUE_TIMEOUT}s): {url[:60]}")
            raise HTTPException(
                status_code=503,
                detail=f"目前查詢人數較多，請稍後再試（等候超過 {SCRAPE_QUEUE_TIMEOUT} 秒）"
            )

        await _queue_to_active()
        print(f"[Queue] ▶️ 開始爬取 (active={_active_count}, queue={_queue_count}): {url[:60]}")

        try:
            # 4. 再檢查一次快取（可能排隊期間別人已經爬過同一個 URL）
            cached = cache_get(url)
            if cached:
                print(f"[Queue] ✅ 排隊期間快取命中: {url[:60]}")
                return cached

            # 5. 實際爬取（帶超時保護）
            product = await asyncio.wait_for(
                scraper.scrape(url),
                timeout=60,  # 單次爬取最多 60 秒
            )

            if product.title:
                cache_set(url, product)

            return product

        except asyncio.TimeoutError:
            print(f"[Queue] ⏰ 爬取超時 (60s): {url[:60]}")
            return ProductInfo(source_url=url)  # 回傳空結果，前端會導向手動輸入

        finally:
            _scrape_semaphore.release()
            await _decrement_active()
            print(f"[Queue] ✅ 爬取完成 (active={_active_count}, queue={_queue_count})")

    except HTTPException:
        # 排隊超時的 HTTPException，直接 re-raise
        async with _queue_lock:
            global _queue_count
            _queue_count -= 1
        raise
    except Exception:
        # 其他意外錯誤
        async with _queue_lock:
            _queue_count -= 1
        raise


# === Models ===

class ScrapeRequest(BaseModel):
    url: str

class ScrapeResponse(BaseModel):
    success: bool
    product: dict | None = None
    pricing: dict | None = None
    error: str | None = None
    queue_info: dict | None = None  # 排隊資訊

class CreateOrderRequest(BaseModel):
    url: str
    title_override: str | None = None

class ManualOrderRequest(BaseModel):
    title: str
    price_jpy: int
    original_price_jpy: int = 0
    image_url: str = ""
    source_url: str = ""

class CreateOrderResponse(BaseModel):
    success: bool
    product_id: int | None = None
    checkout_url: str | None = None
    admin_url: str | None = None
    error: str | None = None


# === Endpoints ===

@app.get("/api/health")
async def health():
    driver_status = scraper.get_driver_status()
    return {
        "status": "ok",
        "service": "daigo-api",
        "version": "3.3.0",
        "cache_size": len(_scrape_cache),
        "cache_ttl": CACHE_TTL,
        "driver": driver_status,
        "queue": {
            "active": _active_count,
            "waiting": _queue_count,
            "max_concurrent": MAX_CONCURRENT_SCRAPES,
        },
    }


@app.get("/api/status")
async def queue_status():
    """前端用：查詢目前排隊狀態"""
    return {
        "active": _active_count,
        "waiting": _queue_count,
        "max_concurrent": MAX_CONCURRENT_SCRAPES,
        "estimated_wait_seconds": _queue_count * 15,  # 粗估每個請求 15 秒
    }


@app.get("/api/rate")
async def get_rate():
    from config import PRICING_TIERS
    return {
        "jpy_to_twd": get_jpy_to_twd_rate(),
        "pricing_tiers": [{"min_jpy": t[0], "max_jpy": t[1], "markup": t[2]} for t in PRICING_TIERS],
    }


@app.post("/api/scrape", response_model=ScrapeResponse, dependencies=[Depends(verify_api_key)])
async def scrape_product(req: ScrapeRequest):
    try:
        url = str(req.url).strip()
        product: ProductInfo = await scrape_with_queue(url)

        if not product.title:
            return ScrapeResponse(
                success=False,
                error="無法從此連結抓取商品資訊",
                queue_info={"active": _active_count, "waiting": _queue_count},
            )

        pricing = calculate_selling_price(product.price_jpy) if product.price_jpy else None
        return ScrapeResponse(
            success=True, product=product.to_dict(), pricing=pricing,
            queue_info={"active": _active_count, "waiting": _queue_count},
        )

    except HTTPException:
        raise  # 排隊超時，直接回傳 503
    except Exception as e:
        print(f"[API] scrape error: {traceback.format_exc()}")
        return ScrapeResponse(success=False, error=f"爬取失敗：{str(e) or type(e).__name__}")


@app.post("/api/create-order", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_order(req: CreateOrderRequest):
    try:
        url = str(req.url).strip()

        # 先查快取（不需要排隊）
        product = cache_get(url)
        if not product:
            print(f"[Cache] ❌ 未命中，重新爬取: {url[:60]}")
            product = await scrape_with_queue(url)

        if not product.title:
            return CreateOrderResponse(success=False, error="無法抓取商品資訊")
        if not product.price_jpy:
            return CreateOrderResponse(success=False, error="無法偵測到商品價格")

        pricing = calculate_selling_price(product.price_jpy)
        title = req.title_override or product.title

        # === SEO 標題生成 ===
        seo = await generate_seo_title(
            original_title=title,
            brand=product.brand,
            source_url=url,
        )
        seo_title = seo.get("title", "")
        seo_tags = seo.get("tags", [])

        result = await shopify.create_daigo_product(
            title=title, price_jpy=pricing["selling_price_jpy"],
            image_url=product.image_url, description=product.description,
            source_url=url, original_price_jpy=product.price_jpy,
            brand=product.brand, extra_images=product.extra_images,
            variants=product.variants, image_base64=product.image_base64,
            extra_tags=["18+", "adult"] if product.is_adult else None,
            seo_title=seo_title, seo_tags=seo_tags,
        )

        return CreateOrderResponse(
            success=True, product_id=result["product_id"],
            checkout_url=result["storefront_url"], admin_url=result["admin_url"],
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] create-order error: {traceback.format_exc()}")
        return CreateOrderResponse(success=False, error=f"建立商品失敗：{str(e)}")


@app.post("/api/create-manual", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_manual_order(req: ManualOrderRequest):
    try:
        if not req.title:
            return CreateOrderResponse(success=False, error="請填寫商品名稱")
        if req.price_jpy <= 0:
            return CreateOrderResponse(success=False, error="價格錯誤")

        # === SEO 標題生成 ===
        seo = await generate_seo_title(
            original_title=req.title,
            source_url=req.source_url,
        )
        seo_title = seo.get("title", "")
        seo_tags = seo.get("tags", [])

        result = await shopify.create_daigo_product(
            title=req.title, price_jpy=req.price_jpy,
            image_url=req.image_url, source_url=req.source_url,
            original_price_jpy=req.original_price_jpy,
            seo_title=seo_title, seo_tags=seo_tags,
        )

        return CreateOrderResponse(
            success=True, product_id=result["product_id"],
            checkout_url=result["storefront_url"], admin_url=result["admin_url"],
        )
    except Exception as e:
        print(f"[API] create-manual error: {traceback.format_exc()}")
        return CreateOrderResponse(success=False, error=f"建立商品失敗：{str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
