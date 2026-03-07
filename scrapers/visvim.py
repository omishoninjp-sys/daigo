"""
visvim WMV Official Web Store 爬蟲 Mixin
Oracle NetSuite 電商平台，JS 渲染，使用 SeleniumBase UC
"""
import asyncio
import time as _time

from scrapers.base import ProductInfo


class VisvimMixin:

    async def _scrape_visvim(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="visvim")

        data = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_visvim_uc, url
        )

        if data:
            product.title       = data.get("title", "")
            product.price_jpy   = data.get("price") or None
            product.description = data.get("description", "")[:500]
            images = data.get("images", [])
            if images:
                product.image_url    = images[0]
                product.extra_images = images[1:9]
            product.variants = data.get("variants", [])

        print(
            f"[visvim] {'✅' if product.title else '⚠️'} "
            f"{product.title[:40] if product.title else '未取得'} / "
            f"¥{product.price_jpy} / {len(product.variants)} variants"
        )
        return product

    def _fetch_visvim_uc(self, url: str) -> dict | None:
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
                        if "InvalidSession" in type(e).__name__:
                            self._driver = None
                            self._create_driver()
                            continue

                    # NetSuite は JS redirect があるので最大 15 回待機
                    for i in range(15):
                        _time.sleep(2)
                        try:
                            html  = driver.page_source
                            title = driver.title
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                break
                            continue

                        # Redirecting... ページは待つ
                        if "Redirecting" in (title or "") and i < 10:
                            print(f"[visvim] redirect 待機中... ({i+1})")
                            continue

                        has_data = (
                            "og:title"              in html or
                            "application/ld+json"   in html or
                            "netsuite"              in html.lower() or
                            "NS.url"                in html or
                            "itemid"                in html.lower()
                        )
                        print(f"[visvim] 嘗試 {i+1}: {len(html)} bytes | data={has_data} | title={title[:40] if title else ''}")

                        if i >= 2 and has_data:
                            break

                    result = driver.execute_script("""
                        var r = {title:'', price:0, images:[], description:'', variants:[]};

                        // --- タイトル ---
                        var h1 = document.querySelector('h1');
                        if (h1) r.title = h1.textContent.trim();
                        if (!r.title) {
                            var og = document.querySelector('meta[property="og:title"]');
                            if (og) r.title = (og.content || '').trim();
                        }

                        // --- 価格 ---
                        // NetSuite は .price クラスや data-rate 属性に入ることが多い
                        var priceSelectors = [
                            '[itemprop="price"]',
                            '[class*="price"]',
                            '[class*="Price"]',
                            '[data-price]',
                            '[class*="amt"]',
                        ];
                        for (var si = 0; si < priceSelectors.length && !r.price; si++) {
                            document.querySelectorAll(priceSelectors[si]).forEach(function(el) {
                                if (r.price) return;
                                var raw = el.getAttribute('content') ||
                                          el.getAttribute('data-price') ||
                                          el.textContent;
                                var m = (raw || '').replace(/,/g,'').match(/[0-9]{3,7}/);
                                if (m) {
                                    var p = parseInt(m[0]);
                                    if (p >= 1000 && p <= 2000000) r.price = p;
                                }
                            });
                        }

                        // JSON-LD fallback
                        if (!r.price) {
                            document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                                try {
                                    var d = JSON.parse(s.textContent);
                                    if (Array.isArray(d)) d = d.find(function(i){return i['@type']==='Product';}) || null;
                                    if (!d || d['@type'] !== 'Product') return;
                                    if (!r.title && d.name) r.title = d.name;
                                    var offers = d.offers || {};
                                    if (Array.isArray(offers)) offers = offers[0] || {};
                                    if (offers.price) {
                                        var p = parseInt(String(offers.price).replace(/,/g,''));
                                        if (p >= 1000) r.price = p;
                                    }
                                    if (d.image) {
                                        var imgs = Array.isArray(d.image) ? d.image : [d.image];
                                        imgs.forEach(function(u){ if (typeof u === 'string') r.images.push(u); });
                                    }
                                    r.description = d.description || '';
                                } catch(e) {}
                            });
                        }

                        // --- 画像 ---
                        if (r.images.length === 0) {
                            var ogImg = document.querySelector('meta[property="og:image"]');
                            if (ogImg && ogImg.content) r.images.push(ogImg.content);
                        }
                        document.querySelectorAll('img').forEach(function(img) {
                            var src = img.src || img.getAttribute('data-src') || '';
                            if (!src) return;
                            // visvim の商品画像 URL パターン
                            if (src.indexOf('visvim') !== -1 &&
                                src.indexOf('logo') === -1 &&
                                src.indexOf('icon') === -1 &&
                                r.images.indexOf(src) === -1 &&
                                r.images.length < 10) {
                                r.images.push(src);
                            }
                        });

                        // --- 説明 ---
                        if (!r.description) {
                            var descEl = document.querySelector(
                                '[itemprop="description"], [class*="description"], [class*="detail"]'
                            );
                            if (descEl) r.description = descEl.textContent.trim().substring(0, 500);
                        }

                        // --- Variants（カラー・サイズ）---
                        var colorOpts = [];
                        var sizeOpts  = [];

                        // select ベースのオプション（NetSuite 標準）
                        document.querySelectorAll('select').forEach(function(sel) {
                            var label = (
                                sel.getAttribute('aria-label') ||
                                sel.getAttribute('name') ||
                                sel.id || ''
                            ).toLowerCase();
                            var opts = [];
                            sel.querySelectorAll('option').forEach(function(opt) {
                                var v = opt.textContent.trim();
                                if (!v || v.indexOf('選択') !== -1 || v.indexOf('Select') !== -1 || v === '-') return;
                                opts.push({label: v, available: !opt.disabled});
                            });
                            if (opts.length === 0) return;
                            if (label.indexOf('color') !== -1 || label.indexOf('colour') !== -1 || label.indexOf('カラー') !== -1) {
                                colorOpts = opts;
                            } else if (label.indexOf('size') !== -1 || label.indexOf('サイズ') !== -1) {
                                sizeOpts = opts;
                            } else if (colorOpts.length === 0) {
                                colorOpts = opts;  // 最初の select をカラーとみなす
                            } else if (sizeOpts.length === 0) {
                                sizeOpts = opts;
                            }
                        });

                        // button ベースのオプション
                        if (colorOpts.length === 0) {
                            document.querySelectorAll('[class*="color"] button, [class*="swatch"] button').forEach(function(btn) {
                                var label = btn.getAttribute('aria-label') || btn.getAttribute('title') || btn.textContent.trim();
                                if (label && label.length < 40) {
                                    colorOpts.push({label: label, available: !btn.disabled});
                                }
                            });
                        }
                        if (sizeOpts.length === 0) {
                            document.querySelectorAll('[class*="size"] button').forEach(function(btn) {
                                var label = btn.getAttribute('aria-label') || btn.textContent.trim();
                                if (label && label.length < 20) {
                                    sizeOpts.push({label: label, available: !btn.disabled});
                                }
                            });
                        }

                        if (colorOpts.length > 0 || sizeOpts.length > 0) {
                            var colors = colorOpts.length > 0 ? colorOpts : [{label: '', available: true}];
                            var sizes  = sizeOpts.length  > 0 ? sizeOpts  : [{label: '', available: true}];
                            colors.forEach(function(c) {
                                sizes.forEach(function(s) {
                                    r.variants.push({
                                        color:    c.label,
                                        size:     s.label,
                                        sku:      c.label + (s.label ? '-' + s.label : ''),
                                        price:    r.price,
                                        in_stock: c.available && s.available,
                                        image:    ''
                                    });
                                });
                            });
                        }

                        return r;
                    """)

                    if result and result.get("title"):
                        return result

                    # タイトルが取れなければ None
                    return None

                except Exception as e:
                    print(f"[visvim] SeleniumBase エラー: {type(e).__name__}: {e}")
                    if attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None

        return None
