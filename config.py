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
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")
# admin 端點（cleanup / cleanup preview / scrape-log）專用金鑰。
# ★ 這把絕對不可以進前端。API_SECRET_KEY 是印在 storefront 頁面的
#   window.DAIKO_CONFIG 裡的（任何人檢視原始碼就看得到），所以它只能擋隨機流量，
#   不能拿來保護「會刪商品」「會吐爬取紀錄」的端點。
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "")

# ⏳ 過渡用：輪替公開金鑰期間，同時接受舊的那一把，避免「Zeabur 換好了但
#    storefront 頁面還沒改」的空窗。
#
# 🔴 **輪替完成當天就要把這個環境變數刪掉。**
#    舊那把是印在 storefront 頁面 window.DAIKO_CONFIG 裡、已經公開很久的字串；
#    留著它就等於門還開著，換新金鑰完全沒有意義。
#    移除時機：改完頁面後，看 Zeabur log 不再出現
#    「[Auth] ⚠️ 仍有請求在用舊的公開金鑰」→ 當天就把 API_SECRET_KEY_OLD 刪除。
#    這個機制只給公開金鑰用，**admin 金鑰永遠不吃舊值**。
API_SECRET_KEY_OLD = os.getenv("API_SECRET_KEY_OLD", "")
# 摰
PRICING_TIERS = [
    (0,      5000,    1.25),
    (5001,   10000,   1.22),
    (10001,  20000,   1.20),
    (20001,  30000,   1.18),
    (30001,  999999,  1.15),
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
# 商品自動刪除（天數，0 = 停用）
DAIGO_AUTO_DELETE_DAYS = int(os.getenv("DAIGO_AUTO_DELETE_DAYS", "30"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://goyoutati.com,https://goyoutati.myshopify.com").split(",")
