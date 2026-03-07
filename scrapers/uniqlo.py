"""
UNIQLO JP 爬蟲 Mixin
使用內部 API + HTML 解析（不需瀏覽器）
"""
import re
import json

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo, normalize_price


class UniqloMixin:

    async def _scrape_uniqlo(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="UNIQLO")

        m = re.search(r'/products/(E?\d[\w-]+)', url)
        if not m:
            print(f"[Uniqlo] ❌ 無法從 URL 提取商品代碼: {url}")
            return product

        product_code = m.group(1)
        product_id = re.sub(r'[^0-9]', '', product_code.split('-')[0])

        color_from_url = ""
        cm = re.search(r'colorDisplayCode=(\w+)', url)
        if cm:
            color_from_url = cm.group(1)

        print(f"[Uniqlo] 商品代碼: {product_code} (ID: {product_id}, color: {color_from_url})")

        browser_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            cookies = {}
            html_text = ""
            try:
                print(f"[Uniqlo] Step 1: 抓 HTML 頁面...")
                resp = await client.get(url, headers=browser_headers)
                html_text = resp.text
                cookies = dict(resp.cookies)
                print(f"[Uniqlo] HTML: {resp.status_code}, {len(html_text)} bytes, cookies: {list(cookies.keys())[:5]}")
                self._parse_uniqlo_html(html_text, product_id, product)
            except Exception as e:
                print(f"[Uniqlo] HTML 抓取錯誤: {type(e).__name__}: {e}")

            embedded_found = False
            if html_text:
                embedded_found = self._parse_uniqlo_embedded_json(html_text, product_code, product_id, product)
                if embedded_found and product.price_jpy and product.variants:
                    print(f"[Uniqlo] ✅ 內嵌 JSON 解析成功: {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
                    return product

            api_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
                "Referer": url,
                "Origin": "https://www.uniqlo.com",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "X-Requested-With": "XMLHttpRequest",
            }

            api_urls = [
                f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products?productIds={product_code}&withPrices=true&withStocks=true&withColors=true&withSizes=true&httpFailure=true",
                f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products?productIds={product_id}&withPrices=true&withStocks=true&withColors=true&withSizes=true",
                f"https://www.uniqlo.com/jp/api/commerce/v3/ja/products?productIds={product_code}",
            ]

            for api_url in api_urls:
                try:
                    print(f"[Uniqlo] Step 3: API {api_url[:90]}...")
                    resp = await client.get(api_url, headers=api_headers, cookies=cookies)
                    print(f"[Uniqlo] API response: {resp.status_code}, {len(resp.text)} bytes")

                    if resp.status_code == 200:
                        api_data = resp.json()
                        print(f"[Uniqlo] API keys: {list(api_data.keys())[:8]}")
                        product = self._parse_uniqlo_api(api_data, product_code, product_id, product)
                        if product.price_jpy and product.variants:
                            print(f"[Uniqlo] ✅ API 完整解析: {product.title[:40]} / ¥{product.price_jpy:,} / {len(product.variants)} variants")
                            return product
                        elif product.price_jpy:
                            print(f"[Uniqlo] API 取得價格 ¥{product.price_jpy:,} 但無 variants，繼續 fallback")
                            break
                        else:
                            print(f"[Uniqlo] API 回傳但未找到價格")
                    elif resp.status_code == 403:
                        print(f"[Uniqlo] API 403 Forbidden")
                    elif resp.status_code == 404:
                        print(f"[Uniqlo] API 404")
                    else:
                        print(f"[Uniqlo] API {resp.status_code}: {resp.text[:200]}")

                except Exception as e:
                    print(f"[Uniqlo] API 錯誤: {type(e).__name__}: {e}")

            if product.title and not product.variants:
                print(f"[Uniqlo] Step 4: 用 HTML 資料建構基本 variants")
                product = self._build_uniqlo_fallback_variants(product, product_id, color_from_url, html_text)

        if product.title:
            print(f"[Uniqlo] 最終結果: {product.title[:40]} / ¥{product.price_jpy or '?'} / {len(product.variants)} variants")
        else:
            print(f"[Uniqlo] ⚠️ 未取得資料")

        return product

    def _parse_uniqlo_embedded_json(self, html: str, product_code: str, product_id: str, product: ProductInfo) -> bool:
        soup = BeautifulSoup(html, "html.parser")

        for script in soup.find_all("script"):
            text = script.string or ""
            if not text or len(text) < 100:
                continue

            if "__NEXT_DATA__" in text or "window.__NEXT_DATA__" in text:
                try:
                    jm = re.search(r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</)', text, re.DOTALL)
                    if jm:
                        next_data = json.loads(jm.group(1))
                        props = next_data.get("props", {}).get("pageProps", {})
                        if props:
                            print(f"[Uniqlo] 找到 __NEXT_DATA__: keys={list(props.keys())[:5]}")
                            for key in ["product", "productDetail", "data", "initialData"]:
                                if key in props:
                                    self._parse_uniqlo_api({"result": {"items": {product_code: props[key]}}}, product_code, product_id, product)
                                    if product.price_jpy:
                                        return True
                except Exception as e:
                    print(f"[Uniqlo] __NEXT_DATA__ 解析錯誤: {e}")

            for pattern in [r'__INITIAL_STATE__\s*=\s*({.+?})\s*;',
                           r'window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*;',
                           r'window\.PRODUCT_DATA\s*=\s*({.+?})\s*;']:
                try:
                    sm = re.search(pattern, text, re.DOTALL)
                    if sm:
                        state = json.loads(sm.group(1))
                        print(f"[Uniqlo] 找到全域狀態: keys={list(state.keys())[:5]}")
                        self._parse_uniqlo_api(state, product_code, product_id, product)
                        if product.price_jpy:
                            return True
                except:
                    pass

            if product_id in text and ("price" in text.lower() or "prices" in text.lower()):
                for jm in re.finditer(r'\{[^{}]*"' + re.escape(product_id) + r'"[^{}]*\}', text):
                    try:
                        chunk = json.loads(jm.group())
                        if "price" in str(chunk).lower():
                            self._parse_uniqlo_api(chunk, product_code, product_id, product)
                            if product.price_jpy:
                                return True
                    except:
                        pass

        return False

    def _parse_uniqlo_api(self, data: dict, product_code: str, product_id: str, product: ProductInfo) -> ProductInfo:
        items = {}
        if "result" in data:
            result = data["result"]
            items = result.get("items", {}) or result.get("products", {}) or {}
            if isinstance(items, list):
                items = {str(i.get("productId", i.get("id", idx))): i for idx, i in enumerate(items) if isinstance(i, dict)}
        elif "items" in data:
            items = data["items"]
        elif "products" in data:
            items = data["products"]

        prod = items.get(product_code) or items.get(product_id)
        if not prod:
            for k, v in items.items():
                if product_id in str(k):
                    prod = v
                    break
        if not prod and items:
            prod = next(iter(items.values()))
        if not prod:
            if "name" in data or "productName" in data or "productId" in data:
                prod = data

        if not prod:
            print(f"[Uniqlo] API 回傳中找不到商品: keys={list(data.keys())[:5]}")
            return product

        name = prod.get("name") or prod.get("productName") or prod.get("title") or ""
        if name:
            product.title = name

        price = self._extract_uniqlo_price(prod)
        if price and price > 0:
            product.price_jpy = price

        images = prod.get("images", {}) or {}
        img_urls = []

        if isinstance(images, dict):
            for img_key in ["main", "sub", "chip", "swatch"]:
                img_list = images.get(img_key, []) or []
                if isinstance(img_list, dict):
                    img_list = list(img_list.values())
                for img in img_list:
                    u = ""
                    if isinstance(img, str):
                        u = img
                    elif isinstance(img, dict):
                        u = img.get("url") or img.get("image") or img.get("src") or ""
                    if u and u not in img_urls:
                        img_urls.append(u)
        elif isinstance(images, list):
            for img in images:
                u = img.get("url", "") if isinstance(img, dict) else str(img)
                if u:
                    img_urls.append(u)

        if not img_urls:
            img_urls.append(f"https://image.uniqlo.com/UQ/ST3/jp/imagesgoods/{product_id}/item/jpgoods_69_{product_id}_3x4.jpg?width=600")

        if img_urls and not product.image_url:
            product.image_url = img_urls[0]
        if len(img_urls) > 1 and not product.extra_images:
            product.extra_images = img_urls[1:9]

        colors = prod.get("colors", {}) or {}
        sizes = prod.get("sizes", {}) or {}
        l2s = prod.get("l2s", []) or prod.get("stocks", []) or []

        variants = []

        if isinstance(colors, dict) and isinstance(sizes, dict) and colors and sizes:
            for color_code, color_info in colors.items():
                color_name = ""
                color_img = ""
                if isinstance(color_info, dict):
                    color_name = color_info.get("displayColorName") or color_info.get("name") or color_code
                    ci = color_info.get("image")
                    if isinstance(ci, dict):
                        color_img = ci.get("url") or ci.get("src") or ""
                    elif isinstance(ci, str):
                        color_img = ci
                    if not color_img:
                        color_img = f"https://image.uniqlo.com/UQ/ST3/AsianCommon/imagesgoods/{product_id}/chip/goods_{color_code}_{product_id}_chip.jpg"
                else:
                    color_name = str(color_info)

                for size_code, size_info in sizes.items():
                    size_name = ""
                    if isinstance(size_info, dict):
                        size_name = size_info.get("displaySizeName") or size_info.get("name") or size_code
                    else:
                        size_name = str(size_info)

                    in_stock = True
                    sku = f"{product_id}-{color_code}-{size_code}"
                    if isinstance(l2s, list):
                        for s in l2s:
                            if isinstance(s, dict):
                                sc = str(s.get("color", {}).get("displayCode", "")) if isinstance(s.get("color"), dict) else str(s.get("colorCode", ""))
                                ss = str(s.get("size", {}).get("displayCode", "")) if isinstance(s.get("size"), dict) else str(s.get("sizeCode", ""))
                                if sc == color_code and ss == size_code:
                                    in_stock = s.get("stock", {}).get("statusCode", "") != "OUT_OF_STOCK" if isinstance(s.get("stock"), dict) else True
                                    sku = s.get("l2Id", sku)
                                    break

                    variants.append({
                        "color": color_name,
                        "size": size_name,
                        "sku": sku,
                        "price": product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image": color_img,
                    })

        if not variants and isinstance(l2s, list) and l2s:
            for s in l2s:
                if not isinstance(s, dict):
                    continue
                color = ""
                size = ""
                if isinstance(s.get("color"), dict):
                    color = s["color"].get("displayColorName", "") or s["color"].get("name", "")
                else:
                    color = s.get("colorDisplayName", "") or s.get("color", "")
                if isinstance(s.get("size"), dict):
                    size = s["size"].get("displaySizeName", "") or s["size"].get("name", "")
                else:
                    size = s.get("sizeDisplayName", "") or s.get("size", "")

                in_stock = True
                if isinstance(s.get("stock"), dict):
                    in_stock = s["stock"].get("statusCode", "") != "OUT_OF_STOCK"

                variants.append({
                    "color": color,
                    "size": size,
                    "sku": s.get("l2Id", "") or s.get("sku", ""),
                    "price": product.price_jpy or 0,
                    "in_stock": in_stock,
                    "image": "",
                })

        if variants:
            product.variants = variants

        return product

    def _extract_uniqlo_price(self, prod: dict) -> int | None:
        for key in ["minPrice", "price", "retailPrice", "salePrice", "originPrice"]:
            v = prod.get(key)
            if v and isinstance(v, (int, float)) and v > 0:
                return int(v)

        prices = prod.get("prices") or prod.get("price") or {}
        if isinstance(prices, (int, float)) and prices > 0:
            return int(prices)

        if isinstance(prices, dict):
            for sub_key in ["promo", "base", "current", "sale", "original"]:
                sub = prices.get(sub_key)
                if isinstance(sub, dict):
                    v = sub.get("value") or sub.get("price") or sub.get("amount")
                    if v and float(v) > 0:
                        return int(float(v))
                elif isinstance(sub, (int, float)) and sub > 0:
                    return int(sub)

            v = prices.get("value") or prices.get("price") or prices.get("amount")
            if v and float(v) > 0:
                return int(float(v))

        return None

    def _build_uniqlo_fallback_variants(self, product: ProductInfo, product_id: str, color_from_url: str, html: str) -> ProductInfo:
        sizes_found = []
        size_pattern = r'\b(XS|S|M|L|XL|XXL|3XL|4XL)\b'
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        size_section = re.search(r'サイズ[：:]\s*(?:男女兼用|レディス|メンズ)?\s*\w+\s+((?:(?:XS|S|M|L|XL|XXL|3XL|4XL)\s*)+)', text)
        if size_section:
            sizes_found = re.findall(size_pattern, size_section.group(1))

        if not sizes_found:
            all_sizes = re.findall(size_pattern, text)
            seen = set()
            for s in all_sizes:
                if s not in seen:
                    seen.add(s)
                    sizes_found.append(s)

        color_name = ""
        color_match = re.search(r'カラー[：:]\s*(\d+)\s+(\w+)', text)
        if color_match:
            color_name = f"{color_match.group(1)} {color_match.group(2)}"
        elif color_from_url:
            color_name = color_from_url

        if not sizes_found:
            sizes_found = ["XS", "S", "M", "L", "XL", "XXL", "3XL"]

        color_img = f"https://image.uniqlo.com/UQ/ST3/AsianCommon/imagesgoods/{product_id}/chip/goods_{color_from_url}_{product_id}_chip.jpg" if color_from_url else ""

        variants = []
        for size in sizes_found:
            variants.append({
                "color": color_name,
                "size": size,
                "sku": f"{product_id}-{color_from_url}-{size}",
                "price": product.price_jpy or 0,
                "in_stock": True,
                "image": color_img,
            })

        product.variants = variants
        return product

    def _parse_uniqlo_html(self, html: str, product_id: str, product: ProductInfo) -> ProductInfo:
        soup = BeautifulSoup(html, "html.parser")

        og_title = soup.find("meta", property="og:title")
        if og_title:
            product.title = og_title.get("content", "").replace("| ユニクロ", "").strip()
        if not product.title:
            title_tag = soup.find("title")
            if title_tag:
                t = title_tag.get_text()
                product.title = t.split("|")[0].strip() if "|" in t else t.strip()

        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            product.image_url = og_img["content"]

        extra = []
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "image.uniqlo.com" in src and product_id in src and src not in extra:
                extra.append(src)
        if extra and not product.image_url:
            product.image_url = extra[0]
        product.extra_images = [u for u in extra if u != product.image_url][:8]

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                ld = _json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = next((x for x in ld if x.get("@type") == "Product"), ld[0] if ld else {})
                if ld.get("@type") == "Product":
                    offers = ld.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price")
                    if price:
                        product.price_jpy = int(float(price))
            except:
                pass

        if not product.price_jpy:
            text = soup.get_text()
            pm = re.search(r'[¥￥]([\d,]+)', text)
            if pm:
                product.price_jpy = int(pm.group(1).replace(",", ""))

        return product
