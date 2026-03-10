"""
GU (gu-global.com) 爬蟲 Mixin
Fast Retailing 系統，與 Uniqlo 相同 API 架構

API 端點（無需 JS 渲染）：
  商品詳情：https://www.gu-global.com/jp/api/commerce/v5/ja/products/{productCode}/details.json
  價格庫存：https://www.gu-global.com/jp/api/commerce/v5/ja/products/{productCode}/prices.json
  圖片：https://image.uniqlo.com/GU/ST3/jp/imagesGoods/{productCode}/{colorCode}_sub1.jpg

URL 格式：https://www.gu-global.com/jp/ja/products/E352935-000/00?colorDisplayCode=84&sizeDisplayCode=004
  → productCode = "E352935-000"
"""
import re
import httpx
from urllib.parse import urlparse, parse_qs

from scrapers.base import ProductInfo

API_BASE = "https://www.gu-global.com/jp/api/commerce/v5/ja"
IMG_BASE = "https://image.uniqlo.com/GU/ST3/jp/imagesGoods"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.gu-global.com/",
}


def _extract_product_code(url: str) -> str | None:
    """從 URL 抽出商品番號，例如 E352935-000"""
    m = re.search(r'/products/([A-Z0-9\-]+)/', url)
    return m.group(1) if m else None


def _img_url(product_code: str, color_code: str, suffix: str = "sub1") -> str:
    """組合 GU 圖片 URL"""
    # GU 圖片路徑：/GU/ST3/jp/imagesGoods/{productCode}/{colorCode}_sub1.jpg
    # 也有 _main.jpg / _sub2.jpg ... _sub9.jpg
    return f"{IMG_BASE}/{product_code}/{color_code}_{suffix}.jpg"


class GUMixin:

    async def _scrape_gu(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="GU")

        product_code = _extract_product_code(url)
        if not product_code:
            print(f"[GU] ❌ 無法解析商品番號: {url}")
            return product

        # URL 中的 colorDisplayCode 參數（決定預設顏色）
        qs = parse_qs(urlparse(url).query)
        default_color = qs.get("colorDisplayCode", [None])[0]

        print(f"[GU] 商品番號: {product_code} / 預設顏色: {default_color}")

        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            verify=False,
            headers=HEADERS,
        ) as client:

            # === 1. 商品詳情 API ===
            detail_url = f"{API_BASE}/products/{product_code}/details.json"
            resp = await client.get(detail_url)
            if resp.status_code != 200:
                print(f"[GU] ❌ details API 失敗 ({resp.status_code}): {detail_url}")
                return product

            data = resp.json()
            detail = data.get("result", data)

            # 標題
            product.title = detail.get("name", "").strip()

            # 描述
            product.description = re.sub(
                r'<[^>]+>', '',
                detail.get("longDescription") or detail.get("catchCopy") or ""
            )[:500]

            # 顏色清單
            colors = detail.get("colors", [])  # [{code, name, ...}]
            if not colors:
                # fallback: chips 結構
                colors = detail.get("chips", {}).get("colors", [])

            # === 2. 價格 & 庫存 API ===
            price_url = f"{API_BASE}/products/{product_code}/prices.json"
            price_resp = await client.get(price_url)
            price_data = {}
            if price_resp.status_code == 200:
                price_data = price_resp.json().get("result", {})

            # 主價格（取 regularPrice，fallback minPrice）
            base_price = 0
            prices_list = price_data.get("summary", {})
            if isinstance(prices_list, dict):
                base_price = prices_list.get("minPrice") or prices_list.get("regularPrice") or 0
            if not base_price:
                # fallback: detail API 內的 price
                base_price = detail.get("minPrice") or detail.get("price") or 0
            if isinstance(base_price, str):
                base_price = int(re.sub(r'[^0-9]', '', base_price) or 0)

            product.price_jpy = int(base_price) if base_price else None

            # === 3. 組合 variants（顏色 × 尺寸）===
            # 庫存資訊：price_data.get("stocks") 或 detail 內
            stocks = price_data.get("stocks", [])
            # stocks 結構通常是 [{colorCode, sizeCode, quantity, ...}]
            stock_map = {}
            for s in stocks:
                k = (str(s.get("colorCode", "")), str(s.get("sizeCode", "")))
                stock_map[k] = int(s.get("quantity", 0)) > 0

            # 尺寸清單
            sizes = detail.get("sizes", [])
            if not sizes:
                sizes = detail.get("chips", {}).get("sizes", [])

            seen_colors = set()
            for color in colors:
                color_code = str(color.get("code", color.get("displayCode", "")))
                color_name = color.get("name", color_code)

                # 顏色圖片（main 圖）
                color_img = _img_url(product_code, color_code, "main")

                # 若沒有尺寸，直接一個 variant
                if not sizes:
                    in_stock = stock_map.get((color_code, ""), True)
                    product.variants.append({
                        "color":    color_name,
                        "size":     "",
                        "sku":      f"{product_code}-{color_code}",
                        "price":    product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image":    color_img,
                    })
                else:
                    for size in sizes:
                        size_code = str(size.get("code", size.get("displayCode", "")))
                        size_name = size.get("name", size_code)
                        in_stock = stock_map.get((color_code, size_code), True)
                        product.variants.append({
                            "color":    color_name,
                            "size":     size_name,
                            "sku":      f"{product_code}-{color_code}-{size_code}",
                            "price":    product.price_jpy or 0,
                            "in_stock": in_stock,
                            "image":    color_img,
                        })

                if color_code not in seen_colors:
                    seen_colors.add(color_code)

            # === 4. 圖片（預設顏色優先，否則第一個顏色）===
            first_color_code = default_color or (str(colors[0].get("code", colors[0].get("displayCode", ""))) if colors else "")
            if first_color_code:
                product.image_url = _img_url(product_code, first_color_code, "main")
                product.extra_images = [
                    _img_url(product_code, first_color_code, f"sub{i}")
                    for i in range(1, 8)
                ]

        print(
            f"[GU] ✅ {product.title} / ¥{product.price_jpy} / "
            f"{len(product.variants)} variants / colors={len(seen_colors)}"
        )
        return product
