"""
runway.py  –  RUNWAY channel (runway-webstore.com) 爬蟲
https://runway-webstore.com/ap/item/i/m/{item_id}

策略：httpx 一發取得 → JSON-LD hasVariant 解析
  - variants / price / stock → <script type="application/ld+json"> の hasVariant
  - brand                   → JSON-LD brand.name（ブランド名）
  - title                   → JSON-LD name
  - description             → JSON-LD description
  - images                  → variant image + img[src*=itemimg] 補完
"""
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": "https://runway-webstore.com/",
}

# 在庫ありとみなす availability 値
_IN_STOCK_VALUES = {
    "http://schema.org/InStock",
    "https://schema.org/InStock",
    "InStock",
    "http://schema.org/PreOrder",
    "https://schema.org/PreOrder",
    "PreOrder",
}


class RunwayMixin:

    async def _scrape_runway(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        # fragment 除去
        clean_url = url.split("#")[0].strip()

        # ── HTML 取得
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                verify=False,
            ) as client:
                resp = await client.get(clean_url, headers=_HEADERS)
                if resp.status_code != 200:
                    print(f"[Runway] HTTP {resp.status_code}: {clean_url}")
                    return product
                html = resp.text
        except Exception as e:
            print(f"[Runway] httpx 失敗: {type(e).__name__}: {e}")
            return product

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. JSON-LD（ProductGroup with hasVariant）
        ld_data = _parse_jsonld(soup)
        if not ld_data:
            print(f"[Runway] ⚠️ JSON-LD 未検出")
            return product

        variants_raw = ld_data.get("hasVariant", [])

        # タイトル
        product.title = ld_data.get("name", "")

        # ブランド
        brand = ld_data.get("brand", {})
        if isinstance(brand, dict):
            product.brand = brand.get("name", "")
        elif isinstance(brand, str):
            product.brand = brand

        # 説明文
        product.description = ld_data.get("description", "")[:800]

        # ── 2. variants 組立
        if variants_raw:
            first_offer = variants_raw[0].get("offers", {})
            price_val = first_offer.get("price")
            if price_val:
                try:
                    product.price_jpy = int(float(price_val))
                except (ValueError, TypeError):
                    pass

            product.variants = []
            for v in variants_raw:
                offer = v.get("offers", {})
                avail = offer.get("availability", "")
                in_stock = avail in _IN_STOCK_VALUES
                img = v.get("image", "")
                # "//" で始まる URL に https: を補完
                if img.startswith("//"):
                    img = "https:" + img
                # サムネ後缀（-240 等）除去して原寸に
                img = re.sub(r"-\d+\.jpg", ".jpg", img)
                product.variants.append({
                    "color": v.get("color", ""),
                    "size": v.get("size", ""),
                    "sku": v.get("sku", ""),
                    "in_stock": in_stock,
                    "image": img,
                })

            print(f"[Runway] JSON-LD variants: {len(product.variants)} (price=¥{product.price_jpy:,})")
        else:
            print(f"[Runway] ⚠️ hasVariant 未検出")

        # ── 3. 画像収集
        imgs = _extract_images(soup)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]
        elif product.variants and product.variants[0].get("image"):
            product.image_url = product.variants[0]["image"]

        # ── ログ
        title_short = (product.title or "")[:50]
        if product.price_jpy:
            print(
                f"[Runway] ✅ {title_short!r} | "
                f"brand={product.brand!r} | ¥{product.price_jpy:,} | "
                f"variants={len(product.variants)} | images={len(imgs)}"
            )
        else:
            print(f"[Runway] ⚠️ 価格未取得 ({title_short!r})")

        return product


# ─────────────────────────────────────────
# helpers
# ─────────────────────────────────────────

def _parse_jsonld(soup: BeautifulSoup) -> dict | None:
    """hasVariant を含む ProductGroup を返す"""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
            # リスト形式の場合
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("hasVariant"):
                        return item
            elif isinstance(data, dict) and data.get("hasVariant"):
                return data
        except Exception:
            continue
    return None


def _extract_images(soup: BeautifulSoup) -> list[str]:
    """itemimg-rcw.runway-webstore.net の画像を収集（重複除去）"""
    seen: set[str] = set()
    result: list[str] = []

    for img in soup.find_all("img", src=True):
        src = img["src"]
        # "//" → "https:"
        if src.startswith("//"):
            src = "https:" + src
        # itemimg ドメインのみ対象、サムネ（-240）除外して元サイズ取得
        if "itemimg-rcw.runway-webstore.net" not in src:
            continue
        # -240.jpg → .jpg（元サイズ）に変換
        src = re.sub(r"-\d+\.jpg", ".jpg", src)
        if src not in seen:
            seen.add(src)
            result.append(src)

    return result
