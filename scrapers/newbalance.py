"""
newbalance.py  –  New Balance Japan 爬蟲 Mixin
https://shop.newbalance.jp/

SFCC (Salesforce Commerce Cloud) 平台，使用內建 Product-Variation API。

策略：
  1. httpx 抓 HTML → 解析 JSON-LD 取商品名/描述/master image
  2. 從 HTML 取 data-attr-value（style/color codes、sizes）
  3. 對每個 color code 呼叫 Product-Variation API 取得色名、圖片
  4. Product-GetInventoryJSON API 取在庫
  5. 組合全 variants
"""
import asyncio
import re
import json
import time

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo

_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://shop.newbalance.jp/",
}


class NewBalanceMixin:

    async def _scrape_newbalance(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="New Balance")

        # URL から master product ID を取得
        # 例: /pd/I996V3_KL-FTW-823690.html
        pid_m = re.search(r'/pd/([^/?#]+?)(?:\.html)?(?:[?#]|$)', url)
        if not pid_m:
            print(f"[NewBalance] ❌ PID 解析失敗: {url}")
            return product

        master_pid = pid_m.group(1)
        # dwvar パラメータ（URLに含まれる場合）
        style_m = re.search(r'dwvar_[^_]+__[^_]+_style=(\w+)', url)
        url_style = style_m.group(1) if style_m else ""

        print(f"[NewBalance] master_pid={master_pid}, url_style={url_style}")

        base_url = "https://shop.newbalance.jp"
        # 同じ URL パターンで SFCC の product key を構成
        # I996V3_KL-FTW-823690 → dwvar キーは I996V3__KL-FTW-823690（アンダースコア2つ）
        pid_key = master_pid.replace("-", "-", 1)
        # SFCC の dwvar キーは最初の _ を __ に変換
        dwvar_key = re.sub(r'_', '__', master_pid, count=1)

        # ── Step 1: HTML 取得（SeleniumBase UC）
        html, nb_cookies, nb_driver = await asyncio.to_thread(self._newbalance_get_html, url)
        if not html:
            print(f"[NewBalance] ❌ HTML 取得失敗")
            return product
        print(f"[NewBalance] HTML: {len(html)} bytes, cookies: {len(nb_cookies)}")

        print(f"[NewBalance] 渡す cookies: {len(nb_cookies)} 個")

        async def browser_get_json(api_url: str):
            """ブラウザ内 fetch で JSON 取得"""
            if nb_driver is None:
                return None
            return await asyncio.to_thread(self._newbalance_fetch_api, nb_driver, api_url)

        if True:  # インデント維持用

            # ── Step 1 完了（HTML は上で取得済み）

            soup = BeautifulSoup(html, "html.parser")

            # ── JSON-LD から基本情報
            for tag in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(tag.string or "")
                    if ld.get("@type") == "Product":
                        product.title = product.title or ld.get("name", "")
                        product.description = product.description or ld.get("description", "")[:600]
                        img = ld.get("image", "")
                        if img and not product.image_url:
                            # scene7 URL を高解像度に変換
                            img = re.sub(r'\?\$[^$]+\$', '', img)  # クエリ除去
                            product.image_url = img
                        break
                except Exception:
                    pass

            # h1 fallback
            if not product.title:
                h1 = soup.find("h1")
                if h1:
                    product.title = h1.get_text(strip=True)

            # ── 価格: .price .sales span[content]
            price_span = soup.select_one(".price .sales span[content]")
            if price_span:
                try:
                    product.price_jpy = int(price_span.get("content", "0"))
                    print(f"[NewBalance] 価格: ¥{product.price_jpy}")
                except Exception:
                    pass

            # fallback: テキストから正規表現
            if not product.price_jpy:
                price_el = soup.select_one(".price")
                if price_el:
                    m = re.search(r'([\d,]+)円', price_el.get_text())
                    if m:
                        product.price_jpy = int(m.group(1).replace(",", ""))

            # ── color codes と size codes を HTML から取得
            color_codes = []
            size_values = []
            seen_c = set()
            seen_s = set()

            for el in soup.find_all(attrs={"data-attr": "style-value"}):
                val = el.get("data-attr-value")
                if val and val not in seen_c:
                    seen_c.add(val)
                    color_codes.append(val)

            # data-attr-value で style っぽいものを取得（data-attr なし場合）
            if not color_codes:
                for el in soup.find_all(attrs={"data-attr-value": True}):
                    val = el.get("data-attr-value", "")
                    # 色コードは英数字大文字、サイズは数字
                    if re.match(r'^[A-Z0-9]{5,}$', val) and val not in seen_c:
                        seen_c.add(val)
                        color_codes.append(val)

            for el in soup.find_all(attrs={"data-attr": "size-value"}):
                val = el.get("data-attr-value")
                if val and val not in seen_s:
                    seen_s.add(val)
                    size_values.append(val)

            if not size_values:
                for el in soup.find_all(attrs={"data-attr-value": True}):
                    val = el.get("data-attr-value", "")
                    if re.match(r'^\d+(\.\d+)?$', val) and val not in seen_s:
                        seen_s.add(val)
                        size_values.append(val)

            print(f"[NewBalance] colors={color_codes}, sizes={size_values}")

            # ── Step 2: 在庫 API
            stock_map: dict[str, bool] = {}
            try:
                inv_url = f"{base_url}/on/demandware.store/Sites-NBJP-Site/ja_JP/Product-GetInventoryJSON?pid={master_pid}"
                inv_data = await browser_get_json(inv_url)
                if inv_data is not None:
                    # {"variants": {"I99690I_4.5": {"available": true}, ...}}
                    for k, v in (inv_data.get("variants") or {}).items():
                        stock_map[k] = v.get("available", False)
                    # トップレベルにある場合
                    if not stock_map:
                        for k, v in inv_data.items():
                            if isinstance(v, dict):
                                stock_map[k] = v.get("available", v.get("inStock", False))
                    print(f"[NewBalance] 在庫 map: {len(stock_map)} entries")
            except Exception as e:
                print(f"[NewBalance] 在庫 API 失敗: {e}")

            # ── Step 3: 各 color に対して Product-Variation API を呼んで色名・画像を取得
            color_info: dict[str, dict] = {}  # code → {name, image}

            for color_code in color_codes:
                try:
                    first_size = size_values[0] if size_values else "6.5"
                    var_url = (
                        f"{base_url}/on/demandware.store/Sites-NBJP-Site/ja_JP/Product-Variation"
                        f"?dwvar_{dwvar_key}_size={first_size}"
                        f"&dwvar_{dwvar_key}_style={color_code}"
                        f"&dwvar_{dwvar_key}_width=W"
                        f"&pid={master_pid}&quantity=1"
                    )
                    referer_url = f"{url.split('?')[0]}?dwvar_{dwvar_key}_style={color_code}"
                    var_headers = {**_API_HEADERS, "Referer": referer_url}
                    var_data = await browser_get_json(var_url)
                    if var_data is not None:
                        # product.variationAttributes から色名と在庫を取得
                        color_name = color_code
                        color_img = ""
                        for attr in (var_data.get("product", {}).get("variationAttributes") or []):
                            if attr.get("attributeId") == "style":
                                for v in attr.get("values") or []:
                                    if v.get("id") == color_code or v.get("value") == color_code:
                                        color_name = v.get("displayValue") or v.get("value") or color_code
                                        # images は dict: {'pdpColorWay': [{absURL, src, ...}]}
                                        imgs_dict = v.get("images") or {}
                                        pdp = imgs_dict.get("pdpColorWay") or []
                                        if pdp:
                                            color_img = pdp[0].get("absURL") or pdp[0].get("url", "")
                                        break
                        # 画像 fallback: product.images.large[0]
                        if not color_img:
                            imgs = var_data.get("product", {}).get("images", {})
                            large = imgs.get("large") or imgs.get("medium") or []
                            if large:
                                color_img = large[0].get("url", "")
                        color_info[color_code] = {"name": color_name, "image": color_img}
                        print(f"[NewBalance] color {color_code} → {color_name}, img={'✅' if color_img else '❌'}")
                        # size の selectable から在庫マップを構築（在庫 API の代替）
                        for attr in (var_data.get("product", {}).get("variationAttributes") or []):
                            if attr.get("attributeId") == "size":
                                for v in attr.get("values") or []:
                                    size_id = v.get("id") or v.get("value", "")
                                    key = f"{color_code}_{size_id}"
                                    stock_map[key] = v.get("selectable", False)
                    else:
                        color_info[color_code] = {"name": color_code, "image": ""}
                        print(f"[NewBalance] Product-Variation {color_code}: fetch 返り null")
                except Exception as e:
                    color_info[color_code] = {"name": color_code, "image": ""}
                    print(f"[NewBalance] color API 失敗 {color_code}: {e}")

            # ── Step 4: variants 組み立て
            if color_codes and size_values:
                for color_code in color_codes:
                    ci = color_info.get(color_code, {})
                    color_name = ci.get("name", color_code)
                    color_img = ci.get("image", "")

                    for size in size_values:
                        # 在庫キーのパターン（複数試す）
                        in_stock = True
                        for key_pat in [
                            f"{color_code}_{size}",
                            f"{master_pid}_{color_code}_{size}",
                            f"{color_code}-{size}",
                        ]:
                            if key_pat in stock_map:
                                in_stock = stock_map[key_pat]
                                break
                        else:
                            # デフォルトは在庫ありとして扱う（stock_map が空の場合）
                            if stock_map:
                                in_stock = False  # stock_map があるなら未登録 = 在庫なし

                        product.variants.append({
                            "color": color_name,
                            "size": size,
                            "sku": f"{master_pid}-{color_code}-{size}",
                            "price": product.price_jpy or 0,
                            "in_stock": in_stock,
                            "image": color_img,
                        })

            elif color_codes:
                for color_code in color_codes:
                    ci = color_info.get(color_code, {})
                    in_stock = stock_map.get(color_code, True)
                    product.variants.append({
                        "color": ci.get("name", color_code),
                        "size": "",
                        "sku": f"{master_pid}-{color_code}",
                        "price": product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image": ci.get("image", ""),
                    })

            # ── 画像補完
            if color_info and not product.image_url:
                for ci in color_info.values():
                    if ci.get("image"):
                        product.image_url = ci["image"]
                        break

            extra = []
            for ci in color_info.values():
                img = ci.get("image", "")
                if img and img != product.image_url and img not in extra:
                    extra.append(img)
            product.extra_images = extra[:8]

            print(
                f"[NewBalance] ✅ {product.title!r} | "
                f"¥{product.price_jpy} | variants={len(product.variants)}"
            )

        return product

    def _newbalance_get_html(self, url: str) -> tuple[str, dict, object]:
        """SeleniumBase UC で HTML 取得、Cookie と driver も返す"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return "", {}, None
            self._clean_driver_tabs()
            driver.get(url)
            time.sleep(3)
            html = driver.page_source
            self._driver_use_count += 1
            cookies = {}
            try:
                for c in driver.get_cookies():
                    cookies[c["name"]] = c["value"]
            except Exception:
                pass
            return html, cookies, driver
        except Exception as e:
            print(f"[NewBalance] SeleniumBase 失敗: {type(e).__name__}: {e}")
            return "", {}, None

    def _newbalance_fetch_api(self, driver, api_url: str):
        """ブラウザ内 fetch() で API を呼び出す（Akamai 回避）"""
        try:
            # execute_async_script: 最後の引数が callback
            js = """
var callback = arguments[arguments.length - 1];
fetch(arguments[0], {
    method: 'GET',
    headers: {'Accept': '*/*', 'X-Requested-With': 'XMLHttpRequest'},
    credentials: 'include'
})
.then(function(r) {
    if (!r.ok) { callback(null); return; }
    return r.json();
})
.then(function(data) { callback(data); })
.catch(function(e) { callback(null); });
"""
            driver.set_script_timeout(15)
            result = driver.execute_async_script(js, api_url)
            return result
        except Exception as e:
            print(f"[NewBalance] browser fetch 失敗: {e}")
            return None
