"""
ハードオフ オフモール（旧ネットモール / netmall.hardoff.co.jp）商品爬取 Mixin
netmall.hardoff.co.jp

平台特性：
- ハードオフグループ官方中古通販站（單品、二手、基本無變體）
- JSP 站，商品詳情（價格 / 商品ランク）由 JS 渲染 → 必須走 SeleniumBase UC
  （純 httpx 只拿得到 meta + 導覽，會跳「JavaScriptを有効にする必要があります」）
- 有 JSON-LD Product schema（name / brand / image array / offers）

**價格陷阱（與 amiami 方向相反，務必注意）**：
- ❌ JSON-LD offers.price = 税抜本体価格（且帶 priceValidUntil，會過期），不可採用
- ✅ DOM <span class="product-detail-price__main"> = 税込価格 ← 這才是 GOYOUTATI 要的進貨價
  實測案例：JSON-LD 50000（税抜）vs DOM 55,000（税込）= 50000 × 1.10
  → 本爬蟲只從 DOM 抓税込，JSON-LD 僅用於 title / brand / images

**缺貨判斷陷阱**：
- 每頁 meta description 都含站台標語「売り切れにご注意ください！」
  → 絕對不可用「売り切れ」字串判庫存（會每頁誤判缺貨）
  → 改用 JSON-LD offers.availability（InStock / OutOfStock）

**二手必備資訊**：商品ランク（成色，例：C RANK），寫進 description 讓客人知道是中古 C 級品。

URL 範例：
  https://netmall.hardoff.co.jp/product/6201209/
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


class NetmallMixin:

    async def _scrape_netmall(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        clean_url = self._netmall_clean_url(url.split("#")[0].strip())
        if clean_url != url:
            product.source_url = clean_url
            print(f"[Netmall] URL 標準化: {url} → {clean_url}")

        html = await asyncio.to_thread(self._netmall_get_html, clean_url)
        if not html:
            print(f"[Netmall] ❌ HTML 取得失敗: {clean_url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── JSON-LD：只取 title / brand / images（**不取 price**，那是税抜陷阱）──
            ld = self._netmall_find_jsonld(soup)
            jsonld_name = ""
            if ld:
                jsonld_name = self._netmall_apply_jsonld(ld, product)

            # 標題 fallback：og:title / DOM h1
            if not jsonld_name:
                jsonld_name = self._netmall_extract_name(soup)

            # ── 型番（型番：MODEL 192DAC MKII/192-24 INTER）──
            model = self._netmall_extract_model(soup)

            # ── 組更有意義的標題：品牌 + 商品名 + 型番 ──
            product.title = self._netmall_build_title(product.brand, jsonld_name, model)

            # ── 價格：唯一可信來源＝DOM 税込（.product-detail-price__main）──
            price = self._netmall_extract_price(soup, ld)
            if price:
                product.price_jpy = price

            # ── 商品ランク（成色）──
            rank = self._netmall_extract_rank(soup)

            # ── 描述：中古資訊（成色 / 型番）──
            product.description = self._netmall_build_desc(rank, model, ld)

            # ── 庫存：JSON-LD availability（不可用「売り切れ」字串）──
            product.in_stock = self._netmall_extract_stock(ld)

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Netmall] ✅ {title_short!r} | ¥{product.price_jpy:,}（税込）| "
                    f"rank={rank or '?'} | in_stock={product.in_stock} | "
                    f"images={1 + len(product.extra_images)}"
                )
            else:
                print(
                    f"[Netmall] ⚠️ 部分資料缺失 ({title_short!r}) | price={product.price_jpy}"
                )

        except Exception as e:
            print(f"[Netmall] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # URL 標準化：只保留 /product/<id>/
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _netmall_clean_url(url: str) -> str:
        m = re.match(
            r'(https?://netmall\.hardoff\.co\.jp/product/\d+/?)', url, re.IGNORECASE
        )
        if m:
            base = m.group(1)
            return base if base.endswith("/") else base + "/"
        return url.split("?")[0].split("#")[0]

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD（@type == "Product"）
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _netmall_find_jsonld(soup: BeautifulSoup) -> dict | None:
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

    def _netmall_apply_jsonld(self, ld: dict, product: ProductInfo) -> str:
        """套用 JSON-LD 的 name / brand / images。**刻意不套用 price（税抜陷阱）**。回傳 name。"""
        name = (ld.get("name") or "").strip()

        # 品牌
        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str) and brand.strip():
            product.brand = brand.strip()

        # 圖片（imageflux，乾淨；非價格欄位可信任 JSON-LD）
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

        return name

    # ─────────────────────────────────────────────────────────────────
    # 標題 / 型番 / 成色
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _netmall_extract_name(soup: BeautifulSoup) -> str:
        # og:title 形如「NORTH STAR DESIGN|D/Aコンバーター|【ハードオフ公式通販】オフモール|...」
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            head = og["content"].split("|")[0].strip()
            # og:title 第一段常是品牌；商品名取 h1 較準
        h1 = soup.select_one(".product-detail-name h1, .product-detail-name")
        if h1:
            t = h1.get_text(strip=True)
            if t:
                return t
        if og and og.get("content"):
            parts = [p.strip() for p in og["content"].split("|") if p.strip()]
            # 過濾站台名稱片段
            parts = [p for p in parts if "オフモール" not in p and "ハードオフ" not in p
                     and "公式通販" not in p and not p.isdigit()]
            if len(parts) >= 2:
                return parts[1]
            if parts:
                return parts[0]
        return ""

    @staticmethod
    def _netmall_extract_model(soup: BeautifulSoup) -> str:
        el = soup.select_one(".product-detail-num")
        if el:
            t = el.get_text(strip=True)
            t = re.sub(r'^型番[:：]?\s*', '', t).strip()
            return t
        return ""

    @staticmethod
    def _netmall_extract_rank(soup: BeautifulSoup) -> str:
        """商品ランク：.product-detail-price__rank 內第一張 img 的 alt（例：'C RANK'）"""
        img = soup.select_one(".product-detail-price__rank img[alt]")
        if img:
            alt = (img.get("alt") or "").strip()
            # 正規化「C RANK」→「C」
            m = re.match(r'([A-Z])\s*RANK', alt, re.IGNORECASE)
            if m:
                return m.group(1).upper()
            return alt
        return ""

    @staticmethod
    def _netmall_build_title(brand: str, name: str, model: str) -> str:
        bits = []
        if brand and (not name or brand not in name):
            bits.append(brand)
        if name:
            bits.append(name)
        if model and model not in name:
            bits.append(model)
        return " ".join(b for b in bits if b).strip()

    @staticmethod
    def _netmall_build_desc(rank: str, model: str, ld: dict | None) -> str:
        bits = ["中古品（ハードオフ オフモール）"]
        if rank:
            bits.append(f"商品ランク：{rank}")
        if model:
            bits.append(f"型番：{model}")
        # JSON-LD description 若非 URL、非空，補進去
        if isinstance(ld, dict):
            d = (ld.get("description") or "").strip()
            if d and not d.startswith("http"):
                bits.append(d[:500])
        return "｜".join(bits)

    # ─────────────────────────────────────────────────────────────────
    # 價格：嚴守 DOM 税込（.product-detail-price__main），不碰 JSON-LD 税抜
    # ─────────────────────────────────────────────────────────────────
    def _netmall_extract_price(self, soup: BeautifulSoup, ld: dict | None) -> int | None:
        """
        Net Mall 價格抓取（與 amiami 相反：JSON-LD 是税抜陷阱，只信 DOM 税込）：
        1. DOM <span class="product-detail-price__main"> 的「N,NNN」← 唯一正解（税込）
           注意：別誤抓 .product-detail-postage-price__main（那是送料参考価格）
        2. fallback：JSON-LD offers.price（税抜）× 1.10 推算税込
           ⚠️ 僅在 DOM 抓不到時使用，並大聲記 log（消費税以 10% 估算）
        """
        # 1. DOM 税込（class token 為 product-detail-price__main，與 postage 版不同）
        el = soup.select_one("span.product-detail-price__main")
        if el:
            txt = el.get_text(strip=True)  # "55,000"
            v = self._netmall_to_int(txt)
            if v:
                print(f"[Netmall] 價格採用 DOM 税込 .product-detail-price__main: ¥{v:,}")
                return v

        # 2. fallback：JSON-LD 税抜 × 1.10（僅在 DOM 失效時，避免整單失敗）
        if isinstance(ld, dict):
            offers = ld.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                taxnuki = self._netmall_to_int(offers.get("price"))
                if taxnuki:
                    est = round(taxnuki * 1.10)
                    print(
                        f"[Netmall] ⚠️ DOM 税込缺失，改用 JSON-LD 税抜 ¥{taxnuki:,} × 1.10 "
                        f"≈ ¥{est:,}（税込估算，消費税 10%）"
                    )
                    return est

        print(f"[Netmall] ⚠️ 找不到税込価格（DOM 與 JSON-LD 皆失敗）")
        return None

    @staticmethod
    def _netmall_to_int(value) -> int | None:
        if value is None:
            return None
        s = str(value).strip().replace(",", "").replace("，", "").replace("¥", "").replace("円", "")
        s = re.sub(r'[^0-9.]', '', s)
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
    # 庫存：JSON-LD availability（**不可用「売り切れ」字串，那是站台標語**）
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _netmall_extract_stock(ld: dict | None) -> bool:
        if isinstance(ld, dict):
            offers = ld.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                avail = str(offers.get("availability", "")).lower()
                if any(k in avail for k in ("outofstock", "soldout", "discontinued")):
                    print(f"[Netmall] 庫存狀態: 缺貨（JSON-LD availability={avail}）")
                    return False
                if "instock" in avail:
                    print(f"[Netmall] 庫存狀態: 可下單（JSON-LD InStock）")
                    return True
        print(f"[Netmall] 庫存狀態: 未知，預設可下單")
        return True

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取（JS 渲染，必須瀏覽器；等 .product-detail-price__main 出現就收）
    # ─────────────────────────────────────────────────────────────────
    def _netmall_get_html(self, url: str) -> str:
        """用 SeleniumBase UC 抓 netmall 頁面。價格元素出現＝JS 渲染完成。"""
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
                if 'product-detail-price__main' in html: score += 5  # 税込価格（JS 渲染完成）
                if 'application/ld+json' in html: score += 3
                if 'product-detail-num' in html: score += 2
                if 'product-detail-name' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                # 價格元素已出現＋頁面夠大 → 提早收工，省 RAM
                if i >= 1 and score >= 10 and len(html) > 30000:
                    print(f"[Netmall][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 10000:
                print(f"[Netmall][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[Netmall][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[Netmall] driver 失敗: {type(e).__name__}: {e}")
            return ""
