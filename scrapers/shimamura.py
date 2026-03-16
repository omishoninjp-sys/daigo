"""
しまむら / バースデイ (shop-shimamura.com) 爬蟲 Mixin
URL 格式：https://www.shop-shimamura.com/item/{jancode}/?cl={colorcode}

HTML 結構（靜態，httpx 可抓）：
  dl.stock
    dt.stock__thumb  ← 一個顏色區塊
      img[src]       ← 顏色圖片
      span           ← 顏色名
    dd.stock__detail
      ul.stock__list
        li.stock__item  ← 一個尺寸
          p.stock__size  → "70cm / 予約受付中"
          p.stock__price → "1,490円"
          button.evt-add-cart（存在 = 可加購）
          button.stock__btn（無 = 缺貨）
"""

import re
import httpx
from bs4 import BeautifulSoup
from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo, normalize_price


class ShimamuraMixin:

    async def _scrape_shimamura(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ja-JP,ja;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.shop-shimamura.com/",
            }
            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=True, headers=headers) as client:
                resp = await client.get(url)
                html = resp.text

            if len(html) < 3000:
                print(f"[Shimamura] ❌ HTML 太短: {url}")
                return product

            soup = BeautifulSoup(html, "html.parser")

            # 標題
            h1 = soup.select_one("h1.item-detail__name, h1.itemName, h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                og = soup.find("meta", property="og:title")
                if og:
                    product.title = og.get("content", "")

            # OG 圖片（主圖）
            og_img = soup.find("meta", property="og:image")
            if og_img:
                product.image_url = og_img.get("content", "")

            # Variants：dt(顏色) + dd(尺寸列表)
            variants = []
            prices = []
            seen_imgs = set()

            dl = soup.select_one("dl.stock")
            if dl:
                dts = dl.select("dt.stock__thumb")
                dds = dl.select("dd.stock__detail")

                for dt, dd in zip(dts, dds):
                    # 顏色名
                    color_span = dt.select_one("span")
                    color_raw = color_span.get_text(strip=True) if color_span else ""
                    # 移除色碼數字（如「オフホワイト 311」→「オフホワイト」）
                    color = re.sub(r'\s+\d{3}$', '', color_raw).strip()

                    # 顏色圖片
                    color_img_el = dt.select_one("img")
                    color_img = color_img_el.get("src", "") if color_img_el else ""
                    if color_img and color_img not in seen_imgs:
                        seen_imgs.add(color_img)
                        if not product.image_url:
                            product.image_url = color_img
                        elif color_img != product.image_url:
                            product.extra_images.append(color_img)

                    # 各尺寸
                    for li in dd.select("li.stock__item"):
                        size_el = li.select_one("p.stock__size")
                        size_raw = size_el.get_text(strip=True) if size_el else ""
                        # 取 "/" 前面的尺寸
                        size = size_raw.split("/")[0].strip() if "/" in size_raw else size_raw.strip()

                        price_el = li.select_one("p.stock__price")
                        price_str = price_el.get_text(strip=True) if price_el else ""
                        # 取第一個價格（稅抜き）
                        price_m = re.search(r'([\d,]+)円', price_str)
                        price = normalize_price(price_m.group(1)) if price_m else None
                        if price:
                            prices.append(price)

                        # 庫存：有 カートに入れる button = 有庫存
                        cart_btn = li.select_one("button.evt-add-cart")
                        in_stock = cart_btn is not None

                        variants.append({
                            "color":    color,
                            "size":     size,
                            "price":    price,
                            "in_stock": in_stock,
                            "image":    color_img,
                        })

            if variants:
                product.variants = variants
                product.in_stock = any(v["in_stock"] for v in variants)
                if prices:
                    product.price_jpy = min(prices)

            # 價格 fallback
            if not product.price_jpy:
                price_el = soup.select_one(".item-detail__price, .price, [class*='price']")
                if price_el:
                    m = re.search(r'([\d,]+)円', price_el.get_text())
                    if m:
                        product.price_jpy = normalize_price(m.group(1))

            # 品牌
            brand_el = soup.select_one(".item-detail__brand, [class*='brand']")
            if brand_el:
                product.brand = brand_el.get_text(strip=True)

            print(
                f"[Shimamura] ✅ {product.title[:40]} / ¥{product.price_jpy} / "
                f"variants={len(product.variants)}"
            )

        except Exception as e:
            import traceback
            print(f"[Shimamura] ❌ 解析失敗: {e}")
            print(traceback.format_exc())

        return product
