"""
GOYOUTATI DAIGO 代購系統 API v3.4
- 快取 scrape 結果（30 分鐘）
- 即時價格平台跳過 cache（snkrdunk、mercari 等）
- 常駐 Chrome 實例
- SEO 最佳化標題（ChatGPT 翻譯）
- 併發限制 + 排隊機制 + 超時保護
"""
import time
import asyncio
import traceback
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
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
import scrape_monitor
print(f"[Config] DAIGO_COLLECTION_ID = '{DAIGO_COLLECTION_ID}'")
print(f"[Config] CACHE_TTL = {CACHE_TTL}s, MAX_CONCURRENT = {MAX_CONCURRENT_SCRAPES}, QUEUE_TIMEOUT = {SCRAPE_QUEUE_TIMEOUT}s")
print(f"[Config] DAIGO_AUTO_DELETE_DAYS = {DAIGO_AUTO_DELETE_DAYS} 天")
# ──────────────────────────────────────────────────────────────────────
# 即時價格平台白名單：這些域名不經 cache，每次都重新爬取
# 因為 snkrdunk、mercari 等平台價格隨時變動，cache 舊價會誤導用戶下單
# ──────────────────────────────────────────────────────────────────────
NO_CACHE_DOMAINS = {
    "snkrdunk.com",       # 球鞋二手交易（價格秒變）
    "jp.mercari.com",     # Mercari 日本（個人賣家可隨時改價/下架）
    "mercari.com",        # Mercari 主域名
    # 未來如有其他即時價格平台可加入這裡
}
def is_no_cache_url(url: str) -> bool:
    """判斷此 URL 是否屬於不可 cache 的平台"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return any(d in host for d in NO_CACHE_DOMAINS)
    except Exception:
        return False
print(f"[Config] NO_CACHE_DOMAINS = {NO_CACHE_DOMAINS}")
# === 背景自動清理任務 ===
async def _auto_cleanup_loop():
    """每 24 小時執行一次自動清理。啟動後先等 60 秒再執行第一次，避免干擾冷啟動。"""
    await asyncio.sleep(60)
    while True:
        try:
            print(f"[AutoCleanup] ⏰ 開始自動清理（刪除超過 {DAIGO_AUTO_DELETE_DAYS} 天的商品）")
            result = await shopify.cleanup_old_daigo_products(days=DAIGO_AUTO_DELETE_DAYS)
            # ★ 印出來的字一定要跟 result["completed"] 一致。
            #   cleanup_old_daigo_products 的四條中止路徑全部是 **return 而不是 raise**
            #   （COLLECTION_ID 未設定／訂單查詢 fail-closed／分頁重試用盡／cursor 重複），
            #   底下的 except 一條都攔不到 —— 以前這裡無條件印「✅ 完成」，而內層才剛
            #   印完「⚠️ 中止」，同一份 log 自相矛盾。
            #   每天實際在跑的是這支、不是 /api/admin/cleanup，這裡印錯等於唯一的
            #   觀測管道整個失效（2026-08-30 就是這樣少刪了 611 件而沒人發現）。
            if result.get("completed", True):
                print(f"[AutoCleanup] ✅ 完成：刪除 {result['deleted_count']} 件，"
                      f"跳過 {result['skipped_count']} 件")
            else:
                print(f"[AutoCleanup] ⚠️ 中止：{result.get('incomplete_reason', '')}，"
                      f"已刪除 {result['deleted_count']} 件，剩餘未處理")
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
app = FastAPI(title="GOYOUTATI DAIGO API", version="3.4.0", lifespan=lifespan)
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
    # 即時價格平台不走 cache
    if is_no_cache_url(url):
        print(f"[Cache] ⏭️  跳過 cache（即時價格平台）: {url[:60]}")
        return None
    if url in _scrape_cache:
        product, ts = _scrape_cache[url]
        if time.time() - ts < CACHE_TTL:
            print(f"[Cache] ✅ 命中快取: {url[:60]}")
            return product
        else:
            del _scrape_cache[url]
    return None
def cache_set(url: str, product: ProductInfo):
    # 即時價格平台不寫入 cache
    if is_no_cache_url(url):
        return
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
    # 1. 快取命中 → 直接回傳（即時價格平台會自動跳過）
    cached = cache_get(url)
    if cached:
        return cached
    # 2. 同 URL 已在爬取中 → 等它完成，共享結果，不開第二個 Chrome
    #    注意：即時價格平台也共享 in-flight 結果（因為同一秒內請求合併不會有舊資料問題）
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
            # 搶到 semaphore 後再確認一次快取（即時價格平台會自動跳過）
            cached = cache_get(url)
            if cached:
                print(f"[Queue] ✅ 排隊期間快取命中: {url[:60]}")
                future.set_result(cached)
                return cached
            # ── 爬取監控（spec-scrape-monitoring.md 第一～三節；目前只記錄不寄信）──
            # 記在這裡而不是 endpoint：/api/scrape 與 /api/create-order 都走這條，
            # 而且快取命中與 in-flight 共享不會走到這裡，天然不會重複計數。
            scrape_monitor.start(url)
            _t0 = time.time()
            try:
                product = await asyncio.wait_for(
                    scraper.scrape(url),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                scrape_monitor.record(url, elapsed_ms=(time.time() - _t0) * 1000,
                                      timed_out=True)
                raise
            except Exception as _scrape_err:
                scrape_monitor.record(url, error=_scrape_err,
                                      elapsed_ms=(time.time() - _t0) * 1000)
                raise
            scrape_monitor.record(url, product=product,
                                  elapsed_ms=(time.time() - _t0) * 1000)
            if product.title:
                cache_set(url, product)  # 即時價格平台不會寫入
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
    blocked: bool = False  # ← True 表示此網站被封鎖（前端應顯示錯誤訊息，不要切到「手動填寫」UI）
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
    blocked: bool = False  # ← True 表示此網站被封鎖
class SearchRequest(BaseModel):
    query: str
    source: str = "rakuten"
    hits: int = 30
    page: int = 1
    translate: bool = True      # True=中文自動翻日文；前端選了候補(已是日文)時送 False
class SearchResultItem(BaseModel):
    title: str
    brand: str = ""
    price_jpy: int | None = None
    selling_price_jpy: int | None = None
    reference_price_twd: int | None = None
    image_url: str = ""
    source_url: str = ""
    in_stock: bool = True
class SearchResponse(BaseModel):
    success: bool
    source: str = ""
    query: str = ""             # 使用者原始輸入
    searched_query: str = ""    # 實際拿去樂天搜的關鍵字(可能是翻譯後)
    count: int = 0
    results: list[SearchResultItem] = []
    error: str | None = None
class SuggestRequest(BaseModel):
    query: str
class SuggestItem(BaseModel):
    label_zh: str
    keyword_jp: str
class SuggestResponse(BaseModel):
    success: bool
    query: str = ""
    suggestions: list[SuggestItem] = []
    error: str | None = None
# === Endpoints ===
@app.get("/api/health")
async def health():
    driver_status = scraper.get_driver_status()
    return {
        "status": "ok",
        "service": "daigo-api",
        "version": "3.4.0",
        "cache_size": len(_scrape_cache),
        "cache_ttl": CACHE_TTL,
        "no_cache_domains": list(NO_CACHE_DOMAINS),
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
        # ★ 先檢查封鎖網站（在 scrape 之前，避免浪費 driver 資源）
        from scrapers.base import detect_blocked, detect_invalid_link
        blocked_reason = detect_blocked(url)
        if blocked_reason:
            print(f"[API] 🚫 封鎖網站: {url[:80]}")
            return ScrapeResponse(
                success=False,
                blocked=True,
                error=blocked_reason,
                queue_info={"active": _active_count, "waiting": _queue_count},
            )
        # ★ 非商品頁連結（圖片直連／搜尋結果／短網址／本站自己）擋在爬取之前，
        #   也不進 scrape_monitor 的失敗紀錄（那份資料是用來排「哪個網域該修」的）
        invalid_reason = detect_invalid_link(url)
        if invalid_reason:
            print(f"[API] 🔗 非商品頁連結: {url[:80]}")
            return ScrapeResponse(
                success=False,
                error=invalid_reason,
                queue_info={"active": _active_count, "waiting": _queue_count},
            )
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
    except ValueError as e:
        # ValueError 通常是 scraper 內部明確擲出的錯誤（如「不支援代購」、「queue-it 中」）
        msg = str(e)
        is_blocked = "不支援代購" in msg or "封鎖" in msg
        print(f"[API] ⚠️ scrape ValueError ({'blocked' if is_blocked else 'normal'}): {msg[:120]}")
        return ScrapeResponse(
            success=False,
            blocked=is_blocked,
            error=msg,
            queue_info={"active": _active_count, "waiting": _queue_count},
        )
    except Exception as e:
        print(f"[API] scrape error: {traceback.format_exc()}")
        return ScrapeResponse(success=False, error=f"爬取失敗：{str(e) or type(e).__name__}")
@app.post("/api/create-order", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_order(req: CreateOrderRequest):
    try:
        url = str(req.url).strip()
        # ★ 先檢查封鎖網站
        from scrapers.base import detect_blocked, detect_invalid_link
        blocked_reason = detect_blocked(url)
        if blocked_reason:
            print(f"[API] 🚫 封鎖網站（建單嘗試）: {url[:80]}")
            return CreateOrderResponse(
                success=False,
                blocked=True,
                error=blocked_reason,
            )
        # ★ 非商品頁連結，同 /api/scrape
        invalid_reason = detect_invalid_link(url)
        if invalid_reason:
            print(f"[API] 🔗 非商品頁連結（建單嘗試）: {url[:80]}")
            return CreateOrderResponse(
                success=False,
                error=invalid_reason,
            )
        # 即時價格平台：強制重抓，不從 cache 拿（價格可能秒變）
        # 一般平台：先試 cache，沒有才爬
        if is_no_cache_url(url):
            print(f"[Order] ⚡ 即時價格平台，強制重抓: {url[:60]}")
            product = await scrape_with_queue(url)
        else:
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
            platform_id=getattr(product, "platform_id", ""), 
        )
        return CreateOrderResponse(
            success=True, product_id=result["product_id"],
            checkout_url=result["storefront_url"], admin_url=result["admin_url"],
        )
    except HTTPException:
        raise
    except ValueError as e:
        # 同樣處理 scraper 主動擲出的 ValueError
        msg = str(e)
        is_blocked = "不支援代購" in msg or "封鎖" in msg
        print(f"[API] ⚠️ create-order ValueError ({'blocked' if is_blocked else 'normal'}): {msg[:120]}")
        return CreateOrderResponse(
            success=False,
            blocked=is_blocked,
            error=msg,
        )
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
        # ★ 新增：source_url 也要過黑名單（手動建單一樣要擋，防止繞過前端攔截）
        if req.source_url:
            from scrapers.base import detect_blocked
            blocked_reason = detect_blocked(req.source_url.strip())
            if blocked_reason:
                print(f"[API] 🚫 封鎖網站（手動建單嘗試）: {req.source_url[:80]}")
                return CreateOrderResponse(
                    success=False,
                    blocked=True,
                    error=blocked_reason,
                )
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
@app.post("/api/search", response_model=SearchResponse, dependencies=[Depends(verify_api_key)])
async def search_products(req: SearchRequest):
    try:
        q = (req.query or "").strip()
        if not q:
            return SearchResponse(success=False, error="請輸入搜尋關鍵字")
        from scrapers import rakuten_api
        from jp_query import translate_to_jp, needs_translation
        searched = q
        if req.translate and needs_translation(q):
            searched = await translate_to_jp(q) or q
        hits = max(1, min(req.hits, 30))
        page = max(1, req.page)
        if req.source == "zozo":
            from scrapers import yahoo_api
            start = (page - 1) * hits + 1          # Yahoo 用 start 位移翻頁
            products = await yahoo_api.search_items(searched, seller_id="zozo", hits=hits, start=start)
        else:
            shop_code = "amiami" if req.source == "amiami" else None
            products = await rakuten_api.search_items(searched, shop_code=shop_code, hits=hits, page=page)
        results = []
        for p in products:
            sell = ref_twd = None
            if p.price_jpy:
                pr = calculate_selling_price(p.price_jpy)
                sell = pr["selling_price_jpy"]
                ref_twd = pr["reference_price_twd"]
            results.append(SearchResultItem(
                title=p.title, brand=p.brand, price_jpy=p.price_jpy,
                selling_price_jpy=sell, reference_price_twd=ref_twd,
                image_url=p.image_url, source_url=p.source_url, in_stock=p.in_stock,
            ))
        # 記錄搜尋詞（需求情報；fire-and-forget）
        if page == 1:
            try:
                from search_log import log_search
                await log_search(raw=q, translated=searched, source=req.source, result_count=len(results))
            except Exception:
                pass
        return SearchResponse(success=True, source=req.source, query=q,
                              searched_query=searched, count=len(results), results=results)
    except Exception:
        print(f"[API] search error: {traceback.format_exc()}")
        return SearchResponse(success=False, error="搜尋失敗，請稍後再試")
@app.get("/api/search-stats", dependencies=[Depends(verify_api_key)])
async def search_stats(days: int = 30):
    """搜尋詞需求情報：熱門詞、零結果詞、每日量。給 search-insights.html 用。"""
    from search_log import stats
    data = await stats(days=max(1, min(days, 365)))
    return {"success": True, **data}
    
@app.get("/admin/insights")
async def insights_page():
    from fastapi.responses import HTMLResponse
    from insights_page import INSIGHTS_HTML
    return HTMLResponse(content=INSIGHTS_HTML)
    
@app.post("/api/suggest", response_model=SuggestResponse, dependencies=[Depends(verify_api_key)])
async def suggest_products(req: SuggestRequest):
    try:
        q = (req.query or "").strip()
        if not q:
            return SuggestResponse(success=True, query="", suggestions=[])
        from jp_query import suggest as jp_suggest
        items = await jp_suggest(q)
        return SuggestResponse(success=True, query=q,
                               suggestions=[SuggestItem(**x) for x in items])
    except Exception:
        print(f"[API] suggest error: {traceback.format_exc()}")
        return SuggestResponse(success=False, error="建議失敗，請稍後再試")
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
    completed: bool = True          # 這輪有沒有掃完；False = 中途中止，不是做完
    incomplete_reason: str = ""
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
        if result.get("completed", True):
            message = f"清理完成：刪除 {result['deleted_count']} 件商品"
        else:
            # 中止就不可以說「完成」：呼叫端（與之後要接的警報）要看得出這輪不完整
            message = (f"⚠️ 清理中止（不完整）：{result.get('incomplete_reason', '')}，"
                       f"已刪除 {result['deleted_count']} 件，剩餘未處理")
        return CleanupResponse(success=True, message=message, **result)
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
    seen_pages = set()
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
                # ★ 分頁一律用 next_page_info()：自己寫 regex 會抓到 previous 的
                #   cursor，在第 1、2 頁之間無限來回（這個端點不刪東西，所以是真的
                #   永遠不會結束）。
                from shopify_client import next_page_info
                page_info = next_page_info(resp.headers.get("Link", ""))
                if not page_info or not resp.json().get("products"):
                    break
                if page_info in seen_pages:
                    break
                seen_pages.add(page_info)
        return {
            "collection_id": DAIGO_COLLECTION_ID,
            "cutoff_date": cutoff.strftime("%Y-%m-%d %H:%M UTC"),
            "days": days,
            "would_delete_count": len(to_delete),
            "products": to_delete,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"預覽失敗：{str(e)}")
# ══════════════════════════════════════════════════════════════════════
# 爬取監控紀錄匯出（spec-scrape-monitoring.md 第六節第 2 步：
# 「兩天後把 JSONL 匯出來看一次，確認分類準不準」）
# 紀錄寫在容器內（/data/scrape_log 或退路），沒有這兩個端點就只能進 shell 撈。
# ══════════════════════════════════════════════════════════════════════
SCRAPE_LOG_MAX_DAYS = 30       # 一天一檔，往回撈太多天沒意義也拖慢回應

# 樣本要給的欄位。error_brief 已經在寫入時截到 200 字、不含 traceback，
# url_path 也已經去掉 query string，所以整筆直接給沒有外洩問題。
_SAMPLE_FIELDS = ("ts", "domain", "platform_id", "source",
                  "http_status", "elapsed_ms", "error_brief", "url_path")


def _scrape_log_days(days: int) -> list[str]:
    if days < 1:
        raise HTTPException(status_code=400, detail="days 至少為 1")
    return scrape_monitor.recent_days(min(days, SCRAPE_LOG_MAX_DAYS))


def _pick_samples(rows: list, limit: int = 3) -> list:
    """
    每種 failure_kind 抽幾筆看細節。

    優先抽不同網域：同一個壞掉的網域連刷 3 筆，看起來像 3 個證據，其實只有 1 個，
    判斷不出分類準不準。網域不夠才拿同網域的補滿。
    """
    picked, seen_domain, seen_id = [], set(), set()
    for r in rows:
        d = r.get("domain")
        if d in seen_domain:
            continue
        seen_domain.add(d)
        seen_id.add(id(r))
        picked.append(r)
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        for r in rows:
            if id(r) in seen_id:
                continue
            picked.append(r)
            if len(picked) >= limit:
                break
    return [{k: r.get(k) for k in _SAMPLE_FIELDS} for r in picked]


@app.get("/api/admin/scrape-log", dependencies=[Depends(verify_api_key)])
async def export_scrape_log(days: int = 2):
    """
    把 scrape_log 的原始 JSONL 拉下來（最近 N 天，新到舊逐日串接，一行一筆）。

    回**純文字**不是 JSON：這樣可以直接存成 .jsonl 給 jq／pandas 吃，
    包成 JSON 字串反而要處理跳脫。空的一天不會有任何輸出，所以檔案是空的時候
    看 response header 才能分辨「沒紀錄」和「路徑撈錯」：
        X-Log-Dir / X-Log-Days / X-Log-Lines

    PowerShell 5.1：
        Invoke-RestMethod "https://<host>/api/admin/scrape-log?days=2" `
          -Headers @{ "X-API-Key" = $env:API_SECRET_KEY } -OutFile scrape.jsonl
    """
    day_list = _scrape_log_days(days)
    lines: list[str] = []
    for day in day_list:
        for line in scrape_monitor.read_raw(day).splitlines():
            if line.strip():
                lines.append(line)
    body = ("\n".join(lines) + "\n") if lines else ""
    return PlainTextResponse(
        body,
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "X-Log-Dir": scrape_monitor.log_dir(),
            "X-Log-Days": ",".join(day_list),
            "X-Log-Lines": str(len(lines)),
        },
    )


@app.get("/api/admin/scrape-log/summary", dependencies=[Depends(verify_api_key)])
async def summarize_scrape_log(days: int = 2):
    """
    在伺服器端算好統計，不用把整份檔案拉下來就能先判斷分類準不準。

    給的東西：總筆數、成功率、各 failure_kind 筆數、各網域失敗次數排序，
    以及每種 failure_kind 抽 3 筆樣本（優先不同網域）。
    """
    day_list = _scrape_log_days(days)

    by_day: dict[str, dict] = {}
    entries: list[dict] = []
    for day in day_list:
        rows = scrape_monitor.read_day(day)
        ok_n = sum(1 for r in rows if r.get("ok"))
        by_day[day] = {"total": len(rows), "ok": ok_n, "failed": len(rows) - ok_n}
        entries.extend(rows)

    total = len(entries)
    ok_count = sum(1 for r in entries if r.get("ok"))
    failed = total - ok_count

    kinds: dict[str, int] = {}
    by_domain: dict[str, dict] = {}
    failures_by_kind: dict[str, list] = {}
    for r in entries:
        dom = r.get("domain") or "(unknown)"
        slot = by_domain.setdefault(dom, {"domain": dom, "total": 0, "failed": 0, "kinds": {}})
        slot["total"] += 1
        if r.get("ok"):
            continue
        kind = r.get("failure_kind") or "other"
        kinds[kind] = kinds.get(kind, 0) + 1
        slot["failed"] += 1
        slot["kinds"][kind] = slot["kinds"].get(kind, 0) + 1
        failures_by_kind.setdefault(kind, []).append(r)

    # 只列有失敗的網域（沒失敗的網域不需要決定任何事），失敗次數多的排前面
    domains = sorted(
        (d for d in by_domain.values() if d["failed"] > 0),
        key=lambda d: (-d["failed"], -d["total"], d["domain"]),
    )

    return {
        "days": day_list,
        "log_dir": scrape_monitor.log_dir(),
        "by_day": by_day,
        "total": total,
        "ok": ok_count,
        "failed": failed,
        "success_rate_pct": round(ok_count / total * 100, 1) if total else None,
        "failure_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "domains_by_failure": domains,
        "samples": {k: _pick_samples(v) for k, v in
                    sorted(failures_by_kind.items(), key=lambda kv: -len(kv[1]))},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
