"""
GOYOUTATI 代購系統 - 後端 API
部署在 Zeabur，提供給 Shopify 前端呼叫

端點：
  POST /api/scrape       → 爬取商品資訊 + 計算售價
  POST /api/create-order → 建立 Shopify 商品並回傳結帳連結
  GET  /api/health       → 健康檢查
  GET  /api/rate          → 取得目前匯率和定價費率
"""
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from config import API_SECRET_KEY, ALLOWED_ORIGINS
from scraper import Scraper, ProductInfo
from pricing import calculate_selling_price, get_jpy_to_twd_rate
from shopify_client import ShopifyClient

app = FastAPI(title="GOYOUTATI 代購 API", version="1.0.0")

# CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

# 初始化模組
scraper = Scraper()
shopify = ShopifyClient()


# ================================================================
# 驗證
# ================================================================

async def verify_api_key(x_api_key: str = Header(default="")):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ================================================================
# Request / Response Models
# ================================================================

class ScrapeRequest(BaseModel):
    url: str  # 商品連結


class ScrapeResponse(BaseModel):
    success: bool
    product: dict | None = None
    pricing: dict | None = None
    error: str | None = None


class CreateOrderRequest(BaseModel):
    url: str
    # 可選：客人可覆蓋自動抓到的資訊
    title_override: str | None = None


class CreateOrderResponse(BaseModel):
    success: bool
    product_id: int | None = None
    checkout_url: str | None = None
    admin_url: str | None = None
    error: str | None = None


class ManualOrderRequest(BaseModel):
    title: str
    price_jpy: int
    original_price_jpy: int = 0
    image_url: str = ""
    source_url: str = ""


# ================================================================
# 端點
# ================================================================

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "daiko-api"}


@app.get("/api/rate")
async def get_rate():
    """取得目前匯率和定價費率（前端參考用）"""
    from config import PRICING_TIERS
    rate = get_jpy_to_twd_rate()
    return {
        "jpy_to_twd": rate,
        "pricing_tiers": [
            {"min_jpy": t[0], "max_jpy": t[1], "markup": t[2]}
            for t in PRICING_TIERS
        ],
    }


@app.post("/api/scrape", response_model=ScrapeResponse, dependencies=[Depends(verify_api_key)])
async def scrape_product(req: ScrapeRequest):
    """
    Step 1: 客人貼連結 → 爬取商品資訊 + 計算售價
    """
    try:
        product: ProductInfo = await scraper.scrape(str(req.url))

        if not product.title:
            return ScrapeResponse(
                success=False,
                error="無法從此連結抓取商品資訊，請確認連結是否正確",
            )

        # 計算定價
        pricing = None
        if product.price_jpy:
            pricing = calculate_selling_price(product.price_jpy)

        return ScrapeResponse(
            success=True,
            product=product.to_dict(),
            pricing=pricing,
        )

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[API] scrape 錯誤: {tb}")
        error_msg = str(e) or type(e).__name__
        return ScrapeResponse(
            success=False,
            error=f"爬取失敗：{error_msg}",
        )


@app.post("/api/create-order", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_order(req: CreateOrderRequest):
    """
    Step 2: 確認商品後 → 在 Shopify 建立商品 → 回傳結帳頁連結
    """
    try:
        # 先爬取
        product: ProductInfo = await scraper.scrape(str(req.url))

        if not product.title:
            return CreateOrderResponse(
                success=False,
                error="無法抓取商品資訊",
            )

        if not product.price_jpy:
            return CreateOrderResponse(
                success=False,
                error="無法偵測到商品價格，請聯繫客服",
            )

        # 計算售價
        pricing = calculate_selling_price(product.price_jpy)

        # 標題（允許覆蓋）
        title = req.title_override or product.title

        # 在 Shopify 建立商品
        result = await shopify.create_daiko_product(
            title=title,
            price_jpy=pricing["selling_price_jpy"],
            image_url=product.image_url,
            description=product.description,
            source_url=str(req.url),
            original_price_jpy=product.price_jpy,
            brand=product.brand,
            extra_images=product.extra_images,
        )

        return CreateOrderResponse(
            success=True,
            product_id=result["product_id"],
            checkout_url=result["storefront_url"],
            admin_url=result["admin_url"],
        )

    except Exception as e:
        return CreateOrderResponse(
            success=False,
            error=f"建立商品失敗：{str(e)}",
        )


@app.post("/api/create-manual", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_manual_order(req: ManualOrderRequest):
    """
    手動建立代購商品（爬取失敗時，客人自己填資訊）
    """
    try:
        if not req.title:
            return CreateOrderResponse(success=False, error="請填寫商品名稱")
        if req.price_jpy <= 0:
            return CreateOrderResponse(success=False, error="價格錯誤")

        result = await shopify.create_daiko_product(
            title=req.title,
            price_jpy=req.price_jpy,
            image_url=req.image_url,
            description="",
            source_url=req.source_url,
            original_price_jpy=req.original_price_jpy,
            brand="",
        )

        return CreateOrderResponse(
            success=True,
            product_id=result["product_id"],
            checkout_url=result["storefront_url"],
            admin_url=result["admin_url"],
        )

    except Exception as e:
        import traceback
        print(f"[API] create-manual 錯誤: {traceback.format_exc()}")
        return CreateOrderResponse(
            success=False,
            error=f"建立商品失敗：{str(e)}",
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
