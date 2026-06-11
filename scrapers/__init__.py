"""
GOYOUTATI DAIGO 商品爬取模組 v5.2 —— Platform 介面化（ZOZO 脫 Mixin；amiami 接官方 API）

路由走 Platform registry（scrapers/platform.py），不再用 scrape() 的巨大 if/elif。
  - 已抽成真 Platform 的來源：寫一支 platform_xxx.py，在下方 register()。
  - 尚未抽的 Mixin：由 LegacyPlatform 原樣導向 _scrape_xxx，零行為變更。
Scraper class 仍是「引擎」（持有 driver + 各 Mixin 方法），供 Platform/Source 委派。

真 Platform：
  - ZozotownPlatform（platform_zozotown.py）：雅虎店 SSR，自含，已脫離 Mixin。
  - AmiamiPlatform（platform_amiami.py）：樂天 Ichiba 官方 API + amiami.jp UC fallback。
        · 官方 API 走共用 scrapers/rakuten_api.py
        · UC fallback 委派 AmiamiMixin._amiami_scrape_jp（故 AmiamiMixin 保留在引擎）
        · rakuten.co.jp 一般站維持原 RakutenMixin（樂天 API 無變體），不動。

新增一支真 Platform：
  1. 寫 scrapers/platform_xxx.py（繼承 Platform，定義 sources）
  2. 在下方 import 並 register(XxxPlatform()) —— 註冊在 LegacyPlatform 之前
"""

from scrapers.base import ProductInfo, detect_platform, normalize_url, normalize_price, detect_adult, detect_blocked
from scrapers.driver import DriverMixin
from scrapers.generic import GenericMixin
from scrapers.amazon import AmazonMixin
from scrapers.uniqlo import UniqloMixin
from scrapers.muji import MujiMixin
from scrapers.beams import BeamsMixin
from scrapers.nijisanji import NijisanjiMixin
from scrapers.palcloset import PalClosetMixin
from scrapers.shopify_jp import ShopifyJpMixin
from scrapers.mercari import MercariMixin
from scrapers.oakley import OakleyMixin
from scrapers.neighborhood import NeighborhoodMixin
from scrapers.wtaps import WtapsMixin
from scrapers.humanmade import HumanMadeMixin
from scrapers.supreme import SupremeMixin
from scrapers.gu import GUMixin
from scrapers.vermicular import VermicularMixin
from scrapers.visvim import VisvimMixin
from scrapers.grail import GrailMixin
from scrapers.pokemoncenter import PokemonCenterMixin
from scrapers.daytona_park import DaytonaParkMixin
from scrapers.runway import RunwayMixin
from scrapers.takaratomy import TakaratomyMixin
from scrapers.newbalance import NewBalanceMixin
from scrapers.adidas import AdidasMixin
from scrapers.graniph import GraniphMixin
from scrapers.fanatics import FanaticsMixin
from scrapers.ysl import YSLMixin
from scrapers.rakuten import RakutenMixin
from scrapers.ecstore import EcStoreMixin
from scrapers.bellemaison import BelleMaisonMixin
from scrapers.biccamera import BiccameraMixin
from scrapers.shimamura import ShimamuraMixin
from scrapers.npb import NpbMixin
from scrapers.disney import DisneyMixin
from scrapers.yoshidakaban import YoshidaKabanMixin
from scrapers.snkrdunk import SnkrdunkMixin
from scrapers.pbandai import PBandaiMixin
from scrapers.shoplist import ShoplistMixin
from scrapers.animate import AnimateMixin
from scrapers.mazdacollection import MazdaCollectionMixin
from scrapers.marukyukoyamaen import MarukyuKoyamaenMixin
from scrapers.amiami import AmiamiMixin  # 保留：amiami UC fallback 委派用
from scrapers.netmall import NetmallMixin
from scrapers.makeshop import MakeShopMixin

# ── Platform 介面層 ──
from scrapers.platform import register, get_platform, LegacyPlatform
from scrapers.platform_zozotown import ZozotownPlatform
from scrapers.platform_amiami import AmiamiPlatform

# 真 Platform 先註冊；LegacyPlatform 最後（catch-all）
register(ZozotownPlatform())
register(AmiamiPlatform())
register(LegacyPlatform())


class Scraper(
    DriverMixin,
    HumanMadeMixin,
    SupremeMixin,
    GUMixin,
    VermicularMixin,
    VisvimMixin,
    GrailMixin,
    PokemonCenterMixin,
    DaytonaParkMixin,
    RunwayMixin,
    TakaratomyMixin,
    NewBalanceMixin,
    AdidasMixin,
    GraniphMixin,
    FanaticsMixin,
    YSLMixin,
    RakutenMixin,
    EcStoreMixin,
    BelleMaisonMixin,
    BiccameraMixin,
    ShimamuraMixin,
    NpbMixin,
    DisneyMixin,
    YoshidaKabanMixin,
    SnkrdunkMixin,
    PBandaiMixin,
    ShoplistMixin,
    AnimateMixin,
    MazdaCollectionMixin,
    MarukyuKoyamaenMixin,
    AmiamiMixin,
    NetmallMixin,
    GenericMixin,
    AmazonMixin,
    UniqloMixin,
    MujiMixin,
    BeamsMixin,
    NijisanjiMixin,
    PalClosetMixin,
    ShopifyJpMixin,
    MercariMixin,
    OakleyMixin,
    NeighborhoodMixin,
    WtapsMixin,
    MakeShopMixin,
):
    """
    商品爬取引擎：持有 driver 與各平台 Mixin 方法。
    路由交給 Platform registry；Platform/Source 透過傳入的 engine（= 本實例）委派 Mixin 方法。
    """

    def __init__(self):
        DriverMixin.__init__(self)

    async def scrape(self, url: str) -> ProductInfo:
        url = normalize_url(url)

        # ── 封鎖網站攔截 ──
        blocked_reason = detect_blocked(url)
        if blocked_reason:
            print(f"[Scraper] 🚫 封鎖網站: {url}")
            raise ValueError(f"此網站不支援代購服務：{blocked_reason}")

        # ── Platform dispatch（取代巨大 if/elif）──
        platform = get_platform(url)
        if platform is None:
            raise RuntimeError("無可用 Platform（registry 為空）")
        product = await platform.fetch(url, self)

        # ── 成人商品偵測 ──
        if product.title and detect_adult(product):
            product.is_adult = True

        return product
