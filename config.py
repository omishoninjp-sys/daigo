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

# ZOZOTOWN 外部爬蟲（選填，備用）
ZOZO_SCRAPER_URL = os.getenv("ZOZO_SCRAPER_URL", "")

# 定價
PRICING_TIERS = [
    (0,      3000,    1.40),
    (3001,   8000,    1.35),
    (8001,   20000,   1.30),
    (20001,  50000,   1.25),
    (50001,  100000,  1.20),
    (100001, 999999,  1.15),
]
MIN_SERVICE_FEE_JPY = int(os.getenv("MIN_SERVICE_FEE_JPY", "300"))

# 匯率
DEFAULT_JPY_TO_TWD_RATE = float(os.getenv("DEFAULT_JPY_TO_TWD_RATE", "0"))

# 爬蟲
SCRAPE_TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "30"))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# 代理（ZOZOTOWN 用，日本住宅 IP 繞過 Akamai IP 信譽檢查）
PROXY_URL = os.getenv("PROXY_URL", "")

# OpenAI（SEO 標題翻譯用）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 快取（秒）— 30 分鐘，減少重複爬取
CACHE_TTL = int(os.getenv("CACHE_TTL", "1800"))

# 併發限制
MAX_CONCURRENT_SCRAPES = int(os.getenv("MAX_CONCURRENT_SCRAPES", "3"))  # 同時爬取上限
SCRAPE_QUEUE_TIMEOUT = int(os.getenv("SCRAPE_QUEUE_TIMEOUT", "90"))     # 排隊等候超時（秒）

# API 安全
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-me-in-production")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://goyoutati.com,https://goyoutati.myshopify.com").split(",")
