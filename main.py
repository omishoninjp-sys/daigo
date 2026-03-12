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
    DAIGO_AUTO_DELETE_DAYS,
)
from scraper import Scraper, ProductInfo
from pricing import calculate_selling_price, get_jpy_to_twd_rate
from shopify_client import ShopifyClient
from seo_title import generate_seo_title

print(f"[Config] DAIGO_COLLECTION_ID = '{DAIGO_COLLECTION_ID}'")
print(f"[Config] CACHE_TTL = {CACHE_TTL}s, MAX_CONCURRENT = {MAX_CONCURRENT_SCRAPES}, QUEUE_TIMEOUT = {SCRAPE_QUEUE_TIMEOUT}s")
print(f"[Config] DAIGO_AUTO_DELETE_DAYS = {DAIGO_AUTO_DELETE_DAYS} 天")

# === 背景自動清理任務 ===

async def _auto_cleanup_loop():
    """每 24 小時執行一次自動清理。啟動後先等 60 秒再執行第一次，避免干擾冷啟動。"""
    await asyncio.sleep(60)
    while True:
        try:
            print(f"[AutoCleanup] ⏰ 開始自動清理（刪除超過 {DAIGO_AUTO_DELETE_DAYS} 天的商品）")
            result = await shopify.cleanup_old_daigo_products(days=DAIGO_AUTO_DELETE_DAYS)
            print(f"[AutoCleanup] ✅ 完成：刪除 {result['deleted_count']} 件，跳過 {result['skipped_count']} 件")
        except Exception as e:
            print(f"[AutoCleanup] ❌ 發生錯誤: {type(e).__name__}: {e}")
        # 等 24 小時再執行下一次
        await asyncio.sleep(24 * 60 * 60)


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動時建立背景清理任務
    task = asyncio.create_task(_auto_cleanup_loop())
    print("[Startup] ✅ 自動清理背景任務已啟動")
    yield
    # 關閉時取消任務
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="GOYOUTATI DAIGO API", version="3.3.1", lifespan=lifespan)

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

# 同 URL 進行中的 Future（防止重複爬取同一頁面開多個 Chrome）
_in_flight: dict[str, asyncio.Future] = {}


async def scrape_with_queue(url: str) -> ProductInfo:
    global _queue_count

    # 1. 快取命中 → 直接回傳
    cached = cache_get(url)
    if cached:
        return cached

    # 2. 同 URL 已在爬取中 → 等它完成，共享結果，不開第二個 Chrome
    if url in _in_flight:
        print(f"[Queue] 🔗 同 URL 已在爬取中，等待共享結果: {url[:60]}")
        try:
            return await asyncio.wait_for(
                asyncio.shield(_in_flight[url]),
                timeout=SCRAPE_QUEUE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail=f"等候逾時（{SCRAPE_QUEUE_TIMEOUT}s），請稍後再試")

    # 3. 建立 Future，讓後續相同 URL 的請求共享
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    _in_flight[url] = future

    position = await _increment_queue()
    print(f"[Queue] 📋 新請求加入排隊 (位置 #{position}): {url[:60]}")

    try:
        # 等 semaphore（限制同時爬取數）
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
            # 搶到 semaphore 後再確認一次快取
            cached = cache_get(url)
            if cached:
                print(f"[Queue] ✅ 排隊期間快取命中: {url[:60]}")
                future.set_result(cached)
                return cached

            product = await asyncio.wait_for(
                scraper.scrape(url),
                timeout=60,
            )

            if product.title:
                cache_set(url, product)

            future.set_result(product)
            return product

        except asyncio.TimeoutError:
            print(f"[Queue] ⏰ 爬取超時 (60s): {url[:60]}")
            result = ProductInfo(source_url=url)
            future.set_result(result)
            return result

        except Exception as e:
            if not future.done():
                future.set_exception(e)
            raise

        finally:
            _get_semaphore().release()
            await _decrement_active()
            _in_flight.pop(url, None)
            print(f"[Queue] ✅ 爬取完成 (active={_active_count}, queue={_queue_count})")

    except HTTPException:
        async with _get_queue_lock():
            _queue_count -= 1
        if not future.done():
            future.cancel()
        _in_flight.pop(url, None)
        raise
    except Exception:
        async with _get_queue_lock():
            _queue_count -= 1
        if not future.done():
            future.cancel()
        _in_flight.pop(url, None)
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
            in_stock=product.in_stock,
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




# === 清理端點（管理員用）===

class CleanupRequest(BaseModel):
    days: int = DAIGO_AUTO_DELETE_DAYS  # 預設值從 config 取


class CleanupResponse(BaseModel):
    success: bool
    deleted_count: int = 0
    deleted_ids: list[int] = []
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = []
    cutoff_date: str = ""
    message: str = ""


@app.post("/api/admin/cleanup", response_model=CleanupResponse, dependencies=[Depends(verify_api_key)])
async def manual_cleanup(req: CleanupRequest):
    """
    手動觸發清理：刪除超過 N 天的 daigo 商品。
    days 預設值從環境變數 DAIGO_AUTO_DELETE_DAYS 取（預設 10 天）。
    """
    if req.days < 1:
        return CleanupResponse(success=False, message="days 至少為 1")
    try:
        result = await shopify.cleanup_old_daigo_products(days=req.days)
        return CleanupResponse(
            success=True,
            message=f"清理完成：刪除 {result['deleted_count']} 件商品",
            **result,
        )
    except Exception as e:
        print(f"[API] cleanup error: {traceback.format_exc()}")
        return CleanupResponse(success=False, message=f"清理失敗：{str(e)}")


@app.get("/api/admin/cleanup/preview", dependencies=[Depends(verify_api_key)])
async def preview_cleanup(days: int = DAIGO_AUTO_DELETE_DAYS):
    """
    預覽哪些商品會被清理（不實際刪除）。只看 DAIGO_COLLECTION_ID 內的商品。
    """
    from datetime import datetime, timezone, timedelta

    if days < 1:
        raise HTTPException(status_code=400, detail="days 至少為 1")
    if not DAIGO_COLLECTION_ID:
        raise HTTPException(status_code=400, detail="DAIGO_COLLECTION_ID 未設定")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    to_delete = []
    page_info = None

    try:
        async with __import__("httpx").AsyncClient(timeout=30) as client:
            while True:
                params = {"collection_id": DAIGO_COLLECTION_ID, "fields": "id,title,created_at,status", "limit": 250}
                if page_info:
                    params = {"page_info": page_info, "limit": 250, "fields": "id,title,created_at,status"}

                resp = await client.get(
                    f"{shopify.base_url}/products.json",
                    headers=shopify.headers,
                    params=params,
                )
                if resp.status_code != 200:
                    break

                for p in resp.json().get("products", []):
                    try:
                        created_at = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                        if created_at < cutoff:
                            age_days = (datetime.now(timezone.utc) - created_at).days
                            to_delete.append({
                                "product_id": p["id"],
                                "title": p["title"],
                                "created_at": p["created_at"],
                                "age_days": age_days,
                                "status": p.get("status"),
                            })
                    except Exception:
                        continue

                import re as _re
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    m = _re.search(r'page_info=([^&>]+).*?rel="next"', link_header)
                    page_info = m.group(1) if m else None
                else:
                    page_info = None

                if not page_info or not resp.json().get("products"):
                    break

        return {
            "collection_id": DAIGO_COLLECTION_ID,
            "cutoff_date": cutoff.strftime("%Y-%m-%d %H:%M UTC"),
            "days": days,
            "would_delete_count": len(to_delete),
            "products": to_delete,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"預覽失敗：{str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
