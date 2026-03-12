"""
scrapers/adidas.py — Adidas Japan (adidas.jp) Mixin
回傳 ProductInfo，與其他平台 Mixin 完全相同的介面。

策略：
  1. 先打隱藏 REST API  /api/products/{model}
                        /api/products/{model}/availability
  2. API 失敗 → Playwright fallback（已由 DriverMixin 提供）
"""

import re
import logging
import requests
from typing import Optional

from scrapers.base import ProductInfo, normalize_price

logger = logging.getLogger(__name__)

# ── Adidas JP 隱藏 API ────────────────────────────────────────
_API_PRODUCT  = "https://www.adidas.jp/api/products/{model}"
_API_AVAIL    = "https://www.adidas.jp/api/products/{model}/availability"
_REQ_HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": "https://www.adidas.jp/",
}


def _extract_model(url: str) -> Optional[str]:
    """從 URL 抽出 model number，例如 KQ5489。"""
    m = re.search(r"/([A-Z]{2}[0-9]{4})\.html", url)
    return m.group(1) if m else None


def _hires(src: str) -> str:
    """把任何 adidas assets URL 換成 2000px 高解析版。"""
    return re.sub(r"[hw]_\d+", "h_2000", src)


# ── API 層 ────────────────────────────────────────────────────

def _fetch_json(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, headers=_REQ_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[AdidasJP] API fetch failed {url}: {e}")
        return None


def _parse_via_api(model: str, source_url: str) -> Optional[ProductInfo]:
    """
    用 API JSON 組裝 ProductInfo。
    回傳 None 代表 API 無效，交由 Playwright fallback。
    """
    data = _fetch_json(_API_PRODUCT.format(model=model))
    if not data or not data.get("name"):
        return None

    title      = data.get("name", "")
    color_name = data.get("attribute_list", {}).get("color", "")
    desc       = data.get("product_description", {}).get("description", "") or ""
    price_raw  = data.get("pricing_information", {}).get("standard_price", 0)
    price_jpy  = normalize_price(price_raw) or 0

    # ── 圖片 ──
    image_url    = ""
    extra_images = []
    for view in data.get("view_list", []):
        src = view.get("image_url", "")
        if not src:
            continue
        src = _hires(src)
        if not image_url:
            image_url = src
        else:
            extra_images.append(src)

    # fallback：assets CDN 固定格式
    if not image_url:
        image_url = (
            f"https://assets.adidas.com/images/h_2000,f_auto,q_auto,"
            f"fl_lossy,c_fill,g_auto/{model}_21_model.jpg"
        )

    # ── 庫存：先打 availability endpoint ──
    in_stock_sizes: set[str] = set()
    avail_data = _fetch_json(_API_AVAIL.format(model=model))
    if avail_data:
        for v in avail_data.get("variation_list", []):
            if int(v.get("availability", 0)) > 0:
                in_stock_sizes.add(v.get("size", ""))

    # fallback：從 product JSON 自帶的 variation_list
    if not in_stock_sizes:
        for v in data.get("variation_list", []):
            if int(v.get("availability", 0)) > 0:
                in_stock_sizes.add(v.get("size", ""))

    # ── variants ──
    seen_sizes: list[str] = []
    variants:   list[dict] = []
    for v in data.get("variation_list", []):
        sz = v.get("size", "")
        if not sz or sz in seen_sizes:
            continue
        seen_sizes.append(sz)
        variants.append({
            "option1":  color_name or "Default",
            "option2":  sz,
            "price":    price_jpy,
            "in_stock": sz in in_stock_sizes,
        })

    # 沒有任何尺寸資訊 → API 資料不完整，fallback
    if not variants:
        return None

    overall_in_stock = any(v["in_stock"] for v in variants)
    full_title = f"{title} — {color_name}" if color_name else title

    return ProductInfo(
        title        = full_title,
        price_jpy    = price_jpy,
        image_url    = image_url,
        extra_images = extra_images,
        description  = desc,
        source_url   = source_url,
        brand        = "adidas",
        currency     = "JPY",
        variants     = variants,
        in_stock     = overall_in_stock,
    )


# ── Mixin ─────────────────────────────────────────────────────

class AdidasMixin:
    """
    Adidas Japan (adidas.jp) 商品爬蟲 Mixin。
    在 __init__.py 的 Scraper 繼承清單加入此 Mixin 即可使用。
    """

    async def _scrape_adidas(self, url: str) -> ProductInfo:
        model = _extract_model(url)

        # ── Step 1：API ──
        if model:
            logger.info(f"[AdidasJP] API scrape: model={model}")
            result = _parse_via_api(model, url)
            if result and result.is_valid:
                logger.info(f"[AdidasJP] API OK → {result.title} ¥{result.price_jpy}")
                return result
            logger.info(f"[AdidasJP] API incomplete, fallback to Playwright")
        else:
            logger.warning(f"[AdidasJP] Cannot extract model from URL: {url}")

        # ── Step 2：Playwright fallback ──
        # 使用 GenericMixin 的 _scrape_with_playwright（DriverMixin 提供）
        logger.info(f"[AdidasJP] Playwright fallback: {url}")
        product = await self._scrape_with_playwright(url)

        # Playwright 取回後補 brand
        if product and product.title:
            product.brand = "adidas"

        return product
