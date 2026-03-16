"""
共用基礎模組：ProductInfo、detect_platform、工具函數
"""
import re
from urllib.parse import urlparse
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
    if "takaratomy.co.jp" in host:
        return "takaratomy"
    if "newbalance.jp" in host:
        return "newbalance"
    if "adidas.jp" in host:
        return "adidas"
    if "graniph.com" in host:
        return "graniph"
    if "fanatics.jp" in host:
        return "fanatics"
    if "mercari.com" in host or "jp.mercari.com" in host:
        return "mercari"
    if "ec-store.net" in host:
        return "ecstore"
    if "bellemaison.jp" in host:
        return "bellemaison"
    if "biccamera.com" in host:
        return "biccamera"
    if "shop-shimamura.com" in host:
        return "shimamura"
    return "generic"


# ============ 工具函數 ============

def normalize_url(url: str) -> str:
    shopserve_m = re.match(r'(https?://[^/]+)/smp/item/(.+)', url)
    if shopserve_m:
        normalized = f"{shopserve_m.group(1)}/SHOP/{shopserve_m.group(2)}"
        print(f"[Normalize] ShopServe 手機版 → PC 版: {url} → {normalized}")
        return normalized
    return url


def normalize_price(price) -> int | None:
    if isinstance(price, (int, float)):
        return int(price)
    if isinstance(price, str):
        cleaned = re.sub(r'[^0-9.]', '', price)
        return int(float(cleaned)) if cleaned else None
    return None


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
