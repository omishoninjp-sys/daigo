"""
daytona_park.py  –  Daytona Park (FREAK'S STORE 公式通販) 爬蟲
https://www.daytona-park.com/item/{item_code}

策略：httpx 一發で取得 → HTML 靜態解析
  - variants / price / stock → <script type="application/ld+json"> の hasVariant
  - brand / category        → <meta property="etm:goods_detail"> JSON
  - title                   → <title> タグ（suffix 除去）
  - images                  → gallery-top img タグ（重複除去）
  - description             → .block-goods-tab-contents-inner p タグ

SeleniumBase 不要。JS 待ち不要。
"""
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo

# ─────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": "https://www.daytona-park.com/",
}

# Schema.org availability → in_stock bool
_IN_STOCK_VALUES = {
    "http://schema.org/InStock",
    "https://schema.org/InStock",
    "InStock",
}


class DaytonaParkMixin:

    async def _scrape_daytona_park(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        # item_code from URL
        m = re.search(r"/item/(\d+)", url)
        if not m:
            print(f"[DaytonaPark] ❌ URL 格式不符: {url}")
            return product
        item_code = m.group(1)

        # ── HTML 取得
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                verify=False,
            ) as client:
                resp = await client.get(url, headers=_HEADERS)
                if resp.status_code != 200:
                    print(f"[DaytonaPark] HTTP {resp.status_code}: {url}")
                    return product
                html = resp.text
        except Exception as e:
            print(f"[DaytonaPark] httpx 失敗: {e}")
            return product

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. JSON-LD（ProductGroup with hasVariant）──────────────
        ld_data = _parse_jsonld(soup)
        variants_raw: list[dict] = ld_data.get("hasVariant", []) if ld_data else []

        if variants_raw:
            # 価格（全 variant 同価格の前提。最初の offer から）
            first_offer = variants_raw[0].get("offers", {})
            price_val = first_offer.get("price")
            if price_val:
                try:
                    product.price_jpy = int(float(price_val))
                except (ValueError, TypeError):
                    pass

            # variants 組立
            product.variants = []
            for v in variants_raw:
                offer = v.get("offers", {})
                avail = offer.get("availability", "")
                in_stock = avail in _IN_STOCK_VALUES
                color = v.get("color", "")
                size = v.get("size", "")
                sku = v.get("sku", "")
                img = v.get("image", "")
                # image は list になる場合あり
                if isinstance(img, list):
                    img = img[0] if img else ""
                product.variants.append({
                    "color": color,
                    "size": size,
                    "sku": sku,
                    "in_stock": in_stock,
                    "image": img,
                })

            print(
                f"[DaytonaPark] JSON-LD variants: {len(product.variants)} "
                f"(price=¥{product.price_jpy:,})"
            )
        else:
            print(f"[DaytonaPark] ⚠️ JSON-LD hasVariant 未検出 → HTML fallback")

        # ── 2. etm:goods_detail meta（brand / category 補完）────────
        etm_meta = soup.find("meta", attrs={"property": "etm:goods_detail"})
        if etm_meta:
            try:
                etm = json.loads(etm_meta.get("content", "{}"))
                if not product.price_jpy:
                    p = etm.get("price")
                    if p:
                        product.price_jpy = int(p)
                product.brand = etm.get("brand_name") or etm.get("brand") or ""
                # カテゴリ（大カテゴリ / 小カテゴリ）
                cat1 = etm.get("category_name1", "")
                cat2 = etm.get("category_name2", "")
                product.category = f"{cat1}/{cat2}".strip("/")
            except Exception as e:
                print(f"[DaytonaPark] etm meta parse error: {e}")

        # ── 3. タイトル ───────────────────────────────────────────
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            # "BRAND/商品名｜Daytona Park(...)" → "BRAND/商品名"
            raw = re.sub(r"\s*[｜|]\s*Daytona Park.*$", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"\s*[｜|]\s*FREAK.*$", "", raw, flags=re.IGNORECASE).strip()
            if raw:
                product.title = raw

        # brand が未確定なら title 先頭の "BRAND/" から取る
        if not product.brand and product.title and "/" in product.title:
            bp = product.title.split("/")[0].strip()
            if bp and len(bp) < 30:
                product.brand = bp

        # ── 4. 説明文 ─────────────────────────────────────────────
        desc_block = soup.select_one(".block-goods-tab-contents-inner")
        if desc_block:
            paras = [p.get_text(separator="\n", strip=True) for p in desc_block.find_all("p")]
            desc_text = "\n\n".join(p for p in paras if p)
            if desc_text:
                product.description = desc_text[:800]

        # ── 5. 画像 ───────────────────────────────────────────────
        imgs = _extract_images(soup, item_code)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]

        # ── 6. color_id → image マッピングで variant 画像を補完 ───
        if product.variants:
            color_img_map = _build_color_image_map(soup, item_code)
            for v in product.variants:
                if not v.get("image") and v.get("color") in color_img_map:
                    v["image"] = color_img_map[v["color"]]

        # ── ログ ──────────────────────────────────────────────────
        title_short = (product.title or "")[:50]
        if product.price_jpy:
            print(
                f"[DaytonaPark] ✅ {title_short!r} | "
                f"brand={product.brand!r} | ¥{product.price_jpy:,} | "
                f"variants={len(product.variants)} | images={len(imgs)}"
            )
        else:
            print(f"[DaytonaPark] ⚠️ 価格未取得 ({title_short!r})")

        return product


# ─────────────────────────────────────────
# helpers
# ─────────────────────────────────────────

def _parse_jsonld(soup: BeautifulSoup) -> dict | None:
    """<script type="application/ld+json"> の ProductGroup を返す"""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
            # ProductGroup に hasVariant があれば採用
            if isinstance(data, dict) and data.get("hasVariant"):
                return data
        except Exception:
            continue
    return None


def _extract_images(soup: BeautifulSoup, item_code: str) -> list[str]:
    """gallery-top のスライダー img から画像 URL を重複なしで収集"""
    seen: set[str] = set()
    result: list[str] = []

    # gallery-top（メイン表示）の img を優先
    for img in soup.select(".gallery-top img[src]"):
        src = img.get("src", "")
        if src and item_code in src and src not in seen:
            seen.add(src)
            result.append(src)

    # 足りない場合 gallery-thumbs からも補完
    if len(result) < 3:
        for img in soup.select(".gallery-thumbs img[src]"):
            src = img.get("src", "")
            if src and item_code in src and src not in seen:
                seen.add(src)
                result.append(src)

    return result


def _build_color_image_map(soup: BeautifulSoup, item_code: str) -> dict[str, str]:
    """
    .block-goods-color-variation-box 内の color 画像から
    {color_name: image_url} マップを作る
    """
    color_map: dict[str, str] = {}
    for box in soup.select(".block-goods-color-variation-box"):
        img = box.select_one(".block-goods-color-variation-img img")
        name_el = box.select_one(".block-goods-color-variation-name-text")
        if img and name_el:
            src = img.get("src", "")
            name = name_el.get_text(strip=True)
            if src and name and item_code in src:
                color_map[name] = src
    return color_map
