"""
Human Made (humanmade.jp) 爬蟲 Mixin
humanmade.jp 使用 Salesforce Commerce Cloud (SFCC) 自建平台，有 WAF 防護，
需要 Playwright headless browser 才能正常讀取。
"""
from scrapers.base import ProductInfo


class HumanMadeMixin:

    async def _scrape_humanmade(self, url: str) -> ProductInfo:
        page = await self._get_playwright_page()
        product = ProductInfo(source_url=url, brand="Human Made")

        # 載入頁面
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            try:
                await page.wait_for_timeout(5000)
            except Exception:
                pass

        # 關閉 Global-e 國際運送彈窗
        try:
            await page.evaluate("""() => {
                const ge = document.getElementById('globalePopupWrapper');
                if (ge) ge.remove();
                document.querySelectorAll('[class*="globale"], [id*="globale"]').forEach(el => {
                    if (getComputedStyle(el).position === 'fixed') el.remove();
                });
            }""")
        except Exception:
            pass

        # 關閉 Cookie 彈窗
        try:
            btn = page.locator('text=同意する').first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        try:
            data = await page.evaluate("""() => {
                const result = {
                    title: '',
                    price_jpy: 0,
                    sizes: [],
                    colors: [],
                    images: [],
                    description: ''
                };

                // === 商品名稱 ===
                for (const sel of ['h1', '.product-title', '.product-name', 'main h1']) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim().length > 2) {
                        result.title = el.textContent.trim();
                        break;
                    }
                }

                // === 價格（專門找結構化 JPY，避免抓到碎片金額）===
                // SFCC 常見 selector
                const priceSelectors = [
                    '.sales .value',
                    '.price-sales .value',
                    '.product-price .sales',
                    '[class*="price-sales"]',
                    '[class*="sales"][class*="price"]',
                    '.pdp-main .price .value',
                    '.product-detail .price'
                ];
                for (const sel of priceSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = el.textContent.trim();
                        const m = text.match(/[¥￥]\\s*([\\d,]+)/);
                        if (m) {
                            const val = parseInt(m[1].replace(/,/g, ''));
                            if (val >= 1000) {
                                result.price_jpy = val;
                                break;
                            }
                        }
                    }
                }

                // Fallback：用 TreeWalker 掃文字節點，找四位數以上的 ¥ 金額
                if (!result.price_jpy) {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        const text = node.textContent.trim();
                        // 只接受「¥XXXXX」格式，且金額 >= 1000
                        const m = text.match(/^[¥￥]\\s*([1-9][\\d,]{3,})$/);
                        if (m) {
                            const val = parseInt(m[1].replace(/,/g, ''));
                            if (val >= 1000 && val <= 500000) {
                                result.price_jpy = val;
                                break;
                            }
                        }
                    }
                }

                // === 尺寸 ===
                const sizeSelectors = [
                    '.attribute[data-attr="size"] button',
                    '.swatches.size button',
                    '.swatches.size label',
                    '[class*="size"] button',
                    '[class*="size"] label',
                    '[class*="Size"] button',
                    '[class*="Size"] label',
                    '[data-option="size"] button',
                    '[data-option="size"] label',
                    'li[data-attr-value]'
                ];
                const sizePattern = /^(XXS|XS|S|M|L|XL|2XL|3XL|4XL|ONE\\s*SIZE|FREE|OS|\\d{2,3})$/i;
                const seenSizes = new Set();
                for (const sel of sizeSelectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const text = el.textContent.trim().toUpperCase();
                        if (sizePattern.test(text) && !seenSizes.has(text)) {
                            seenSizes.add(text);
                            result.sizes.push(text);
                        }
                    });
                    if (result.sizes.length > 0) break;
                }

                // === 顏色 ===
                const colorSelectors = [
                    '.attribute[data-attr="color"] button',
                    '.swatches.color button',
                    '[class*="color"] label',
                    '[class*="color"] button'
                ];
                const seenColors = new Set();
                for (const sel of colorSelectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const text = (el.title || el.dataset.attrValue || el.textContent).trim();
                        if (text && text.length < 30 && !seenColors.has(text)) {
                            seenColors.add(text);
                            result.colors.push(text);
                        }
                    });
                    if (result.colors.length > 0) break;
                }

                // === 圖片 ===
                const excludePatterns = [
                    'icon', 'logo', 'svg', 'pixel', 'tracking', 'spacer',
                    'blank', 'globale', 'banner', 'badge', 'flag', 'payment'
                ];
                const seenImgs = new Set();
                const imgSelectors = [
                    '.primary-images img',
                    '.product-images img',
                    '.pdp-images img',
                    '.product-detail img',
                    '[class*="carousel"] img',
                    '[class*="gallery"] img',
                    'main img[src]'
                ];
                for (const sel of imgSelectors) {
                    document.querySelectorAll(sel).forEach(img => {
                        let src = img.src || img.dataset.src || '';
                        if (!src && img.srcset) {
                            src = img.srcset.split(',')[0].trim().split(' ')[0];
                        }
                        if (src && src.startsWith('http') && !seenImgs.has(src)) {
                            const sl = src.toLowerCase();
                            if (!excludePatterns.some(p => sl.includes(p))) {
                                seenImgs.add(src);
                                result.images.push(src);
                            }
                        }
                    });
                    if (result.images.length >= 5) break;
                }

                // === 商品說明 ===
                for (const sel of [
                    '#collapsible-description-1',
                    '.value.content',
                    '.product-description',
                    '.pdp-description'
                ]) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 20) {
                        result.description = el.innerText.trim();
                        break;
                    }
                }

                return result;
            }""")

            if data:
                product.title = data.get('title', '')
                price = data.get('price_jpy', 0)
                product.price_jpy = price if price and price >= 1000 else None
                product.description = data.get('description', '')

                images = data.get('images', [])
                if images:
                    product.image_url = images[0]
                    product.extra_images = images[1:]

                # variants：尺寸 × 顏色 組合
                sizes = data.get('sizes', [])
                colors = data.get('colors', [])

                if sizes or colors:
                    variants = []
                    if sizes and colors:
                        for color in colors:
                            for size in sizes:
                                variants.append({
                                    'option1': color,
                                    'option2': size,
                                    'title': f"{color} / {size}"
                                })
                    elif sizes:
                        for size in sizes:
                            variants.append({
                                'option1': size,
                                'title': size
                            })
                    elif colors:
                        for color in colors:
                            variants.append({
                                'option1': color,
                                'title': color
                            })
                    product.variants = variants

                print(f"[HumanMade] ✓ {product.title} ¥{product.price_jpy} "
                      f"sizes={sizes} colors={colors} images={len(images)}")

        except Exception as e:
            print(f"[HumanMade] ✗ 解析失敗 {url}: {e}")

        return product
