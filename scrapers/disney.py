"""
Disney Store Japan 爬蟲 Mixin
架構：Salesforce Commerce Cloud（Demandware），SSR 頁面，httpx 直接抓
URL 格式：https://store.disney.co.jp/goods/{JAN碼}.html
"""
import re
from bs4 import BeautifulSoup

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


class DisneyMixin:

    async def _scrape_disney(self, url: str) -> ProductInfo:
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
                r = await client.get(url)
                if r.status_code != 200:
                    print(f"[Disney] ❌ HTTP {r.status_code}")
                    return product

                html = r.text
                soup = BeautifulSoup(html, "html.parser")

                # ── 標題
                h1 = soup.select_one("h1")
                if h1:
                    product.title = h1.get_text(strip=True)
                if not product.title:
                    og = soup.find("meta", property="og:title")
                    if og:
                        # 去掉「【公式】ディズニーストア.jp | 」前綴
                        raw = og.get("content", "")
                        product.title = re.sub(r'^.*?[|｜]\s*', '', raw).strip() or raw.strip()

                # ── 價格
                # <span class="sales ..."><span class="value" content="2800">
                val_el = soup.select_one("span.sales .value[content]")
                if val_el:
                    try:
                        product.price_jpy = int(val_el["content"])
                        print(f"[Disney DEBUG] span.sales .value[content] → ¥{product.price_jpy}")
                    except (ValueError, TypeError):
                        pass

                # fallback：從 .value 的文字取
                if not product.price_jpy:
                    val_el2 = soup.select_one("span.sales .value, span.value")
                    if val_el2:
                        m = re.search(r'([\d,]+)', val_el2.get_text())
                        if m:
                            product.price_jpy = int(m.group(1).replace(",", ""))
                            print(f"[Disney DEBUG] .value text fallback → ¥{product.price_jpy}")

                # ── 圖片
                # 主圖：og:image（JAN碼，高解析度）
                og_img = soup.find("meta", property="og:image")
                if og_img:
                    base_img = og_img.get("content", "")
                    if base_img:
                        product.image_url = base_img

                # 副圖：thumbnail-carousel 的 data-image-base，去重，排除 slick-cloned
                seen = set()
                extra = []
                for el in soup.select(".thumbnail-carousel__item:not(.slick-cloned)"):
                    img_base = el.get("data-image-base", "")
                    if img_base and img_base not in seen:
                        seen.add(img_base)
                        # 主圖不重複放進 extra
                        if img_base != product.image_url:
                            extra.append(img_base)
                product.extra_images = extra[:8]
                print(f"[Disney DEBUG] extra_images={len(product.extra_images)}")

                # ── 庫存
                # 有 add-to-bag-btn 且沒有 disabled → 有庫存
                cart_btn = soup.select_one("button.add-to-bag-btn")
                if cart_btn:
                    product.in_stock = cart_btn.get("disabled") is None
                else:
                    # fallback：找 js-add-to-bag 文字
                    js_btn = soup.select_one(".js-add-to-bag")
                    product.in_stock = js_btn is not None

                print(
                    f"[Disney] ✅ {product.title[:40]} / ¥{product.price_jpy} / "
                    f"in_stock={product.in_stock} / extra_imgs={len(product.extra_images)}"
                )

        except Exception as e:
            import traceback
            print(f"[Disney] ❌ 例外: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product
