"""
GOYOUTATI 隞?頃蝟餌絞 (DAIGO) - 閮剖?瑼?
"""
from dotenv import load_dotenv
load_dotenv()

import os
# Shopify
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "your-store.myshopify.com")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
DAIGO_COLLECTION_ID = os.getenv("DAIGO_COLLECTION_ID", "")
STORE_DOMAIN = os.getenv("STORE_DOMAIN", "goyoutati.com")
# ZOZOTOWN 憭?祈嚗憛恬??嚗?
ZOZO_SCRAPER_URL = os.getenv("ZOZO_SCRAPER_URL", "")
# 摰
PRICING_TIERS = [
    (0,      3000,    1.40),
    (3001,   8000,    1.35),
    (8001,   20000,   1.30),
    (20001,  50000,   1.25),
    (50001,  100000,  1.20),
    (100001, 999999,  1.15),
]
MIN_SERVICE_FEE_JPY = int(os.getenv("MIN_SERVICE_FEE_JPY", "300"))
# ?舐?
DEFAULT_JPY_TO_TWD_RATE = float(os.getenv("DEFAULT_JPY_TO_TWD_RATE", "0"))
# ?祈
SCRAPE_TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "30"))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
# 隞??嚗OZOTOWN ?剁??交雿? IP 蝜? Akamai IP 靽∟亳瑼Ｘ嚗?
PROXY_URL = os.getenv("PROXY_URL", "")
# OpenAI嚗EO 璅?蝧餉陌?剁?
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# 敹怠?嚗?嚗?30 ??嚗?撠?銴??
CACHE_TTL = int(os.getenv("CACHE_TTL", "1800"))
# 雿萇?
MAX_CONCURRENT_SCRAPES = int(os.getenv("MAX_CONCURRENT_SCRAPES", "3"))
SCRAPE_QUEUE_TIMEOUT = int(os.getenv("SCRAPE_QUEUE_TIMEOUT", "90"))
# API 摰
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-me-in-production")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://goyoutati.com,https://goyoutati.myshopify.com").split(",")
