"""
HOKA (hoka.com/jp/) 商品爬取 Mixin

平台：SFCC (Salesforce Commerce Cloud)
資料策略：
1. JSON-LD <script type="application/ld+json"> 主資料源（標題/價格/SKU/圖片/品牌/描述）
2. <button data-attr-color-swatch> 找 color 變體（含 title 可讀名稱）
3. <button class="options-select"> 找 size 變體（class 含 out-of-stock = 缺貨）
4. 圖片來自 Cloudinary CDN (dms.deckers.com)，不需 base64

URL 範例：
  https://www.hoka.com/jp/ora-primo/1141570.html
  https://www.hoka.com/jp/clifton-9/1127895.html
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 5_000_000


class HokaMixin:

    async def _scrape_hoka(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="HOKA")
        clean_url = url.split("#")[0].strip()

        html = await asyncio.to_thread(self._hoka_get_html, clean_url)
        if not html:
            print(f"[Hoka] ❌ HTML 取得失敗: {clean_url}")
            return product

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. JSON-LD 主資料 ──
        ld = self._hoka_find_jsonld(soup)
        if ld:
            self._hoka_apply_jsonld(ld, product)

        # ── 2. fallback 標題 ──
        if not product.title:
            og = soup.find("meta", attrs={"property": "og:title"})
            if og and og.get("content"):
                product.title = og["content"].strip()
            else:
                t = soup.find("title")
                if t:
                    title = t.get_text(strip=True)
                    title = re.split(r'\s*[\|｜]\s*', title)[0].strip()
                    product.title = title

        # ── 3. fallback 價格（從 .accessible-price-summary 拿）──
        if not product.price_jpy:
            v = self._hoka_extract_price_from_html(html)
            if v:
                product.price_jpy = v

        # ── 4. PID（從 URL 抽取，e.g. /1141570.html → 1141570）──
        pid = ""
        m = re.search(r'/(\d{6,})\.html', clean_url)
        if m:
            pid = m.group(1)
        elif ld and ld.get("sku"):
            pid = str(ld["sku"])

        # ── 5. Colors + 庫存 ──
        colors_data = self._hoka_extract_colors(soup, pid)
        # ── 6. Sizes + 庫存 ──
        sizes_data = self._hoka_extract_sizes(soup, pid)

        # ── 7. 圖片整理（JSON-LD 已給但只是同一張不同尺寸）──
        # JSON-LD image 是 [w_65, w_140, w_414, w_900, w_1650]，取最大那張
        if product.image_url:
            # 把 w_xxx 統一改成 w_1650 取最大版
            product.image_url = re.sub(r'/w_\d+/', '/w_1650/', product.image_url)
        # extra_images 暫時不從 JSON-LD 拿（都是同一張），改從 .product-tile / 顏色 swatch 收集
        extra = self._hoka_extract_extra_images(soup, pid, product.image_url)
        if extra:
            product.extra_images = extra[:9]

        # ── 8. 組 variants ──
        if not colors_data:
            colors_data = [("", "", True, "")]  # (code, label, available, image)
        if not sizes_data:
            sizes_data = [("", True)]

        variants = []
        for color_code, color_label, color_avail, color_img in colors_data:
            for size, size_avail in sizes_data:
                # 整體可用 = color 可用 AND size 可用
                in_stock = color_avail and size_avail

                # color 顯示名：優先用 title 內可讀名（e.g. "ブラック / ブラック"）
                color_display = color_label or color_code

                label_parts = [p for p in [color_display, size] if p]
                base = pid or "hoka"
                sku = (
                    f"{base}-{color_code or 'x'}-{size or 'x'}"
                    .lower()
                    .replace(" ", "-")
                    .replace(".", "-")
                    .replace("/", "-")
                )
                variants.append({
                    "color": color_display,
                    "size": size,
                    "sku": sku,
                    "price": product.price_jpy or 0,
                    "in_stock": in_stock,
                    "image": color_img or product.image_url,
                })

        # 過濾空 variant
        if len(variants) == 1 and not variants[0]["color"] and not variants[0]["size"]:
            product.variants = []
        else:
            product.variants = variants

        product.in_stock = (
            any(v["in_stock"] for v in product.variants)
            if product.variants
            else (ld.get("offers", {}).get("availability", "").lower().endswith("instock") if ld else True)
        )

        title_short = (product.title or "")[:60]
        if product.price_jpy:
            print(
                f"[Hoka] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                f"colors={len(colors_data)} sizes={len(sizes_data)} "
                f"variants={len(product.variants)} | pid={pid}"
            )
        else:
            print(f"[Hoka] ⚠️ 価格未取得 ({title_short!r})")

        return product

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _hoka_find_jsonld(soup: BeautifulSoup) -> dict | None:
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

    def _hoka_apply_jsonld(self, ld: dict, product: ProductInfo) -> None:
        # 標題
        if ld.get("name"):
            product.title = str(ld["name"]).strip()

        # 品牌
        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str):
            product.brand = brand.strip()

        # 描述（HTML 格式，要 strip tag）
        desc = ld.get("description") or ""
        if desc:
            soup_desc = BeautifulSoup(desc, "html.parser")
            text = soup_desc.get_text("\n", strip=True)
            product.description = text[:3000]

        # 價格
        offers = ld.get("offers")
        if isinstance(offers, dict):
            v = self._hoka_to_int(offers.get("price"))
            if v:
                product.price_jpy = v

        # 主圖（取最後一張，通常是最大尺寸）
        imgs = ld.get("image") or []
        if isinstance(imgs, list) and imgs:
            # 偏好最大尺寸（找 w_1650 > w_900 > 其他）
            best = imgs[-1]  # JSON-LD 通常按 size 升序
            for img in imgs:
                if isinstance(img, str) and "/w_1650/" in img:
                    best = img
                    break
            product.image_url = best
        elif isinstance(imgs, str):
            product.image_url = imgs

    @staticmethod
    def _hoka_to_int(v) -> int | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            n = int(v)
        else:
            s = str(v).strip().replace(",", "")
            if not s:
                return None
            try:
                n = int(float(s))
            except (ValueError, TypeError):
                return None
        if _MIN_PRICE <= n <= _MAX_PRICE:
            return n
        return None

    # ─────────────────────────────────────────────────────────────────
    # 價格 fallback
    # ─────────────────────────────────────────────────────────────────
    def _hoka_extract_price_from_html(self, html: str) -> int | None:
        """
        Fallback：找 .accessible-price-summary 內的「<數字> JPY」
        但要避開推薦商品（取頁面內第一個或從 JSON-LD pid 旁邊取）
        """
        # 取所有 price summary
        prices = re.findall(
            r'class="accessible-price-summary[^"]*"[^>]*>\s*([\d,]+)\s*JPY',
            html,
        )
        if not prices:
            # 也可能是 ¥xxx 文字
            prices = re.findall(r'¥\s*([\d,]{3,})', html)

        # 主商品價格通常是「最常見的」或第一個
        from collections import Counter
        valid = []
        for p in prices:
            v = self._hoka_to_int(p)
            if v:
                valid.append(v)
        if valid:
            counter = Counter(valid)
            return counter.most_common(1)[0][0]
        return None

    # ─────────────────────────────────────────────────────────────────
    # Colors
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _hoka_extract_colors(soup: BeautifulSoup, pid: str) -> list[tuple[str, str, bool, str]]:
        """
        回傳 [(color_code, color_label, available, swatch_image_url), ...]

        SFCC 結構（hoka）：
        <div data-attr="color">
          ...
          <button title="ブラック / ブラック" class="..."
                  value="...?dwvar_PID_color=BBLC...">
            <span data-attr-value="BBLC" style="background-image:url(...)" />
          </button>
        """
        colors: list[tuple[str, str, bool, str]] = []
        seen: set[str] = set()

        # 找 data-attr="color" 區塊
        wrapper = soup.find(attrs={"data-attr": "color"})
        if not wrapper:
            return colors

        # 找所有 color button（不重複 - 可能有 desktop + mobile 兩份）
        for btn in wrapper.find_all("button"):
            value = btn.get("value", "")
            if not value:
                continue
            # 從 value 中抽 color code: dwvar_xxx_color=BBLC
            m = re.search(r'dwvar_\d+_color=([^&]+)', value)
            if not m:
                continue
            code = m.group(1).strip()
            if code in seen:
                continue
            seen.add(code)

            # 可讀名稱（title 屬性最準）
            label = btn.get("title", "").strip() or code

            # 庫存
            cls = " ".join(btn.get("class", []))
            available = "out-of-stock" not in cls and "unselectable" not in cls

            # swatch 圖片（從 <span> background-image 抽）
            swatch_img = ""
            inner_span = btn.find("span", attrs={"data-attr-value": code}) or btn.find("span")
            if inner_span:
                style = inner_span.get("style", "")
                m_bg = re.search(
                    r'background-image\s*:\s*url\([\'"]?([^\'")\s]+)[\'"]?\)',
                    style,
                )
                if m_bg:
                    swatch_img = m_bg.group(1).strip()
                    if swatch_img.startswith("//"):
                        swatch_img = "https:" + swatch_img

            colors.append((code, label, available, swatch_img))

        return colors

    # ─────────────────────────────────────────────────────────────────
    # Sizes
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _hoka_extract_sizes(soup: BeautifulSoup, pid: str) -> list[tuple[str, bool]]:
        """
        回傳 [(size, available), ...]

        SFCC 結構（hoka）：
        <div data-attr="size">
          <button class="options-select [out-of-stock]" role="radio"
                  value="...?dwvar_PID_size=23.0...">
        """
        sizes: list[tuple[str, bool]] = []
        seen: set[str] = set()

        wrapper = soup.find(attrs={"data-attr": "size"})
        if not wrapper:
            return sizes

        for btn in wrapper.find_all("button"):
            value = btn.get("value", "")
            if not value:
                continue
            m = re.search(r'dwvar_\d+_size=([^&]+)', value)
            if not m:
                continue
            size = m.group(1).strip()
            if size in seen:
                continue
            seen.add(size)

            cls = " ".join(btn.get("class", []))
            available = "out-of-stock" not in cls and "unselectable" not in cls

            sizes.append((size, available))

        return sizes

    # ─────────────────────────────────────────────────────────────────
    # 額外圖片（從色彩 swatch 收集每個顏色的代表圖）
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _hoka_extract_extra_images(soup: BeautifulSoup, pid: str, main_url: str) -> list[str]:
        """
        Hoka 圖片在 dms.deckers.com Cloudinary 上
        URL 格式: https://dms.deckers.com/hoka/image/upload/.../v.../<pid>-<color_code>_<idx>.png

        從頁面內找所有 <pid>-XXX_N.png 圖檔，取大圖版本
        """
        urls: list[str] = []
        seen: set[str] = set()
        if main_url:
            seen.add(main_url)

        # 找所有 dms.deckers.com 上的圖片
        if pid:
            pattern = re.compile(
                rf'(https?://dms\.deckers\.com/[^"\'<>\s]+{re.escape(pid)}-[A-Z]+_\d+\.(?:png|jpg|jpeg|webp)[^"\'<>\s]*)',
                re.IGNORECASE,
            )
        else:
            pattern = re.compile(
                r'(https?://dms\.deckers\.com/[^"\'<>\s]+\.(?:png|jpg|jpeg|webp)[^"\'<>\s]*)',
                re.IGNORECASE,
            )

        html_str = str(soup)
        for m in pattern.finditer(html_str):
            raw = m.group(1)
            # 解 HTML entity
            raw = raw.replace("&amp;", "&")
            # 統一轉成大圖（w_1650）
            big = re.sub(r'/w_\d+/', '/w_1650/', raw)
            # 去重 by filename
            fname = big.split("?")[0].split("/")[-1]
            key = fname
            if key in seen:
                continue
            seen.add(key)
            urls.append(big)

        return urls

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _hoka_get_html(self, url: str) -> str:
        """SeleniumBase UC 取得 HTML（hoka.com 用 Cloudflare）"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()
            try:
                driver.uc_open_with_reconnect(url, reconnect_time=6)
            except Exception:
                driver.get(url)
            time.sleep(3)

            # 等到頁面有 ld+json 或 og:price 等關鍵特徵
            best_html = ""
            best_score = 0
            for i in range(6):
                time.sleep(2)
                try:
                    html = driver.page_source
                except Exception:
                    continue

                score = 0
                if 'application/ld+json' in html: score += 5
                if '"@type":"Product"' in html or '"@type": "Product"' in html: score += 5
                if 'data-attr="size"' in html: score += 3
                if 'data-attr="color"' in html: score += 3
                if 'options-select' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html
                if i >= 1 and score >= 8:
                    print(f"[Hoka][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    return html

            if best_html:
                print(f"[Hoka][fetch] 最佳: score={best_score}, size={len(best_html)//1024}KB")
                self._driver_use_count += 1
                return best_html

            return ""
        except Exception as e:
            print(f"[Hoka] driver 失敗: {type(e).__name__}: {e}")
            return ""
