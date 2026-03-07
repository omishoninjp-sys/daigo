"""
にじさんじオフィシャルストア爬蟲 Mixin
Salesforce Commerce Cloud / SSR 頁面，httpx 即可
"""
import re

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


class NijisanjiMixin:

    async def _scrape_nijisanji(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="にじさんじ")

        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
            }

            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    print(f"[Nijisanji] HTTP {resp.status_code}")
                    return product
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                og = soup.find("meta", property="og:title")
                if og:
                    product.title = og.get("content", "").replace("｜にじさんじオフィシャルストア", "").strip()

            # === 圖片 ===
            base = "https://shop.nijisanji.jp"
            imgs = []
            seen_imgs = set()
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if "nijisanji-master-catalog" in src and ("physical" in src or "digital" in src):
                    if not src.startswith("http"):
                        src = base + src
                    if src not in seen_imgs:
                        seen_imgs.add(src)
                        imgs.append(src)

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:9]

            # === Variants（只處理葉節點 li，避免父層 li 包含所有子項文字）===
            variants = []
            min_price = None

            for li in soup.find_all("li"):
                # 跳過有子 li 的容器節點
                if li.find("li"):
                    continue

                text = li.get_text(" ", strip=True)

                price_m = re.search(r'[¥￥]([\d,]+)\s*税込', text)
                if not price_m:
                    price_m = re.search(r'([\d,]+)\s*税込', text)
                if not price_m:
                    continue

                price = int(price_m.group(1).replace(",", ""))
                if price < 100 or price > 500000:
                    continue

                name = text
                name = re.sub(r'[¥￥][\d,]+\s*税込', '', name).strip()
                name = re.sub(r'[\d,]+\s*税込', '', name).strip()
                name = re.sub(r'\+\s*まもなく(終了|販売)', '', name).strip()
                name = re.sub(r'まもなく(終了|販売)', '', name).strip()
                name = re.sub(r'\s+', ' ', name).strip()

                if len(name) < 3:
                    continue
                if any(skip in name for skip in ["カート", "ログイン", "お気に入り", "ページ", "TOP", "閉じる", "選択してください"]):
                    continue

                if min_price is None or price < min_price:
                    min_price = price

                variants.append({
                    "color": "",
                    "size": name,
                    "sku": "",
                    "price": price,       # 各 variant 儲存自己的正確價格
                    "in_stock": "在庫なし" not in text and "売り切れ" not in text,
                    "image": product.image_url,
                })

            # 重複排除
            seen_v = set()
            unique_variants = []
            for v in variants:
                key = f"{v['size']}|{v['price']}"
                if key not in seen_v:
                    seen_v.add(key)
                    unique_variants.append(v)
            product.variants = unique_variants

            # === 價格（取最低價作為商品主價）===
            if min_price:
                product.price_jpy = min_price
            else:
                for pat in [r'[¥￥]([\d,]+)\s*税込', r'[¥￥]([\d,]+)']:
                    pm = re.search(pat, html)
                    if pm:
                        p = int(pm.group(1).replace(",", ""))
                        if 100 < p < 500000:
                            product.price_jpy = p
                            break

            # === 說明 ===
            for section_title in ["商品説明", "商品仕様"]:
                tag = soup.find(lambda t: t.name and t.get_text(strip=True) == section_title)
                if tag:
                    next_el = tag.find_next_sibling()
                    if next_el:
                        product.description = next_el.get_text(" ", strip=True)[:500]
                        break

            print(f"[Nijisanji] ✅ {product.title[:40]} / ¥{product.price_jpy} / {len(product.variants)} variants")
            for v in product.variants:
                print(f"  - {v['size']}: ¥{v['price']} ({'有庫存' if v['in_stock'] else '無庫存'})")

        except Exception as e:
            print(f"[Nijisanji] ❌ 錯誤: {type(e).__name__}: {e}")

        return product
