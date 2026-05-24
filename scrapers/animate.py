"""
animate (アニメイト) Online Shop 商品爬取 Mixin
www.animate-onlineshop.jp

平台特性：
- 日本最大動漫周邊連鎖店的線上商城（フィギュア、グッズ、CD/DVD、書籍等）
- UTF-8 編碼
- JSON-LD 只有 BreadcrumbList（無 Product schema）→ 主要靠 og: meta + HTML 結構
- 價格在 .item_price 區塊：
  - 有折扣時：<p class="price_down">...通常価格 5,500円...</p>
              <p class="price new_price">4,950円(税込)</p>  ← 實際售價
  - 無折扣時：只有 <p class="price">XXX円(税込)</p>
- 庫存狀態：.mc-stock-type，例「〇 予約受付中」「在庫あり」「品切れ」
- 圖片走 techorus-cdn（resize_image.php?image=檔名&width=&height=）
- 商品多為單品（無 color/size 變體，只有數量選擇）

URL 範例：
  https://www.animate-onlineshop.jp/pn/.../pd/3463086/
"""
import asyncio
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000

# 庫存判斷關鍵字
_IN_STOCK_KW = ["予約受付中", "在庫あり", "発売中", "予約商品", "入荷待ち", "予約受付"]
_OUT_OF_STOCK_KW = ["品切れ", "売り切れ", "在庫なし", "販売終了", "予約受付終了", "完売"]


class AnimateMixin:

    async def _scrape_animate(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="animate")
        clean_url = url.split("#")[0].strip()

        html = await asyncio.to_thread(self._animate_get_html, clean_url)
        if not html:
            print(f"[Animate] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 標題：og:title（去掉「| アニメイト」尾巴）──
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                title = og_title["content"].strip()
                title = re.split(r'\s*[｜\|]\s*アニメイト', title)[0].strip()
                product.title = title

            # ── 描述：優先抓商品說明區塊 .detail_info，否則用 og:description ──
            desc_el = soup.select_one(".detail_info")
            if desc_el:
                desc_text = desc_el.get_text(" ", strip=True)
                product.description = self._animate_clean_description(desc_text)
            if not product.description:
                og_desc = soup.find("meta", attrs={"property": "og:description"})
                if og_desc and og_desc.get("content"):
                    product.description = self._animate_clean_description(og_desc["content"].strip())

            # ── 主圖：og:image ──
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_img and og_img.get("content"):
                img_url = og_img["content"].strip().replace("&amp;", "&")
                product.image_url = img_url

            # ── 價格：核心邏輯，特價優先 ──
            price = self._animate_extract_price(soup, html)
            if price:
                product.price_jpy = price

            # ── 庫存狀態 ──
            product.in_stock = self._animate_extract_stock(soup, html)

            # ── 額外圖片：從 item_images 區塊抓 ──
            product.extra_images = self._animate_extract_images(html, product.image_url)

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Animate] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"in_stock={product.in_stock} | images={1 + len(product.extra_images)}"
                )
            else:
                print(
                    f"[Animate] ⚠️ 部分資料缺失 ({title_short!r}) | price={product.price_jpy}"
                )

        except Exception as e:
            print(f"[Animate] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # 價格
    # ─────────────────────────────────────────────────────────────────
    def _animate_extract_price(self, soup: BeautifulSoup, html: str) -> int | None:
        """
        animate 價格解析（特價優先）

        HTML 結構：
        有折扣：
          <div class="item_price"><div class="inner">
            <p class="price_down"><span class="reduction_price">10%OFF</span>
               <span>通常価格</span><span>5,500<span>円(税込)</span></span></p>
            <p class="price new_price">4,950<span>円</span><span>(税込)</span></p>
          </div></div>
        無折扣：
          <p class="price">XXX円(税込)</p>

        優先順序：new_price（特價）> price（一般售價）> 通常価格（原價，最後手段）
        """
        # 1. 特價 .price.new_price（最優先 — 客人實際付的價）
        new_price_el = soup.select_one("p.price.new_price, .new_price")
        if new_price_el:
            v = self._animate_parse_price_text(new_price_el.get_text(" ", strip=True))
            if v:
                print(f"[Animate] 價格採用「特價 new_price」: ¥{v}")
                return v

        # 2. 一般售價 .price（排除 price_down / new_price）
        for p_el in soup.select("p.price"):
            cls = p_el.get("class", [])
            if "new_price" in cls or "price_down" in cls:
                continue
            v = self._animate_parse_price_text(p_el.get_text(" ", strip=True))
            if v:
                print(f"[Animate] 價格採用「一般售價 price」: ¥{v}")
                return v

        # 3. item_price 區塊內任何 (税込) 金額（fallback）
        item_price_el = soup.select_one(".item_price")
        if item_price_el:
            txt = item_price_el.get_text(" ", strip=True)
            # 排除「通常価格」那段，找其餘的 (税込) 價
            # 先嘗試抓所有 円(税込) 金額
            tax_prices = re.findall(r'([\d,]+)\s*円\s*[（(]?\s*税込', txt)
            candidates = []
            for tp in tax_prices:
                v = self._animate_to_int(tp)
                if v:
                    candidates.append(v)
            if candidates:
                # 有多個時取最小（特價通常較低；通常価格較高）
                chosen = min(candidates)
                print(f"[Animate] 價格採用「item_price 區塊最低税込価」: ¥{chosen} (候選 {candidates})")
                return chosen

        # 4. 全頁 fallback：任何 円(税込)
        tax_prices = re.findall(r'([\d,]+)\s*円\s*(?:<[^>]*>)?\s*[（(]?\s*税込', html)
        candidates = [self._animate_to_int(tp) for tp in tax_prices]
        candidates = [c for c in candidates if c]
        if candidates:
            chosen = min(candidates)
            print(f"[Animate] 價格採用「全頁 fallback 最低税込価」: ¥{chosen}")
            return chosen

        print(f"[Animate] ⚠️ 找不到價格")
        return None

    def _animate_parse_price_text(self, text: str) -> int | None:
        """從一段文字抓第一個合理金額（如 '4,950 円 (税込)' → 4950）"""
        m = re.search(r'([\d,]+)\s*円', text)
        if m:
            return self._animate_to_int(m.group(1))
        # 沒有「円」也試純數字
        m = re.search(r'([\d,]{3,})', text)
        if m:
            return self._animate_to_int(m.group(1))
        return None

    @staticmethod
    def _animate_to_int(value) -> int | None:
        if value is None:
            return None
        s = str(value).strip().replace(",", "").replace("，", "")
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
    def _animate_extract_stock(soup: BeautifulSoup, html: str) -> bool:
        """
        從 .mc-stock-type 判斷庫存
        例「〇 予約受付中」「在庫あり」→ 可下單
           「品切れ」「販売終了」→ 缺貨
        找不到 → 預設可下單（讓客人聯繫確認）
        """
        stock_el = soup.select_one(".mc-stock-type")
        stock_text = ""
        if stock_el:
            stock_text = stock_el.get_text(" ", strip=True)
        else:
            # fallback：全頁找庫存關鍵字
            for kw in _OUT_OF_STOCK_KW + _IN_STOCK_KW:
                if kw in html:
                    stock_text = kw
                    break

        # 先判斷缺貨（缺貨關鍵字優先）
        for kw in _OUT_OF_STOCK_KW:
            if kw in stock_text:
                print(f"[Animate] 庫存狀態: 缺貨（偵測到「{kw}」）")
                return False
        for kw in _IN_STOCK_KW:
            if kw in stock_text:
                print(f"[Animate] 庫存狀態: 可下單（偵測到「{kw}」）")
                return True

        print(f"[Animate] 庫存狀態: 未知（stock_text={stock_text!r}），預設可下單")
        return True

    # ─────────────────────────────────────────────────────────────────
    # 圖片
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _animate_extract_images(html: str, main_image: str = "") -> list:
        """
        從 HTML 抓商品額外圖片
        animate 圖片走 techorus-cdn 的 resize_image.php?image=檔名
        """
        result = []
        seen = set()

        # 主圖檔名（去重用）
        main_file = ""
        if main_image:
            m = re.search(r'image=([^&]+)', main_image)
            if m:
                main_file = m.group(1)
                seen.add(main_file)

        # 找所有 resize_image.php?image=XXX
        for m in re.finditer(r'resize_image\.php\?image=([^&"\'\s]+)', html):
            filename = m.group(1)
            if filename in seen:
                continue
            seen.add(filename)
            # 重組為高解析度 URL（width=1200）
            url = (
                f"https://tc-animate.techorus-cdn.com/resize_image/resize_image.php"
                f"?image={filename}&width=1200&height=1200"
            )
            result.append(url)
            if len(result) >= 9:
                break

        return result

    # ─────────────────────────────────────────────────────────────────
    # 描述清理
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _animate_clean_description(desc: str) -> str:
        """清理 animate 描述：去掉預約期間／免責聲明等雜訊，但保留商品說明本體"""
        # 去掉「※ご予約期間～YYYY/MM/DD」
        desc = re.sub(r'※?ご予約期間[～~][\d/]+', '', desc)
        # 去掉返品不可、通販特別価格等聲明片段
        desc = re.sub(r'本商品は通販特別価格でのご提供です。', '', desc)
        desc = re.sub(r'アニメイト店頭では販売価格が異なる場合がございます。', '', desc)
        desc = re.sub(r'(?:また、)?いかなる理由があっても返品[・･]?キャンセルは不可となります。', '', desc)
        desc = re.sub(r'予めご了承の上ご注文ください。', '', desc)
        # 去掉「関連ワード:」之後的內容
        desc = re.split(r'関連ワード\s*[:：]', desc)[0]
        # 壓縮空白
        desc = re.sub(r'\s+', ' ', desc).strip()
        return desc[:1500]

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _animate_get_html(self, url: str) -> str:
        """用 SeleniumBase UC 抓 animate 頁面"""
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
                if 'og:title' in html: score += 3
                if 'item_price' in html: score += 5
                if 'price new_price' in html or '"price"' in html: score += 3
                if 'mc-stock-type' in html: score += 3
                if 'techorus-cdn' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 11 and len(html) > 30000:
                    print(f"[Animate][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 10000:
                print(f"[Animate][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Animate][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Animate] driver 失敗: {type(e).__name__}: {e}")
            return ""
