"""
SNIDEL（snidel.com / MASH USSE 平台）Platform
=============================================
商品頁伺服器 HTML 內嵌 schema.org JSON-LD，但 **JSON-LD 的 offers.price 是税拔價**
（例：71000）。頁面實際售價為 **税込**（71000×1.1=78100），來源是 USSE 平台的
JS 變數 productSpecialPrice（特價，税込）/ productNormalPrice（正常價，税込）。

因此本平台：標題／圖／品牌沿用共用 JSON-LD 解析，**價格改抓頁面税込價**覆寫。
（BookOff 等「JSON-LD 即税込」的站不受影響，維持用共用解析。）

MASH 集團多品牌（gelato pique、FRAY I.D、Mila Owen…）共用同一套 USSE 平台與
同樣的 productNormalPrice/SpecialPrice 變數，若要一併支援：把網域加進 hosts，
parse_usse 可直接沿用。

註冊（scrapers/__init__.py）：
  from scrapers.platform_snidel import SnidelPlatform
  register(SnidelPlatform())          # LegacyPlatform 之前
"""
import re

from scrapers.base import ProductInfo
from scrapers.jsonld import (
    parse_jsonld_product,
    JsonLdHttpxSource,
    JsonLdSeleniumSource,
    JsonLdPlatform,
)

# USSE 平台的税込價 JS 變數
_SPECIAL_RE = re.compile(r"productSpecialPrice\s*=\s*'([0-9,]*)'")
_NORMAL_RE = re.compile(r"productNormalPrice\s*=\s*'([0-9,]+)'")


def _digits(s):
    s = re.sub(r"[^0-9]", "", s or "")
    return int(s) if s else None


def _usse_taxed_price(html: str):
    """USSE 頁面税込售價：優先特價，否則正常價。"""
    m = _SPECIAL_RE.search(html)
    if m:
        v = _digits(m.group(1))
        if v:
            return v
    m = _NORMAL_RE.search(html)
    if m:
        v = _digits(m.group(1))
        if v:
            return v
    return None


def parse_usse(html: str, url: str = ""):
    """
    SNIDEL/USSE：標題/圖/品牌用 JSON-LD，價格改用頁面税込價覆寫。
    找不到税込價時退回 JSON-LD 的税拔價（保底，理論上少見）。
    """
    product = parse_jsonld_product(html, url)   # 帶回 title/image/brand（price=税拔）
    taxed = _usse_taxed_price(html)
    if product is None:
        if not taxed:
            return None
        product = ProductInfo(source_url=url)
    if taxed:
        product.price_jpy = taxed               # 覆寫成税込
    return product


class SnidelPlatform(JsonLdPlatform):
    id = "snidel"
    hosts = ("snidel.com",)
    sources = [
        JsonLdHttpxSource(parser=parse_usse, tag="SNIDEL"),
        JsonLdSeleniumSource(parser=parse_usse, tag="SNIDEL"),
    ]
