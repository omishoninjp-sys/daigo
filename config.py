"""
GOYOUTATI 代購系統 (DAIGO) - 設定檔
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
# ZOZOTOWN 外部爬蟲（選填，備用）
ZOZO_SCRAPER_URL = os.getenv("ZOZO_SCRAPER_URL", "")
# API 安全
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
# 定價
# ★ 最後一段的上限要大到實務上不可能超過。原本是 999999，原價一超過 ¥999,999
#   就掉出整張表，改套 pricing.py 的預設 1.30，比應有的 1.15 多收一大截
#   （¥1,000,000 會從 ¥1,150,000 變成 ¥1,300,000）。
#   不用 float("inf") 是因為這張表會經 /api/rate 序列化成 JSON，Infinity 不是合法 JSON。
PRICING_TIERS = [
    (0,      5000,        1.25),
    (5001,   10000,       1.22),
    (10001,  20000,       1.20),
    (20001,  30000,       1.18),
    (30001,  999_999_999, 1.15),
]
# ─────────────────────────────────────────────────────────────────────
# 數值型環境變數：解析不了就退回預設值，**不可以讓整個服務起不來**
# ─────────────────────────────────────────────────────────────────────
# 🔴 為什麼需要這個（2026-09-03）
#   原本 9 個變數都是 int(os.getenv("X", "預設"))。os.getenv 的預設值只在
#   「變數不存在」時生效 —— **變數存在但值是空字串時，拿到的是 ""**，
#   int("") 直接 ValueError，config 載入失敗，**整個 API 起不來**：
#   不只摘要不寄，連 create-order 一起掛。
#   而「在 Zeabur 建了變數但值留空」是很容易發生的操作。
#   其中 MIN_SERVICE_FEE_JPY 直接進售價運算、DAIGO_AUTO_DELETE_DAYS 決定
#   刪哪些商品 —— 這兩個尤其不能因為一個空格就讓服務整個停擺。
#
# ★ 警告訊息**不印值本身**，只印型別與長度：環境變數很容易被誤填成金鑰、
#   token 之類的東西，而這行會進 Zeabur 的 Runtime Logs。
# ★ 「沒有設定」不算降級（那是設計好的預設路徑），不逐條警告，
#   改在最後印一行彙總；「設了但用不了」才是靜默降級，每一個都要吵。

# 這兩個一個進售價運算、一個決定刪哪些商品，退回預設值要更醒目
_CRITICAL_ENV = {
    "MIN_SERVICE_FEE_JPY": "直接進售價運算 —— 退回預設值代表報價可能與你以為的不同",
    "DAIGO_AUTO_DELETE_DAYS": "決定刪掉幾天前的商品 —— 退回預設值代表刪除範圍可能與你以為的不同",
}

_env_defaults_used = []          # 沒設定的（正常），最後彙總印一行


def _describe_raw(raw) -> str:
    """描述拿到的是什麼，**不印值本身**（可能是誤填的金鑰）。"""
    if raw is None:
        return "未設定"
    if raw == "":
        return "空字串（長度 0）"
    if not raw.strip():
        return f"只有空白字元（{type(raw).__name__}，長度 {len(raw)}）"
    return f"{type(raw).__name__}，長度 {len(raw)}"


def _num_env(name: str, default, cast):
    raw = os.getenv(name)
    if raw is None:
        _env_defaults_used.append(f"{name}={default}")
        return default
    try:
        # int()／float() 本身就會去頭尾空白，這個 .strip() 不改變行為 ——
        # 留著是為了讓「空白會被容忍」這件事在程式碼上看得見，
        # 而不是靠讀者知道 Python 的實作細節。拿掉它不會壞，但也不要以為
        # 拿掉是在簡化：真正決定行為的是下面這個 except。
        return cast(raw.strip())
    except (TypeError, ValueError):
        pass
    note = _CRITICAL_ENV.get(name)
    mark = "🔴🔴" if note else "⚠️"
    print(f"[Config] {mark} {name} 解析失敗（拿到 {_describe_raw(raw)}）"
          f"→ 退回預設值 {default}")
    if note:
        print(f"[Config] {mark} ↳ {name} {note}。"
              f"請到 Zeabur 檢查這個環境變數是不是留空或打錯。")
    return default


def _int_env(name: str, default: int) -> int:
    return _num_env(name, default, int)


def _float_env(name: str, default: float) -> float:
    return _num_env(name, default, float)


MIN_SERVICE_FEE_JPY = _int_env("MIN_SERVICE_FEE_JPY", 300)
# 匯率
DEFAULT_JPY_TO_TWD_RATE = _float_env("DEFAULT_JPY_TO_TWD_RATE", 0.0)
# 爬蟲
SCRAPE_TIMEOUT = _int_env("SCRAPE_TIMEOUT", 30)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
# 代理（ZOZOTOWN 用，日本住宅 IP 繞過 Akamai IP 信譽檢查）
PROXY_URL = os.getenv("PROXY_URL", "")
# OpenAI（SEO 標題翻譯用）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# 快取（秒）— 30 分鐘，減少重複爬取
CACHE_TTL = _int_env("CACHE_TTL", 1800)
# 併發限制
MAX_CONCURRENT_SCRAPES = _int_env("MAX_CONCURRENT_SCRAPES", 3)  # 同時爬取上限
SCRAPE_QUEUE_TIMEOUT = _int_env("SCRAPE_QUEUE_TIMEOUT", 90)     # 排隊等候超時（秒）
# 商品自動刪除（天數，0 = 停用）
DAIGO_AUTO_DELETE_DAYS = _int_env("DAIGO_AUTO_DELETE_DAYS", 30)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://goyoutati.com,https://goyoutati.myshopify.com").split(",")
# 每日爬取摘要信（spec-scrape-monitoring.md 第四、五節）
# ★ DIGEST_ENABLED 預設 false —— 程式部署了不會突然開始寄信，
#   確認過信件格式再到 Zeabur 打開。
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
DIGEST_TO = os.getenv("DIGEST_TO", "omishoninjp@gmail.com")
DIGEST_FROM = os.getenv("DIGEST_FROM", "")      # Resend 要求寄件網域已驗證
DIGEST_HOUR_UTC = _int_env("DIGEST_HOUR_UTC", 1)   # 1 UTC = 台灣早上 9 點
DIGEST_STREAK_DAYS = _int_env("DIGEST_STREAK_DAYS", 7)  # 連續失敗往回看幾天
# 寄信用 Resend，不用 Gmail SMTP（規格第五節：要應用程式密碼、容易被判垃圾信）
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")


# ★ 沒設定的變數彙總成一行 —— 逐條印會在每次啟動洗掉九行，
#   但完全不印就沒辦法回答「線上現在到底用哪個值」。
if _env_defaults_used:
    print("[Config] 數值變數未設定，使用預設值："
          + "、".join(_env_defaults_used))
