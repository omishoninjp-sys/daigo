"""
daytona_park.py  –  Daytona Park (FREAK'S STORE 公式通販) 爬蟲
https://www.daytona-park.com/item/{item_code}

Akamai CDN がデータセンター IP からの httpx をブロックするため
Playwright でページ取得 → HTML を BeautifulSoup で解析。

データ取得元：
  - variants / price / stock → <script type="application/ld+json"> の hasVariant
  - brand / category        → <meta property="etm:goods_detail"> JSON
  - title                   → <title> タグ（suffix 除去）
  - images                  → .gallery-top img
  - description             → .block-goods-tab-contents-inner p
"""
import json
import re

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo

# Schema.org availability → in_stock bool
_IN_STOCK_VALUES = {
    "http://schema.org/InStock",
    "https://schema.org/InStock",
    "InStock",
}


class DaytonaParkMixin:

    async def _scrape_daytona_park(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        # # fragment 除去
        clean_url = url.split("#")[0].strip()

        # item_code from URL
        m = re.search(r"/item/(\d+)", clean_url)
        if not m:
            print(f"[DaytonaPark] ❌ URL 格式不符: {clean_url}")
            return product
        item_code = m.group(1)

        # ── Playwright でページ取得（Akamai 回避）
        html = await self._playwright_get_html(clean_url)
        if not html:
            print(f"[DaytonaPark] ❌ Playwright HTML 取得失敗")
            return product

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. JSON-LD（hasVariant）
        ld_data = _parse_jsonld(soup)
        variants_raw: list[dict] = ld_data.get("hasVariant", []) if ld_data else []

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
                if isinstance(img, list):
                    img = img[0] if img else ""
                product.variants.append({
                    "color": v.get("color", ""),
                    "size": v.get("size", ""),
                    "sku": v.get("sku", ""),
                    "in_stock": in_stock,
                    "image": img,
                })

            print(f"[DaytonaPark] JSON-LD variants: {len(product.variants)} (price=¥{product.price_jpy:,})")
        else:
            print(f"[DaytonaPark] ⚠️ JSON-LD hasVariant 未検出")

        # ── 2. etm:goods_detail meta
        etm_meta = soup.find("meta", attrs={"property": "etm:goods_detail"})
        if etm_meta:
            try:
                etm = json.loads(etm_meta.get("content", "{}"))
                if not product.price_jpy:
                    p = etm.get("price")
                    if p:
                        product.price_jpy = int(p)
                product.brand = etm.get("brand_name") or etm.get("brand") or ""
                cat1 = etm.get("category_name1", "")
                cat2 = etm.get("category_name2", "")
                product.category = f"{cat1}/{cat2}".strip("/")
            except Exception as e:
                print(f"[DaytonaPark] etm meta parse error: {e}")

        # ── 3. タイトル
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            raw = re.sub(r"\s*[｜|]\s*Daytona Park.*$", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"\s*[｜|]\s*FREAK.*$", "", raw, flags=re.IGNORECASE).strip()
            if raw:
                product.title = raw

        if not product.brand and product.title and "/" in product.title:
            bp = product.title.split("/")[0].strip()
            if bp and len(bp) < 30:
                product.brand = bp

        # ── 4. 説明文
        desc_block = soup.select_one(".block-goods-tab-contents-inner")
        if desc_block:
            paras = [p.get_text(separator="\n", strip=True) for p in desc_block.find_all("p")]
            desc_text = "\n\n".join(p for p in paras if p)
            if desc_text:
                product.description = desc_text[:800]

        # ── 5. 画像
        imgs = _extract_images(soup, item_code)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]

        # ── 6. variant 画像補完
        if product.variants:
            color_img_map = _build_color_image_map(soup, item_code)
            for v in product.variants:
                if not v.get("image") and v.get("color") in color_img_map:
                    v["image"] = color_img_map[v["color"]]

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

    async def _playwright_get_html(self, url: str) -> str:
        """
        Playwright でページ HTML を取得して返す。
        DriverMixin の self._driver / self._page を流用。
        既存の _scrape_with_playwright と同じ仕組み。
        """
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="ja-JP",
                    extra_http_headers={
                        "Accept-Language": "ja-JP,ja;q=0.9",
                    },
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # JSON-LD が <head> にあるので domcontentloaded で十分
                html = await page.content()
                await browser.close()
                return html
        except Exception as e:
            print(f"[DaytonaPark] Playwright 失敗: {type(e).__name__}: {e}")
            return ""


# ─────────────────────────────────────────
# helpers
# ─────────────────────────────────────────

def _parse_jsonld(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, dict) and data.get("hasVariant"):
                return data
        except Exception:
            continue
    return None


def _extract_images(soup: BeautifulSoup, item_code: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for img in soup.select(".gallery-top img[src]"):
        src = img.get("src", "")
        if src and item_code in src and src not in seen:
            seen.add(src)
            result.append(src)
    if len(result) < 3:
        for img in soup.select(".gallery-thumbs img[src]"):
            src = img.get("src", "")
            if src and item_code in src and src not in seen:
                seen.add(src)
                result.append(src)
    return result


def _build_color_image_map(soup: BeautifulSoup, item_code: str) -> dict[str, str]:
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
