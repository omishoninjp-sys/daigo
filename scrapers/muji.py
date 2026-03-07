"""
MUJI JP 爬蟲 Mixin
使用 HTML 解析 + 內部 API，Chrome fallback
"""
import re
import json

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT, PROXY_URL
from scrapers.base import ProductInfo
from scrapers.driver import VALID_SIZES


class MujiMixin:

    async def _scrape_muji(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="無印良品")

        m = re.search(r'/detail/(\d{10,14})', url)
        if not m:
            print(f"[MUJI] ❌ 無法從 URL 提取商品代碼: {url}")
            return product

        jan_code = m.group(1)
        print(f"[MUJI] JAN: {jan_code}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

        proxy_arg = PROXY_URL if PROXY_URL else None

        async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:
            html_text = ""
            cookies = {}
            try:
                resp = await client.get(url, headers=headers)
                html_text = resp.text
                cookies = dict(resp.cookies)
                print(f"[MUJI] HTML: {resp.status_code}, {len(html_text)} bytes")
            except Exception as e:
                print(f"[MUJI] HTML 錯誤: {type(e).__name__}: {e}")

            if html_text:
                soup = BeautifulSoup(html_text, "html.parser")

                title_tag = soup.find("title")
                if title_tag:
                    t = title_tag.get_text()
                    product.title = t.replace("| 無印良品", "").replace("|無印良品", "").strip()

                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    t = og_title["content"].replace("| 無印良品", "").strip()
                    if t:
                        product.title = t

                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get("content"):
                    product.image_url = og_img["content"]

                if not product.image_url:
                    product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

                extra = []
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if "muji.com" in src and jan_code in src and src not in extra:
                        extra.append(src)
                product.extra_images = [u for u in extra if u != product.image_url][:8]

                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(script.string or "")
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

                self._parse_muji_embedded_json(soup, jan_code, product)

                if product.price_jpy and product.title:
                    return product

            api_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja-JP,ja;q=0.9",
                "Referer": url,
                "Origin": "https://www.muji.com",
                "X-Requested-With": "XMLHttpRequest",
            }

            api_urls = [
                f"https://www.muji.com/jp/api/store/cmdty/detail/{jan_code}",
                f"https://www.muji.com/jp/api/store/v1/cmdty/{jan_code}",
                f"https://www.muji.com/jp/api/product/{jan_code}",
                f"https://www.muji.com/jp/api/v1/product/{jan_code}",
                f"https://www.muji.com/jp/store/api/cmdty/detail/{jan_code}",
            ]

            for api_url in api_urls:
                try:
                    resp = await client.get(api_url, headers=api_headers, cookies=cookies)
                    if resp.status_code == 200:
                        try:
                            api_data = resp.json()
                            self._parse_muji_api(api_data, jan_code, product)
                            if product.price_jpy:
                                break
                        except:
                            pass
                except Exception as e:
                    print(f"[MUJI] API 錯誤: {type(e).__name__}: {e}")

        if not product.price_jpy:
            try:
                await self._muji_chrome_fallback(url, product, jan_code)
            except Exception as e:
                print(f"[MUJI] Chrome UC 錯誤: {type(e).__name__}: {e}")

        if not product.image_url:
            product.image_url = f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"

        return product

    def _parse_muji_embedded_json(self, soup, jan_code: str, product: ProductInfo):
        for script in soup.find_all("script"):
            text = script.string or ""
            if not text or len(text) < 50:
                continue

            if "__NEXT_DATA__" in text:
                try:
                    jm = re.search(r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</)', text, re.DOTALL)
                    if jm:
                        data = json.loads(jm.group(1))
                        props = data.get("props", {}).get("pageProps", {})
                        self._parse_muji_api(props, jan_code, product)
                        if product.price_jpy:
                            return
                except Exception as e:
                    print(f"[MUJI] __NEXT_DATA__ 錯誤: {e}")

            for pat in [r'__INITIAL_STATE__\s*=\s*({.+?})\s*;',
                       r'window\.PRODUCT\s*=\s*({.+?})\s*;',
                       r'window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*;']:
                try:
                    sm = re.search(pat, text, re.DOTALL)
                    if sm:
                        data = json.loads(sm.group(1))
                        self._parse_muji_api(data, jan_code, product)
                        if product.price_jpy:
                            return
                except:
                    pass

            if jan_code in text and ('"price"' in text or '"salePrice"' in text):
                price_m = re.search(r'"(?:sale)?[Pp]rice"\s*:\s*(\d{3,6})', text)
                if price_m and not product.price_jpy:
                    product.price_jpy = int(price_m.group(1))

    def _parse_muji_api(self, data: dict, jan_code: str, product: ProductInfo):
        prod = None

        if "janCode" in data or "commodityCode" in data:
            prod = data

        for key in ["product", "cmdty", "detail", "commodity", "data", "item"]:
            if key in data and isinstance(data[key], dict):
                prod = data[key]
                break

        if not prod:
            return

        name = prod.get("name") or prod.get("commodityName") or prod.get("productName") or ""
        if name and not product.title:
            product.title = name

        for pk in ["price", "salePrice", "sellingPrice", "retailPrice", "displayPrice", "priceIncTax", "priceExcTax"]:
            v = prod.get(pk)
            if v and isinstance(v, (int, float)) and v > 0:
                product.price_jpy = int(v)
                break

        if not product.price_jpy:
            prices = prod.get("prices", {}) or prod.get("price", {})
            if isinstance(prices, dict):
                for pk in ["selling", "retail", "sale", "current", "value"]:
                    v = prices.get(pk)
                    if v and isinstance(v, (int, float)):
                        product.price_jpy = int(v)
                        break

        images = prod.get("images", []) or prod.get("imageList", []) or []
        if isinstance(images, list):
            for img in images:
                u = img.get("url", "") if isinstance(img, dict) else str(img)
                if u and "muji.com" in u:
                    if not product.image_url:
                        product.image_url = u
                    elif u != product.image_url and len(product.extra_images) < 8:
                        product.extra_images.append(u)

        variants_data = prod.get("skuList", []) or prod.get("variants", []) or prod.get("sizes", []) or prod.get("colors", []) or []
        if isinstance(variants_data, list):
            for v in variants_data:
                if not isinstance(v, dict):
                    continue
                variant = {
                    "color": v.get("colorName", "") or v.get("color", ""),
                    "size": v.get("sizeName", "") or v.get("size", ""),
                    "sku": v.get("janCode", "") or v.get("sku", "") or v.get("skuCode", ""),
                    "price": product.price_jpy or 0,
                    "in_stock": v.get("stockStatus", "") != "OUT_OF_STOCK",
                    "image": "",
                }
                if variant["color"] or variant["size"]:
                    product.variants.append(variant)

    async def _muji_chrome_fallback(self, url: str, product: ProductInfo, jan_code: str) -> bool:
        import time as _time

        with self._driver_lock:
          for attempt in range(2):
            try:
                driver = self._ensure_driver()
                if not driver:
                    return False

                self._driver_use_count += 1
                self._clean_driver_tabs()

                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception as e:
                    err_name = type(e).__name__
                    if "InvalidSession" in err_name or "invalid session" in str(e).lower():
                        self._driver = None
                        self._create_driver()
                        continue

                html = ""
                session_dead = False
                for i in range(8):
                    _time.sleep(2)
                    try:
                        html = driver.page_source
                        title = driver.title
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            session_dead = True
                            break
                        continue

                    has_data = (
                        'application/ld+json' in html or
                        '無印良品' in html or
                        'og:title' in html or
                        '税込' in html or
                        '円' in html
                    )

                    if i >= 1 and has_data:
                        break

                if session_dead:
                    self._driver = None
                    self._create_driver()
                    continue

                if not html or len(html) < 5000:
                    return False

                soup = BeautifulSoup(html, "html.parser")

                if not product.title:
                    og_title = soup.find("meta", property="og:title")
                    if og_title and og_title.get("content"):
                        product.title = og_title["content"].replace("| 無印良品", "").strip()
                    else:
                        title_tag = soup.find("title")
                        if title_tag:
                            product.title = title_tag.get_text().replace("| 無印良品", "").replace("|無印良品", "").strip()

                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(script.string or "")
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

                self._parse_muji_embedded_json(soup, jan_code, product)

                if not product.price_jpy:
                    page_text = soup.get_text(" ", strip=True)
                    for pat in [r'¥\s*([\d,]+)', r'([\d,]+)\s*円', r'([\d,]+)\s*[（(]税込']:
                        pm = re.search(pat, page_text)
                        if pm:
                            try:
                                p = int(pm.group(1).replace(",", ""))
                                if 50 < p < 500000:
                                    product.price_jpy = p
                                    break
                            except:
                                pass

                og_img = soup.find("meta", property="og:image")
                img_url = og_img["content"] if og_img and og_img.get("content") else f"https://www.muji.com/public/media/img/item/{jan_code}_org.jpg"
                product.image_url = img_url

                try:
                    b64 = driver.execute_script("""
                        return await fetch(arguments[0])
                            .then(r => r.blob())
                            .then(b => new Promise((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onload = () => resolve(reader.result.split(',')[1]);
                                reader.onerror = reject;
                                reader.readAsDataURL(b);
                            }));
                    """, img_url)
                    if b64 and len(b64) > 100:
                        product.image_base64 = b64
                except Exception as e:
                    print(f"[MUJI] 圖片下載失敗: {type(e).__name__}: {e}")

                self._extract_muji_variants_from_html(driver, soup, product, jan_code)

                return bool(product.price_jpy)

            except Exception as e:
                err_name = type(e).__name__
                if "InvalidSession" in err_name and attempt == 0:
                    self._driver = None
                    self._create_driver()
                    continue
                return False
          return False

    def _extract_muji_variants_from_html(self, driver, soup, product: ProductInfo, jan_code: str):
        try:
            valid_sizes_js = json.dumps(list(VALID_SIZES))

            variants_js = driver.execute_script(f"""
                var validSizes = new Set({valid_sizes_js});
                var results = {{sizes: [], colors: []}};

                var allBtns = document.querySelectorAll(
                    '[class*="size"] button, [class*="size"] a, ' +
                    '[class*="Size"] button, [class*="Size"] a, ' +
                    '.cmdty-size-list button, .cmdty-size-list a'
                );
                allBtns.forEach(function(el) {{
                    var text = el.textContent.trim();
                    if (validSizes.has(text) && !results.sizes.includes(text)) {{
                        results.sizes.push(text);
                    }}
                }});

                var colorEls = document.querySelectorAll(
                    '[class*="color"] button, [class*="Color"] button, ' +
                    '.cmdty-color-list button, [aria-label*="カラー"]'
                );
                colorEls.forEach(function(el) {{
                    var text = (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent).trim();
                    if (text && text.length < 15 && !text.includes('カート') &&
                        !text.includes('閉じる') && !text.includes('確認') &&
                        !results.colors.includes(text)) {{
                        results.colors.push(text);
                    }}
                }});

                document.querySelectorAll('select').forEach(function(sel) {{
                    var label = (sel.getAttribute('aria-label') || sel.name || '').toLowerCase();
                    sel.querySelectorAll('option').forEach(function(opt) {{
                        var val = opt.textContent.trim();
                        if (!val || val.includes('選択') || val.includes('選んで')) return;
                        if (label.includes('size') || label.includes('サイズ')) {{
                            if (validSizes.has(val) && !results.sizes.includes(val)) results.sizes.push(val);
                        }} else if (label.includes('color') || label.includes('カラー')) {{
                            if (!results.colors.includes(val)) results.colors.push(val);
                        }}
                    }});
                }});

                return results;
            """)

            sizes = variants_js.get("sizes", []) if variants_js else []
            colors = variants_js.get("colors", []) if variants_js else []

            if not sizes:
                page_text = soup.get_text(" ", strip=True)
                size_section = re.search(r'サイズ[：:\s]*(?:[\w・]+\s*)?((?:(?:XS|S|M|L|XL|XXL|3XL|4XL|F|フリー)\s*[/／・]?\s*)+)', page_text)
                if size_section:
                    sizes = re.findall(r'\b(XS|S|M|L|XL|XXL|3XL|4XL|F|フリー)\b', size_section.group(1))
                    sizes = list(dict.fromkeys(sizes))

            if sizes or colors:
                if not colors:
                    colors = [""]
                if not sizes:
                    sizes = [""]

                for color in colors:
                    for size in sizes:
                        label_parts = [p for p in [color, size] if p]
                        variant = {
                            "color": color,
                            "size": size,
                            "sku": f"{jan_code}-{'-'.join(label_parts)}" if label_parts else jan_code,
                            "price": product.price_jpy or 0,
                            "in_stock": True,
                            "image": "",
                        }
                        product.variants.append(variant)

        except Exception as e:
            print(f"[MUJI] variant 提取錯誤: {type(e).__name__}: {e}")
