"""
楽天市場 (item.rakuten.co.jp) 爬蟲 Mixin
- httpx 直接抓，手動處理 EUC-JP 編碼
- 樂天商品通常無結構化 variants（各 SKU 是獨立 item），當單品處理
"""
import re
import json

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


class RakutenMixin:

    async def _scrape_rakuten(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(url)

                # 樂天頁面是 EUC-JP，手動解碼
                try:
                    html = resp.content.decode("euc-jp", errors="replace")
                except Exception:
                    html = resp.text

                # ── 商品名稱（og:title）──
                title_m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not title_m:
                    title_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html)
                if title_m:
                    title = title_m.group(1).strip()
                    # 去掉「| 店名」或「：店名」suffix（全形/半形冒號都處理）
                    title = re.split(r'[|｜：:]\s*\S+(?:楽天|店|ショップ)', title)[0].strip()
                    title = re.sub(r'\s*[-－]\s*楽天市場.*$', '', title).strip()
                    product.title = title

                # ── 価格（複数 pattern 試行）──
                price_patterns = [
                    r'class="price2"[^>]*>\s*([\d,]+)\s*円',
                    r'class="price2"[^>]*>.*?([\d,]+)(?:\s*円|\s*<)',
                    r'itemprop="price"[^>]+content="(\d+)"',
                    r'"price":\s*"?(\d[\d,]+)"?',
                    r'¥\s*([\d,]+)',
                ]
                for pat in price_patterns:
                    m = re.search(pat, html)
                    if m:
                        try:
                            price = int(m.group(1).replace(",", ""))
                            if 100 <= price <= 10_000_000:
                                product.price_jpy = price
                                print(f"[Rakuten DEBUG] 價格 pattern={pat!r} → ¥{price}")
                                break
                        except Exception:
                            continue

                # ── JSON-LD fallback ──
                ld_matches = re.findall(
                    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    html, re.DOTALL
                )
                if not product.price_jpy:
                    for ld_raw in ld_matches:
                        try:
                            ld = json.loads(ld_raw)
                            if isinstance(ld, list):
                                ld = ld[0]
                            offers = ld.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0]
                            price_val = offers.get("price") or offers.get("lowPrice")
                            if price_val:
                                price = int(float(str(price_val)))
                                if 100 <= price <= 10_000_000:
                                    product.price_jpy = price
                                    print(f"[Rakuten DEBUG] JSON-LD 價格 → ¥{price}")
                                    break
                        except Exception:
                            continue

                # ── 主圖（og:image）──
                img_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not img_m:
                    img_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
                if img_m:
                    product.image_url = re.sub(r'\?_ex=\d+x\d+.*$', '', img_m.group(1).strip())

                # ── 描述（og:description）──
                desc_m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not desc_m:
                    desc_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', html)
                if desc_m:
                    product.description = desc_m.group(1).strip()[:500]

                # ── 庫存 ──
                # 樂天：有「カートに入れる」input/button = 有庫存
                # 只有明確找不到購物車 AND 有缺貨文字才判為缺貨
                has_cart = bool(re.search(
                    r'value="[^"]*カートに入れる[^"]*"|買い物かごに入れる|name="cartinsert"',
                    html
                ))
                has_soldout_text = bool(re.search(r'品切れ|売り切れ|SOLD\s*OUT', html))
                if has_cart:
                    product.in_stock = True
                elif has_soldout_text:
                    product.in_stock = False
                else:
                    product.in_stock = True  # 預設有庫存，讓客人下單再確認

                # ── brand（JSON-LD → fallback 店名）──
                for ld_raw in ld_matches:
                    try:
                        ld = json.loads(ld_raw)
                        if isinstance(ld, list):
                            ld = ld[0]
                        brand = ld.get("brand", "")
                        if isinstance(brand, dict):
                            brand = brand.get("name", "")
                        if brand:
                            product.brand = str(brand)
                            break
                    except Exception:
                        continue
                # brand 仍空 → 從 URL 取店家 ID（如 wondergoo）
                if not product.brand:
                    shop_m = re.search(r'item\.rakuten\.co\.jp/([^/]+)/', url)
                    if shop_m:
                        product.brand = shop_m.group(1)

                print(f"[Rakuten] ✅ {product.title[:40]} / ¥{product.price_jpy} / in_stock={product.in_stock}")
                return product

        except Exception as e:
            print(f"[Rakuten] 例外: {type(e).__name__}: {e}，改用通用 Playwright")

        return await self._scrape_with_playwright(url)
