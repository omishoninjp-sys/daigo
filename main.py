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

app = FastAPI(title="GOYOUTATI DAIGO API", version="3.3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

scraper = Scraper()
shopify = ShopifyClient()

# === 併發控制（lazy init，避免 Python 3.10+ 無 event loop 的問題）===
_scrape_semaphore: asyncio.Semaphore | None = None
_queue_lock: asyncio.Lock | None = None
_queue_count = 0
_active_count = 0


def _get_semaphore() -> asyncio.Semaphore:
    global _scrape_semaphore
    if _scrape_semaphore is None:
        _scrape_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)
    return _scrape_semaphore


def _get_queue_lock() -> asyncio.Lock:
    global _queue_lock
    if _queue_lock is None:
        _queue_lock = asyncio.Lock()
    return _queue_lock


async def _increment_queue():
    global _queue_count
    async with _get_queue_lock():
        _queue_count += 1
        pos = _queue_count + _active_count
    return pos


async def _queue_to_active():
    global _queue_count, _active_count
    async with _get_queue_lock():
        _queue_count -= 1
        _active_count += 1


async def _decrement_active():
    global _active_count
    async with _get_queue_lock():
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
    now = time.time()
    expired = [k for k, (_, ts) in _scrape_cache.items() if now - ts > CACHE_TTL]
    for k in expired:
        del _scrape_cache[k]


async def verify_api_key(x_api_key: str = Header(default="")):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# === 帶併發控制的爬取 ===

async def scrape_with_queue(url: str) -> ProductInfo:
    cached = cache_get(url)
    if cached:
        return cached

    position = await _increment_queue()
    print(f"[Queue] 📋 新請求加入排隊 (位置 #{position}): {url[:60]}")

    try:
        try:
            await asyncio.wait_for(
                _get_semaphore().acquire(),
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
            cached = cache_get(url)
            if cached:
                print(f"[Queue] ✅ 排隊期間快取命中: {url[:60]}")
                return cached

            product = await asyncio.wait_for(
                scraper.scrape(url),
                timeout=60,
            )

            if product.title:
                cache_set(url, product)

            return product

        except asyncio.TimeoutError:
            print(f"[Queue] ⏰ 爬取超時 (60s): {url[:60]}")
            return ProductInfo(source_url=url)

        finally:
            _get_semaphore().release()
            await _decrement_active()
            print(f"[Queue] ✅ 爬取完成 (active={_active_count}, queue={_queue_count})")

    except HTTPException:
        global _queue_count
        async with _get_queue_lock():
            _queue_count -= 1
        raise
    except Exception:
        async with _get_queue_lock():
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
    queue_info: dict | None = None

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
        "version": "3.3.1",
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
    return {
        "active": _active_count,
        "waiting": _queue_count,
        "max_concurrent": MAX_CONCURRENT_SCRAPES,
        "estimated_wait_seconds": _queue_count * 15,
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
        raise
    except Exception as e:
        print(f"[API] scrape error: {traceback.format_exc()}")
        return ScrapeResponse(success=False, error=f"爬取失敗：{str(e) or type(e).__name__}")


@app.post("/api/create-order", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_order(req: CreateOrderRequest):
    try:
        url = str(req.url).strip()

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
