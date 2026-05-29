"""
あみあみ (amiami.jp) 商品爬取 Mixin
www.amiami.jp / amiami.jp

平台特性：
- 日本最大動漫模型／玩具預訂網站
- UTF-8 編碼
- 有完整 JSON-LD Product schema（name / brand / image array / offers.price）
- **價格陷阱**：頁面同時有「参考価格」和「販売価格」兩個價格
    <div class="selling_price">4,400円(税込)</div>   ← 参考価格（原価，需避開）
    <div class="price" data-item-price="3,740">     ← 販売価格（實際售價）
        3,740円(税込)
    </div>
- amiami 不用 <del>/<s> 包原価，所以 generic.py 的劃線過濾抓不到
- 商品多為單品（プラモ／フィギュア／グッズ），通常無 color/size 變體

URL 範例：
  https://www.amiami.jp/top/detail/detail?gcode=FIGURE-199356
  https://www.amiami.jp/top/detail/detail?scode=FIGURE-199356
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


class AmiamiMixin:

    async def _scrape_amiami(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        clean_url = url.split("#")[0].strip()
        # URL 標準化：去掉 page=related_item 等追蹤參數，保留 gcode / scode
        clean_url = self._amiami_clean_url(clean_url)
        if clean_url != url:
            product.source_url = clean_url
            print(f"[Amiami] URL 標準化: {url} → {clean_url}")

        html = await asyncio.to_thread(self._amiami_get_html, clean_url)
        if not html:
            print(f"[Amiami] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題：JSON-LD name 最乾淨 ──
            ld = self._amiami_find_jsonld(soup)
            if ld:
                self._amiami_apply_jsonld(ld, product)

            # JSON-LD 沒有 → og:title fallback（去 [メーカー名] 尾巴）
            if not product.title:
                og_title = soup.find("meta", attrs={"property": "og:title"})
                if og_title and og_title.get("content"):
                    title = og_title["content"].strip()
                    title = re.sub(r'\s*\[[^\]]+\]\s*$', '', title)
                    product.title = title

            # ── 價格：「販売価格」優先（避開「参考価格」陷阱）──
            price = self._amiami_extract_price(soup, html)
            if price:
                product.price_jpy = price

            # ── 庫存判斷 ──
            product.in_stock = self._amiami_extract_stock(html)

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Amiami] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"brand={product.brand!r} | in_stock={product.in_stock} | "
                    f"images={1 + len(product.extra_images)}"
                )
            else:
                print(
                    f"[Amiami] ⚠️ 部分資料缺失 ({title_short!r}) | price={product.price_jpy}"
                )

        except Exception as e:
            print(f"[Amiami] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # URL 標準化
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _amiami_clean_url(url: str) -> str:
        """
        amiami 的商品 URL 形式：
          https://www.amiami.jp/top/detail/detail?gcode=FIGURE-199356
          https://www.amiami.jp/top/detail/detail?scode=FIGURE-199356&page=related_item
        保留 gcode 或 scode，去掉 page / utm 等追蹤參數
        """
        m = re.search(r'(g?s?code)=([\w\-]+)', url, re.IGNORECASE)
        if not m:
            return url
        key, val = m.group(1), m.group(2)
        # 同時保留 https://www.amiami.jp/top/detail/detail 前綴
        base_m = re.match(r'(https?://[^/]+/top/detail/detail)', url, re.IGNORECASE)
        if base_m:
            return f"{base_m.group(1)}?{key}={val}"
        return url

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _amiami_find_jsonld(soup: BeautifulSoup) -> dict | None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        return None

    def _amiami_apply_jsonld(self, ld: dict, product: ProductInfo) -> None:
        # 標題
        name = (ld.get("name") or "").strip()
        if name:
            product.title = name

        # 品牌
        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str) and brand.strip():
            product.brand = brand.strip()

        # 描述
        desc = (ld.get("description") or "").strip()
        if desc:
            product.description = desc[:1500]

        # 圖片
        images = ld.get("image")
        if isinstance(images, str):
            images = [images]
        if isinstance(images, list) and images:
            product.image_url = str(images[0]).strip()
            seen = {product.image_url}
            extra = []
            for img in images[1:]:
                u = str(img).strip()
                if u and u not in seen:
                    seen.add(u)
                    extra.append(u)
                if len(extra) >= 9:
                    break
            product.extra_images = extra

        # 價格：JSON-LD offers.price 是「販売価格」（特價），這個正確
        # 但仍會在主流程用 HTML 二次驗證
        offers = ld.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            v = self._amiami_to_int(offers.get("price"))
            if v:
                product.price_jpy = v

    # ─────────────────────────────────────────────────────────────────
    # 價格：嚴守「販売価格」不抓「参考価格」
    # ─────────────────────────────────────────────────────────────────
    def _amiami_extract_price(self, soup: BeautifulSoup, html: str) -> int | None:
        """
        amiami 價格抓取策略（嚴守販売価格，避開参考価格陷阱）：
        1. data-item-price="3,740" 屬性（最可靠）
        2. <div class="price" id="detail_detail__item_price"> 內的「N,NNN円(税込)」
        3. JSON-LD offers.price（已在 _amiami_apply_jsonld 處理）
        4. 絕對不抓 .selling_price（那是参考価格＝原価）
        """
        # 1. data-item-price 屬性
        price_el = soup.select_one('[data-item-price]')
        if price_el:
            raw = price_el.get("data-item-price", "")
            v = self._amiami_to_int(raw)
            if v:
                print(f"[Amiami] 價格採用 data-item-price: ¥{v}")
                return v

        # 2. .price 區塊內的金額（不是 .selling_price）
        price_div = soup.select_one(
            'div.price#detail_detail__item_price, div.price[id*="item_price"]'
        )
        if price_div:
            txt = price_div.get_text(" ", strip=True)
            m = re.search(r'([\d,]+)\s*円', txt)
            if m:
                v = self._amiami_to_int(m.group(1))
                if v:
                    print(f"[Amiami] 價格採用 .price 區塊: ¥{v}")
                    return v

        # 3. JSON-LD 應已處理；若 product.price_jpy 已有值就用它
        # 此處只是 fallback：找頁面販売価格附近的金額
        m = re.search(
            r'販売価格.*?(?:</[^>]+>\s*){0,3}.*?(\d+%OFF)?\s*([\d,]+)\s*円\s*[（(]\s*税込',
            html,
            re.DOTALL,
        )
        if m:
            v = self._amiami_to_int(m.group(2))
            if v:
                print(f"[Amiami] 價格採用「販売価格」文字: ¥{v}")
                return v

        print(f"[Amiami] ⚠️ 找不到販売価格（不採用参考価格 fallback）")
        return None

    @staticmethod
    def _amiami_to_int(value) -> int | None:
        if value is None:
            return None
        s = str(value).strip().replace(",", "").replace("，", "").replace("¥", "").replace("円", "")
        if not s:
            return None
        try:
            v = int(float(s))
        except (ValueError, TypeError):
            return None
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
        return None

    # ─────────────────────────────────────────────────────────────────
    # 庫存
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _amiami_extract_stock(html: str) -> bool:
        """
        從按鈕文字判斷庫存
        可下單：「カートに追加」「予約注文する」「注文する」
        缺貨：「販売停止」「販売終了」「受注終了」「完売」「在庫切れ」
        """
        out_kw = ["販売停止", "販売終了", "受注終了", "受付終了", "完売", "在庫切れ", "再販未定"]
        in_kw = ["カートに追加", "予約注文", "注文する"]

        for kw in out_kw:
            if kw in html:
                print(f"[Amiami] 庫存狀態: 缺貨（偵測到「{kw}」）")
                return False
        for kw in in_kw:
            if kw in html:
                print(f"[Amiami] 庫存狀態: 可下單（偵測到「{kw}」）")
                return True

        print(f"[Amiami] 庫存狀態: 未知，預設可下單")
        return True

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _amiami_get_html(self, url: str) -> str:
        """用 SeleniumBase UC 抓 amiami 頁面"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()

            try:
                driver.uc_open_with_reconnect(url, reconnect_time=5)
            except Exception:
                driver.get(url)
            time.sleep(2)

            best_html = ""
            best_score = 0

            for i in range(5):
                time.sleep(1.5)
                try:
                    html = driver.page_source
                except Exception:
                    continue

                score = 0
                if 'application/ld+json' in html: score += 3
                if 'data-item-price' in html: score += 5
                if 'detail_detail__item_price' in html: score += 3
                if 'og:title' in html: score += 2
                if '販売価格' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 13 and len(html) > 30000:
                    print(f"[Amiami][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 10000:
                print(f"[Amiami][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Amiami][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Amiami] driver 失敗: {type(e).__name__}: {e}")
            return ""
