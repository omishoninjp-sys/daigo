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
        for v in product.variants:
            print(f"  - {v.get('color','')} / {v.get('size','')} / {'有庫存' if v.get('in_stock') else 'Sold Out'}")
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

                    for i in range(15):
                        _time.sleep(2)
                        try:
                            html  = driver.page_source
                            title = driver.title
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                break
                            continue

                        if "Redirecting" in (title or "") and i < 10:
                            print(f"[visvim] redirect 待機中... ({i+1})")
                            continue

                        has_data = (
                            "og:title"            in html or
                            "application/ld+json" in html or
                            "NS.url"              in html or
                            "itemid"              in html.lower()
                        )
                        print(f"[visvim] 嘗試 {i+1}: {len(html)} bytes | data={has_data} | title={title[:50] if title else ''}")

                        if i >= 2 and has_data:
                            break

                    result = driver.execute_script("""
                        var r = {title:'', price:0, images:[], description:'', variants:[], debug:''};

                        // --- タイトル ---
                        var h1 = document.querySelector('h1');
                        if (h1) r.title = h1.textContent.trim();
                        if (!r.title) {
                            var og = document.querySelector('meta[property="og:title"]');
                            if (og) r.title = (og.content || '').trim();
                        }

                        // --- 価格 ---
                        var priceSelectors = ['[itemprop="price"]','[class*="price"]','[class*="Price"]','[data-price]'];
                        for (var si = 0; si < priceSelectors.length && !r.price; si++) {
                            document.querySelectorAll(priceSelectors[si]).forEach(function(el) {
                                if (r.price) return;
                                var raw = el.getAttribute('content') || el.getAttribute('data-price') || el.textContent;
                                var m = (raw || '').replace(/,/g,'').match(/[0-9]{4,7}/);
                                if (m) {
                                    var p = parseInt(m[0]);
                                    if (p >= 1000 && p <= 2000000) r.price = p;
                                }
                            });
                        }
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
                            var descEl = document.querySelector('[itemprop="description"], [class*="description"]');
                            if (descEl) r.description = descEl.textContent.trim().substring(0, 500);
                        }

                        // ============================================================
                        // Variants 抽出 v3
                        //
                        // visvim ページ構造（実測）:
                        //   カラーラベル（DK.BROWN / BROWN）が縦方向に並ぶ
                        //   各カラーの横に W6〜W10 のサイズ行
                        //   カラーラベルは垂直中央揃えのため W6 より Y が大きい場合あり
                        //
                        // 戦略:
                        //   1. サイズ行（W\d+ で始まる要素）を収集
                        //   2. カラーラベル（大文字短文・無視ワード除外）を収集
                        //   3. 各カラーが担当するサイズ範囲を Y 座標で分割して割り当て
                        //
                        // 無視ワード（言語・地域切替）:
                        //   Japanese, English, JAPAN, NORTH AMERICA, EUROPE, ASIA, OCEANIA, NEW, SALE
                        // ============================================================

                        var IGNORE_SET = {
                            'Japanese':1, 'English':1,
                            'JAPAN':1, 'NORTH AMERICA':1, 'EUROPE':1,
                            'ASIA':1, 'OCEANIA':1, 'ASIA / OCEANIA':1,
                            'USD':1, 'JPY':1, 'EUR':1, 'GBP':1, 'CNY':1, 'KRW':1, 'HKD':1, 'TWD':1,
                            'NEW':1, 'SALE':1, 'SOLD OUT':1, 'ADD TO CART':1,
                            'MEN':1, "MEN'S":1, 'WOMEN':1, "WOMEN'S":1, 'UNISEX':1,
                        };

                        // カラーラベルのパターン:
                        //   大文字・数字・スペース・ドット・スラッシュ・ハイフンのみ
                        //   2〜25文字、数字で始まらない
                        var COLOR_RE = /^[A-Z][A-Z0-9 .\/\-]*$/;
                        var SIZE_RE  = /^W\d+$/;

                        var allEls = Array.from(document.body.querySelectorAll('*'));

                        // --- サイズ行を収集（W6, W7, ... にマッチする最小要素）---
                        var sizeRows = [];
                        allEls.forEach(function(el) {
                            if (['SCRIPT','STYLE','NAV','HEADER','FOOTER','BUTTON'].indexOf(el.tagName) !== -1) return;
                            // 葉ノードか、子が1〜2個程度の薄い要素
                            if (el.children.length > 3) return;
                            var t = el.textContent.replace(/\s+/g,' ').trim();
                            var m = t.match(/^(W\d+)\b/);
                            if (!m) return;
                            var size = m[1];
                            // 在庫判定
                            var inStock = t.indexOf('Sold Out') === -1 &&
                                          t.indexOf('SOLD OUT') === -1 &&
                                          t.indexOf('sold out') === -1;
                            // ボタン有無でも判定補強
                            var btn = el.querySelector('button') ||
                                      el.closest('tr,li,div') && el.closest('tr,li,div').querySelector('button[class*="cart"], button[class*="add"]');
                            if (btn) {
                                var bt = btn.textContent.trim().toLowerCase();
                                if (bt.indexOf('sold') !== -1) inStock = false;
                                else if (bt.indexOf('cart') !== -1 || bt.indexOf('かご') !== -1) inStock = true;
                            }
                            var rect = el.getBoundingClientRect();
                            // 画面外（表示されていない）要素は除外
                            if (rect.width === 0 && rect.height === 0) return;
                            sizeRows.push({el:el, size:size, inStock:inStock, y: rect.top + window.scrollY});
                        });

                        // 重複除去（同じ size + 近い Y は同じ行）
                        var dedupedSizes = [];
                        sizeRows.sort(function(a,b){return a.y - b.y;});
                        sizeRows.forEach(function(row) {
                            var dup = dedupedSizes.find(function(d){ return d.size === row.size && Math.abs(d.y - row.y) < 5; });
                            if (!dup) dedupedSizes.push(row);
                        });
                        sizeRows = dedupedSizes;

                        r.debug += ' sizeRows:' + sizeRows.length;

                        // --- カラーラベルを収集 ---
                        var colorCands = [];
                        allEls.forEach(function(el) {
                            // 葉ノードのみ
                            if (el.children.length > 0) return;
                            var t = el.textContent.trim();
                            if (!t || t.length < 2 || t.length > 25) return;
                            if (!COLOR_RE.test(t)) return;
                            if (IGNORE_SET[t] || IGNORE_SET[t.toUpperCase()]) return;
                            if (SIZE_RE.test(t)) return;
                            if (/^\d/.test(t)) return;
                            // 数字のみは除外
                            if (/^[0-9 ]+$/.test(t)) return;
                            var rect = el.getBoundingClientRect();
                            if (rect.width === 0 && rect.height === 0) return;
                            // カラーサムネイル画像が近くにあるか確認（visvim はサムネあり）
                            var nearImg = '';
                            var parent = el.parentElement;
                            for (var d = 0; d < 6; d++) {
                                if (!parent) break;
                                var img = parent.querySelector('img[src*="visvim"]');
                                if (img && img.src) { nearImg = img.src; break; }
                                parent = parent.parentElement;
                            }
                            colorCands.push({
                                color: t,
                                y: rect.top + window.scrollY,
                                image: nearImg
                            });
                        });

                        // 重複除去
                        var dedupedColors = [];
                        colorCands.sort(function(a,b){return a.y - b.y;});
                        colorCands.forEach(function(cc) {
                            var dup = dedupedColors.find(function(d){ return d.color === cc.color; });
                            if (!dup) dedupedColors.push(cc);
                        });
                        colorCands = dedupedColors;

                        r.debug += ' colorCands:' + colorCands.length;
                        r.debug += ' colors:[' + colorCands.map(function(c){return c.color;}).join(',') + ']';

                        var variants = [];

                        if (sizeRows.length > 0 && colorCands.length > 0) {
                            // 各カラーが担当する Y 範囲を決める
                            // カラー i の範囲: colorCands[i].y - margin  〜  colorCands[i+1].y - margin
                            // margin = 各カラー間距離の半分、またはサイズ行の平均高さ

                            colorCands.forEach(function(cc, idx) {
                                var rangeStart = cc.y - 80;   // カラーラベルより上のサイズも含む（垂直中央揃え対策）
                                var rangeEnd   = idx < colorCands.length - 1
                                                    ? colorCands[idx + 1].y - 80
                                                    : Infinity;

                                sizeRows.forEach(function(row) {
                                    if (row.y >= rangeStart && row.y < rangeEnd) {
                                        variants.push({
                                            color:    cc.color,
                                            size:     row.size,
                                            sku:      cc.color + '-' + row.size,
                                            price:    r.price,
                                            in_stock: row.inStock,
                                            image:    cc.image
                                        });
                                    }
                                });
                            });

                        } else if (sizeRows.length > 0) {
                            // カラーなし → サイズのみ
                            sizeRows.forEach(function(row) {
                                variants.push({
                                    color:    '',
                                    size:     row.size,
                                    sku:      row.size,
                                    price:    r.price,
                                    in_stock: row.inStock,
                                    image:    ''
                                });
                            });
                        }

                        // 重複除去（color+size キー）
                        var seen = {};
                        r.variants = variants.filter(function(v) {
                            var key = v.color + '|' + v.size;
                            if (seen[key]) return false;
                            seen[key] = true;
                            return true;
                        });

                        r.debug += ' finalVariants:' + r.variants.length;
                        return r;
                    """)

                    if result:
                        print(f"[visvim] debug: {result.get('debug','')}")
                        if result.get("title"):
                            return result

                    return None

                except Exception as e:
                    print(f"[visvim] SeleniumBase エラー: {type(e).__name__}: {e}")
                    if attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None

        return None
