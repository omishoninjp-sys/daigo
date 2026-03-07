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

                        has_data = "detail-shoppingbag-list-size-no" in html or "og:title" in html
                        print(f"[visvim] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                        if i >= 1 and has_data:
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
                        // ページ内全テキストから ¥XXX,XXX パターンを探す
                        var allText = document.body.innerText || '';
                        var priceMatches = allText.match(/¥[\d,]+/g) || [];
                        priceMatches.forEach(function(pm) {
                            if (r.price) return;
                            var p = parseInt(pm.replace(/[¥,]/g, ''));
                            if (p >= 10000 && p <= 2000000) r.price = p;
                        });
                        // JSON-LD fallback
                        if (!r.price) {
                            document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                                if (r.price) return;
                                try {
                                    var d = JSON.parse(s.textContent);
                                    if (Array.isArray(d)) d = d.find(function(i){return i['@type']==='Product';}) || null;
                                    if (!d || d['@type'] !== 'Product') return;
                                    var offers = d.offers || {};
                                    if (Array.isArray(offers)) offers = offers[0] || {};
                                    if (offers.price) {
                                        var p = parseInt(String(offers.price).replace(/,/g,''));
                                        if (p >= 1000) r.price = p;
                                    }
                                } catch(e) {}
                            });
                        }

                        // --- 画像 ---
                        var ogImg = document.querySelector('meta[property="og:image"]');
                        if (ogImg && ogImg.content) r.images.push(ogImg.content);

                        // --- Variants ---
                        // 戦略: td.detail-shoppingbag-list-size-no を全部取得し、
                        //       各要素から上に tr → td(兄弟th) をたどってカラー名取得
                        //
                        // DOM:
                        //   table.detail-shoppingbag-list-color
                        //     tbody > tr
                        //       th → img + span(カラー名)
                        //       td
                        //         table.detail-shoppingbag-list-size
                        //           tbody > tr
                        //             td.detail-shoppingbag-list-size-no   ← ここから上へたどる
                        //             td.detail-shoppingbag-list-size-stock
                        //             td.detail-shoppingbag-list-size-btn

                        var sizeNoCells = document.querySelectorAll('td.detail-shoppingbag-list-size-no');
                        r.debug += ' sizeNoCells:' + sizeNoCells.length;

                        sizeNoCells.forEach(function(sizeNoCell) {
                            var size = sizeNoCell.textContent.trim();
                            if (!size) return;

                            // 同じ tr の中の stock / btn セル
                            var sizeRow   = sizeNoCell.parentElement; // <tr>
                            var stockCell = sizeRow ? sizeRow.querySelector('.detail-shoppingbag-list-size-stock') : null;
                            var btnCell   = sizeRow ? sizeRow.querySelector('.detail-shoppingbag-list-size-btn')   : null;

                            var stockText = stockCell ? stockCell.textContent.trim() : '';
                            var inStock   = stockText !== 'Sold Out' && stockText !== 'SOLD OUT';

                            // カラー名を探す:
                            // sizeNoCell → tr → tbody → table.detail-shoppingbag-list-size
                            // → td（親） → tr（カラー行） → th → span
                            var colorName = '';
                            var colorImg  = '';

                            var sizeTable = sizeNoCell.closest('table');
                            if (sizeTable) {
                                var colorTd = sizeTable.parentElement; // <td>
                                if (colorTd) {
                                    var colorTr = colorTd.parentElement; // <tr>
                                    if (colorTr) {
                                        var th = colorTr.querySelector('th');
                                        if (th) {
                                            var span = th.querySelector('span');
                                            colorName = span ? span.textContent.trim() : '';
                                            var img = th.querySelector('img');
                                            if (img) {
                                                colorImg = img.getAttribute('data-thumb') ||
                                                           img.getAttribute('data-src') ||
                                                           img.src || '';
                                                if (colorImg && !colorImg.startsWith('http')) {
                                                    colorImg = 'https://shop.visvim.tv' + colorImg;
                                                }
                                                // サムネ → ラージ変換
                                                colorImg = colorImg.replace('_S0.', '_L0.').replace('_400_', '_800_');
                                                if (colorImg && r.images.indexOf(colorImg) === -1) {
                                                    r.images.push(colorImg);
                                                }
                                            }
                                        }
                                    }
                                }
                            }

                            r.variants.push({
                                color:    colorName,
                                size:     size,
                                sku:      (colorName ? colorName + '-' : '') + size,
                                price:    r.price,
                                in_stock: inStock,
                                image:    colorImg
                            });
                        });

                        r.debug += ' variants:' + r.variants.length;
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
