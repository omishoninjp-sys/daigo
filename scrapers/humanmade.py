"""
Human Made (humanmade.jp) 爬蟲 Mixin

平台變更紀錄（2026-04）：
- 舊：SFCC（Salesforce Commerce Cloud），自建商品頁 `/shoes/XXX.html`
- 新：Shopify，標準 URL `/products/<handle>`
- 舊 URL 仍可訪問（透過 redirect）但前端 selector 已全變

策略：
1. 主路徑：URL 抽出 handle → 打 Shopify `.json` API → 拿到完整 variants（color × size）
2. Fallback：HTML 解析（JSON-LD → ProductJson script → meta → 文字）
3. 維持原本的 driver fetch（有 WAF/Cloudflare 防護，httpx 抓不到）

URL 範例：
  https://www.humanmade.jp/shoes/XX31GD063.html       ← 舊 SFCC 風格 URL（仍可訪問）
  https://humanmade.jp/products/xx31gd063              ← 新 Shopify 標準 URL
  https://humanmade.jp/products/xx31gd063.json         ← Shopify 公開 API
  https://humanmade.jp/en/products/xx31gd063          ← 英文版（去 /en/）
"""
import json
import re
import time as _time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


# 從各種 URL 格式抽出 Shopify handle（小寫）
_HANDLE_FROM_PRODUCTS = re.compile(r'/products/([^/?#]+)', re.IGNORECASE)
_HANDLE_FROM_LEGACY = re.compile(r'/[a-z]+/([A-Za-z0-9]+)\.html', re.IGNORECASE)


def _extract_handle(url: str) -> str | None:
    """從 URL 抽出 Shopify product handle（小寫）"""
    parsed = urlparse(url)
    path = parsed.path

    # 1. /products/<handle>
    m = _HANDLE_FROM_PRODUCTS.search(path)
    if m:
        return m.group(1).lower().replace('.html', '')

    # 2. 舊 SFCC 風格 /<category>/<HANDLE>.html
    m = _HANDLE_FROM_LEGACY.search(path)
    if m:
        return m.group(1).lower()

    return None


def _strip_lang_prefix(url: str) -> str:
    """去除 /en/ /zh-CHT/ 等語系前綴"""
    return re.sub(r'^/(?:en|zh-CHT|zh-CN|ko)(/|$)', r'\1', urlparse(url).path) or url


class HumanMadeMixin:

    async def _scrape_humanmade(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Human Made")

        # ── URL 正規化：去語系前綴
        url = re.sub(r'humanmade\.jp/(?:en|zh-CHT|zh-CN|ko)/', 'humanmade.jp/', url, flags=re.IGNORECASE)

        # ── 抽 handle
        handle = _extract_handle(url)
        if not handle:
            print(f"[HumanMade] ⚠️ 無法從 URL 抽出 handle: {url}")

        # ── 主路徑：Shopify .json API
        if handle:
            json_url = f"https://humanmade.jp/products/{handle}.json"
            print(f"[HumanMade] 嘗試 Shopify JSON API: {json_url}")
            json_data = await self._humanmade_fetch_json(json_url)
            if json_data:
                self._humanmade_parse_shopify_json(json_data, product, handle)
                # JSON 抓到了核心資料，但描述補從 HTML
                if not product.description:
                    html = await self._humanmade_fetch_html(url)
                    if html:
                        self._humanmade_parse_description(html, product)
                if product.is_valid:
                    print(
                        f"[HumanMade] ✅ JSON: {product.title} / ¥{product.price_jpy} / "
                        f"variants={len(product.variants)}"
                    )
                    return product

        # ── Fallback：HTML 解析
        print(f"[HumanMade] JSON 失敗，fallback 到 HTML 解析")
        html = await self._humanmade_fetch_html(url)
        if not html:
            print(f"[HumanMade] ❌ 無法取得 HTML: {url}")
            return product

        try:
            self._humanmade_parse_html(html, product, url)
        except Exception as e:
            print(f"[HumanMade] ❌ HTML 解析失敗: {type(e).__name__}: {e}")

        return product

    # ─────────────────────────────────────────────────────────────────
    # Shopify JSON API 解析
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_parse_shopify_json(self, data: dict, product: ProductInfo, handle: str) -> None:
        """解析 Shopify product.json 回傳的資料"""
        try:
            p = data.get("product") or data
            if not isinstance(p, dict):
                return

            # 標題
            title = p.get("title") or ""
            if title:
                product.title = title

            # 品牌（Shopify 是 vendor）
            vendor = p.get("vendor") or ""
            if vendor:
                product.brand = vendor

            # 描述
            body_html = p.get("body_html") or ""
            if body_html:
                product.description = BeautifulSoup(body_html, "html.parser").get_text("\n", strip=True)

            # 圖片
            images = []
            for img in (p.get("images") or []):
                src = img.get("src") if isinstance(img, dict) else (img if isinstance(img, str) else None)
                if src:
                    if src.startswith("//"):
                        src = "https:" + src
                    images.append(src)

            if images:
                product.image_url = images[0]
                product.extra_images = images[1:8]

            # Options 欄位順序：判斷 color / size 是 option1 還是 option2
            options = p.get("options") or []
            color_idx = -1
            size_idx = -1
            for i, opt in enumerate(options):
                name = (opt.get("name") if isinstance(opt, dict) else str(opt)).lower()
                if any(k in name for k in ["color", "colour", "カラー", "色"]):
                    color_idx = i
                elif any(k in name for k in ["size", "サイズ", "尺寸"]):
                    size_idx = i

            # Variants
            variants_raw = p.get("variants") or []
            color_to_image: dict[str, str] = {}

            # 先建立 color → variant_image 對應（從 variant.featured_image）
            for v in variants_raw:
                if not isinstance(v, dict):
                    continue
                color = ""
                if color_idx >= 0:
                    color = (v.get(f"option{color_idx + 1}") or "").strip()
                feat = v.get("featured_image")
                if color and feat:
                    img_src = feat.get("src") if isinstance(feat, dict) else feat
                    if img_src and color not in color_to_image:
                        if img_src.startswith("//"):
                            img_src = "https:" + img_src
                        color_to_image[color] = img_src

            # 確保主圖優先用第一個有顏色的 variant 圖
            if color_to_image and not product.image_url:
                product.image_url = next(iter(color_to_image.values()))

            # 統一價格（取第一個 variant 的價格作為主價，特殊情況下用 min）
            prices = []
            for v in variants_raw:
                if isinstance(v, dict):
                    raw_price = v.get("price")
                    val = self._humanmade_to_int(raw_price)
                    if val:
                        prices.append(val)

            if prices:
                # Shopify price 通常是字串 "5500" 直接是日圓整數
                # 如果是 "55.00" 就是分（cent），但 humanmade.jp 是 JPY 不會有分
                product.price_jpy = min(prices)

            # 組 variants
            variant_list = []
            for v in variants_raw:
                if not isinstance(v, dict):
                    continue
                color = ""
                size = ""
                if color_idx >= 0:
                    color = (v.get(f"option{color_idx + 1}") or "").strip()
                if size_idx >= 0:
                    size = (v.get(f"option{size_idx + 1}") or "").strip()

                v_price = self._humanmade_to_int(v.get("price")) or product.price_jpy or 0
                v_avail = v.get("available", True)

                variant_image = color_to_image.get(color) or product.image_url

                label_parts = [p_ for p_ in [color, size] if p_]
                variant_list.append({
                    "color": color,
                    "size": size,
                    "sku": (v.get("sku") or f"hm-{handle}-{'-'.join(label_parts)}").lower().replace(" ", "-"),
                    "price": v_price,
                    "in_stock": bool(v_avail),
                    "image": variant_image or "",
                })
            product.variants = variant_list

            # 整體庫存：任一 variant 有貨就算有貨
            product.in_stock = any(v["in_stock"] for v in variant_list) if variant_list else True

            # 更新 source_url 為標準 Shopify URL
            product.source_url = f"https://humanmade.jp/products/{handle}"

        except Exception as e:
            print(f"[HumanMade] ❌ Shopify JSON 解析錯誤: {type(e).__name__}: {e}")

    @staticmethod
    def _humanmade_to_int(value) -> int | None:
        """Shopify price 可能是 '5500' / '5500.00' / 5500 / None"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip()
            if not s:
                return None
            # Shopify .json 的 price 通常是字串「5500」直接整數，但也可能是「5500.00」
            try:
                v = int(float(s))
            except (ValueError, TypeError):
                return None
        if 100 <= v <= 10_000_000:
            return v
        return None

    # ─────────────────────────────────────────────────────────────────
    # HTML fallback 解析
    # ─────────────────────────────────────────────────────────────────
    def _humanmade_parse_html(self, html: str, product: ProductInfo, url: str) -> None:
        """HTML fallback：從頁面找 Shopify 內嵌的 product JSON 或 JSON-LD"""
        soup = BeautifulSoup(html, "html.parser")

        # 1. Shopify ProductJson script tag（最完整）
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            sid = script.get("id", "") or ""
            if "product" in sid.lower() or "ProductJson" in sid:
                try:
                    data = json.loads(script.string or "{}")
                    if isinstance(data, dict) and (data.get("variants") or data.get("product")):
                        handle = _extract_handle(url) or data.get("handle", "")
                        self._humanmade_parse_shopify_json(
                            {"product": data} if not data.get("product") else data,
                            product,
                            handle,
                        )
                        if product.is_valid:
                            print(f"[HumanMade] ✅ HTML 內嵌 ProductJson 命中")
                            return
                except (json.JSONDecodeError, AttributeError):
                    continue

        # 2. JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") not in ("Product", "ProductGroup"):
                        continue
                    if not product.title:
                        product.title = item.get("name", "")
                    if not product.brand:
                        brand = item.get("brand")
                        if isinstance(brand, dict):
                            product.brand = brand.get("name", "Human Made")
                        elif isinstance(brand, str):
                            product.brand = brand
                    if not product.image_url:
                        img = item.get("image")
                        if isinstance(img, list) and img:
                            product.image_url = img[0]
                        elif isinstance(img, str):
                            product.image_url = img
                    offers = item.get("offers")
                    if isinstance(offers, dict):
                        v = self._humanmade_to_int(offers.get("price") or offers.get("lowPrice"))
                        if v:
                            product.price_jpy = v
                    elif isinstance(offers, list):
                        prices = []
                        for off in offers:
                            if isinstance(off, dict):
                                v = self._humanmade_to_int(off.get("price"))
                                if v:
                                    prices.append(v)
                        if prices:
                            product.price_jpy = min(prices)
            except (json.JSONDecodeError, AttributeError):
                continue

        # 3. 標題 fallback
        if not product.title:
            for sel_args in [("h1", {}), ("meta", {"property": "og:title"})]:
                el = soup.find(*sel_args[:1], **(sel_args[1] if len(sel_args) > 1 else {}))
                if el:
                    txt = el.get("content") if el.name == "meta" else el.get_text(strip=True)
                    if txt:
                        product.title = txt.strip()
                        break

        # 4. 圖片 fallback (og:image)
        if not product.image_url:
            og = soup.find("meta", attrs={"property": "og:image"})
            if og and og.get("content"):
                product.image_url = og["content"]

        # 5. 價格 fallback
        if not product.price_jpy:
            # 從 meta 找
            for sel in [
                {"property": "og:price:amount"},
                {"property": "product:price:amount"},
                {"itemprop": "price"},
            ]:
                el = soup.find("meta", attrs=sel) or soup.find(attrs=sel)
                if el:
                    val = el.get("content") or el.get_text(strip=True)
                    v = self._humanmade_to_int(val)
                    if v:
                        product.price_jpy = v
                        break

            # 從文字找 ¥xxxx
            if not product.price_jpy:
                page_text = soup.get_text(" ", strip=True)
                # 優先找 (税込) 附近
                m = re.search(r'[¥￥]\s*([\d,]+)\s*[（(]\s*税込', page_text)
                if not m:
                    m = re.search(r'[¥￥]\s*([1-9][\d,]{3,})', page_text)
                if m:
                    v = self._humanmade_to_int(m.group(1))
                    if v:
                        product.price_jpy = v

        self._humanmade_parse_description(html, product)
        print(
            f"[HumanMade] HTML fallback 結果: {product.title} / "
            f"¥{product.price_jpy} / variants={len(product.variants)}"
        )

    @staticmethod
    def _humanmade_parse_description(html: str, product: ProductInfo) -> None:
        """從 HTML 抽出描述（補強用）"""
        if product.description:
            return
        try:
            soup = BeautifulSoup(html, "html.parser")
            for sel in [
                {"class_": re.compile(r"product.*description", re.I)},
                {"class_": re.compile(r"description", re.I)},
                {"id": re.compile(r"description", re.I)},
            ]:
                el = soup.find(**sel)
                if el:
                    text = el.get_text("\n", strip=True)
                    if 20 < len(text) < 5000:
                        product.description = text
                        return
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # JSON 抓取（用 driver 繞過 WAF）
    # ─────────────────────────────────────────────────────────────────
    async def _humanmade_fetch_json(self, json_url: str) -> dict | None:
        """用 driver 開 Shopify .json URL，page_source 會包含 JSON 文字"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1
                    self._clean_driver_tabs()

                    try:
                        driver.uc_open_with_reconnect(json_url, reconnect_time=4)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__ or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    # 等待 JSON 載入
                    raw = ""
                    for i in range(5):
                        _time.sleep(1.5)
                        try:
                            # JSON URL 通常瀏覽器會包成 <pre>{...}</pre>
                            raw = driver.execute_script("return document.body.innerText;") or ""
                        except Exception:
                            try:
                                raw = driver.page_source
                            except Exception:
                                continue

                        if raw and ('"product"' in raw or '"variants"' in raw or '"handle"' in raw):
                            break

                    if not raw:
                        return None

                    # 嘗試直接 parse
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        pass

                    # page_source 模式：從 HTML 抽出 JSON
                    try:
                        soup = BeautifulSoup(raw, "html.parser")
                        pre = soup.find("pre") or soup.find("body")
                        if pre:
                            txt = pre.get_text(strip=True)
                            if txt.startswith("{"):
                                return json.loads(txt)
                    except (json.JSONDecodeError, AttributeError):
                        pass

                    # 最後嘗試從 raw 中正則抽 JSON
                    m = re.search(r'\{.*"product"\s*:.*\}', raw, re.DOTALL)
                    if m:
                        try:
                            return json.loads(m.group(0))
                        except json.JSONDecodeError:
                            pass

                    print(f"[HumanMade] JSON 抓到資料但無法 parse: {raw[:200]}")
                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[HumanMade] JSON fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取（保留原邏輯）
    # ─────────────────────────────────────────────────────────────────
    async def _humanmade_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase UC driver 取得 JS 渲染後的 HTML"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1
                    self._clean_driver_tabs()

                    try:
                        driver.uc_open_with_reconnect(url, reconnect_time=6)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__ or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    # 等待頁面渲染，關閉彈窗
                    html = ""
                    session_dead = False
                    for i in range(8):
                        _time.sleep(2)
                        try:
                            # 嘗試關閉 Global-e 彈窗
                            driver.execute_script("""
                                const ge = document.getElementById('globalePopupWrapper');
                                if (ge) ge.remove();
                                document.querySelectorAll('[class*="globale"], [id*="globale"]').forEach(el => {
                                    try {
                                        if (getComputedStyle(el).position === 'fixed') el.remove();
                                    } catch(e) {}
                                });
                            """)
                        except Exception:
                            pass

                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # 確認頁面已載入（有商品標題區塊 or 有 ¥ 金額 or 有 Shopify ProductJson）
                        if i >= 1 and len(html) > 5000 and (
                            '¥' in html or 'product' in html.lower() or 'shopify' in html.lower()
                        ):
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 5000:
                        return html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[HumanMade] HTML fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
