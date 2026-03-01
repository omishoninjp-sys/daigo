"""
GOYOUTATI DAIGO 代購系統 API v3
- Amazon: requests（快速）
- ZOZOTOWN: 代理到本機 product-fetcher
- 其他網站: Playwright
"""
import traceback
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import API_SECRET_KEY, ALLOWED_ORIGINS, ZOZO_SCRAPER_URL, DAIGO_COLLECTION_ID
from scraper import Scraper, ProductInfo
from pricing import calculate_selling_price, get_jpy_to_twd_rate
from shopify_client import ShopifyClient

print(f"[Config] DAIGO_COLLECTION_ID = '{DAIGO_COLLECTION_ID}'")

app = FastAPI(title="GOYOUTATI DAIGO API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

scraper = Scraper()
shopify = ShopifyClient()


async def verify_api_key(x_api_key: str = Header(default="")):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# === Models ===

class ScrapeRequest(BaseModel):
    url: str

class ScrapeResponse(BaseModel):
    success: bool
    product: dict | None = None
    pricing: dict | None = None
    error: str | None = None

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
    return {
        "status": "ok",
        "service": "daigo-api",
        "version": "3.0.0",
        "scrapers": {
            "amazon": "requests (direct)",
            "zozotown": f"external ({ZOZO_SCRAPER_URL})" if ZOZO_SCRAPER_URL else "undetected-chromedriver (built-in)",
            "generic": "playwright",
        },
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

        product: ProductInfo = await scraper.scrape(url)

        if not product.title:
            return ScrapeResponse(success=False, error="無法從此連結抓取商品資訊")

        pricing = calculate_selling_price(product.price_jpy) if product.price_jpy else None
        return ScrapeResponse(success=True, product=product.to_dict(), pricing=pricing)

    except Exception as e:
        print(f"[API] scrape error: {traceback.format_exc()}")
        return ScrapeResponse(success=False, error=f"爬取失敗：{str(e) or type(e).__name__}")


@app.post("/api/create-order", response_model=CreateOrderResponse, dependencies=[Depends(verify_api_key)])
async def create_order(req: CreateOrderRequest):
    try:
        url = str(req.url).strip()
        product: ProductInfo = await scraper.scrape(url)

        if not product.title:
            return CreateOrderResponse(success=False, error="無法抓取商品資訊")
        if not product.price_jpy:
            return CreateOrderResponse(success=False, error="無法偵測到商品價格")

        pricing = calculate_selling_price(product.price_jpy)
        title = req.title_override or product.title

        result = await shopify.create_daigo_product(
            title=title, price_jpy=pricing["selling_price_jpy"],
            image_url=product.image_url, description=product.description,
            source_url=url, original_price_jpy=product.price_jpy,
            brand=product.brand, extra_images=product.extra_images,
            variants=product.variants,
        )

        return CreateOrderResponse(
            success=True, product_id=result["product_id"],
            checkout_url=result["storefront_url"], admin_url=result["admin_url"],
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

        result = await shopify.create_daigo_product(
            title=req.title, price_jpy=req.price_jpy,
            image_url=req.image_url, source_url=req.source_url,
            original_price_jpy=req.original_price_jpy,
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
