"""
共用基礎模組：ProductInfo、detect_platform、工具函數
"""
import re
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass, asdict, field
from collections import Counter

from config import SCRAPE_TIMEOUT, USER_AGENT


# ============ ProductInfo ============

@dataclass
class ProductInfo:
    title: str = ""
    price_jpy: int | None = None
    image_url: str = ""
    description: str = ""
    source_url: str = ""
    brand: str = ""
    currency: str = "JPY"
    extra_images: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    image_base64: str = ""   # 當 Shopify 無法直接下載圖片時，用 base64 上傳
    is_adult: bool = False   # 成人商品標記
    in_stock: bool = True    # 商品整體庫存（無 variants 時使用）

    def to_dict(self):
        d = asdict(self)
        d.pop("image_base64", None)  # 不回傳 base64 到前端（太大）
        return d

    @property
    def is_valid(self):
        return bool(self.title and self.price_jpy and self.price_jpy > 0)


# ============ 封鎖網站清單 ============

BLOCKED_DOMAINS = {
    "duty-free-japan.jp": (
        "此商品來自 Duty Free Japan（日本免稅店），"
        "商品需於機場現場取貨，無法透過代購服務寄送。"
        "如需代購其他日本商品，請改用 Amazon JP、ZOZOTOWN 等購物網站。"
    ),
    "tw.mercari.com": (
        "您提供的是 Mercari 台灣版（tw.mercari.com）的連結，"
        "Mercari 台灣版為本地二手平台，非日本商品，無法提供代購服務。"
        "若要代購日本 Mercari 商品，請至 jp.mercari.com 取得正確連結後再試一次。"
    ),
    "bibian.co.jp": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "dokodemo.world": (
        "dokodemo（どこでも）為日本代購轉送平台，本服務不代購其他代購平台的商品。"
        "如需代購，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
),
    "hoka.com": (
        "HOKA 官網明文規定禁止轉載資訊，在我們取得授權前，暫時無法抓取商品資料。"
        "如需代購 HOKA 商品，請於 LINE @544kaytb 直接傳送商品截圖、款式、尺寸與顏色，"
        "我們將為您手動報價並建立訂單。造成不便敬請見諒。"
    ),
    "amazon.com": (
        "您貼上的是 Amazon 美國站（amazon.com）的連結，本服務僅代購日本商品。"
        "請至 Amazon 日本站（amazon.co.jp）挑選商品後再試一次。"
    ),
    # ── 其他代購平台（本服務不代購其他代購商）──
    "tokukai.com": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "letao.com.tw": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "zenmarket.jp": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "daigobang.com": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "go1buy1.com": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "jpgoodbuy.com": (
        "此商品來自其他日本代購平台，本服務不代購其他代購平台的商品。"
        "如需代購日本商品，請直接提供日本原始商店（Amazon JP、ZOZOTOWN 等）的商品連結。"
    ),
    "buyma.com": (
        "BUYMA 為日本個人代購（Personal Shopper）平台，本服務不代購其他代購平台的商品。"
        "BUYMA 上的商品本身就是賣家代購的，建議直接提供商品原本品牌的官方網站連結，"
        "可省下中間代購費用。如需代購日本商品，請改用 Amazon JP、ZOZOTOWN 等購物網站。"
    ),
    "buyee.jp": (
        "Buyee 為日本代購／轉運平台，本服務不代購其他代購平台的商品。"
        "Buyee 上的商品多來自 Mercari、Yahoo 拍賣、樂天等網站，建議直接提供原始網站連結"
        "（例如 jp.mercari.com 的商品連結），可省下中間代購費用。"
    ),
}


def detect_blocked(url: str) -> str | None:
    """
    若 URL 屬於封鎖清單，回傳原因說明字串；否則回傳 None。

    🔴 2026-09-08：由 `if domain in host` 改成 `_host_matches`。
       舊寫法與 CLAUDE.md「網域比對絕不可用 in」直接牴觸，只是還沒被踩到 ——
       對 295 個真實網域回測**誤擋 0**，所以它一直看起來沒問題。
       但機制上這幾個網域都會被誤擋（回測樣本裡剛好都沒有）：
         amazon.com  → amazon.com.au、amazon.com.br、notamazon.com …
         hoka.com    → hoka.com.tw、hoka.com.au …
         buyee.jp    → xbuyee.jp、mybuyee.jp …
         buyma.com   → notbuyma.com、buyma.com.tw …

       ⚠️ 2026-09-08 實際連過（第一版註解寫「都是各品牌的各國官網」是**猜的**，
          而且猜錯了，見 CLAUDE.md 2-1）：
            amazon.com.au / amazon.com.br  真的是 Amazon 的澳洲／巴西站
            amazon.com.tw                  **不存在**（DNS 查不到）
            hoka.com.tw                    存在，但是「閎康彩色印刷有限公司」，
                                           手工紙盒，跟 HOKA 一點關係都沒有
            hoka.com.au                    存在，但是 "Australia's Largest Range
                                           of Plastic Duct Fittings"，塑膠管配件
            xbuyee.jp / mybuyee.jp / notbuyma.com / notamazon.com
                                           查不到，是構造出來的反例
          結論不變（這個寫法會誤擋不相干的網域），但**理由要寫對的那個**：
          誤擋的是「剛好以某個字串結尾／開頭的無關網站」，不是「品牌的各國官網」。
       這正是 `t.co` 誤擋 7 家商店的同一種病，只是換了一張清單。
       **「目前沒有誤擋」不等於「這個寫法是對的」** —— 前者是語料的巧合，
       後者才是機制。改完的回測見 tests/verify_invalid_link.py。
    """
    host = (urlparse(url).hostname or "").lower()
    for domain, reason in BLOCKED_DOMAINS.items():
        if _host_matches(host, domain):
            return reason
    return None


# ── 商品頁 404／410：專屬訊息 ─────────────────────────────────
#
# 2026-09-08 實測（線上 scrape_log 近 12 天 1,039 筆）：
#   404/410 共 32 筆 —— zozo.jp 26、store.shopping.yahoo.co.jp 2、
#   style-mixer.smart-ec.io 2、suruga-ya.jp 2。
#   最極端的一筆：zozo.jp /sp/shop/ellnoloset/goods/110151716/
#   在 2026-09-04 的 10:18–16:17 之間被重試 **13 次**，13 次全是 404。
#   同一件商品的 `/sp/` 與非 `/sp/` 兩種寫法都試過，都是 404
#   （所以不是我們的網址正規化壞掉 —— `/sp/` 路徑同期有 32 筆成功）。
#
# 🔴 客人會重試 13 次，是因為他收到的訊息是「無法從此連結抓取商品資訊」——
#    那句話**沒有告訴他發生什麼事，也沒有告訴他該怎麼辦**，
#    於是唯一想得到的動作就是再貼一次。
#
# ⚠️ 這則刻意只認 HTTP 404/410，**不認 gone_hint**（頁面內容出現「販売終了」
#    「sold out」那類字樣）。那組字在正常商品頁上也會出現（例如某個尺寸完售），
#    範圍大很多而且沒有回測語料。要擴大前先拿資料，同 CLAUDE.md 的子字串教訓。
MSG_PRODUCT_GONE = (
    "這個商品頁在日本站上已經不存在了（回應 404），通常是商品下架、售完，"
    "或賣家換了網址 —— 再貼一次同一條連結還是會一樣，"
    "所以先不用重試。"
    "如果你還想要這件商品，麻煩用 LINE @544kaytb 把連結傳給我們，"
    "我們幫你找找看還有沒有別的地方買得到；"
    "或者回到該商店重新搜尋商品名稱，取得新的網址再貼一次。"
)


# ============ 非商品頁連結攔截 ============
#
# 客人貼錯連結（圖片直連、搜尋結果、短網址、本站自己）在爬取「之前」就擋掉：
#   · 省掉一次沒有意義的爬取（generic 那條要開瀏覽器，很貴）
#   · 更重要的是不要進 scrape_monitor 的失敗紀錄 —— 那份資料是用來決定
#     「哪個網域該修」的，混進客人貼錯的連結就沒法排序了
#
# ★ 網域比對一律用 _host_matches()（完整網域或其子網域），絕不可用子字串 in。
#   `"t.co" in host` 會命中 tocco-closet.co.jp、golfdigest.co.jp、dot-st.com、
#   newart.co.jp、uniformnext.com、lilith-soft.com 等一堆正常商店 ——
#   實測第一版就是這樣誤擋了 7 家。

_IMAGE_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    ".avif", ".heic", ".tiff", ".tif", ".ico",
)

# 主機名第一段是這些字 → 圖片/靜態資源主機（image.rakuten.co.jp 這類）
_ASSET_HOST_LABELS = {"img", "image", "images", "static", "assets", "cdn"}

# 已知的圖床／CDN
_IMAGE_HOSTS = (
    "filestackcontent.com", "cdn.shopify.com", "imgz.jp",
    "akamaized.net", "cloudfront.net", "googleusercontent.com",
)

# 搜尋引擎與短網址（短網址展不開就不知道客人要買什麼）
_SEARCH_SHORTLINK_HOSTS = (
    "google.com", "google.co.jp", "share.google", "goo.gl",
    "bing.com", "t.co", "bit.ly", "lin.ee", "reurl.cc",
    "pse.is", "tinyurl.com",
)

# 明顯不是商店的網域：社群／通訊／協作／影音（2026-09-03）
#
# 客人偶爾會貼這些進來，爬蟲照樣跑一輪，然後在每日摘要的「需要處理」區
# 佔一行 —— 但沒有人該去修 facebook.com 的解析器。
# 實測 7 天 535 筆裡有 2 筆：
#   facebook.com/share/r/...            → parse_failed（title 有、price 無）
#   evolutivelabs.slack.com/archives/…  → blocked
# 擋在爬取之前的另一個好處：這類網址的 path 會被寫進爬取紀錄，
# 那筆 slack 的 path 是 /archives/D0A9323B718/…，D 開頭是 **DM 頻道 ID**。
#
# 🔴 一律走 _host_matches（完整網域或其子網域），絕不可以用 `in` ——
#    `evolutivelabs.slack.com` 靠 endswith(".slack.com") 命中就夠了。
# 🔴 刻意不放進來的：
#    line.me   —— LINE GIFT／LINE ショッピング 是真的商店，擋了會誤傷
#    note.com  —— 日本的 note 有賣數位商品，不是純社群
#    google.com / t.co / bit.ly —— 已經在 _SEARCH_SHORTLINK_HOSTS 裡
# 加新網域之前先問：它有沒有可能是某個人真的要代購的商品頁？
_NON_SHOP_HOSTS = (
    "facebook.com", "instagram.com", "threads.net", "twitter.com", "x.com",
    "slack.com", "discord.com", "notion.so", "github.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com",
    "reddit.com", "linkedin.com",
)

# 本站自己
_OWN_HOSTS = ("goyoutati.com", "myshopify.com")

_MSG_IMAGE = (
    "您貼的是圖片檔的直接連結，不是商品頁面，我們無法從圖片取得商品名稱與價格。"
    "請回到該商品的購物網站，複製網址列上的商品頁連結（通常會包含商品名稱或商品編號）再試一次。"
)
_MSG_SEARCH = (
    "您貼的是搜尋結果或短網址，不是商品頁面。"
    "請點進您要購買的那一件商品，進入商品頁後再複製網址列上的連結給我們。"
)
_MSG_NON_SHOP = (
    "您貼的是社群、通訊或影音平台的連結，不是購物網站的商品頁面。"
    "請到商品所在的日本購物網站（Amazon JP、樂天、ZOZOTOWN、Yahoo 商店街等），"
    "點進您要購買的那一件商品，再複製網址列上的連結給我們。"
)
_MSG_OWN = (
    "您貼的是本站自己的商品頁連結。"
    "如果要代購新商品，請提供日本購物網站（Amazon JP、樂天、ZOZOTOWN、Yahoo 商店街等）的商品連結；"
    "若是要購買本站已上架的商品，直接在該商品頁下單即可。"
)
_MSG_HOMEPAGE = (
    "您貼的是網站首頁（或語言切換頁），不是商品頁面，上面通常有很多件商品，"
    "我們無法判斷您要買哪一件。請點進您要購買的那一件商品，"
    "進入商品頁後再複製網址列上的連結給我們。"
)
_MSG_MALFORMED = (
    "這不是一個有效的商品網址。"
    "請從購物網站的商品頁複製完整網址（以 http:// 或 https:// 開頭）再試一次。"
)
_MSG_BUYBACK = (
    "您貼的是駿河屋的「買取（收購）」頁面 —— 那是駿河屋**向賣家收購**商品的報價頁，"
    "不是可以下單購買的商品頁。"
    "請在駿河屋站內搜尋同一件商品的販售頁面（網址會是 /product/detail/...），"
    "再把連結給我們。"
)


def _host_matches(host: str, domain: str) -> bool:
    """
    完整網域或其子網域才算命中。

    ★ 絕對不要改成子字串比對。`domain in host` 會讓 "t.co" 命中
    tocco-closet.co.jp、dot-st.com 等正常商店，客人會被無故擋下。
    """
    host = (host or "").lower().strip().rstrip(".")
    domain = (domain or "").lower().strip().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


# 只有一段路徑、而且那一段是語言代碼 → 語系首頁，不可能是商品頁
_LANG_ONLY_SEGMENTS = {
    "zh", "zh-tw", "zh-cn", "zh-hk", "zh-hant", "zh-hans", "tw", "cn",
    "en", "en-us", "en-gb", "ja", "ja-jp", "jp", "ko", "ko-kr",
    "th", "vi", "id", "fr", "de", "es", "it", "pt", "ru",
}


def _host_is_structurally_valid(host: str) -> bool:
    """
    主機名結構上可不可能是一個網域。

    擋的是「絕對不可能存在」的形狀，不是「我沒看過」的網域 ——
    誤擋一家正常商店的代價遠高於放行一個壞連結：
      · 空 label（.mercari.com、jp..mercari.com）
      · 沒有點（單一 label，不是公開網域）
      · TLD 少於兩個字，或不是字母（punycode 的 xn-- 例外）

    2026-08-30：紀錄裡 jp.mercari.com 被拆成三列，其中 .mercari.com 這種形狀
    根本不可能連得上，卻照樣被送去爬，在統計裡生出一個幽靈網域。
    """
    host = (host or "").strip().rstrip(".")
    if not host:
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(not lab for lab in labels):
        return False
    tld = labels[-1]
    if tld.startswith("xn--"):
        return True
    return len(tld) >= 2 and tld.isalpha()


def detect_invalid_link(url: str) -> str | None:
    """
    非商品頁連結 → 回傳給客人看的繁中說明；正常商品連結 → None。

    掛在 /api/scrape 與 /api/create-order，detect_blocked 之後、爬取之前。
    """
    raw = (url or "").strip()

    # ── 4. 結構不成立：非 http(s)、沒有 host ──
    try:
        parsed = urlparse(raw)
    except Exception:
        return _MSG_MALFORMED
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return _MSG_MALFORMED
    if not _host_is_structurally_valid(host):
        return _MSG_MALFORMED

    # ── 3. 本站自己 ──
    for domain in _OWN_HOSTS:
        if _host_matches(host, domain):
            return _MSG_OWN

    # ── 2. 搜尋引擎與短網址 ──
    for domain in _SEARCH_SHORTLINK_HOSTS:
        if _host_matches(host, domain):
            return _MSG_SEARCH

    # ── 2b. 明顯不是商店（社群／通訊／協作／影音）──
    for domain in _NON_SHOP_HOSTS:
        if _host_matches(host, domain):
            return _MSG_NON_SHOP

    # ── 1. 圖片直連 ──
    for domain in _IMAGE_HOSTS:
        if _host_matches(host, domain):
            return _MSG_IMAGE
    # 主機名第一段是 img/image/images/static/assets/cdn
    labels = host.split(".")
    if len(labels) > 1 and labels[0] in _ASSET_HOST_LABELS:
        return _MSG_IMAGE
    # 路徑副檔名是圖片（只看 path，query string 不算）
    path = (parsed.path or "").lower()
    if path.endswith(_IMAGE_EXTS):
        return _MSG_IMAGE

    # ── 5. 首頁／語系首頁 ──
    #
    # 2026-08-30 實測 coldbeer.jp/zh：語系首頁照樣被爬，generic 從 og 標籤抓到
    # 店名「冷啤酒店」加上某件商品的 ¥41,800，直接建出一件不存在的商品 ——
    # **這是會被下單的假商品，不是統計問題。**
    #
    # ★ 有 query string 就一律不擋：カラーミーショップ 的商品網址長這樣
    #   https://xxx.shop-pro.jp/?pid=123456789 —— path 是空的但確實是商品頁。
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    if not (parsed.query or "").strip():
        if not segments:
            return _MSG_HOMEPAGE
        if len(segments) == 1 and segments[0].lower() in _LANG_ONLY_SEGMENTS:
            return _MSG_HOMEPAGE

    # ── 6. 駿河屋的「買取（收購）」頁：那是駿河屋向賣家收購的報價頁，不是販售頁 ──
    #
    # 客人貼這種連結本來就不是要買東西，履約在物理上不可能 —— 爬下去只會生出
    # 一件「有標題有價格、但那個價格是收購價」的商品，比爬失敗更糟。
    # 2026-09-07 的 source_url 分析在 12 個月訂單裡撈到 1 筆
    # （/kaitori/kaitori_detail/…），量很小，但它是**確定不可能成交**的那一類。
    #
    # ★ 為什麼是 host + 第一段路徑，不是子字串：
    #   「kaitori」這個字出現在別家商店的網址裡完全可能（買取専門店多得是），
    #   只看 `"kaitori" in url` 會誤擋。綁在 suruga-ya.jp 的第一段路徑上，
    #   而它的販售頁一律是 /product/detail/{id}，兩者不會互相誤傷。
    if _host_matches(host, "suruga-ya.jp") and segments:
        if segments[0].lower() == "kaitori":
            return _MSG_BUYBACK

    return None


# ============ Platform Detection ============

def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "yslb.jp" in host:
        return "ysl"
    if "gu-global.com" in host:
        return "gu"
    if "zozo" in host:
        return "zozotown"
    if "amazon.co.jp" in host or "amazon.jp" in host or "amzn.asia" in host or "amzn.to" in host:
        return "amazon"
    if "uniqlo.com" in host:
        return "uniqlo"
    if "muji.com" in host:
        return "muji"
    if "beams.co.jp" in host:
        return "beams"
    if "nijisanji.jp" in host:
        return "nijisanji"
    if "palcloset.jp" in host:
        return "palcloset"
    if "rakuten.co.jp" in host:
        return "rakuten"
    if "nanouniverse" in host or "store.nanouniverse.jp" in host:
        return "shopify_jp"
    if "ancellm.com" in host:
        return "shopify_jp"
    if "neighborhood.jp" in host:
        return "neighborhood"
    if "wtaps.com" in host:
        return "wtaps"
    if "humanmade.jp" in host:
        return "humanmade"
    if "supreme.com" in host:
        return "supreme"
    if "shop.vermicular.jp" in host:
        return "vermicular"
    if "shop.visvim.tv" in host:
        return "visvim"
    if "grail.bz" in host:
        return "grail"
    if "pokemoncenter-online.com" in host:
        return "pokemoncenter"
    if "daytona-park.com" in host:  # ← 新增
        return "daytona_park"
    if "runway-webstore.com" in host:
        return "runway"
    if "takaratomy.co.jp" in host or "takaratomymall.jp" in host:
        return "takaratomy"
    # queue-it 排隊系統的 URL（c=takaratomy 表示是 takaratomy 的 queue）
    if "queue-it.net" in host and "takaratomy" in url.lower():
        return "takaratomy"
    if "snkrdunk.com" in host:
        return "snkrdunk"
    if "p-bandai.jp" in host:
        return "pbandai"
    if "shop-list.com" in host:
        return "shoplist"
    if "animate-onlineshop.jp" in host:
        return "animate"
    if "mazdacollection.jp" in host:
        return "mazdacollection"
    if "marukyu-koyamaen.co.jp" in host:
        return "marukyukoyamaen"
    if "amiami.jp" in host:
        return "amiami"
    if "netmall.hardoff.co.jp" in host:  # ← 新增（ハードオフ オフモール，中古單品；JS 渲染走 SeleniumBase）
        return "netmall"
    if "newbalance.jp" in host:
        return "newbalance"
    if "adidas.jp" in host:
        return "adidas"
    if "graniph.com" in host:
        return "graniph"
    if "fanatics.jp" in host or "softbankhawksstore.jp" in host:  # ← 軟銀鷹官方店同為 Fanatics 平台
        return "fanatics"
    if "mercari.com" in host or "jp.mercari.com" in host:
        return "mercari"
    if "shop.npb.or.jp" in host:
        return "npb"
    if "store.disney.co.jp" in host:
        return "disney"
    if "yoshidakaban.com" in host:  # ← 新增
        return "yoshidakaban"
    if "ec-store.net" in host:
        return "ecstore"
    if "bellemaison.jp" in host:
        return "bellemaison"
    if "biccamera.com" in host:
        return "biccamera"
    if "shop-shimamura.com" in host:
        return "shimamura"
    if "/view/item/" in url:
        return "makeshop"
    return "generic"


# ============ 工具函數 ============

def normalize_url(url: str) -> str:
    # ShopServe 手機版 → PC 版
    shopserve_m = re.match(r'(https?://[^/]+)/smp/item/(.+)', url)
    if shopserve_m:
        normalized = f"{shopserve_m.group(1)}/SHOP/{shopserve_m.group(2)}"
        print(f"[Normalize] ShopServe 手機版 → PC 版: {url} → {normalized}")
        return normalized

    # YoshidaKaban: 去掉 /zh-CHT/ /zh-CN/ /en/ /ko/ 等語系前綴，強制走日文版
    # 否則商品頁顯示外幣價格（TWD/HKD/USD/KRW），會被 scraper 當成 JPY 抓進來
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if "yoshidakaban.com" in host:
            new_path = re.sub(
                r"^/(zh-CHT|zh-CN|en|ko)(/|$)",
                "/",
                parsed.path,
                flags=re.IGNORECASE,
            )
            if new_path != parsed.path:
                normalized = urlunparse(parsed._replace(path=new_path))
                print(f"[Normalize] YoshidaKaban 語系前綴去除: {url} → {normalized}")
                return normalized
    except Exception as e:
        print(f"[Normalize] YoshidaKaban 處理失敗: {e}")

    return url


def normalize_price(price) -> int | None:
    if isinstance(price, (int, float)):
        return int(price)
    if isinstance(price, str):
        cleaned = re.sub(r'[^0-9.]', '', price)
        return int(float(cleaned)) if cleaned else None
    return None


# ============ 價格合理範圍：單一數值來源 ============
#
# 🔴 這裡是**唯一**可以寫價格上下限數字的地方。
#    2026-09-08 之前有三套並存且互相矛盾的範圍：
#      generic.GenericMixin._PRICE_MIN/_PRICE_MAX      100 / 1,000,000
#      generic._scrape_with_playwright 與 _extract_json_ld  100 / 1000000（各自寫死）
#      amiami / animate / netmall / pbandai / …        100 / 10,000,000
#    同一件商品走不同 scraper 會有不同的接受範圍，而沒有任何地方說得出為什麼。
#
# ★ 數值依據（2026-09-08，Shopify 連接器 bulk，全庫 5,309 件商品中
#   1,526 件帶 daigo.original_price_jpy metafield）：
#      最低      ¥110（郵票）        → 沒有任何一件低於 ¥100
#      最高合法  ¥972,612（ASUS Ascent GX10 迷你電腦，amazon.co.jp）
#      次高      ¥440,000（CITIZEN 腕錶）、¥415,000（AIRBOW 音響，ippinkan.jp）
#      >¥1,000,000 的合法商品：0 件
#
# ★ 上限取 10,000,000 而不是 1,000,000，理由是**收斂必須挑一個值**，
#   而 1,000,000 對 generic 太緊：ippinkan.jp（音響）已經出現 ¥415,000，
#   那個品類 ¥1,000,000 以上是常態，而超出上限的後果是 price_jpy 被清成 None
#   →「無法偵測到商品價格」，客人看不出是被範圍擋掉的（靜默失敗）。
#   原本 1,000,000 這個上限也**從來沒有擋下過**它看起來要擋的東西 ——
#   實際出事的 ¥999,999（dot-st）剛好在它底下。那件事現在由
#   detect_sentinel_price() 負責，不是靠上限。
PRICE_MIN_JPY = 100
PRICE_MAX_JPY = 10_000_000

# ⚠️ Yahoo 系（yahoo_api / platform_yahoo_store / platform_zozotown）原本就是 50，
#    收斂時**刻意不改成 100**：這一版的目標是「數字只有一個出處」，
#    不是「順便改行為」。全庫最低 ¥110，所以 50 與 100 在現有語料上等價，
#    真要統一應該另外開一次、拿 Yahoo 自己的資料驗。
PRICE_MIN_JPY_YAHOO = 50


def price_in_range(value, min_jpy: int = PRICE_MIN_JPY,
                   max_jpy: int = PRICE_MAX_JPY) -> bool:
    """價格是否落在合理範圍內。None／非數字一律 False。"""
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return min_jpy <= value <= max_jpy


# ============ 成人商品偵測 ============

ADULT_KEYWORDS = [
    # 日文
    "オナホ", "オナニー", "バイブ", "ローター", "アダルト",
    "大人のおもちゃ", "性具", "ラブグッズ", "コンドーム",
    "潤滑", "ローション", "電動マッサージ", "アダルトグッズ",
    "セクシーランジェリー", "セクシー下着", "ボディストッキング",
    "SM", "拘束", "エッチ", "18禁", "R-18", "R18",
    # 英文
    "masturbat", "vibrator", "dildo", "adult toy", "sex toy",
    "fleshlight", "onahole", "tenga", "lube ", "lubricant",
    "bondage", "fetish",
]


def detect_adult(product: ProductInfo) -> bool:
    """偵測是否為成人商品"""
    import re as _re
    text = f"{product.title} {product.description} {product.source_url}".lower()
    # 需要全字匹配的關鍵字（避免 SM 誤判 SMART 等）
    WHOLE_WORD_KW = {"sm", "r-18", "r18", "18禁"}
    for kw in ADULT_KEYWORDS:
        kw_lower = kw.lower()
        if kw_lower in WHOLE_WORD_KW:
            # 用 word boundary 或前後非字母數字
            if _re.search(r'(?<![a-z0-9])' + _re.escape(kw_lower) + r'(?![a-z0-9])', text):
                print(f"[Adult] ⚠️ 偵測到成人商品關鍵字: '{kw}'")
                return True
        else:
            if kw_lower in text:
                print(f"[Adult] ⚠️ 偵測到成人商品關鍵字: '{kw}'")
                return True
    return False


# ============ 受限品類偵測 ============
#
# detect_blocked() 看網址，在爬取前擋。但「寶可夢卡牌」「抽選販売」是**商品層級**
# 的問題 —— 同一個 Pokémon Center 網域，周邊商品買得到、卡牌擴充包買不到，
# 網址層級擋不掉。所以只能等爬完拿到 title 再判斷。
#
# 資料來源（2026-09 實測，近 50 筆訂單）：作廢 12 筆＝24%，其中寶可夢卡 10 筆
# ¥131,293、BEYBLADE X 1 筆 ¥9,149、海賊王卡 1 筆 ¥14,054，
# 多數在下單後 20–95 秒內被取消。

# ── 硬擋：不生成商品頁，直接回錯誤 ────────────────────────────────
#
# 刻意不塞進同一條 regex —— 判斷形態根本不同，混在一起就會像 2026-09 那次，
# 方向寫反了卻被別條 alternation 蓋住，測試全過但實際上沒作用：
#   1. 標題關鍵字命中就算（航海王卡牌、抽選販售）
#   2. **兩個詞都要出現、順序不拘**（寶可夢＋擴充包、週年慶祝＋寶可夢）
#   3. 純網域判斷（BEYBLADE 只擋官方專門站）
# 另外有「命中但豁免」的第四種：一番賞在二手平台上是真的買得到的現貨。
#
# 說明要講「為什麼」，不能只寫「不支援」—— 客人才不會覺得被拒絕得莫名其妙。
# 擋得掉但買得到的品項，訊息要**告訴客人改貼哪裡的連結**，那是還救得回來的營收。

# ── 寶可夢卡牌 ──
# 三條路：a) 日文寫死的卡牌字樣　b) 寶可夢＋擴充包兩個詞都在　c) 週年慶祝＋寶可夢
#
# 🔴 b 舊版寫成 `(?:拡張パック|擴充包|擴展包).{0,12}(?:ポケモン|寶可夢)`，
#    **只認「擴充包在前、寶可夢在後」**。但真實標題全部長成
#    「寶可夢 卡片擴充包 - 卡牌 MEGA 擴充包 風暴綠寶石」—— 寶可夢在前，
#    所以這條等於沒作用，2026-09 訂單回測有 8 個商品行因此漏掉。
#    當時 28 條單元測試全過是假象：那些樣本另外含 30th CELEBRATION，
#    靠字面命中蓋住了方向錯誤。**fixture 要用「只有待驗差異」的樣本。**
_POKEMON_WORD = re.compile(r"ポケモン|寶可夢|宝可梦|pok[eé]mon", re.I)
_POKEMON_CARD_DIRECT = re.compile(
    r"ポケモンカード|ポケカ|pokemon\s*card|ポケカゲーム"
    r"|ポケモン.{0,12}(?:拡張パック|拡張ボックス|BOX|ボックス)",
    re.I,
)
_POKEMON_PACK_WORD = re.compile(
    r"拡張パック|拡張ボックス|拡張包|擴充包|擴展包|擴張包", re.I)
# 🔴 「30th CELEBRATION」單獨看是**品牌無關字串** —— SEIKO 30th、任何週年商品都會中。
#    一定要同一 haystack 內有寶可夢上下文才算。目前 5,634 筆真實標題掃出來 0 誤擋，
#    所以這是預防性修正：**被誤擋的客人不會來反映，只會默默離開，沒有回饋迴路**，
#    跟低估售價的 bug 同一種不對稱。
_POKEMON_ANNIV = re.compile(
    r"30th\s*CELEBRATION|30\s*[周週]年慶祝|30\s*[周週]年慶典", re.I)

# ── 四則硬擋訊息共用的替代通路清單 ──────────────────────────
# ★ 改這一行就好。四則訊息的口徑必須一致，否則客人照著其中一則去貼，
#   結果那個站不在 _SECONDHAND_HOSTS 或不在前端 ALLOW 白名單裡，等於白跑一趟。
#   這裡列的站要同時滿足兩件事：在下面的 _SECONDHAND_HOSTS 裡，且在
#   theme 的 daigo.liquid ALLOW 清單裡。動這一行時兩邊都要一起檢查。
# ⚠️ Yahoo 拍賣（auctions.yahoo.co.jp）刻意不列：該站同時有競標與即決，
#    純競標我們做不了，而網域層級分不出來。即決的判定規則定案前不主動導流。
_ALT_CHANNELS = "Mercari、駿河屋、PayPay フリマ"

_MSG_POKEMON_CARD = (
    "寶可夢卡牌的擴充包與卡盒在日本採抽選／限量販售，官方通路開賣即完售，"
    "我們無法保證取得，這類商品目前不開放直接下單。"
    f"請改貼 {_ALT_CHANNELS} 上的現貨連結，那些我們照常代購；"
    "單張卡、卡冊與周邊配件不受影響。找不到的話歡迎用 LINE @544kaytb 詢問。"
)

# ── BEYBLADE：只擋官方專門站 ──
# 🔴 絕對不可以連帶擋掉 takaratomy.co.jp —— 那個網域有專屬 scraper、賣其他玩具，
#    全擋會誤傷。一律 _host_matches（完整網域或其子網域），不可以用子字串 in。
#
# 標題關鍵字（ベイブレード／BEYBLADE／戰鬥陀螺）**已經拿掉**，理由只有一個：
# **攔不到**。SEO 標題會把它翻成「貝克力德」「贝贝旋风」，或整個丟掉只剩型番
# UX-21，關鍵字對這些標題本來就無效（2026-09 回測有 17 個商品行是這樣漏的）。
#
# 🔴 不要把「其他通路履約得了」寫成放行的理由 —— 資料不支持。
#    2025-09～2026-09 十二個月的 BEYBLADE 訂單，各通路取消率：
#        Amazon     23 筆 18 取消（78%）
#        樂天       26 筆 21 取消（81%）
#        Yodobashi  10 筆  9 取消（90%）
#    這三個通路一樣難履約，只是**不在本規則的攔截範圍內**（規則只認官方網域）。
#    「規則不擋它們」與「它們做得起來」是兩件事，不可混為一談。
#    （早先一版註解引用 GYT20262353 當「Amazon 履約得了」的證據 —— 那是朋友的
#      訂單，已列入 .restricted_backtest_exclude：買得到是因為關係，
#      不能拿來當通路穩定的依據。兩處說法矛盾，以本段為準。）
_BEYBLADE_HOSTS = ("beyblade.takaratomy.co.jp",)

# タカラトミーモール：官方商城，限定／抽選的 BEYBLADE 都在這裡，但它**同時賣
# トミカ等正常玩具**，所以不可以整域擋。條件是「host 命中 ＋ 型番樣式命中」。
# 🔴 型番樣式先驗過才用的（2026-09-05，Admin API 全部 5,807 件商品）：
#    命中 20 件，全部是 BEYBLADE，沒有一件トミカ或其他系列；
#    takaratomymall.jp 上的 7 件商品中 6 件命中（全是 BEYBLADE），
#    另一件「購物車玩具」不命中 —— 規則範圍內誤中 0。
# ★ 邊界用 (?<![0-9A-Za-z]) 而不是 \b：CJK 在 Python re 裡算 \w，
#   「限定BX-46」這種沒有空格的寫法 \b 會失效。實測兩種在現有語料上
#   命中完全相同（20 vs 20），這個寫法只是把未來的漏洞補掉。
_BEYBLADE_MALL_HOSTS = ("takaratomymall.jp",)
_BEYBLADE_MODEL = re.compile(r"(?<![0-9A-Za-z])[BUCX]X-\d{2}(?![0-9A-Za-z])")
# ⚠️ 這則訊息刻意**不推薦** Amazon／樂天／Yodobashi。
#    規則不擋那三個通路，但 12 個月資料顯示它們的 BEYBLADE 取消率是
#    78%／81%／90% —— 把客人導過去只是把取消從「攔截前」搬到「付款後」，
#    對客人更糟（要等、要退款），對我們也更貴（要人工作廢）。
#    只推二手平台：商品已經在賣家手上，是現貨。
_MSG_BEYBLADE = (
    "BEYBLADE 在日本官方通路（タカラトミーモール／官方專門站）採抽選／限量販售，"
    "開賣即完售，我們無法保證取得。"
    f"如果要找同款，建議到 {_ALT_CHANNELS} 這些二手平台看現貨 —— "
    "商品已經在賣家手上，貼那邊的連結我們照常代購。"
)

# ── 一番賞：官方通路擋，二手平台放行 ──
# 規則前提是「店頭限量抽選、線上取得不了」—— 對官方通路成立，對二手現貨不成立。
# 而且擋下來的訊息自己寫著「歡迎詢問二手現貨」，卻擋在 Mercari 的現貨商品頁上，
# 自相矛盾。2026-09 全站回測：32 筆硬擋裡有 3 筆是這種二手平台連結。
# ★ 網址是空的（手動建單沒填來源）時**不豁免** —— 判斷不出來就照擋。
#
# ⚠️ 這份清單目前混合了兩種依據，新增時要自己說清楚是哪一種：
#     (a) 驗證過收得了單 —— 有實際成交紀錄
#         mercari.com / jp.mercari.com（12 個月 136 件成交）
#         suruga-ya.jp（36 件）、paypayfleamarket.yahoo.co.jp（10 件）
#     (b) 只是「有專屬 scraper 且確實是二手通路」，**沒有成交佐證**
#         netmall.hardoff.co.jp —— 12 個月訂單 0 筆、10 天爬取 0 次、
#         現存商品 0 件。留著是因為 scrapers/netmall.py 寫得完整
#         （實測它正確取税込、避開了 JSON-LD 的税抜），且零流量所以風險≈0。
#
# 🔴 日後新增一律要有一致標準。建議：(a) 才直接加；只有 (b) 的先觀察，
#    或在這裡註明「無成交佐證」，不要讓兩種依據混在一起看不出差別。
_SECONDHAND_HOSTS = (
    "mercari.com",                  # jp.mercari.com 是它的子網域，_host_matches 涵蓋得到
    "jp.mercari.com",               # 寫出來只是為了讀的人看得懂
    "paypayfleamarket.yahoo.co.jp",
    "suruga-ya.jp",
    "netmall.hardoff.co.jp",
    # (b) 二手專門店，性質同駿河屋；12 個月訂單 0 筆、10 天爬取 0 次，
    #     **無成交佐證**。2026-09-07 依使用者指示加入，同批也加進 theme 的 ALLOW。
    "mandarake.co.jp",
)
# ⚠️ auctions.yahoo.co.jp 刻意**不在**豁免清單裡。
#    該站同時有純競標與即決（Buy It Now），只有即決做得了，
#    而兩者網址完全相同（/jp/auction/{id}），網域層級分不出來。
#    留在豁免清單裡，會讓一番賞在**純競標頁**上被放行 —— 純競標我們履約不了。
#    判準是頁面上有沒有「即決価格」欄位，但 auctions.yahoo 目前走 generic、
#    沒有專屬 scraper 抓得到那個欄位：實測 generic 取的是現在価格／開始時の価格，
#    PING 那筆少 ¥9,000、EIZO 那筆少 ¥1,100（低估售價，客人不會來反映）。
#    → 即決 scraper 寫好、能取到即決価格之後，再把這行加回來。
_ICHIBAN = re.compile(r"一番くじ|一番賞|イチバンくじ", re.I)
# ⚠️ 這則是**純網域硬擋**（1kuji.com）在用的，不是標題規則在用的。
#    1kuji.com 整站 100% 一番賞、12 個月請到款 0 元，整域擋沒有營收損失。
_MSG_ICHIBAN = (
    "一番賞是日本店頭限量抽選商品，官方通路線上無法穩定取得，這類連結目前不開放代購。"
    f"若你想要特定賞品，可以貼 {_ALT_CHANNELS} 這些二手平台的現貨連結，"
    "那些我們照常代購。"
)
# ★ 標題命中「一番くじ／一番賞」時改判 soft（2026-09-08）——
#   精確率 66.7%，也就是每三件裡有一件其實買得到，硬擋等於連那一件一起丟掉。
#   soft 的處置是「商品頁照建，但不上架到線上商店」，客人不能自己結帳、
#   要來詢問，錯判的成本從「完全買不到」降成「多問一句」。
_MSG_ICHIBAN_SOFT = (
    "一番賞多半是日本店頭的限量抽選商品，官方通路線上不一定拿得到，"
    "所以這一件我們先幫你建好商品頁、但不開放直接結帳。"
    "麻煩用 LINE @544kaytb 跟我們確認現在還買不買得到，可以的話馬上幫你開通。"
    f"如果你想要的是現貨，也可以直接貼 {_ALT_CHANNELS} 這些二手平台的連結，那些我們照常代購。"
)

# ── 航海王卡牌 ──
_ONEPIECE_CARD = re.compile(
    r"ワンピースカード|ONE\s*PIECE\s*カードゲーム|海賊王\s*卡牌|OPカードゲーム",
    re.I,
)
# ★ 2026-09-08 由 hard 改判 soft：精確率 61.5%，是幾條規則裡最低的一條 ——
#   將近四成命中其實買得到，硬擋把那四成一起丟掉。
_MSG_ONEPIECE = (
    "航海王卡牌多半採限量／抽選販售，官方通路不一定拿得到，"
    "所以這一件我們先幫你建好商品頁、但不開放直接結帳。"
    "麻煩用 LINE @544kaytb 跟我們確認一下現況，買得到的話馬上幫你開通。"
    f"想要現貨的話也可以直接貼 {_ALT_CHANNELS} 上的連結，那些我們照常代購。"
)

# ── 抽選販售頁 ──
# 🔴 繁中變體一定要一起列。SEO 標題是繁中的，寫的是「抽選販**售**」，
#    而日文是「抽選販**売**」—— 兩個是不同的字，只列日文等於對站上大部分
#    標題失效。2026-09-05 實測「塔卡拉托米 玩具 - 抽選販售 BEYBLADE X CX-18
#    隨機增強包｜takaratomymall.jp」回 None，就是漏在這裡。
_CHUSEN = re.compile(
    r"抽選販売|抽選受付|抽選予約|応募受付|【抽選】"
    r"|抽選販售|抽選販賣|抽選受理|抽選預約|隨機增強包",
    re.I,
)
_MSG_CHUSEN = (
    "這個商品頁是日本官方的「抽選販售」（抽籤制），中籤才能購買，"
    "我們無法代為參加抽選。若之後開放一般販售，再麻煩你重新貼連結。"
)

# ── 軟擋網域：整站不硬擋，但不開放自助結帳 ─────────────────────
#
# 🔴 進這張表的門檻**比 _RESTRICTED_HOSTS 低一階**：
#    只需要「12 個月請到款 = 0」，不需要「整站單一品類」。
#    因為 soft 不會讓客人買不到 —— 商品頁照建，只是要先問一句。
#
# 2026-09-08 實測（Shopify 連接器 bulk，12 個月 1,302 筆訂單／2,301 line item，
#                  日期範圍 2025-09-07 → 2026-09-07）：
#   official-store.jfa.jp   23 個 line item，**23 個全部 VOIDED 且已取消，
#                           請到款 0 元**。
#                           ★ 這 23 件實際上是 BEYBLADE X ×日本代表 聯名
#                             （UX-00 サムライセイバー5-60K），但 SEO 標題被
#                             寫成「日本足球協會 運動器材／武士劍」，
#                             **關鍵字規則一條都不命中**；而 _BEYBLADE_MODEL
#                             只在 takaratomymall.jp 生效，所以型番也擋不到。
#                             → 這是「只有網域擋得住」的典型案例。
#   ⚠️ 不放進硬擋表的理由：該站同時賣一般日本代表隊周邊（球衣、圍巾），
#      那些我們做得到，只是 12 個月剛好沒有人買過。
_MSG_JFA = (
    "這個商品來自日本足球協會官方商店。這個網站上的聯名／限定商品我們過去"
    "一件都沒有成功買到過（近一年 23 筆全部取消），所以先幫你建好商品頁、"
    "但不開放直接結帳。麻煩用 LINE @544kaytb 跟我們確認能不能取得，"
    "可以的話馬上幫你開通。"
)
_SOFT_HOSTS = (
    ("official-store.jfa.jp", _MSG_JFA),
)

# ── Pokémon Center 的受注生產 ────────────────────────────────
#
# 已經有的 _SOFT_WARNING 只認「受注生産／受注販売／受注期間」，
# **不認單獨一個「受注」**。而 Pokémon Center 的 SEO 標題正好都是單獨的：
#   「寶可夢中心 皮卡丘 毛絨玩具 - 受注皮卡丘毛絨玩具」
#   「寶可夢中心 皮卡丘 玩偶 - 受注 皮卡丘玩偶・公仔」
# 2026-09-08 實測 12 個月訂單，這四筆的結果是 2 PAID／2 VOIDED（50%）——
# 買得到，但一半會失敗，正好是 soft 的定義。
#
# ★ 為什麼把單獨的「受注」限定在這個網域，不做成全站規則：
#   全站語料裡單獨「受注」只有這四筆，沒有其他樣本可以驗誤擋率。
#   沒驗過的範圍不放進規則 —— 要放大要先拿資料。
_POKECEN_HOSTS = ("pokemoncenter-online.com",)
_JUCHU_BARE = re.compile(r"受注", re.I)
_MSG_POKECEN_JUCHU = (
    "這是 Pokémon Center 的「受注生產」商品（先接單後生產），"
    "日本端的接單期間與出貨時間都由官方決定，我們過去成功率大約一半。"
    "所以先幫你建好商品頁、但不開放直接結帳 —— "
    "麻煩用 LINE @544kaytb 跟我們確認接單狀況，可以的話馬上幫你開通。"
)

# ── 軟擋：仍可生成商品頁，但要提醒交期 ───────────────────────────
# 刻意不放「限定」兩個字：日本商品標題極常見（日本限定、店舗限定），
# 多數其實買得到，放進來會天天誤報。
_SOFT_WARNING = [
    (
        re.compile(
            r"予約|受注生産|受注販売|お届け予定|発売予定|入荷予定|ヶ月待ち|カ月待ち",
            re.I,
        ),
        "這是日本端的預購／接單生產商品，出貨時間由賣家決定，可能等待數週至數個月，"
        "不適用一般 7–14 個工作天的時程。下單前請先確認交期。",
    ),
    (
        re.compile(r"数量限定|完全受注|限定生産|受注期間", re.I),
        "這是數量限定／限期接單商品，可能在我們下單前就結束販售。"
        "若採購失敗會全額退款，但仍請留意。",
    ),
]


# ── 純網域硬擋表：整站商品都不接的官方通路 ──────────────────────────
#
# ★ 這張表的判斷**不需要 title**，所以呼叫端要擺在爬取「之前」
#   （見 detect_restricted_host）。放在爬取之後就等於沒有用：
#   2026-09-07 線上實測，beyblade.takaratomy.co.jp 與 takaratomymall.jp
#   都爬不出 title（各逾時 60 秒），`if not product.title: return` 先命中，
#   detect_restricted_category 根本沒被呼叫，客人拿到的是
#   「無法從此連結抓取商品資訊」＋手動填寫表單，而不是攔截訊息。
#
# 🔴 進這張表的門檻有兩條，缺一不可：
#    (1) **整站單一品類**，沒有我們做得到的東西
#    (2) **12 個月請到款 = 0**（「未取消」不算，這家店手動請款，
#        EXPIRED 是授權過期沒收到錢）
#
#    2026-09-07 驗證（12 個月完整訂單，1,309 筆／2,308 line item）：
#      1kuji.com               一番くじ倶楽部＝BANDAI SPIRITS 官方**情報站**，
#                              無購物車、無外連購物站，全站 100% 一番賞。
#                              2 件下單（NT$25,000）／請到款 0
#      30th.pokemon-card.com   ポケモンカード30周年**特別站**，有税込價格但無購物車，
#                              全站 100% TCG。3 件下單（NT$45,384）／請到款 0
#      beyblade.takaratomy.co.jp  BEYBLADE X **入口站**，無購物車，
#                              外連只到 takaratomymall.jp，全站 100% BEYBLADE。
#                              2 件下單（NT$6,717）／請到款 0
#    三站合計 7 件、NT$77,101 下單，**請到款 0 元** → 整域擋零營收損失。
#
# 🔴 刻意**不**進這張表的兩個站（同站有做得到的商品，維持標題條件）：
#    pokemoncenter-online.com  12 個月唯一請到款的是 **Switch 遊戲軟體**
#                              『口袋妖怪 火紅版』NT$23,759，不是卡牌；
#                              「Pokémon Center」標籤另有 4 件請到款 NT$10,822，
#                              全是皮卡丘毛絨玩具與卡片插畫收藏，
#                              而取消的 36 件（NT$275,321）全是卡包／擴充包。
#                              → 標題條件方向正確：擋卡牌、放行布偶文具。
#    takaratomymall.jp         12 個月 26 件、請到款 15 件 NT$42,422，
#                              成交率 58%（同期 Amazon 22%／樂天 19%／
#                              Yodobashi 10%）。⚠️ 但那 15 件**全部命中**
#                              下面的 _BEYBLADE_MODEL —— 現行規則擋掉的
#                              正好是這個站唯一有成交紀錄的品項。
#                              成交全部集中在 2026-03~05，2026-06 之後
#                              同型號（UX-00 前 28 件成交 14／後 31 件成交 0，
#                              CX-00 前 4/2、後 27/0）一件都做不到，
#                              而該站近 30 天爬取只有 1 次。
#                              → 現在擋是對的，但理由是「供給端斷了」，
#                                不是舊註解說的「它同時賣トミカ」——
#                                トミカ 在 12 個月訂單裡**一筆都沒有**，
#                                那句話沒有成交佐證。
_RESTRICTED_HOSTS = (
    ("1kuji.com", _MSG_ICHIBAN),
    ("30th.pokemon-card.com", _MSG_POKEMON_CARD),
    ("beyblade.takaratomy.co.jp", _MSG_BEYBLADE),
)


def _restricted_host(url: str) -> str:
    """從 url 取 host；取不到回空字串（手動建單常常沒有網址）。"""
    try:
        return (urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _is_pokemon_card(haystack: str) -> bool:
    """寶可夢卡牌：直接字樣，或「寶可夢」與「擴充包／週年慶祝」兩個詞都出現。"""
    if _POKEMON_CARD_DIRECT.search(haystack):
        return True
    if not _POKEMON_WORD.search(haystack):
        return False
    # ★ 順序不拘 —— 「寶可夢 卡片擴充包」與「拡張パック ポケモン」都要算
    return bool(_POKEMON_PACK_WORD.search(haystack) or _POKEMON_ANNIV.search(haystack))


def detect_restricted_host(url: str) -> tuple[str, str] | None:
    """
    只看網域的硬擋。**在爬取之前呼叫**（detect_blocked / detect_invalid_link 之後）。

    回傳 ("hard", 說明) 或 None。

    ★ 與 detect_restricted_category 的分工：
        這支    純網域，不需要 title，擋得掉「爬不動的官方站」
        那支    需要 title（品類關鍵字、型番），只能擺在爬取之後
      兩支都要呼叫，順序是先這支再那支。

    ★ 這支也是手動建單漏洞的補丁：手動表單的標題是客人自己打的，
      打中文泛稱（「卡盒」「BLEACH 公仔」）就繞得過關鍵字規則，
      但繞不過網域。2026-09-07 實測繞過情形：
        寶可夢官方 + 打「ポケモンカード 拡張パック」→ 擋下
        寶可夢官方 + 打「寶可夢卡盒」            → **放行**
        1kuji 官方 + 打「BLEACH 公仔」          → **放行**
    """
    host = _restricted_host(url)
    if not host:
        return None
    for domain, msg in _RESTRICTED_HOSTS:
        if _host_matches(host, domain):
            return ("hard", msg)
    return None


def detect_restricted_category(title: str, url: str = "") -> tuple[str, str] | None:
    """
    依商品標題（必要時加網址）判斷是否為受限品類。

    回傳:
        ("hard", 說明)  → 不要生成商品頁，直接回 blocked
        ("soft", 說明)  → 商品頁照建，但**不發布到線上商店**，客人只能詢問
        None            → 正常

    呼叫端務必在 scrape 完成後才呼叫（要有 title 才判斷得出來）。
    url 有給就會一起看：BEYBLADE 完全靠網域判斷，一番賞靠網域決定要不要豁免。

    ★ 判斷順序：**hard 全部先跑完，才輪到 soft**（2026-09-08 起）。
      同一個標題可能同時命中兩層（例：一番賞 + 抽選販売），
      hard 在前才不會被先命中的 soft 蓋掉。
    """
    if not title:
        return None
    haystack = f"{title} {url or ''}"
    host = _restricted_host(url)

    # ───────── hard：整件不接 ─────────
    if _is_pokemon_card(haystack):
        return ("hard", _MSG_POKEMON_CARD)

    # 純網域：官方專門站整域擋，takaratomy.co.jp 其餘商品不受影響
    if any(_host_matches(host, d) for d in _BEYBLADE_HOSTS):
        return ("hard", _MSG_BEYBLADE)

    # 官方商城：**只擋型番命中的那些**，同網域的トミカ等正常玩具照常放行
    if (any(_host_matches(host, d) for d in _BEYBLADE_MALL_HOSTS)
            and _BEYBLADE_MODEL.search(haystack)):
        return ("hard", _MSG_BEYBLADE)

    if _CHUSEN.search(haystack):
        return ("hard", _MSG_CHUSEN)

    # ───────── soft：商品頁照建，但不上架 ─────────
    # 一番賞：二手平台上的是現貨，買得到 → 完全豁免（連 soft 都不掛）
    if _ICHIBAN.search(haystack):
        if not any(_host_matches(host, d) for d in _SECONDHAND_HOSTS):
            return ("soft", _MSG_ICHIBAN_SOFT)

    if _ONEPIECE_CARD.search(haystack):
        return ("soft", _MSG_ONEPIECE)

    for domain, msg in _SOFT_HOSTS:
        if _host_matches(host, domain):
            return ("soft", msg)

    if (any(_host_matches(host, d) for d in _POKECEN_HOSTS)
            and _JUCHU_BARE.search(haystack)):
        return ("soft", _MSG_POKECEN_JUCHU)

    for pattern, reason in _SOFT_WARNING:
        if pattern.search(haystack):
            return ("soft", reason)
    return None


# ══════════════════════════════════════════════════════════════════════
# 哨兵值：落在合理範圍內、但幾乎不可能是真實售價的「魔術數字」
# ══════════════════════════════════════════════════════════════════════
#
# 為什麼範圍檢查不夠：¥999,999 比 generic 的舊上限 1,000,000 **小 1 圓**，
# 所以爬取、計價、建商品三關全過。實際發生過：
#   2026-07-31 建立的「Classical Elf T恤｜Dot-st」原價寫成 ¥999,999
#   （真價約 ¥1,499），售價 ¥1,149,998。訂單 GYT20262427 有人下單，
#   最後**是人工把 variant price 改成 ¥1,874 才收到錢** ——
#   metafield 到今天還停在 999,999（daigo.original_price_jpy 建立後永不更新）。
#
# 🔴 dot-st.com 現在還在壞（2026-09-08 查全庫）：該網域 4 件商品
#    **全部**是 ¥999,999，其中 3 件是 2026-09-08 當天建的。
#
# ─────────────────────────────────────────────────────────────────────
# 清單怎麼定的（2026-09-08 實測，Shopify 連接器 bulk，商家身分）
# ─────────────────────────────────────────────────────────────────────
# 語料 A：全庫 1,526 件帶 daigo.original_price_jpy 的商品
# 語料 B：12 個月訂單 1,302 筆／2,301 line item
#         （日期範圍 2025-09-07 → 2026-09-07，與 cancellation-2026-09 的
#           1,302／448 對得上；**不可以用 SHOPIFY_ACCESS_TOKEN 重跑，
#           那把 token 沒有 read_all_orders，會靜默只回近 60 天**）
#
#   哨兵值      語料 A 命中   語料 B 命中           判定
#   0                0 件      0                    收（全庫最低 ¥110）
#   1                0 件      0                    收
#   99,999           0 件      0                    收
#   999,999          4 件      1（GYT20262427）     收 ★ 4 件全是 dot-st 的壞值；
#                                                    那筆訂單是人工改價才成立的，
#                                                    攔下來正是要的結果
#   1,000,000        0 件      0                    收
#   9,999,999        0 件      0                    收
#
# 🔴 9,999 **不收** —— 使用者原本的清單裡有，回測證明會誤擋：
#      語料 A：9 件，全部 jp.mercari.com（GBA SP 兩台、Z/X UR 卡、
#              HUMAN MADE 馬克杯…）
#      語料 B：3 個 line item，**全部 PAID 且未取消**
#              GYT20262587 一張單裡三台 GBA 分別是
#              ¥9,999 / ¥10,000 / ¥9,999，客人實付 ¥12,198 / ¥12,199 / ¥12,198，
#              **沒有任何人工改價** —— 那就是 Mercari 上的真實開價。
#    ¥9,999 是二手平台的心理定價（壓在 ¥10,000 以下），和 ¥100 一樣是
#    合理商品價。收它會擋掉真實成交，違反「誤擋接近 0」這個前提本身。
#
# ★ 只檢查**商品層的原價**（product.price_jpy／手動表單的原價），
#   不檢查各變體的價格 —— 變體價沒有回測語料，沒驗過的東西不擋。
_SENTINEL_PRICES_JPY = (0, 1, 99_999, 999_999, 1_000_000, 9_999_999)

_MSG_SENTINEL_PRICE = (
    "這個連結抓到的日本原價是 ¥{price:,}，這種數字通常是網站的預設值或佔位數，"
    "不是真正的售價（我們遇過真價 ¥1,499 被抓成 ¥999,999 的情況）。"
    "為了避免用錯的金額幫你下單，這一筆需要人工確認過才能開商品頁。"
    "麻煩用 LINE @544kaytb 把這條連結傳給我們，確認價格後馬上幫你處理。"
)


def detect_sentinel_price(price_jpy) -> tuple[str, str] | None:
    """
    原價是不是哨兵值（看起來合理、實際上是佔位數的魔術數字）。

    回傳 ("hard", 說明) 或 None。回傳形狀刻意與 detect_restricted_category
    一致，好讓 main.py 的三個端點用同一種接線方式。

    ★ `None` 不算命中 —— 那是「沒抓到價格」，是另一種失敗，
      由呼叫端原本的 `if not product.price_jpy` 處理。
      但 `0` 算命中：0 是被明確抓到的值。
    """
    if price_jpy is None or isinstance(price_jpy, bool):
        return None
    if not isinstance(price_jpy, (int, float)):
        return None
    v = int(price_jpy)
    if v != price_jpy:            # 有小數就不是這幾個整數魔術值
        return None
    if v not in _SENTINEL_PRICES_JPY:
        return None
    return ("hard", _MSG_SENTINEL_PRICE.format(price=v))
