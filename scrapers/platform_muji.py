"""
MUJI 無印良品（muji.com）Platform
=================================
商品頁伺服器 HTML 內嵌 schema.org JSON-LD（Product/offers），價格為税込、
含該變體的 availability，沿用共用 scrapers/jsonld.py 預設解析即可。

單變體策略（見對話決議 A）：
  MUJI 把每個「顏色×尺寸」都做成獨立 GTIN 商品頁（cmdty/detail/{jan}），
  變體矩陣與逐一尺寸庫存在 Next.js RSC（self.__next_f）串流裡、不易穩定取得。
  因此本平台「貼哪個 GTIN 就抓哪個」：正確價格 + 該變體庫存，不組尺寸/顏色選擇器。
  客人貼他要的那件即可；需要其他尺寸就貼該尺寸的網址。

註冊（scrapers/__init__.py）：
  from scrapers.platform_muji import MujiPlatform
  register(MujiPlatform())          # LegacyPlatform 之前
"""
from scrapers.jsonld import JsonLdPlatform


class MujiPlatform(JsonLdPlatform):
    id = "muji"
    hosts = ("muji.com",)
    # sources 沿用 JsonLdPlatform 預設（httpx + Selenium 退路，parse_jsonld_product）
