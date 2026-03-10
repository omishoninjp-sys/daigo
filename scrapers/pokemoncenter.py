"""
Pokémon Center Online (pokemoncenter-online.com) 爬蟲 Mixin
使用 SeleniumBase UC driver（網站有 bot 防護）

頁面結構：
  圖片：div.slideBox > div.photoList .slick-slide:not(.slick-cloned) img
  價格：p.price span.txt → "2,750"（含逗號，無 ¥）
  variant：div.variation-buttons-container（部分商品為空）
  商品無尺寸時直接單一 variant
"""
import re
import time as _time

from scrapers.base import ProductInfo


class PokemonCenterMixin:

    async def _scrape_pokemoncenter(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="Pokémon Center")

        html = await self._pokemoncenter_fetch_html(url)
        if not html:
            print(f"[PokemonCenter] ❌ 無法取得 HTML: {url}")
            return product

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # === 標題 ===
            for sel in [
                ("h1", {"class": re.compile(r"item.?name|product.?name|title", re.I)}),
                ("h1", {}),
                ("p",  {"class": re.compile(r"item.?name|product.?name", re.I)}),
            ]:
                el = soup.find(sel[0], sel[1])
                if el and el.get_text(strip=True):
                    product.title = el.get_text(strip=True)
                    break

            # === 價格：p.price span.txt → "2,750" ===
            price_el = soup.find("p", class_=re.compile(r"\bprice\b"))
            if price_el:
                txt = price_el.find("span", class_="txt")
                if txt:
                    m = re.search(r'[\d,]+', txt.get_text())
                    if m:
                        product.price_jpy = int(m.group(0).replace(',', ''))

            # Fallback 價格
            if not product.price_jpy:
                for text in soup.stripped_strings:
                    m = re.match(r'^([1-9][\d,]+)\s*円', text.strip())
                    if m:
                        val = int(m.group(1).replace(',', ''))
                        if 100 <= val <= 100000:
                            product.price_jpy = val
                            break

            # === 圖片：slick slide（排除 slick-cloned 避免重複）===
            imgs = []
            seen = set()
            slide_container = soup.find("div", class_=re.compile(r"photoList|slideBox"))
            if slide_container:
                for slide in slide_container.find_all("div", class_="slick-slide"):
                    # 跳過 clone
                    if "slick-cloned" in (slide.get("class") or []):
                        continue
                    for img in slide.find_all("img"):
                        src = img.get("src", "")
                        if (src and
                                "pokemoncenter-online.com" in src and
                                "/img/item/" in src and
                                src not in seen):
                            seen.add(src)
                            imgs.append(src)

            # Fallback：找所有 pokemoncenter-online.com/a/img/item/ 圖片
            if not imgs:
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if ("/a/img/item/" in src and
                            "pokemoncenter-online.com" in src and
                            src not in seen):
                        seen.add(src)
                        imgs.append(src)

            if imgs:
                product.image_url = imgs[0]
                product.extra_images = imgs[1:8]

            # === 庫存：<input id="availability" value="true/false"> ===
            avail_el = soup.find("input", id="availability")
            in_stock = True  # default
            if avail_el:
                in_stock = avail_el.get("value", "true").strip().lower() == "true"
            print(f"[PokemonCenter] 庫存狀態: {'有庫存' if in_stock else '❌ 無庫存'}")

            # === Variants ===
            # variation-buttons-container 有內容時解析選項
            var_container = soup.find("div", class_="variation-buttons-container")
            parsed_variants = []

            if var_container and var_container.get_text(strip=True):
                # 找 radio / button 選項
                for btn in var_container.find_all(["button", "input", "label"]):
                    val = (btn.get("value") or btn.get_text(strip=True) or "").strip()
                    if val and len(val) < 30:
                        parsed_variants.append(val)

            if parsed_variants:
                for v in parsed_variants:
                    product.variants.append({
                        "color": "",
                        "size":  v,
                        "sku":   f"poke-{v}".lower().replace(" ", "-"),
                        "price": product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image": product.image_url,
                    })
            # variant なし → 単一商品（variants 空のまま、in_stock を product に記録）
            if not parsed_variants:
                product.in_stock = in_stock

            print(
                f"[PokemonCenter] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / images={len(imgs)}"
            )

        except Exception as e:
            import traceback
            print(f"[PokemonCenter] ❌ 解析失敗: {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    async def _pokemoncenter_fetch_html(self, url: str) -> str | None:
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

                    html = ""
                    session_dead = False
                    for i in range(8):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        # 価格と画像が両方揃ってから返す
                        if i >= 1 and "price" in html and "/a/img/item/" in html and len(html) > 5000:
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
                    print(f"[PokemonCenter] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
