"""
NPB オフィシャルオンラインショップ 爬蟲 Mixin
shop.npb.or.jp
JS 動態レンダリング → SeleniumBase UC mode
"""
import re
import time
from urllib.parse import urlparse, parse_qs

from scrapers.base import ProductInfo


class NpbMixin:

    async def _scrape_npb(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            ggcd = parse_qs(urlparse(url).query).get("ggcd", [None])[0]

            html = await self._npb_selenium(url)
            if not html or len(html) < 3000:
                print(f"[NPB] ❌ HTML 取得失敗 (len={len(html) if html else 0})")
                return product

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            page_text = soup.get_text(" ", strip=True)

            # ── タイトル: <h3><span id="goodsGroupNamePC">商品名</span></h3>
            name_span = soup.find("span", id="goodsGroupNamePC")
            if name_span:
                product.title = name_span.get_text(strip=True)
            if not product.title:
                for h3 in soup.find_all("h3"):
                    txt = h3.get_text(strip=True)
                    if txt and "NPBオフィシャル" not in txt and len(txt) > 3:
                        product.title = txt
                        break

            # ── 価格: <span id="goodsDisplayPrice">¥12,000</span>
            price_span = soup.find("span", id="goodsDisplayPrice")
            if price_span:
                pm = re.search(r'[0-9]+[0-9,]*', price_span.get_text())
                if pm:
                    v = pm.group().replace(",", "")
                    if v.isdigit(): product.price_jpy = int(v)
            if not product.price_jpy:
                pm2 = re.search(r'[¥￥]\s*([\d,]+)', page_text)
                if pm2:
                    product.price_jpy = int(pm2.group(1).replace(",", ""))

            # ── 画像
            # 構造: #thmbs > .thmbsbox > a[href="/npbshop/g_images/{ggcd}/pc{N}_l.jpg"]
            BASE = "https://shop.npb.or.jp"
            thumb_box = soup.find(id="thmbs")
            if thumb_box:
                hrefs = []
                for a in thumb_box.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = BASE + href
                    hrefs.append(href)
                if hrefs:
                    product.image_url = hrefs[0]
                    product.extra_images = hrefs[1:9]
                    print(f"[NPB] 画像 {len(hrefs)} 枚取得")
            # fallback: ggcd から pc1_l を組み立て
            if not product.image_url and ggcd:
                product.image_url = f"{BASE}/npbshop/g_images/{ggcd}/pc1_l.jpg"

            # ── 在庫テーブルから選手×サイズ×在庫を直接取得
            # unitSelect2（サイズ）は選手選択後に動的ロードされるため select は使わない
            # 在庫テーブル構造: 選手 | サイズ | 価格 | 在庫状況
            stock_table = None
            for tbl in soup.find_all("table"):
                if "在庫状況" in tbl.get_text():
                    stock_table = tbl
                    break

            if stock_table:
                rows = stock_table.find_all("tr")
                cols = [c.get_text(strip=True) for c in rows[0].find_all(["th","td"])] if rows else []
                print(f"[NPB] 在庫テーブル cols={cols}")

                def _idx(name, default):
                    try: return cols.index(name)
                    except ValueError: return default

                ip  = _idx("選手", 0)
                iz  = _idx("サイズ", 1)
                ipr = _idx("価格", 2)
                ist = _idx("在庫状況", 3)

                for row in rows[1:]:
                    cells = row.find_all(["td","th"])
                    if len(cells) < 2:
                        continue

                    def _cell(i):
                        return cells[i].get_text(strip=True) if i < len(cells) else ""

                    player   = _cell(ip)
                    size     = _cell(iz)
                    stock_st = _cell(ist)
                    in_stock = stock_st in ("○", "◯", "△", "残りわずか", "予約")

                    v_price = product.price_jpy or 0
                    pm3 = re.search(r'[0-9]+[0-9,]*', _cell(ipr))
                    if pm3:
                        v3 = pm3.group().replace(',', '')
                        if v3.isdigit(): v_price = int(v3)

                    if not player and not size:
                        continue

                    product.variants.append({
                        "color": player,
                        "size": size,
                        "sku": f"npb-{player}-{size}".replace(" ", "_"),
                        "price": v_price,
                        "in_stock": in_stock,
                        "image": "",
                    })

                print(f"[NPB] ✅ variants={len(product.variants)}")

            else:
                # 在庫テーブルなし → unitSelect1（選手）だけ取得
                sel1 = soup.find("select", id="unitSelect1")
                if sel1:
                    for opt in sel1.find_all("option"):
                        val = opt.get("value", "")
                        name = opt.get_text(strip=True)
                        if val and name != "選択してください":
                            product.variants.append({
                                "color": name,
                                "size": "",
                                "sku": f"npb-{name}".replace(" ", "_"),
                                "price": product.price_jpy or 0,
                                "in_stock": True,
                                "image": "",
                            })
                    print(f"[NPB] ✅ variants(選手のみ)={len(product.variants)}")
                else:
                    print(f"[NPB] ⚠️ 在庫テーブル・select ともになし → 単品")
                    product.in_stock = True

            product.brand = "NPB"
            print(f"[NPB] title={product.title!r} price={product.price_jpy}")

        except Exception as e:
            import traceback
            print(f"[NPB] ❌ {type(e).__name__}: {e}")
            traceback.print_exc()

        return product

    async def _npb_selenium(self, url: str) -> str | None:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._npb_selenium_sync, url)

    def _npb_selenium_sync(self, url: str) -> str | None:
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1
                    self._clean_driver_tabs()

                    try:
                        driver.uc_open_with_reconnect(url, reconnect_time=5)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__:
                            self._driver = None
                            self._create_driver()
                            continue

                    # JS レンダリング待ち
                    # unitSelect2（サイズ）に option が入るまで待つ
                    for i in range(10):
                        time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                break
                            continue

                        # 在庫テーブルが出るまで待つ
                        has_title  = "goodsGroupNamePC" in html
                        has_price  = "goodsDisplayPrice" in html
                        has_stock_table = "在庫状況" in html and "<table" in html

                        if i >= 1 and has_title and has_price and has_stock_table:
                            print(f"[NPB] Selenium 取得成功 (試行 {i+1}, len={len(html)})")
                            return html
                        print(f"[NPB] 待機中 {i+1}/10: title={has_title} price={has_price} stock_table={has_stock_table}")

                    # タイムアウトしても返す
                    html = driver.page_source
                    if html and len(html) > 5000:
                        return html
                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    return None
        return None
