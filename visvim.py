"""
visvim WMV Official Web Store 爬蟲 Mixin
Oracle NetSuite 電商平台，JS 渲染，使用 SeleniumBase UC

DOM 構造（実測確認済み）:
  table.detail-shoppingbag-list-color
    tr（カラーごと）
      th → img[alt=色名] + span（色名）
      td → table.detail-shoppingbag-list-size
             tr（サイズごと）
               td.detail-shoppingbag-list-size-no    → "W6"
               td.detail-shoppingbag-list-size-stock → "Sold Out" or ""
               td.detail-shoppingbag-list-size-btn   → button or empty
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

                    # ページ読み込み待機（NetSuite JS redirect 対応）
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
                            "detail-shoppingbag-list-color" in html or
                            "og:title" in html
                        )
                        print(f"[visvim] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                        if i >= 1 and has_data:
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

                        // --- 価格（税込）---
                        // .detail-price 系クラス or ページ内最初の ¥XXXXX
                        var priceSelectors = [
                            '.detail-price',
                            '.detail-price-now',
                            '[class*="detail-price"]',
                            '[itemprop="price"]',
                        ];
                        for (var si = 0; si < priceSelectors.length && !r.price; si++) {
                            var el = document.querySelector(priceSelectors[si]);
                            if (!el) continue;
                            var raw = el.getAttribute('content') || el.textContent;
                            var m = (raw || '').replace(/,/g, '').match(/[0-9]{4,7}/);
                            if (m) {
                                var p = parseInt(m[0]);
                                if (p >= 1000 && p <= 2000000) r.price = p;
                            }
                        }
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
                        // 商品メイン画像
                        document.querySelectorAll('.detail-image img, [class*="detail-img"] img').forEach(function(img) {
                            var src = img.getAttribute('data-thumb') || img.getAttribute('data-src') || img.src || '';
                            if (src && src.indexOf('visvim') !== -1 && r.images.indexOf(src) === -1) {
                                r.images.push(src);
                            }
                        });

                        // --- 説明 ---
                        var descEl = document.querySelector('.detail-description, [class*="detail-desc"], [itemprop="description"]');
                        if (descEl) r.description = descEl.textContent.trim().substring(0, 500);

                        // --- Variants（実測DOM構造に基づく）---
                        // table.detail-shoppingbag-list-color の各 tr がカラーグループ
                        var colorTable = document.querySelector('table.detail-shoppingbag-list-color');
                        if (colorTable) {
                            var colorRows = colorTable.querySelectorAll(':scope > tbody > tr');
                            colorRows.forEach(function(colorRow) {
                                // th → カラー画像 + カラー名
                                var th = colorRow.querySelector('th');
                                if (!th) return;

                                var colorSpan = th.querySelector('span');
                                var colorName = colorSpan ? colorSpan.textContent.trim() : '';

                                var colorImg = '';
                                var img = th.querySelector('img');
                                if (img) {
                                    colorImg = img.getAttribute('data-thumb') ||
                                               img.getAttribute('data-src') ||
                                               img.src || '';
                                    // 相対 URL → 絶対 URL
                                    if (colorImg && !colorImg.startsWith('http')) {
                                        colorImg = 'https://shop.visvim.tv' + colorImg;
                                    }
                                    // サムネをラージ画像に変換（_S0 → _L0）
                                    colorImg = colorImg.replace(/_S0\./, '_L0.').replace(/_400_/, '_800_');
                                    // カラー画像を extra_images にも追加
                                    if (colorImg && r.images.indexOf(colorImg) === -1) {
                                        r.images.push(colorImg);
                                    }
                                }

                                // td → table.detail-shoppingbag-list-size の各 tr がサイズ行
                                var sizeTable = colorRow.querySelector('table.detail-shoppingbag-list-size');
                                if (!sizeTable) return;

                                var sizeRows = sizeTable.querySelectorAll('tr');
                                sizeRows.forEach(function(sizeRow) {
                                    var sizeNo   = sizeRow.querySelector('.detail-shoppingbag-list-size-no');
                                    var sizeStock = sizeRow.querySelector('.detail-shoppingbag-list-size-stock');
                                    var sizeBtn   = sizeRow.querySelector('.detail-shoppingbag-list-size-btn');

                                    if (!sizeNo) return;
                                    var size = sizeNo.textContent.trim();
                                    if (!size) return;

                                    // 在庫判定:
                                    //   stock セルが "Sold Out" → 在庫なし
                                    //   btn セルに button あり → 在庫あり
                                    //   stock セルが空 → 在庫あり
                                    var stockText = sizeStock ? sizeStock.textContent.trim() : '';
                                    var hasButton = sizeBtn && sizeBtn.querySelector('button') !== null;
                                    var inStock   = stockText !== 'Sold Out' && stockText !== 'SOLD OUT';

                                    r.variants.push({
                                        color:    colorName,
                                        size:     size,
                                        sku:      colorName + '-' + size,
                                        price:    r.price,
                                        in_stock: inStock,
                                        image:    colorImg
                                    });
                                });
                            });
                        }

                        return r;
                    """)

                    if result and result.get("title"):
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
