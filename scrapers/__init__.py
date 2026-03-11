"""
GOYOUTATI DAIGO 商品爬取模組 v4.0
已拆分為獨立平台模組，透過 Mixin 組合。

新增平台方法：
1. 在 scrapers/ 下建立新的 xxx.py（繼承對應 Mixin 命名）
2. 在此檔案 import 並加入 Scraper 繼承清單
3. 在 scrape() 的 if/elif 鏈裡加入新平台的路由
"""

from scrapers.base import ProductInfo, detect_platform, normalize_url, normalize_price, detect_adult
from scrapers.driver import DriverMixin
from scrapers.generic import GenericMixin
from scrapers.amazon import AmazonMixin
from scrapers.zozotown import ZozotownMixin
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
from scrapers.daytona_park import DaytonaParkMixin  # ← 新增
from scrapers.runway import RunwayMixin


class Scraper(
    DriverMixin,
    HumanMadeMixin,
    SupremeMixin,
    GUMixin,
    VermicularMixin,
    VisvimMixin,
    GrailMixin,
    PokemonCenterMixin,
    DaytonaParkMixin,  # ← 新增
    RunwayMixin,
    GenericMixin,
    AmazonMixin,
    ZozotownMixin,
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
):
    """
    商品爬取主 class
    透過 Mixin 繼承各平台爬蟲邏輯。
    新增平台只需建立對應 Mixin 並加入繼承清單。
    """

    def __init__(self):
        DriverMixin.__init__(self)

    async def scrape(self, url: str) -> ProductInfo:
        url = normalize_url(url)
        platform = detect_platform(url)

        if platform == "zozotown":
            product = await self._scrape_zozotown(url)
        elif platform == "amazon":
            product = await self._scrape_amazon(url)
        elif platform == "uniqlo":
            product = await self._scrape_uniqlo(url)
        elif platform == "muji":
            product = await self._scrape_muji(url)
        elif platform == "beams":
            product = await self._scrape_beams(url)
        elif platform == "nijisanji":
            product = await self._scrape_nijisanji(url)
        elif platform == "palcloset":
            product = await self._scrape_palcloset(url)
        elif platform == "shopify_jp":
            product = await self._scrape_shopify_jp(url)
        elif platform == "mercari":
            product = await self._scrape_mercari(url)
        elif platform == "neighborhood":
            product = await self._scrape_neighborhood(url)
        elif platform == "wtaps":
            product = await self._scrape_wtaps(url)
        elif platform == "humanmade":
            product = await self._scrape_humanmade(url)
        elif platform == "supreme":
            product = await self._scrape_supreme(url)
        elif platform == "gu":
            product = await self._scrape_gu(url)
        elif platform == "vermicular":
            product = await self._scrape_vermicular(url)
        elif platform == "visvim":
            product = await self._scrape_visvim(url)
        elif platform == "grail":
            product = await self._scrape_grail(url)
        elif platform == "pokemoncenter":
            product = await self._scrape_pokemoncenter(url)
        elif platform == "daytona_park":  # ← 新增
            product = await self._scrape_daytona_park(url)
        elif platform == "runway":
            product = await self._scrape_runway(url)
        elif "oakley.com" in url:
            product = await self._scrape_oakley(url)
        else:
            product = await self._scrape_with_playwright(url)

        # 成人商品偵測
        if product.title and detect_adult(product):
            product.is_adult = True

        return product
