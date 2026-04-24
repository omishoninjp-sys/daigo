"""
MakeShop 通用爬蟲 Mixin
httpx が 403 でブロックされるため SeleniumBase UC driver を使用。

検出：URL パス /view/item/ が含まれる → "makeshop" (base.py で判定)
"""
import re
import json
import time as _time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo, normalize_price


class MakeShopMixin:

    async def _scrape_makeshop(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        html = await self._makeshop_fetch_html(url)
        if not html:
            print(f"[MakeShop] ❌ HTML 取得失敗: {url}")
            return product

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── タイトル
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
            if not product.title:
                og = soup.find("meta", property="og:title")
                if og:
                    product.title = og.get("content", "").strip()
            if not product.title:
                title_tag = soup.find("title")
                if title_tag:
                    raw = title_tag.get_text(strip=True)
                    product.title = re.split(r'[｜|]', raw)[0].strip()

            # ── ブランド（breadcrumb 2番目）
            for sel in ["ol li a", "ul.breadcrumb li a", "#breadcrumb li a", ".breadcrumb a"]:
                crumbs = soup.select(sel)
                if len(crumbs) >= 2:
                    product.brand = crumbs[1].get_text(strip=True)
                    break

            # ── 価格
            page_text = soup.get_text(" ", strip=True)

            # 1. JSON-LD
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    d = json.loads(script.string or "")
                    if isinstance(d, list):
                        d = next((x for x in d if x.get("@type") == "Product"), {})
                    if d.get("@type") == "Product":
                        offers = d.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        p = offers.get("price")
                        if p:
                            product.price_jpy = normalize_price(p)
                except Exception:
                    pass

            # 2. ￥X,XXX (税込)
            if not product.price_jpy:
                m = re.search(r'[￥¥]\s*([\d,]+)\s*[\(（]?\s*税込', page_text)
                if m:
                    p = normalize_price(m.group(1))
                    if p and 100 <= p <= 2_000_000:
                        product.price_jpy = p

            # 3. X円（税込）
            if not product.price_jpy:
                m = re.search(r'([\d,]+)\s*円\s*[（\(]\s*税込', page_text)
                if m:
                    p = normalize_price(m.group(1))
                    if p and 100 <= p <= 2_000_000:
                        product.price_jpy = p

            # 4. class*=price
            if not product.price_jpy:
                for el in soup.select("[class*='price'], [id*='price']"):
                    m = re.search(r'[￥¥]([\d,]+)', el.get_text(strip=True))
                    if m:
                        p = normalize_price(m.group(1))
                        if p and 100 <= p <= 2_000_000:
                            product.price_jpy = p
                            break

            # ── 画像
            seen: set[str] = set()
            imgs: list[str] = []
            for img in soup.find_all("img"):
                src = img.get("src", "") or img.get("data-src", "")
                if "makeshop-multi-images.akamaized.net" not in src:
                    continue
                if src not in seen:
                    seen.add(src)
                    imgs.append(src)

            # shopimages 優先（高解像度）
            sorted_imgs = [s for s in imgs if "/shopimages/" in s] + \
                          [s for s in imgs if "/itemimages/" in s]

            if not sorted_imgs:
                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get("content"):
                    sorted_imgs.append(og_img["content"])

            if sorted_imgs:
                product.image_url = sorted_imgs[0]
                product.extra_images = sorted_imgs[1:10]

            # ── 在庫
            # 先找主商品区域（h1 の近く）で判定。
            # 関連商品セクションに SOLD OUT が含まれる場合があるため、
            # 有庫存サインを優先チェックする。
            main_area = ""
            main_el = soup.find("h1")
            if main_el:
                # h1 の親を最大 5 段階まで遡って商品エリアを特定
                parent = main_el
                for _ in range(5):
                    if parent.parent:
                        parent = parent.parent
                    if len(parent.get_text()) > 200:
                        main_area = parent.get_text(" ", strip=True)
                        break

            check_text = main_area if main_area else page_text

            if re.search(r'在庫あり|カートに入れる|〇在庫', check_text):
                product.in_stock = True
            elif re.search(r'SOLD\s*OUT|在庫なし|売り切れ|品切れ|×在庫', check_text):
                product.in_stock = False
            # どちらも見つからなければデフォルト True のまま

            # ── Variants
            select = soup.find("select", id=re.compile(r"skuinfo|sku|unit", re.I))
            if not select:
                select = soup.find("select", attrs={"name": re.compile(r"sku|unit|size|color", re.I)})
            if select:
                for opt in select.find_all("option"):
                    val = opt.get("value", "").strip()
                    txt = opt.get_text(strip=True)
                    if not val or val in ("0", ""):
                        continue
                    in_stock = not re.search(r'売切|在庫なし|SOLD\s*OUT', txt, re.I)
                    size = re.sub(r'\s*(売切|在庫なし|SOLD\s*OUT)\s*', '', txt, flags=re.I).strip()
                    product.variants.append({
                        "color": "",
                        "size": size,
                        "sku": val,
                        "price": product.price_jpy or 0,
                        "in_stock": in_stock,
                        "image": product.image_url,
                    })

            # ── 説明文
            for desc_sel in [".itemDetail", ".item-detail", ".product-detail",
                              "#item-detail", ".detail-text", ".goods-detail"]:
                desc_el = soup.select_one(desc_sel)
                if desc_el:
                    product.description = desc_el.get_text(" ", strip=True)[:500]
                    break
            if not product.description:
                og_desc = soup.find("meta", property="og:description")
                if og_desc:
                    product.description = og_desc.get("content", "")[:500]

            print(
                f"[MakeShop] ✅ {product.title[:40]} / ¥{product.price_jpy} / "
                f"in_stock={product.in_stock} / images={len(sorted_imgs)} / "
                f"variants={len(product.variants)}"
            )

        except Exception as e:
            import traceback
            print(f"[MakeShop] ❌ {type(e).__name__}: {e}")
            print(traceback.format_exc())

        return product

    async def _makeshop_fetch_html(self, url: str) -> str | None:
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

                        if i >= 1 and "makeshop" in html.lower() and len(html) > 5000:
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
                    print(f"[MakeShop] fetch 失敗: {e}")
                    return None

        return None
