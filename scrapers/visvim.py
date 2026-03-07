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

                    # ===== DEBUG DUMP =====
                    debug_info = driver.execute_script("""
                        var result = {
                            w_elements: [],
                            options: [],
                            selects: [],
                            price_els: [],
                            body_snippet: ''
                        };

                        // W[数字] を含む全要素
                        document.querySelectorAll('*').forEach(function(el) {
                            var own = '';
                            for (var i = 0; i < el.childNodes.length; i++) {
                                if (el.childNodes[i].nodeType === 3) {
                                    own += el.childNodes[i].textContent;
                                }
                            }
                            own = own.trim();
                            if (/W[0-9]/.test(own) && own.length < 50) {
                                result.w_elements.push(
                                    el.tagName + '.' + (el.className||'').toString().replace(/\\s+/g,'_').substring(0,40)
                                    + ' => "' + own.substring(0,40) + '"'
                                );
                            }
                        });

                        // 全 select と option
                        document.querySelectorAll('select').forEach(function(sel) {
                            var info = 'SELECT name=' + (sel.name||sel.id||'?') + ' options=[';
                            var opts = [];
                            sel.querySelectorAll('option').forEach(function(o) {
                                opts.push('"' + o.textContent.trim().substring(0,20) + '"');
                            });
                            info += opts.join(',') + ']';
                            result.selects.push(info);
                        });

                        // 全 option（select 外も含む）
                        document.querySelectorAll('option').forEach(function(o) {
                            result.options.push('val=' + o.value + ' text=' + o.textContent.trim().substring(0,30));
                        });

                        // 価格っぽい要素
                        document.querySelectorAll('[class*="price"],[class*="Price"],[itemprop="price"]').forEach(function(el) {
                            result.price_els.push(
                                el.tagName + '.' + (el.className||'').toString().replace(/\\s+/g,'_').substring(0,30)
                                + ' content=' + (el.getAttribute('content')||'')
                                + ' text="' + el.textContent.trim().substring(0,30) + '"'
                            );
                        });

                        // body 先頭 3000 文字（構造確認用）
                        result.body_snippet = document.body.innerHTML.substring(0, 3000);

                        return result;
                    """)

                    print(f"[visvim][DEBUG] W要素({len(debug_info.get('w_elements',[]))}件):")
                    for x in debug_info.get('w_elements', []):
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] SELECT({len(debug_info.get('selects',[]))}件):")
                    for x in debug_info.get('selects', []):
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] OPTION({len(debug_info.get('options',[]))}件):")
                    for x in debug_info.get('options', []):
                        print(f"  {x}")

                    print(f"[visvim][DEBUG] PRICE_ELS({len(debug_info.get('price_els',[]))}件):")
                    for x in debug_info.get('price_els', []):
                        print(f"  {x}")

                    snippet = debug_info.get('body_snippet', '')
                    print(f"[visvim][DEBUG] body_snippet(先頭3000文字):")
                    # 500文字ずつ分割して出力
                    for i in range(0, min(len(snippet), 3000), 500):
                        print(f"  [{i}] {snippet[i:i+500]}")

                    # variants は今は空で返す（デバッグ優先）
                    title_el = driver.execute_script("""
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

                    if title_el and title_el.get('title'):
                        return {
                            'title': title_el['title'],
                            'price': title_el.get('price', 0),
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
