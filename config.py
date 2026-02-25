"""
GOYOUTATI 代購系統 (DAIGO) - 設定檔
"""
import os

# Shopify
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "your-store.myshopify.com")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
DAIGO_COLLECTION_ID = os.getenv("DAIGO_COLLECTION_ID", "")
STORE_DOMAIN = os.getenv("STORE_DOMAIN", "goyoutati.com")

# 定價
PRICING_TIERS = [
    (0,      3000,    1.40),
    (3001,   8000,    1.35),
    (8001,   20000,   1.30),
    (20001,  50000,   1.25),
    (50001,  999999,  1.20),
]
MIN_SERVICE_FEE_JPY = int(os.getenv("MIN_SERVICE_FEE_JPY", "300"))

# 匯率
DEFAULT_JPY_TO_TWD_RATE = float(os.getenv("DEFAULT_JPY_TO_TWD_RATE", "0"))

# 爬蟲
SCRAPE_TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "30"))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# API 安全
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-me-in-production")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://your-store.myshopify.com").split(",")
