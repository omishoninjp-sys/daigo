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
                        // Variants 抽出
                        // visvim ページ構造（スクリーンショットより）：
                        //   左側：カラー縮圖 + カラー名（BROWN / SAND）
                        //   右側：W6〜W10 の行 × 各カラー
                        //   各行：サイズ名 + "Sold Out" テキスト or "かごに入れる" ボタン
                        //
                        // 禁止ワード：言語・地域切替ボタン（Japanese/English/JAPAN/NORTH AMERICA/EUROPE/ASIA/OCEANIA）
                        // ============================================================

                        var IGNORE = /^(Japanese|English|JAPAN|NORTH AMERICA|EUROPE|ASIA|OCEANIA|USD|JPY|EUR|GBP|CNY|KRW|HKD|TWD)$/i;
                        var COLOR_WORDS = /^(BROWN|SAND|BLACK|WHITE|NAVY|GREY|GRAY|RED|BLUE|GREEN|BEIGE|KHAKI|OLIVE|NATURAL|CAMEL|TAN|COGNAC|ECRU|OFF WHITE|OFF-WHITE|IVORY|CHARCOAL|STONE|BURGUNDY|WINE|RUST|ORANGE|YELLOW|PINK|PURPLE|SILVER|GOLD|MULTI|CAMO|CHECK|STRIPE|DARK BROWN|LIGHT GRAY|LIGHT GREY|DARK NAVY)$/i;
                        var SIZE_RE = /^W\d+$/;

                        var variants = [];

                        // 全葉ノードをスキャンしてサイズ行を特定
                        var sizeRows = [];
                        var allEls = Array.from(document.body.querySelectorAll('*'));
                        allEls.forEach(function(el) {
                            if (['SCRIPT','STYLE','NAV','HEADER','FOOTER','META','LINK'].indexOf(el.tagName) !== -1) return;
                            if (el.children.length > 4) return;
                            var t = el.textContent.replace(/\s+/g, ' ').trim();
                            if (!SIZE_RE.test(t.split(' ')[0]) && !SIZE_RE.test(t)) {
                                // W6, W7... で始まる行 or 完全一致
                                var m = t.match(/^(W\d+)\b/);
                                if (!m) return;
                            }
                            var sizeMatch = t.match(/^(W\d+)/);
                            if (!sizeMatch) return;
                            var size = sizeMatch[1];
                            var inStock = t.indexOf('Sold Out') === -1 && t.indexOf('SOLD OUT') === -1 && t.indexOf('sold out') === -1;
                            // ボタンが子要素にあれば在庫あり確定
                            var btn = el.querySelector('button');
                            if (btn) {
                                var bt = btn.textContent.trim();
                                if (bt.indexOf('Sold') !== -1 || bt.indexOf('SOLD') !== -1) inStock = false;
                                else if (bt.length > 1) inStock = true;
                            }
                            sizeRows.push({el: el, size: size, inStock: inStock, rect: el.getBoundingClientRect()});
                        });

                        r.debug += ' sizeRows:' + sizeRows.length;

                        // カラー候補を収集（COLOR_WORDSに一致する葉ノード）
                        var colorCands = [];
                        allEls.forEach(function(el) {
                            if (el.children.length > 0) return;
                            var t = el.textContent.trim();
                            if (COLOR_WORDS.test(t) && !IGNORE.test(t)) {
                                colorCands.push({el: el, color: t.toUpperCase(), rect: el.getBoundingClientRect()});
                            }
                        });

                        r.debug += ' colorCands:' + colorCands.length;

                        if (sizeRows.length > 0) {
                            sizeRows.forEach(function(row) {
                                var rowY = row.rect.top + window.scrollY;

                                // 自分より上（Y座標が小さい）で最も近いカラー候補
                                var bestColor = '';
                                var bestImg   = '';
                                var bestDist  = 99999;

                                colorCands.forEach(function(cc) {
                                    var ccY = cc.rect.top + window.scrollY;
                                    var dy  = rowY - ccY;
                                    if (dy >= -5 && dy < bestDist) {
                                        bestDist  = dy;
                                        bestColor = cc.color;
                                        // カラーラベル近辺のサムネイル画像を探す
                                        var parent = cc.el.parentElement;
                                        for (var d = 0; d < 6; d++) {
                                            if (!parent) break;
                                            var img = parent.querySelector('img');
                                            if (img && img.src && img.src.indexOf('visvim') !== -1) {
                                                bestImg = img.src;
                                                break;
                                            }
                                            parent = parent.parentElement;
                                        }
                                    }
                                });

                                variants.push({
                                    color:    bestColor,
                                    size:     row.size,
                                    sku:      (bestColor ? bestColor + '-' : '') + row.size,
                                    price:    r.price,
                                    in_stock: row.inStock,
                                    image:    bestImg
                                });
                            });
                        }

                        // 重複除去
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
