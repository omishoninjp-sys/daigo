"""
ZOZOTOWN 爬蟲 Mixin
使用 SeleniumBase UC + Xvfb 繞過 Akamai
"""
import asyncio

import httpx

from config import ZOZO_SCRAPER_URL, PROXY_URL
from scrapers.base import ProductInfo


class ZozotownMixin:

    async def _scrape_zozotown(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_zozo_uc, url
            )
            if data and data.get("title"):
                product.title = data.get("title", "")
                product.price_jpy = data.get("price", 0) or None
                product.brand = data.get("brand", "")
                product.description = data.get("description", "")[:500]
                images = data.get("images", [])
                if images:
                    product.image_url = images[0]
                    product.extra_images = images[1:9]
                variants = data.get("variants", [])
                if variants:
                    product.variants = variants
            else:
                print("[ZOZO] ⚠️ 未取得資料")
        except Exception as e:
            print(f"[ZOZO] ❌ 錯誤: {e}")

        if not product.title and ZOZO_SCRAPER_URL:
            result = await self._scrape_zozo_via_proxy(url)
            if result and result.title:
                return result

        return product

    def _fetch_zozo_uc(self, url: str) -> dict | None:
        import time as _time

        with self._driver_lock:
            try:
                driver = self._ensure_driver()
                if not driver:
                    return None

                if not self._proxy_verified and PROXY_URL:
                    try:
                        driver.get('http://httpbin.org/ip')
                        _time.sleep(1)
                        src = driver.page_source
                        print(f"[ZOZO] proxy IP: {src[:100]}")
                        self._proxy_verified = True
                    except Exception as e:
                        print(f"[ZOZO] proxy 測試: {type(e).__name__}")

                self._clean_driver_tabs()
                self._driver_use_count += 1

                print(f"[ZOZO] 載入: {url}")
                try:
                    driver.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception as e:
                    print(f"[ZOZO] uc_open: {type(e).__name__}: {e}")

                for i in range(12):
                    _time.sleep(3 if i < 3 else 2)
                    try:
                        html = driver.page_source
                        title = driver.title
                    except:
                        continue

                    has_data = ('application/ld+json' in html or
                               '__NEXT_DATA__' in html or
                               'og:title' in html)

                    print(f"[ZOZO] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                    if has_data:
                        result = driver.execute_script(r"""
                            var r = {title:'', brand:'', price:0, price_text:'',
                                     original_price:0, original_price_text:'', discount:'',
                                     images:[], description:'', item_id:'', in_stock:true,
                                     variants:[], variant_debug:''};

                            var m = location.pathname.match(/\/goods(?:-sale)?\/(\d+)/);
                            if (m) r.item_id = m[1];

                            document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                                try {
                                    var d = JSON.parse(s.textContent);
                                    if (Array.isArray(d)) d = d.find(function(i){return i['@type']==='Product'}) || d[0];
                                    if (d && d['@type'] === 'Product') {
                                        r.title = d.name || '';
                                        var b = d.brand || '';
                                        r.brand = (typeof b === 'object') ? (b.name || '') : String(b);
                                        r.description = d.description || '';
                                        var img = d.image || [];
                                        if (typeof img === 'string') img = [img];
                                        r.images = img.filter(function(i){return typeof i === 'string' && i.indexOf('c.imgz.jp') !== -1}).slice(0,15);
                                        var offers = d.offers || {};
                                        if (Array.isArray(offers)) {
                                            offers.forEach(function(o) {
                                                if (o.price && !r.price) {
                                                    r.price = parseInt(o.price);
                                                    r.price_text = '\u00a5' + r.price.toLocaleString();
                                                }
                                            });
                                            offers = offers[0] || {};
                                        } else {
                                            if (offers.price) {
                                                r.price = parseInt(offers.price);
                                                r.price_text = '\u00a5' + r.price.toLocaleString();
                                            }
                                        }
                                        if (offers.availability && offers.availability.indexOf('OutOfStock') !== -1) r.in_stock = false;
                                    }
                                } catch(e) {}
                            });

                            var nd = document.getElementById('__NEXT_DATA__');
                            if (nd) {
                                try {
                                    var ndata = JSON.parse(nd.textContent);
                                    var props = ndata.props && ndata.props.pageProps ? ndata.props.pageProps : {};
                                    var prod = props.product || props.goods || props.item ||
                                              (props.initialState && props.initialState.product) || {};

                                    if (!r.title && prod.name) r.title = prod.name;
                                    if (!r.brand && prod.brandName) r.brand = prod.brandName;
                                    if (!r.price && prod.price) {
                                        r.price = parseInt(prod.price);
                                        r.price_text = '\u00a5' + r.price.toLocaleString();
                                    }
                                    if (r.images.length === 0 && prod.images) {
                                        r.images = prod.images.map(function(i){return i.url || i}).slice(0,15);
                                    }

                                    var items = prod.items || prod.skus || prod.variants ||
                                               prod.colorSizes || prod.detail && prod.detail.items || [];

                                    if (items.length > 0) {
                                        items.forEach(function(item) {
                                            var v = {
                                                color: item.colorName || item.color || item.colorLabel || '',
                                                size: item.sizeName || item.size || item.sizeLabel || '',
                                                sku: item.skuId || item.id || item.sku || '',
                                                price: item.price ? parseInt(item.price) : r.price,
                                                in_stock: item.soldout !== true && item.inStock !== false,
                                                image: item.imageUrl || item.image || ''
                                            };
                                            if (v.color || v.size) r.variants.push(v);
                                        });
                                    }

                                    r.variant_debug = 'pageProps keys: ' + Object.keys(props).join(',');
                                } catch(e) {
                                    r.variant_debug = 'NEXT_DATA error: ' + e.message;
                                }
                            }

                            if (r.variants.length === 0) {
                                var dts = document.querySelectorAll('dt.p-goods-information-action__term');

                                dts.forEach(function(dt) {
                                    var colorName = dt.textContent.trim();
                                    var dd = dt.nextElementSibling;
                                    if (!dd) return;

                                    var dlParent = dt.parentElement;
                                    var thumbImg = null;
                                    if (dlParent) {
                                        thumbImg = dlParent.querySelector('img[src*="imgz.jp"], img[src*="zozo"]');
                                    }
                                    if (!thumbImg && dd) {
                                        thumbImg = dd.querySelector('img');
                                    }
                                    var colorImage = '';
                                    if (thumbImg) {
                                        colorImage = thumbImg.src || thumbImg.getAttribute('data-src') || '';
                                        if (colorImage.indexOf('_35.') !== -1) {
                                            colorImage = colorImage.replace(/_35\./, '_500.');
                                        }
                                    }

                                    var sizeItems = dd.querySelectorAll('li.p-goods-add-cart-list__item');
                                    sizeItems.forEach(function(li) {
                                        var fullText = li.textContent.replace(/\s+/g, ' ').trim();
                                        var sizeMatch = fullText.match(/^\s*([A-Z0-9SMLXF]+(?:\s*[\-~]\s*[A-Z0-9SMLXF]+)?)\s*[\/／]/);
                                        if (!sizeMatch) sizeMatch = fullText.match(/^\s*(フリー|FREE|F|ONE\s*SIZE|ワンサイズ|\d+(?:\.\d+)?(?:cm)?)\s*[\/／]/i);
                                        var size = sizeMatch ? sizeMatch[1].trim() : '';
                                        if (!size) return;

                                        var inStock = fullText.indexOf('在庫あり') !== -1;
                                        var soldOut = fullText.indexOf('SOLD') !== -1;

                                        var sku = '';
                                        var form = li.querySelector('form');
                                        if (form) {
                                            form.querySelectorAll('input[type="hidden"]').forEach(function(inp) {
                                                var n = (inp.name || '').toLowerCase();
                                                if (n === 'did' || n === 'sid' || n === 'detail_id' || n === 'gid') sku = inp.value || '';
                                            });
                                        }

                                        r.variants.push({
                                            color: colorName,
                                            size: size,
                                            sku: sku,
                                            price: r.price,
                                            in_stock: inStock && !soldOut,
                                            image: colorImage
                                        });
                                    });
                                });

                                if (r.variants.length === 0) {
                                    var items = document.querySelectorAll('li.p-goods-add-cart-list__item');
                                    items.forEach(function(li) {
                                        var fullText = li.textContent.replace(/\s+/g, ' ').trim();
                                        var sizeMatch = fullText.match(/^\s*([A-Z0-9SMLXF]+(?:\s*[\-~]\s*[A-Z0-9SMLXF]+)?)\s*[\/／]/);
                                        if (!sizeMatch) sizeMatch = fullText.match(/^\s*(フリー|FREE|F|ONE\s*SIZE|ワンサイズ|\d+(?:cm)?)\s*[\/／]/i);
                                        var size = sizeMatch ? sizeMatch[1].trim() : '';
                                        if (!size) return;

                                        var inStock = fullText.indexOf('在庫あり') !== -1;
                                        var soldOut = fullText.indexOf('SOLD') !== -1;
                                        var sku = '';
                                        var form = li.querySelector('form');
                                        if (form) {
                                            form.querySelectorAll('input[type="hidden"]').forEach(function(inp) {
                                                var n = (inp.name || '').toLowerCase();
                                                if (n === 'did' || n === 'sid' || n === 'detail_id' || n === 'gid') sku = inp.value || '';
                                            });
                                        }

                                        r.variants.push({
                                            color: '',
                                            size: size,
                                            sku: sku,
                                            price: r.price,
                                            in_stock: inStock && !soldOut,
                                            image: ''
                                        });
                                    });
                                }

                                var seen = {};
                                var unique = [];
                                r.variants.forEach(function(v) {
                                    var key = v.color.replace(/s+/g,'') + '|' + v.size.replace(/s+/g,'');
                                    if (!seen[key]) {
                                        seen[key] = true;
                                        unique.push(v);
                                    }
                                });
                                r.variants = unique;
                            }

                            if (!r.title) {
                                var og = document.querySelector('meta[property="og:title"]');
                                if (og) r.title = og.content.replace(/\s*[-|]\s*ZOZOTOWN.*$/, '');
                            }
                            if (r.images.length === 0) {
                                var ogImg = document.querySelector('meta[property="og:image"]');
                                if (ogImg && ogImg.content) r.images.push(ogImg.content);
                            }

                            if (!r.price) {
                                document.querySelectorAll('[class*="price"], [class*="Price"]').forEach(function(el) {
                                    if (!r.price) {
                                        var pm = el.textContent.match(/[\u00a5\uffe5]([\d,]+)/);
                                        if (pm) { r.price = parseInt(pm[1].replace(/,/g,'')); r.price_text = '\u00a5' + r.price.toLocaleString(); }
                                    }
                                });
                            }

                            var seen = {};
                            r.images.forEach(function(u){ seen[u] = true; });
                            document.querySelectorAll('img[src*="c.imgz.jp"], img[data-src*="c.imgz.jp"]').forEach(function(img) {
                                var src = img.src || img.getAttribute('data-src') || '';
                                if (src && !seen[src] && img.naturalWidth > 50) {
                                    r.images.push(src);
                                    seen[src] = true;
                                }
                            });
                            r.images = r.images.slice(0, 20);
                            var liClasses = new Set();
                            document.querySelectorAll('li').forEach(function(el) {
                                if (el.className) liClasses.add(el.className.trim().split(' ')[0]);
                            });
                            var dtClasses = new Set();
                            document.querySelectorAll('dt').forEach(function(el) {
                                if (el.className) dtClasses.add(el.className.trim().split(' ')[0]);
                            });
                            r.variant_debug += ' | li_classes: ' + Array.from(liClasses).slice(0,15).join(',') +
                                               ' | dt_classes: ' + Array.from(dtClasses).slice(0,10).join(',') +
                                               ' | NEXT_DATA: ' + (document.getElementById('__NEXT_DATA__') ? 'yes' : 'no');
                            return r;
                        """)
                        if result:
                            vd = result.get('variant_debug', '')
                            vs = result.get('variants', [])
                            print(f"[ZOZO] variant_debug: {vd}")
                            print(f"[ZOZO] variants: {len(vs)} 個")

                        # === 用 GetSizeMappinngList API 修正庫存 ===
                        gid = (result or {}).get('item_id', '')
                        if gid:
                            try:
                                stock_data = driver.execute_async_script("""
                                    var callback = arguments[arguments.length - 1];
                                    var gid = arguments[0];
                                    var gtid = '';
                                    var gtcid = '';
                                    try { gtid = window.__adsInnerGoodspv.item.category_id || ''; } catch(e) {}
                                    document.querySelectorAll('script:not([src])').forEach(function(s) {
                                        var t = s.textContent;
                                        var m = t.match(/gtcid[\'":\\s]+([0-9]+)/);
                                        if (m && !gtcid) gtcid = m[1];
                                    });
                                    if (!gtid) { callback(null); return; }
                                    var url = '/sp/?command=GetSizeMappinngList&gid=' + gid
                                            + '&gtid=' + gtid
                                            + (gtcid ? '&gtcid=' + gtcid : '');
                                    fetch(url, {credentials: 'include'})
                                        .then(function(r){ return r.json(); })
                                        .then(function(d){ callback(d); })
                                        .catch(function(e){ callback(null); });
                                """, gid)

                                if stock_data:
                                    print(f"[ZOZO] GetSizeMappinngList 取得: {str(stock_data)[:300]}")
                                    stock_list = (stock_data.get('list') or
                                                  stock_data.get('sizeList') or
                                                  stock_data.get('result') or [])
                                    if isinstance(stock_list, dict):
                                        stock_list = stock_list.get('list') or stock_list.get('sizeList') or []
                                    did_stock = {}      # detailId → in_stock
                                    color_size_stock = {}  # "colorName|sizeName" → in_stock
                                    for s in (stock_list if isinstance(stock_list, list) else []):
                                        did = str(s.get('detailId') or s.get('did') or s.get('goodsDetailId') or '')
                                        color = str(s.get('colorName') or s.get('color') or s.get('colorLabel') or '')
                                        size = str(s.get('sizeName') or s.get('size') or s.get('sizeLabel') or '')
                                        sold = s.get('isSoldOut') or s.get('soldOut') or s.get('soldout') or False
                                        qty = int(s.get('quantity') or s.get('stock') or s.get('stockCount') or (0 if sold else 1))
                                        in_stk = (not sold) and (qty > 0)
                                        if did:
                                            did_stock[did] = in_stk
                                        if color and size:
                                            color_size_stock[color + '|' + size] = in_stk

                                    if (did_stock or color_size_stock) and result and result.get('variants'):
                                        for v in result['variants']:
                                            sku = str(v.get('sku', ''))
                                            color = str(v.get('color', ''))
                                            size = str(v.get('size', ''))
                                            cs_key = color + '|' + size
                                            if sku in did_stock:
                                                # 最精確：detailId 直接匹配
                                                v['in_stock'] = did_stock[sku]
                                            elif cs_key in color_size_stock:
                                                # 次精確：顏色+尺寸匹配
                                                v['in_stock'] = color_size_stock[cs_key]
                                            # 不 fallback size-only，避免跨顏色污染
                                        print(f"[ZOZO] 庫存修正完成 did:{len(did_stock)} cs:{len(color_size_stock)}")
                            except Exception as e:
                                print(f"[ZOZO] GetSizeMappinngList 失敗: {e}")

                        return result

                    if 'access denied' in (title or '').lower() and i >= 2:
                        print("[ZOZO] 被 Akamai 擋住")
                        break

                print("[ZOZO] ⚠️ 未取得資料")

            except Exception as e:
                print(f"[ZOZO] SeleniumBase 錯誤: {e}")
                try:
                    self._driver.quit()
                except:
                    pass
                self._driver = None

            return None

    async def _scrape_zozo_via_proxy(self, url: str) -> ProductInfo | None:
        product = ProductInfo(source_url=url)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{ZOZO_SCRAPER_URL.rstrip('/')}/api/fetch",
                    json={"url": url},
                )
                data = resp.json()

            if data.get("error"):
                return None

            product.title = data.get("title", "")
            product.price_jpy = data.get("price", 0) or None
            product.brand = data.get("brand", "")
            product.description = data.get("description", "")[:500]
            images = data.get("images", [])
            if images:
                product.image_url = images[0]
                product.extra_images = images[1:9]
            return product if product.title else None

        except Exception as e:
            print(f"[ZOZO] 外部爬蟲連線失敗: {e}")
            return None
