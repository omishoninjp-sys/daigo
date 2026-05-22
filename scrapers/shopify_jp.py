"""
Shopify 日本商店爬蟲 Mixin
價格策略（依序嘗試）：
  0. 頁面税込価優先（部分店家 variant.price 是税抜価，頁面才是税込）
  1. Cookie localization=JP + Accept-Language: ja
  2. URL 加 ?currency=JPY
  各方式都加 DEBUG log 顯示實際抓到的 raw price
"""
import json
import re
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, urljoin

import httpx

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


def _extract_price_from_html(html: str) -> tuple[int, str]:
    """從 data-selected-variant 取 price cents，回傳 (cents, raw_str)"""
    sv_match = re.search(r'data-selected-variant[^>]*>(\{[^<]+\})', html)
    if not sv_match:
        return 0, "NOT_FOUND"
    try:
        sv = json.loads(sv_match.group(1))
        cents = sv.get("price", 0)
        return cents, str(cents)
    except Exception as e:
        return 0, f"PARSE_ERROR:{e}"


def _extract_tax_included_price(html: str) -> int:
    """
    從頁面抓「実際に表示されている税込価格」。

    部分日本 Shopify 店（如 Bushiroad）後台 variant.price 設定為「税抜価」，
    但頁面實際售價是「税込価」。此時 .json / data-selected-variant 抓到的
    price 會少 10% 消費稅，導致代購售價算錯。

    策略：抓商品本體 <span class="price"> 區塊內含「販売価格 / 通常価格」
    且帶「(税込)」標示的金額。抓不到回傳 0（讓上層用原本邏輯）。

    回傳：税込価格（int, 日圓），找不到回傳 0
    """
    # Pattern 1：標準 Shopify price 區塊
    #   <span class="price"> ... 販売価格/通常価格 ... 33,000円 <span>(税込)</span>
    pat1 = re.compile(
        r'<span class="price">'
        r'(?:(?!</span>\s*</div>).)*?'              # 在這個 price span 範圍內
        r'(?:販売価格|通常価格|セール価格)'           # 必須含這些 label 之一
        r'.*?'
        r'([\d,]+)\s*円'                            # 金額
        r'\s*<span>\s*[（(]?\s*税込',               # 緊接 (税込)
        re.DOTALL,
    )
    m = pat1.search(html)
    if m:
        try:
            price = int(m.group(1).replace(",", ""))
            if 100 <= price <= 10_000_000:
                return price
        except (ValueError, TypeError):
            pass

    # Pattern 2：寬鬆版 — 任何 class 含 price 的元素，內含金額 + (税込)
    #   只取「第一個」(通常是商品本體，推薦商品在後面)
    pat2 = re.compile(
        r'class="[^"]*\bprice\b[^"]*"[^>]*>'
        r'(?:(?!</span>).)*?'
        r'([\d,]+)\s*円'
        r'\s*(?:<span>)?\s*[（(]?\s*税込',
        re.DOTALL,
    )
    m = pat2.search(html)
    if m:
        try:
            price = int(m.group(1).replace(",", ""))
            if 100 <= price <= 10_000_000:
                return price
        except (ValueError, TypeError):
            pass

    return 0


class ShopifyJpMixin:

    async def _scrape_shopify_jp(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.hostname}"

        path_parts = parsed.path.strip("/").split("/")
        handle = ""
        if "products" in path_parts:
            idx = path_parts.index("products")
            handle = path_parts[idx + 1] if idx + 1 < len(path_parts) else ""

        if not handle:
            return await self._scrape_with_playwright(url)

        json_url = f"{base_url}/products/{handle}.json"
        js_url   = f"{base_url}/products/{handle}.js"

        # ── 方法1：Cookie localization=JP
        headers_with_cookie = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Cookie": "localization=JP; cart_currency=JPY",
        }

        # ── 方法2：URL + currency=JPY
        url_with_currency = f"{url}{'&' if '?' in url else '?'}currency=JPY"

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:

                # ══ 方法1：Cookie ══
                print(f"[Shopify] 方法1 Cookie(localization=JP): {url}")
                r1 = await client.get(url, headers=headers_with_cookie)
                cents1, raw1 = _extract_price_from_html(r1.text)
                price1 = cents1 // 100 if cents1 > 10000 else cents1
                print(f"[Shopify DEBUG] 方法1 raw={raw1!r} → ¥{price1}")

                # ══ 方法2：?currency=JPY ══
                print(f"[Shopify] 方法2 ?currency=JPY: {url_with_currency}")
                r2 = await client.get(url_with_currency, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ja-JP,ja;q=0.9",
                })
                cents2, raw2 = _extract_price_from_html(r2.text)
                price2 = cents2 // 100 if cents2 > 10000 else cents2
                print(f"[Shopify DEBUG] 方法2 raw={raw2!r} → ¥{price2}")

                # ══ 決定使用哪個 ══
                # 優先選較大的（SGD 會比 JPY 小很多）
                if price1 > price2:
                    product.price_jpy = price1
                    print(f"[Shopify DEBUG] 採用方法1 ¥{price1}")
                    html = r1.text
                elif price2 > price1:
                    product.price_jpy = price2
                    print(f"[Shopify DEBUG] 採用方法2 ¥{price2}")
                    html = r2.text
                else:
                    product.price_jpy = price1
                    print(f"[Shopify DEBUG] 兩方法相同 ¥{price1}")
                    html = r1.text

                # ══ 方法0（最優先）：頁面実際表示の税込価 ══
                # 部分店家後台 variant.price 設「税抜価」，頁面顯示才是「税込価」
                # 例：Bushiroad — variant.price=30000(税抜)，頁面=33,000円(税込)
                # 若頁面抓到税込価且明顯高於 variant 價（差距 > 3%），採用税込価
                tax_inc_price = _extract_tax_included_price(html)
                if tax_inc_price > 0:
                    variant_price = product.price_jpy or 0
                    if variant_price > 0:
                        ratio = tax_inc_price / variant_price
                        # 税込価應該 ≥ 税抜価（約 1.08~1.12 倍）；若比例落在合理區間就採用
                        if 1.03 <= ratio <= 1.20:
                            print(
                                f"[Shopify DEBUG] ⚠️ 偵測到税抜価陷阱: "
                                f"variant ¥{variant_price}(税抜?) → 頁面 ¥{tax_inc_price}(税込), "
                                f"ratio={ratio:.3f} → 採用税込価 ¥{tax_inc_price}"
                            )
                            product.price_jpy = tax_inc_price
                        elif abs(ratio - 1.0) < 0.03:
                            # 幾乎相同 → variant 本來就是税込，不動
                            print(f"[Shopify DEBUG] 頁面税込価 ¥{tax_inc_price} ≈ variant 価，無税抜陷阱")
                        else:
                            # 差太多（可能抓到推薦商品價）→ 不信任，保留 variant 価
                            print(
                                f"[Shopify DEBUG] 頁面税込価 ¥{tax_inc_price} 與 variant ¥{variant_price} "
                                f"差距異常(ratio={ratio:.3f})，忽略，保留 variant 価"
                            )
                    else:
                        # variant 価抓不到 → 直接用頁面税込価
                        print(f"[Shopify DEBUG] variant 価缺失，採用頁面税込価 ¥{tax_inc_price}")
                        product.price_jpy = tax_inc_price

                # ── .json 取圖片/選項/商品資訊
                images, options, variants_json = [], [], []
                try:
                    jr = await client.get(json_url, headers={
                        "Accept": "application/json",
                        "Cookie": "localization=JP; cart_currency=JPY",
                    })
                    if jr.status_code == 200:
                        prod = jr.json().get("product", {})
                        product.title = prod.get("title", "")
                        product.brand = prod.get("vendor", "")
                        body = prod.get("body_html") or ""
                        product.description = re.sub(r'<[^>]+>', '', body).strip()[:500]
                        images = prod.get("images", [])
                        options = prod.get("options", [])
                        variants_json = prod.get("variants", [])
                        if images:
                            product.image_url = images[0]["src"]
                            product.extra_images = [img["src"] for img in images[1:5]]
                        print(f"[Shopify DEBUG] images count={len(images)}, extra={len(product.extra_images)}")

                        # 価格が取れていない場合 → variants[0].price から取得
                        if not product.price_jpy and variants_json:
                            raw_price = variants_json[0].get("price", "0")
                            try:
                                cents = int(str(raw_price).replace(",", "").replace(".", ""))
                                product.price_jpy = cents // 100 if cents > 100000 else cents
                                print(f"[Shopify DEBUG] .json variant price フォールバック → ¥{product.price_jpy}")
                            except Exception as pe:
                                print(f"[Shopify DEBUG] variant price 解析失敗: {pe}")
                except Exception as e:
                    print(f"[Shopify] .json 失敗: {e}")

                if not product.title:
                    t = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
                    product.title = t.group(1).strip() if t else ""

                # ── .js 取庫存
                available_map = {}
                try:
                    jsr = await client.get(js_url, headers={"Accept": "application/json"})
                    if jsr.status_code == 200:
                        for v in jsr.json().get("variants", []):
                            available_map[v["id"]] = v.get("available", False)
                        print(f"[Shopify DEBUG] available_map: { {k: v for k, v in available_map.items()} }")
                    else:
                        print(f"[Shopify DEBUG] .js HTTP {jsr.status_code}，庫存 fallback False")
                except Exception as e:
                    print(f"[Shopify DEBUG] .js 例外: {e}，庫存 fallback False")

                # ── 建立 variants
                for v in variants_json:
                    vid = v["id"]
                    in_stock = available_map.get(vid, False)
                    print(f"[Shopify DEBUG] variant {vid} in_stock={in_stock}")
                    option1 = v.get("option1", "") or ""
                    option2 = v.get("option2", "") or ""
                    option3 = v.get("option3", "") or ""
                    color, size = "", ""

                    for opt in options:
                        name = opt.get("name", "").lower()
                        pos  = opt.get("position", 1)
                        val  = option1 if pos == 1 else (option2 if pos == 2 else option3)
                        if any(k in name for k in ["color", "colour", "カラー", "色"]):
                            color = val
                        elif any(k in name for k in ["size", "サイズ", "寸"]):
                            size = val
                        elif not color:
                            color = val

                    img_src = ""
                    img_id = v.get("image_id")
                    if img_id:
                        for img in images:
                            if img["id"] == img_id:
                                img_src = img["src"]
                                break
                    if not img_src and images:
                        img_src = images[0]["src"]

                    product.variants.append({
                        "color":    color,
                        "size":     size,
                        "in_stock": in_stock,
                        "image":    img_src,
                        "sku":      v.get("sku", ""),
                    })

                print(f"[Shopify] ✅ {product.title[:40]} / ¥{product.price_jpy} ({len(product.variants)} variants)")
                return product

        except Exception as e:
            print(f"[Shopify] 例外: {type(e).__name__}: {e}，改用 Playwright")

        return await self._scrape_with_playwright(url)
