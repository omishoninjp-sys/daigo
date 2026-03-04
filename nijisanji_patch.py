"""
scraper.py 需要加入的兩段程式碼

=== 修改 1: detect_platform 函數，在 "if "rakuten.co.jp" in host:" 那行前面加入 ===

    if "shop.nijisanji.jp" in host or "nijisanji.jp" in host:
        return "nijisanji"

=== 修改 2: 在 scrape() 方法的 if/elif 鏈裡加入（在 elif platform == "generic": 前） ===

        elif platform == "nijisanji":
            product = await self._scrape_nijisanji(url)

=== 修改 3: 在 Scraper class 裡加入這個新方法（放在 _scrape_beams 前面） ===
"""

# 貼到 scraper.py 的 Scraper class 裡
async def _scrape_nijisanji(self, url: str) -> ProductInfo:
    """
    にじさんじオフィシャルストア（Salesforce Commerce Cloud / Demandware）
    - SSR 頁面，httpx 即可，不需要 Chrome
    - variant 在 .c-modal__content 的 li 裡
    """
    product = ProductInfo(source_url=url, brand="にじさんじ")

    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
        }

        async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"[Nijisanji] HTTP {resp.status_code}")
                return product
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")

        # === 標題 ===
        h1 = soup.find("h1")
        if h1:
            product.title = h1.get_text(strip=True)
        if not product.title:
            og = soup.find("meta", property="og:title")
            if og:
                product.title = og.get("content", "").replace("｜にじさんじオフィシャルストア", "").strip()

        # === 圖片（URL 前綴補上） ===
        base = "https://shop.nijisanji.jp"
        imgs = []
        seen_imgs = set()
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if "nijisanji-master-catalog" in src and "physical" in src:
                if not src.startswith("http"):
                    src = base + src
                if src not in seen_imgs:
                    seen_imgs.add(src)
                    imgs.append(src)

        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:9]

        # === Variants（商品選択モーダル内のリスト） ===
        # HTML 結構: <ul class="c-product-list__items"> → <li> → 商品名 + 価格
        # 或者是 modal 裡的 li 直接含文字
        variants = []
        min_price = None

        # 方法 1: 找包含 ¥ 的 li 清單
        for li in soup.find_all("li"):
            text = li.get_text(" ", strip=True)
            # 找包含價格的 li（¥X,XXX 稅込）
            price_m = re.search(r'[¥￥]([\d,]+)\s*税込', text)
            if not price_m:
                price_m = re.search(r'([\d,]+)\s*税込', text)
            if not price_m:
                continue

            price = int(price_m.group(1).replace(",", ""))
            if price < 100 or price > 500000:
                continue

            # 商品名：去掉價格和多餘空白
            name = text
            name = re.sub(r'[¥￥][\d,]+\s*税込', '', name).strip()
            name = re.sub(r'[\d,]+\s*税込', '', name).strip()
            name = re.sub(r'\+\s*まもなく(終了|販売)', '', name).strip()
            name = re.sub(r'まもなく(終了|販売)', '', name).strip()
            name = re.sub(r'\s+', ' ', name).strip()

            # 去掉太短或只是數字的
            if len(name) < 3:
                continue
            # 去掉導覽列文字
            if any(skip in name for skip in ["カート", "ログイン", "お気に入り", "ページ", "TOP", "閉じる"]):
                continue

            if min_price is None or price < min_price:
                min_price = price

            variants.append({
                "color": "",
                "size": name,   # にじさんじは「サイズ」がなく商品種別で分かれる → size欄に入れる
                "sku": "",
                "price": price,
                "in_stock": "在庫なし" not in text and "売り切れ" not in text,
                "image": product.image_url,
            })

        # 重複排除
        seen_v = set()
        unique_variants = []
        for v in variants:
            key = f"{v['size']}|{v['price']}"
            if key not in seen_v:
                seen_v.add(key)
                unique_variants.append(v)
        product.variants = unique_variants

        # === 価格（variantの最安値、または直接ページから） ===
        if min_price:
            product.price_jpy = min_price
        else:
            # fallback: ページ内の最初の価格
            for pat in [r'[¥￥]([\d,]+)\s*税込', r'[¥￥]([\d,]+)']:
                pm = re.search(pat, html)
                if pm:
                    p = int(pm.group(1).replace(",", ""))
                    if 100 < p < 500000:
                        product.price_jpy = p
                        break

        # === 説明 ===
        desc_section = soup.find(lambda tag: tag.name and tag.get_text(strip=True) == "商品説明")
        if desc_section:
            next_el = desc_section.find_next_sibling()
            if next_el:
                product.description = next_el.get_text(" ", strip=True)[:500]

        print(f"[Nijisanji] ✅ {product.title[:40]} / ¥{product.price_jpy} / {len(product.variants)} variants")

    except Exception as e:
        print(f"[Nijisanji] ❌ 錯誤: {type(e).__name__}: {e}")

    return product
