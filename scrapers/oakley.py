"""
Oakley JP 爬蟲 Mixin
SFCC JS rendered，使用 SeleniumBase UC
"""
import asyncio
import time as _time

from scrapers.base import ProductInfo


class OakleyMixin:

    async def _scrape_oakley(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Oakley")

        data = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_oakley_uc, url
        )

        if data:
            product.title = data.get("title", "")
            product.price_jpy = data.get("price") or None
            product.description = data.get("description", "")[:500]
            images = data.get("images", [])
            if images:
                product.image_url = images[0]
                product.extra_images = images[1:9]
            product.variants = data.get("variants", [])

        print(f"[Oakley] {'✅' if product.title else '⚠️'} {product.title[:40] if product.title else '未取得'} / ¥{product.price_jpy} / {len(product.variants)} variants")
        return product

    def _fetch_oakley_uc(self, url: str) -> dict | None:
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
                        if "InvalidSession" in type(e).__name__:
                            self._driver = None
                            self._create_driver()
                            continue

                    for i in range(10):
                        _time.sleep(3)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                break
                            continue

                        has_data = (
                            'og:title' in html or
                            'application/ld+json' in html or
                            'addToCart' in html or
                            'productName' in html
                        )
                        print(f"[Oakley] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                        if i >= 1 and has_data:
                            break

                    result = driver.execute_script("""
                        var r = {title:'', price:0, images:[], description:'', variants:[]};

                        var h1 = document.querySelector('h1');
                        if (h1) r.title = h1.textContent.trim();

                        if (!r.title) {
                            var og = document.querySelector('meta[property="og:title"]');
                            if (og) r.title = (og.content || '').replace(/\\s*\\|.*$/, '').trim();
                        }

                        (function() {
                            var h1el = document.querySelector('h1');
                            if (h1el) {
                                var parent = h1el.parentElement;
                                for (var depth = 0; depth < 8; depth++) {
                                    if (!parent) break;
                                    var txt = parent.textContent;
                                    var allPrices = txt.match(/[\\u00a5\\uffe5]([\\d,]{3,7})(?:\\s*(?:\\(税込\\)|税込))?/g);
                                    if (allPrices && allPrices.length > 0) {
                                        for (var i = 0; i < allPrices.length; i++) {
                                            var numMatch = allPrices[i].match(/([\\d,]+)/);
                                            if (numMatch) {
                                                var p = parseInt(numMatch[1].replace(/,/g,''));
                                                if (p >= 100 && p <= 2000000) {
                                                    r.price = p;
                                                    break;
                                                }
                                            }
                                        }
                                        if (r.price) break;
                                    }
                                    parent = parent.parentElement;
                                }
                            }
                        })();

                        var currentPath = location.pathname;
                        var bestLd = null;
                        document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                            try {
                                var d = JSON.parse(s.textContent);
                                if (Array.isArray(d)) {
                                    d = d.find(function(i){ return i['@type'] === 'Product'; }) || null;
                                }
                                if (!d || d['@type'] !== 'Product') return;
                                if (!bestLd) bestLd = d;
                            } catch(e) {}
                        });
                        if (bestLd) {
                            if (!r.title) r.title = bestLd.name || '';
                            if (!r.price && bestLd.offers) {
                                var offers = bestLd.offers;
                                if (Array.isArray(offers)) offers = offers[0];
                                if (offers && offers.price) r.price = parseInt(offers.price);
                            }
                            if (bestLd.image) {
                                var imgs = Array.isArray(bestLd.image) ? bestLd.image : [bestLd.image];
                                imgs.forEach(function(i){ if (typeof i === 'string') r.images.push(i); });
                            }
                            r.description = bestLd.description || '';
                        }

                        if (r.images.length === 0) {
                            var ogImg = document.querySelector('meta[property="og:image"]');
                            if (ogImg && ogImg.content) r.images.push(ogImg.content);
                        }

                        var colorVariants = [];
                        var sizeVariants = [];

                        var colorSelectors = [
                            '[data-testid*="color"] button',
                            '[data-testid*="swatch"] button',
                            '[class*="ColorSwatch"] button',
                            '[class*="colorSwatch"] button',
                            '[class*="color-swatch"] button',
                            '[class*="SwatchWrapper"] button',
                        ];
                        colorSelectors.forEach(function(sel) {
                            document.querySelectorAll(sel).forEach(function(btn) {
                                var label = (btn.getAttribute('aria-label') || btn.getAttribute('title') || '').trim();
                                if (!label || label.length > 60) return;
                                var skip = ['カート', 'ログイン', '検索', 'メニュー', 'Close', 'Back', 'Next', 'Prev', 'Add to'];
                                for (var i = 0; i < skip.length; i++) { if (label.indexOf(skip[i]) !== -1) return; }
                                var alreadyAdded = colorVariants.some(function(v){ return v.label === label; });
                                if (!alreadyAdded) {
                                    var unavailable = btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('disabled');
                                    colorVariants.push({label: label, available: !unavailable});
                                }
                            });
                        });

                        var sizeSelectors = [
                            '[data-testid*="size"] button',
                            '[class*="SizeOption"] button',
                            '[class*="sizeOption"] button',
                            '[class*="SizeButton"] button',
                        ];
                        sizeSelectors.forEach(function(sel) {
                            document.querySelectorAll(sel).forEach(function(btn) {
                                var label = (btn.getAttribute('aria-label') || btn.textContent || '').trim();
                                if (!label || label.length > 20) return;
                                var alreadyAdded = sizeVariants.some(function(v){ return v.label === label; });
                                if (!alreadyAdded) {
                                    var unavailable = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
                                    sizeVariants.push({label: label, available: !unavailable});
                                }
                            });
                        });

                        if (colorVariants.length > 0 || sizeVariants.length > 0) {
                            var colors = colorVariants.length > 0 ? colorVariants : [{label: '', available: true}];
                            var sizes = sizeVariants.length > 0 ? sizeVariants : [{label: '', available: true}];
                            colors.forEach(function(c) {
                                sizes.forEach(function(s) {
                                    r.variants.push({
                                        color: c.label,
                                        size: s.label,
                                        sku: c.label + (s.label ? '-' + s.label : ''),
                                        price: r.price,
                                        in_stock: c.available && s.available,
                                        image: ''
                                    });
                                });
                            });
                        }

                        return r;
                    """)

                    if result:
                        return result

                    return None

                except Exception as e:
                    print(f"[Oakley] SeleniumBase 錯誤: {type(e).__name__}: {e}")
                    if attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None

        return None
