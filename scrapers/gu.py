"""
GU (gu-global.com) 爬蟲 Mixin

正確 API 端點：
  https://www.gu-global.com/jp/api/commerce/v5/ja/products?productIds={productId}&imageRatio=3x4

URL 格式：
  https://www.gu-global.com/jp/ja/products/E359683-000/00?colorDisplayCode=84&sizeDisplayCode=004
  → productId = "E359683-000"

JSON 結構：
  result.items[0]
    .name           → 標題
    .prices.base.value → 價格（JPY）
    .colors[]       → [{displayCode, name}, ...]
    .sizes[]        → [{name}, ...]
    .images.main    → {colorDisplayCode: {image: url}, ...}
    .images.sub     → [{image: url}, ...]
"""
import re
import httpx
from urllib.parse import urlparse, parse_qs

from scrapers.base import ProductInfo

API_BASE = "https://www.gu-global.com/jp/api/commerce/v5/ja"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.gu-global.com/",
}


def _extract_product_id(url: str) -> str | None:
    """從 URL 抽出 productId，例如 E359683-000"""
    m = re.search(r'/products/([A-Z0-9\-]+)(?:/|$|\?)', url)
    return m.group(1) if m else None


# ── 爬取監控埋點（fail-safe：監控壞掉絕不影響爬取）──────────────────
# ★ 2026-09-02 補。原本三條失敗路徑只 print 就 return，record() 收到的是
#   一個空的 ProductInfo（error=None），於是 JSONL 只留下
#   failure_kind='other'、error_brief='' 的空紀錄 —— 三條分辨不出來，
#   而它們的處置完全不同：
#     403        → 機房 IP 被擋，要住宅代理
#     items 為空  → 商品下架或 API 改版
#     番號解析失敗 → 客人貼的 URL 格式不對
def _note_error(error, where=""):
    try:
        import scrape_monitor
        scrape_monitor.note_error(error, where)
    except Exception:
        pass


def _note_http(status, body="", final_url=""):
    """★ 少了這個，403/429 會被 classify_failure 分成 other 而不是 blocked ——
    blocked 這個分類存在的目的就是回答「要不要買住宅代理」。"""
    try:
        import scrape_monitor
        scrape_monitor.note_http(status, body, final_url)
    except Exception:
        pass


class GUMixin:

    async def _scrape_gu(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="GU")

        product_id = _extract_product_id(url)
        if not product_id:
            print(f"[GU] ❌ 無法解析商品番號: {url}")
            _note_error("商品番號解析失敗（URL 不含 /products/XXXXXX-000）", "GU")
            return product

        # URL 中的 colorDisplayCode（決定主圖顏色）
        qs = parse_qs(urlparse(url).query)
        default_color = qs.get("colorDisplayCode", [None])[0]

        print(f"[GU] 商品番號: {product_id} / 預設顏色: {default_color}")

        api_url = f"{API_BASE}/products?productIds={product_id}&imageRatio=3x4"

        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            verify=False,
            headers=HEADERS,
        ) as client:
            resp = await client.get(api_url)

        _note_http(resp.status_code, resp.text[:500], str(resp.url))
        if resp.status_code != 200:
            print(f"[GU] ❌ API 失敗 ({resp.status_code}): {api_url}")
            _note_error(f"內部 API HTTP {resp.status_code}", "GU")
            return product

        data = resp.json()
        items = data.get("result", {}).get("items", [])
        if not items:
            print(f"[GU] ❌ items 為空: {product_id}")
            _note_error(f"API 回 200 但 result.items 為空（{product_id} 下架或 API 改版）", "GU")
            return product

        item = items[0]

        # === 基本資訊 ===
        product.title = item.get("name", "").strip()
        product.price_jpy = int(item.get("prices", {}).get("base", {}).get("value", 0) or 0)

        # === 顏色 & 尺寸 ===
        colors = item.get("colors", [])   # [{displayCode, name}, ...]
        sizes  = item.get("sizes", [])    # [{name}, ...]
        images_main = item.get("images", {}).get("main", {})  # {colorCode: {image: url}}
        images_sub  = item.get("images", {}).get("sub", [])   # [{image: url}, ...]

        # === 組合 variants ===
        for color in colors:
            color_code = str(color.get("displayCode", ""))
            color_name = color.get("name", color_code)
            color_img  = (images_main.get(color_code) or {}).get("image", "")

            if not sizes:
                product.variants.append({
                    "color":    color_name,
                    "size":     "",
                    "sku":      f"{product_id}-{color_code}",
                    "price":    product.price_jpy,
                    "in_stock": True,
                    "image":    color_img,
                })
            else:
                for size in sizes:
                    size_name = size.get("name", "")
                    product.variants.append({
                        "color":    color_name,
                        "size":     size_name,
                        "sku":      f"{product_id}-{color_code}-{size_name}",
                        "price":    product.price_jpy,
                        "in_stock": True,
                        "image":    color_img,
                    })

        # === 主圖 & 附圖 ===
        first_color = default_color or (str(colors[0].get("displayCode", "")) if colors else "")
        if first_color and first_color in images_main:
            product.image_url = images_main[first_color]["image"]

        product.extra_images = [s["image"] for s in images_sub if s.get("image")]

        print(
            f"[GU] ✅ {product.title} / ¥{product.price_jpy} / "
            f"{len(product.variants)} variants / {len(colors)} colors × {len(sizes)} sizes"
        )
        return product
