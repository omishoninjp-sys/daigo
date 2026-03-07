"""
向下相容層 - main.py 使用 `from scraper import Scraper, ProductInfo` 不需改動
實際邏輯已移至 scrapers/ 套件。
"""
from scrapers import Scraper, ProductInfo

__all__ = ["Scraper", "ProductInfo"]
