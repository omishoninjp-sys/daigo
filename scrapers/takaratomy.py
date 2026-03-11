"""
takaratomy.py  –  タカラトミー商品ページ爬蟲
https://beyblade.takaratomy.co.jp/ 等

httpx がトップページにリダイレクトされるため SeleniumBase UC を使用。
  - 価格       → .price テキスト（税込）
  - 商品名     → <title> suffix 除去
  - 説明文     → .spec
  - 画像       → _image/ 配下の商品画像（@1 サイズ、_list 除外）
  - variants   → なし（単品）
  - ブランド   → タカラトミー固定
"""
import asyncio
import re
import time

from bs4 import BeautifulSoup
from urllib.parse import urljoin

from scrapers.base import ProductInfo


class TakaratomyMixin:

    async def _scrape_takaratomy(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        clean_url = url.split("#")[0].strip()

        html = await asyncio.to_thread(self._takaratomy_get_html, clean_url)
        if not html:
            print(f"[Takaratomy] ❌ HTML 取得失敗")
            return product

        soup = BeautifulSoup(html, "html.parser")

        # ── 商品名（<title> suffix 除去）
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            raw = re.sub(r"\s*[｜|].*$", "", raw).strip()
            if raw:
                product.title = raw

        # ── 価格（.price → 税込テキスト）
        price_el = soup.select_one(".price")
        if price_el:
            price_text = price_el.get_text(strip=True).split("（")[0]
            m = re.search(r"[\d,]+", price_text)
            if m:
                product.price_jpy = int(m.group().replace(",", ""))

        # ── ブランド
        product.brand = "タカラトミー"

        # ── 説明文
        spec_el = soup.select_one(".spec")
        if spec_el:
            product.description = spec_el.get_text(separator="\n", strip=True)[:800]

        # ── 画像（_image/ 配下の @1 サイズ）
        seen: set[str] = set()
        imgs: list[str] = []
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if "_list" in src:
                continue
            if not re.match(r"_image/", src):
                continue
            full = urljoin(clean_url, src)
            if full not in seen:
                seen.add(full)
                imgs.append(full)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]

        title_short = (product.title or "")[:50]
        if product.price_jpy:
            print(
                f"[Takaratomy] ✅ {title_short!r} | "
                f"¥{product.price_jpy:,} | images={len(imgs)}"
            )
        else:
            print(f"[Takaratomy] ⚠️ 価格未取得 ({title_short!r})")

        return product

    def _takaratomy_get_html(self, url: str) -> str:
        """SeleniumBase UC で HTML 取得"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()
            driver.get(url)
            time.sleep(3)
            html = driver.page_source
            self._driver_use_count += 1
            return html
        except Exception as e:
            print(f"[Takaratomy] SeleniumBase 失敗: {type(e).__name__}: {e}")
            return ""
