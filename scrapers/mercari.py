"""
Mercari 爬蟲 Mixin
使用 httpx + __NEXT_DATA__ 解析，Chrome fallback
"""
import re
import json
import time as _time

import httpx
from bs4 import BeautifulSoup

from config import USER_AGENT
from scrapers.base import ProductInfo, normalize_price


class MercariMixin:

    async def _scrape_mercari(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        try:
            html = None
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(15.0, connect=8.0),
                    follow_redirects=True,
                    headers={
                        'User-Agent': USER_AGENT,
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'ja,en-US;q=0.9',
                    },
                ) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        html = resp.text
                        if self._extract_mercari_next_data(html, product):
                            return product
                        soup = BeautifulSoup(html, "html.parser")
                        self._extract_json_ld(soup, product)
                        self._extract_og_tags(soup, product)
                        if product.title and product.price_jpy:
                            return product
            except Exception as e:
                print(f"[Mercari] httpx 失敗: {type(e).__name__}: {e}")

            chrome_data = await self._mercari_chrome_extract(url)

            if chrome_data:
                if chrome_data.get("title"):
                    product.title = chrome_data["title"]
                if chrome_data.get("price"):
                    product.price_jpy = normalize_price(chrome_data["price"])
                if chrome_data.get("image"):
                    product.image_url = chrome_data["image"]
                if chrome_data.get("extra_images"):
                    product.extra_images = chrome_data["extra_images"][:4]
                if chrome_data.get("description"):
                    product.description = chrome_data["description"][:500]
                if chrome_data.get("brand"):
                    product.brand = chrome_data["brand"]

                condition = chrome_data.get("condition", "")
                if condition and product.description:
                    product.description = f"【{condition}】\n{product.description}"
                elif condition:
                    product.description = f"【{condition}】"

        except Exception as e:
            print(f"[Mercari] ❌ 錯誤: {type(e).__name__}: {e}")

        return product

    def _extract_mercari_next_data(self, html: str, product: ProductInfo) -> bool:
        try:
            m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if not m:
                return False

            data = json.loads(m.group(1))

            def find_item(obj, depth=0):
                if depth > 8 or not isinstance(obj, dict):
                    return None
                if obj.get("name") and obj.get("price") and (obj.get("photos") or obj.get("thumbnails") or obj.get("imageUrls")):
                    return obj
                if obj.get("item") and isinstance(obj["item"], dict):
                    return find_item(obj["item"], depth + 1)
                for v in obj.values():
                    if isinstance(v, dict):
                        result = find_item(v, depth + 1)
                        if result:
                            return result
                    elif isinstance(v, list):
                        for item in v[:5]:
                            if isinstance(item, dict):
                                result = find_item(item, depth + 1)
                                if result:
                                    return result
                return None

            item = find_item(data)
            if not item:
                return False

            product.title = item.get("name", "") or item.get("productName", "")
            price = item.get("price", 0)
            if price:
                product.price_jpy = normalize_price(price)
            product.description = (item.get("description", "") or "")[:500]

            photos = item.get("photos") or item.get("thumbnails") or item.get("imageUrls") or []
            if isinstance(photos, list) and photos:
                if isinstance(photos[0], dict):
                    urls = [p.get("url") or p.get("imageUrl") or p.get("src", "") for p in photos]
                else:
                    urls = [str(p) for p in photos]
                urls = [u for u in urls if u]
                if urls:
                    product.image_url = urls[0]
                    product.extra_images = urls[1:5]

            brand = item.get("brand", {})
            if isinstance(brand, dict):
                product.brand = brand.get("name", "")
            elif isinstance(brand, str):
                product.brand = brand

            return bool(product.title and product.price_jpy)

        except (json.JSONDecodeError, KeyError, TypeError):
            return False

    async def _mercari_chrome_extract(self, url: str) -> dict | None:
        with self._driver_lock:
          for attempt in range(2):
            try:
                driver = self._ensure_driver()
                if not driver:
                    return None

                self._driver_use_count += 1
                self._clean_driver_tabs()

                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=8)
                except Exception as e:
                    err_name = type(e).__name__
                    if "InvalidSession" in err_name or "invalid session" in str(e).lower():
                        self._driver = None
                        self._create_driver()
                        continue

                result = None
                for i in range(8):
                    _time.sleep(2)
                    try:
                        result = driver.execute_script("""
                            var r = {title:'', price:'', image:'', extra_images:[],
                                     description:'', brand:'', condition:'', debug:''};

                            try {
                                var nd = document.getElementById('__NEXT_DATA__');
                                if (nd) {
                                    var data = JSON.parse(nd.textContent);
                                    function findItem(obj, depth) {
                                        if (depth > 8 || !obj || typeof obj !== 'object') return null;
                                        if (obj.name && obj.price && (obj.photos || obj.thumbnails || obj.imageUrls)) return obj;
                                        if (obj.item && typeof obj.item === 'object') {
                                            var found = findItem(obj.item, depth+1);
                                            if (found) return found;
                                        }
                                        for (var k in obj) {
                                            if (typeof obj[k] === 'object') {
                                                var found = findItem(obj[k], depth+1);
                                                if (found) return found;
                                            }
                                        }
                                        return null;
                                    }
                                    var item = findItem(data, 0);
                                    if (item) {
                                        r.title = item.name || item.productName || '';
                                        r.price = String(item.price || '');
                                        r.description = (item.description || '').substring(0, 500);
                                        var brand = item.brand;
                                        if (brand && typeof brand === 'object') r.brand = brand.name || '';
                                        else if (typeof brand === 'string') r.brand = brand;
                                        var photos = item.photos || item.thumbnails || item.imageUrls || [];
                                        if (photos.length > 0) {
                                            var urls = photos.map(function(p) {
                                                return (typeof p === 'object') ? (p.url || p.imageUrl || p.src || '') : String(p);
                                            }).filter(function(u) { return u; });
                                            if (urls.length > 0) {
                                                r.image = urls[0];
                                                r.extra_images = urls.slice(1, 5);
                                            }
                                        }
                                        r.debug += 'next_data_ok';
                                    }
                                }
                            } catch(e) { r.debug += 'next_err:' + e.message + ' '; }

                            if (!r.title) {
                                var h1 = document.querySelector('h1');
                                if (h1) r.title = h1.textContent.trim();
                                if (!r.title) {
                                    var og = document.querySelector('meta[property="og:title"]');
                                    if (og) r.title = og.content || '';
                                }
                            }

                            if (!r.price) {
                                var priceEls = document.querySelectorAll('[class*="price"], [class*="Price"], [data-testid*="price"]');
                                for (var i = 0; i < priceEls.length; i++) {
                                    var txt = priceEls[i].textContent;
                                    var m = txt.match(/[¥￥]\\s*([\\d,]+)/);
                                    if (m) { r.price = m[1].replace(/,/g, ''); break; }
                                }
                            }

                            if (!r.image) {
                                var ogImg = document.querySelector('meta[property="og:image"]');
                                if (ogImg && ogImg.content) r.image = ogImg.content;
                                if (!r.image) {
                                    var imgs = document.querySelectorAll('img[src*="static.mercdn.net"]');
                                    var goodImgs = [];
                                    for (var i = 0; i < imgs.length; i++) {
                                        var src = imgs[i].src || imgs[i].dataset.src || '';
                                        if (src && src.indexOf('thumb') === -1 && src.indexOf('icon') === -1) {
                                            goodImgs.push(src);
                                        }
                                    }
                                    if (goodImgs.length > 0) {
                                        r.image = goodImgs[0];
                                        r.extra_images = goodImgs.slice(1, 5);
                                    }
                                }
                            }

                            return r;
                        """)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            self._driver = None
                            self._create_driver()
                            break
                        continue

                    if result and result.get("title") and result.get("price"):
                        return result

                if result and (result.get("title") or result.get("price")):
                    return result

                return None

            except Exception as e:
                if attempt == 0:
                    self._driver = None
                    self._create_driver()
                    continue
                return None

        return None
