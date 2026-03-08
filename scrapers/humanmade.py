"""
Human Made (humanmade.jp) 爬蟲 Mixin
humanmade.jp 使用自建平台（原 SFCC），有 WAF 防護，
使用 SeleniumBase UC driver 繞過封鎖。
"""
import re
import time as _time

from scrapers.base import ProductInfo


class HumanMadeMixin:

    async def _scrape_humanmade(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Human Made")

        html = await self._humanmade_fetch_html(url)
        if not html:
            print(f"[HumanMade] ❌ 無法取得 HTML: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 商品名稱 ===
            for sel in [
                ("h1", {}),
                ("h1", {"class": re.compile(r"product")}),
                ("div", {"class": re.compile(r"product-name|product-title")}),
            ]:
                el = soup.find(sel[0], sel[1])
                if el and el.get_text(strip=True):
                    product.title = el.get_text(strip=True)
                    break

            # === 價格（只接受 ¥ + 四位數以上，避免抓到碎片）===
            price_jpy = 0
            # 先找結構化 selector
            for cls_pattern in [
                re.compile(r"sales"),
                re.compile(r"price-sales"),
                re.compile(r"product-price"),
            ]:
                el = soup.find(class_=cls_pattern)
                if el:
                    text = el.get_text(strip=True)
                    m = re.search(r'[¥￥]\s*([\d,]+)', text)
                    if m:
                        val = int(m.group(1).replace(',', ''))
                        if val >= 1000:
                            price_jpy = val
                            break

            # Fallback：掃全部文字節點，找 ¥XXXXX 格式（四位數以上）
            if not price_jpy:
                for text in soup.stripped_strings:
                    m = re.match(r'^[¥￥]\s*([1-9][\d,]{3,})$', text.strip())
                    if m:
                        val = int(m.group(1).replace(',', ''))
                        if 1000 <= val <= 500000:
                            price_jpy = val
                            break

            product.price_jpy = price_jpy if price_jpy >= 1000 else None

            # === 圖片 ===
            exclude = ['icon', 'logo', 'svg', 'pixel', 'tracking',
                       'spacer', 'blank', 'globale', 'banner', 'badge',
                       'flag', 'payment']
            imgs = []
            seen = set()
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
                if not src:
                    srcset = img.get("srcset", "")
                    if srcset:
                        src = srcset.split(",")[0].strip().split(" ")[0]
                if src and src.startswith("http") and src not in seen:
                    sl = src.lower()
                    if not any(p in sl for p in exclude):
                        seen.add(src)
                        imgs.append(src)
                if len(imgs) >= 8:
                    break

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:]

            # === 尺寸 ===
            sizes = []
            seen_sizes = set()
            size_pattern = re.compile(
                r'^(XXS|XS|S|M|L|XL|2XL|3XL|4XL|ONE\s*SIZE|FREE|OS|\d{2,3})$',
                re.IGNORECASE
            )
            # 嘗試找 size 相關 container
            size_containers = soup.find_all(
                class_=re.compile(r'size', re.I)
            ) + soup.find_all(attrs={"data-attr": "size"})

            for container in size_containers:
                for el in container.find_all(["button", "label", "li", "span"]):
                    text = el.get_text(strip=True).upper()
                    if size_pattern.match(text) and text not in seen_sizes:
                        seen_sizes.add(text)
                        sizes.append(text)

            # === 顏色 ===
            colors = []
            seen_colors = set()
            color_containers = soup.find_all(
                class_=re.compile(r'color', re.I)
            ) + soup.find_all(attrs={"data-attr": "color"})

            # 排除這些不是顏色名稱的文字
            color_exclude = {
                "color", "カラー", "色", "colour",
                "size", "サイズ", "寸法",
                "select", "選択", "選擇",
            }
            for container in color_containers:
                for el in container.find_all(["button", "label", "span"]):
                    text = (
                        el.get("title") or
                        el.get("data-attr-value") or
                        el.get_text(strip=True)
                    ).strip()
                    if (text and len(text) < 30
                            and text not in seen_colors
                            and text.lower() not in color_exclude):
                        seen_colors.add(text)
                        colors.append(text)

            # === 商品說明 ===
            for desc_sel in [
                {"id": "collapsible-description-1"},
                {"class": re.compile(r"product-description|pdp-description|value.*content", re.I)},
            ]:
                el = soup.find(**{"attrs": desc_sel} if "class" in desc_sel or "id" in desc_sel else desc_sel)
                if el:
                    text = el.get_text(separator="\n", strip=True)
                    if len(text) > 20:
                        product.description = text
                        break

            # === variants 組合 ===
            if sizes or colors:
                variants = []
                if sizes and colors:
                    for color in colors:
                        for size in sizes:
                            variants.append({
                                "color": color,
                                "size": size,
                                "sku": f"hm-{color}-{size}".lower().replace(" ", "-"),
                                "price": product.price_jpy or 0,
                                "in_stock": True,
                                "image": product.image_url,
                            })
                elif sizes:
                    for size in sizes:
                        variants.append({
                            "color": "",
                            "size": size,
                            "sku": f"hm-{size}".lower(),
                            "price": product.price_jpy or 0,
                            "in_stock": True,
                            "image": product.image_url,
                        })
                elif colors:
                    for color in colors:
                        variants.append({
                            "color": color,
                            "size": "",
                            "sku": f"hm-{color}".lower().replace(" ", "-"),
                            "price": product.price_jpy or 0,
                            "in_stock": True,
                            "image": product.image_url,
                        })
                product.variants = variants

            print(
                f"[HumanMade] ✅ {product.title} / ¥{product.price_jpy} / "
                f"sizes={sizes} / colors={colors} / images={len(imgs)}"
            )

        except Exception as e:
            print(f"[HumanMade] ❌ 解析失敗 {url}: {type(e).__name__}: {e}")

        return product

    async def _humanmade_fetch_html(self, url: str) -> str | None:
        """使用 SeleniumBase UC driver 取得 JS 渲染後的 HTML"""
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
                        if "InvalidSession" in type(e).__name__ or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    # 等待頁面渲染，關閉彈窗
                    html = ""
                    session_dead = False
                    for i in range(8):
                        _time.sleep(2)
                        try:
                            # 嘗試關閉 Global-e 彈窗
                            driver.execute_script("""
                                const ge = document.getElementById('globalePopupWrapper');
                                if (ge) ge.remove();
                                document.querySelectorAll('[class*="globale"], [id*="globale"]').forEach(el => {
                                    try {
                                        if (getComputedStyle(el).position === 'fixed') el.remove();
                                    } catch(e) {}
                                });
                            """)
                        except Exception:
                            pass

                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # 確認頁面已載入（有商品標題區塊 or 有 ¥ 金額）
                        if i >= 1 and len(html) > 5000 and ('¥' in html or 'product' in html.lower()):
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    if html and len(html) > 5000:
                        return html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[HumanMade] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
