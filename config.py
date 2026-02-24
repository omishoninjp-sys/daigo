"""
GOYOUTATI 代購系統 - 設定檔
"""
import os

# ============================================================
# Shopify 設定
# ============================================================
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "your-store.myshopify.com")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")

# 代購商品要放入的 Collection ID（在 Shopify 後台建立一個「代購商品」Collection）
DAIKO_COLLECTION_ID = os.getenv("DAIKO_COLLECTION_ID", "")

# ============================================================
# 定價邏輯 - 依日幣價格區間設不同費率
# ============================================================
# 格式: (最低價, 最高價, 加成倍率)
# 例如 1.35 表示日幣原價 × 1.35
PRICING_TIERS = [
    (0,      3000,    1.40),   # ¥0 ~ ¥3,000     → 40% 加成
    (3001,   8000,    1.35),   # ¥3,001 ~ ¥8,000  → 35% 加成
    (8001,   15000,   1.30),   # ¥8,001 ~ ¥20,000 → 30% 加成
    (15001,  50000,   1.20),   # ¥20,001 ~ ¥50,000 → 20% 加成
    (50001,  999999,  1.15),   # ¥50,001 以上       → 15% 加成
]

# 最低代購手續費（日幣），避免低價商品利潤太薄
MIN_SERVICE_FEE_JPY = int(os.getenv("MIN_SERVICE_FEE_JPY", "500"))

# ============================================================
# 匯率設定
# ============================================================
# 台幣參考匯率（JPY → TWD），用於前端顯示參考價
# 設 0 則會自動從 API 抓取即時匯率
DEFAULT_JPY_TO_TWD_RATE = float(os.getenv("DEFAULT_JPY_TO_TWD_RATE", "0"))

# ============================================================
# 爬蟲設定
# ============================================================
SCRAPE_TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "30"))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ============================================================
# API 安全
# ============================================================
# 前端呼叫 API 時需帶上此 key（避免被濫用）
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-me-in-production")

# CORS 設定
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://your-store.myshopify.com").split(",")
