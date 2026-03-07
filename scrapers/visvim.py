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

                    # 初回ロード待機
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

                    # ===== スクロールして lazy load を発火 =====
                    try:
                        driver.execute_script("window.scrollTo(0, 300);")
                        _time.sleep(1)
                        driver.execute_script("window.scrollTo(0, 600);")
                        _time.sleep(1)
                        driver.execute_script("window.scrollTo(0, 1200);")
                        _time.sleep(2)
                        driver.execute_script("window.scrollTo(0, 0);")
                        _time.sleep(1)
                    except Exception:
                        pass

                    # ===== DEBUG DUMP v2 =====
                    debug_info = driver.execute_script("""
                        var result = {
                            total_html_len: document.body.innerHTML.length,
                            w_elements: [],
                            selects: [],
                            options: [],
                            price_els: [],
                            sold_out_elements: [],
                            product_section: ''
                        };

                        // W[数字] を直接テキストに持つ要素（自分のテキストノードのみ）
                        document.querySelectorAll('*').forEach(function(el) {
                            var own = '';
                            for (var i = 0; i < el.childNodes.length; i++) {
                                if (el.childNodes[i].nodeType === 3) {
                                    own += el.childNodes[i].textContent;
                                }
                            }
                            own = own.trim();
                            if (/W[0-9]/.test(own) && own.length < 60) {
                                result.w_elements.push(
                                    el.tagName
                                    + '[class=' + (el.className||'').toString().substring(0,40) + ']'
                                    + ' => "' + own.substring(0,50) + '"'
                                );
                            }
                        });

                        // 全 select
                        document.querySelectorAll('select').forEach(function(sel) {
                            var opts = [];
                            sel.querySelectorAll('option').forEach(function(o) {
                                opts.push(o.value.substring(0,20) + ':' + o.textContent.trim().substring(0,20));
                            });
                            result.selects.push('name=' + (sel.name||sel.id||'?') + ' [' + opts.join(' | ') + ']');
                        });

                        // 全 option
                        document.querySelectorAll('option').forEach(function(o) {
                            result.options.push('val="' + o.value.substring(0,30) + '" text="' + o.textContent.trim().substring(0,30) + '"');
                        });

                        // 価格要素
                        document.querySelectorAll('[class*="price"],[class*="Price"],[itemprop="price"],[class*="amount"],[class*="Amount"]').forEach(function(el) {
                            result.price_els.push(
                                el.tagName + '[' + (el.className||'').toString().substring(0,30) + ']'
                                + ' content="' + (el.getAttribute('content')||'') + '"'
                                + ' text="' + el.textContent.trim().substring(0,40) + '"'
                            );
                        });

                        // Sold Out テキストを持つ要素
                        document.querySelectorAll('*').forEach(function(el) {
                            if (el.children.length > 3) return;
                            var t = el.textContent.trim();
                            if (t === 'Sold Out' || t === 'SOLD OUT') {
                                result.sold_out_elements.push(
                                    el.tagName + '[' + (el.className||'').toString().substring(0,40) + ']'
                                );
                            }
                        });

                        // 商品詳細セクション（h1 の親要素の innerHTML）
                        var h1 = document.querySelector('h1');
                        if (h1) {
                            var parent = h1.parentElement;
                            for (var d = 0; d < 5; d++) {
                                if (!parent) break;
                                if (parent.innerHTML.length > 500 && parent.innerHTML.length < 20000) {
                                    result.product_section = parent.innerHTML.substring(0, 5000);
                                    break;
                                }
                                parent = parent.parentElement;
                            }
                        }

                        return result;
                    """)

                    print(f"[visvim][DEBUG] HTML総サイズ: {debug_info.get('total_html_len')} bytes")

                    print(f"[visvim][DEBUG] W要素({len(debug_info.get('w_elements',[]))}件):")
                    for x in debug_info.get('w_elements', []):
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] SELECT({len(debug_info.get('selects',[]))}件):")
                    for x in debug_info.get('selects', []):
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] OPTION({len(debug_info.get('options',[]))}件):")
                    for x in debug_info.get('options', [])[:30]:
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] PRICE_ELS({len(debug_info.get('price_els',[]))}件):")
                    for x in debug_info.get('price_els', []):
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] SOLD_OUT要素({len(debug_info.get('sold_out_elements',[]))}件):")
                    for x in debug_info.get('sold_out_elements', [])[:10]:
                        print(f"  {x}")

                    section = debug_info.get('product_section', '')
                    if section:
                        print(f"[visvim][DEBUG] product_section(5000文字):")
                        for i in range(0, min(len(section), 5000), 500):
                            print(f"  [{i}] {section[i:i+500]}")
                    else:
                        print(f"[visvim][DEBUG] product_section: 見つからず")

                    # 最低限のデータだけ返す（デバッグ優先）
                    basic = driver.execute_script("""
                        var h1 = document.querySelector('h1');
                        var price = 0;
                        document.querySelectorAll('[class*="price"],[itemprop="price"]').forEach(function(el) {
                            if (price) return;
                            var raw = el.getAttribute('content') || el.textContent;
                            var m = (raw||'').replace(/,/g,'').match(/[0-9]{4,7}/);
                            if (m) { var p = parseInt(m[0]); if (p >= 1000 && p <= 2000000) price = p; }
                        });
                        var og = document.querySelector('meta[property="og:title"]');
                        return {
                            title: h1 ? h1.textContent.trim() : (og ? og.content : ''),
                            price: price
                        };
                    """)

                    if basic and basic.get('title'):
                        return {
                            'title': basic['title'],
                            'price': basic.get('price', 0),
                            'images': [],
                            'description': '',
                            'variants': []
                        }

                    return None

                except Exception as e:
                    print(f"[visvim] SeleniumBase エラー: {type(e).__name__}: {e}")
                    if attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None

        return None
