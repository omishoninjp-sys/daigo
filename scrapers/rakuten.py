"""
楽天市場 (item.rakuten.co.jp) 爬蟲 Mixin
- httpx 直接抓基本資料（標題、價格、圖、描述），手動處理 EUC-JP 編碼
- 樂天新版 RMS 模板的 SKU 選項是 React 動態渲染，httpx 連標識字串都拿不到
  → variants=0 時無條件用 driver fallback
v1.5: 移除 _rakuten_likely_has_sku 判斷，無條件 driver fallback（前一版判斷不準）
v1.4: SKU 選項抓不到時自動降級用 driver（解決 JS render 問題）
v1.3: 支援新版 RMS 模板與傳統 select 模板
v1.2: SKU 選項抓取（色分類、サイズ等）並展開為 variant 格式給 shopify_client 使用
"""
import asyncio
import re
import json
import time
from itertools import product as _cartesian

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


_SKU_MIN_PRICE = 100
_SKU_MAX_PRICE = 10_000_000

# _build_variants 判斷顏色軸用的關鍵字（_parse_sku_json 沿用同一組，行為一致）
_COLOR_KEYWORDS = ["色分類", "カラー", "color", "色"]


def _grab_json_array(html: str, key: str):
    """
    從 HTML 找出 key 之後的第一個 JSON 陣列，用括號配對取完整片段再 json.loads。

    樂天商品頁把整包 itemInfoSku 塞在 <script> 裡，沒有獨立的 script id 可抓，
    只能從 key 往後做括號配對。字串內的中括號要略過（商品說明裡有大量 HTML），
    否則會在字串中間斷開。取不到回 None。
    """
    j = html.find(key)
    if j < 0:
        return None
    try:
        start = html.index("[", j)
    except ValueError:
        return None

    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(html)):
        ch = html[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:k + 1])
                except Exception:
                    return None
    return None


def _parse_sku_json(html: str):
    """
    解析樂天商品頁內嵌的 itemInfoSku SKU 資料（httpx 的原始 HTML 就有，不必 driver）。

    三塊資料：
      "variantSelectors"        軸定義 [{label:"サイズ", values:[…]}, …]，順序對應 selectorValues
      "sku"                     真實存在的 SKU：selectorValues / taxIncludedPrice /
                                images[].location / merchantDefinedSkuId / hidden
      "variantMappedInventories" 逐 SKU 庫存 [{sku:"r-sku…", quantity:46}, …]

    比 _parse_sku_options（笛卡爾積）好的地方：
      · 逐變體真實價格（笛卡爾積版本一律 price=0＝全部套主商品價）
      · 逐變體庫存（笛卡爾積版本一律 in_stock=True）
      · 逐變體圖片
      · 只有實際存在的組合（笛卡爾積會生出賣家根本沒有的色×尺寸）
      · 軸名直接讀 label，不必用關鍵字猜哪一軸是顏色

    回傳 (variants, found, any_in_stock)：
      found=False        → 這頁沒有這塊 JSON（單品或舊模板）→ 呼叫端退回笛卡爾積解析
      any_in_stock=False → 有 SKU 但全部缺貨 → 呼叫端把整件標為缺貨
    variants 預設只含有庫存的 SKU；但「全部缺貨」時會保留完整清單（見下方註解），
    不會因為庫存判斷把商品變成零變體。濾完剩 <=1 個時回 []（視為單品，不建選單）。
    """
    skus = _grab_json_array(html, '"sku":[')
    if not isinstance(skus, list) or not skus:
        return [], False, False

    selectors = _grab_json_array(html, '"variantSelectors":[') or []
    labels = [str((s or {}).get("label") or "").strip()
              for s in selectors if isinstance(s, dict)]

    inventories = _grab_json_array(html, '"variantMappedInventories":[') or []
    qty_by_sku = {}
    for entry in inventories:
        if isinstance(entry, dict) and entry.get("sku") is not None:
            qty_by_sku[str(entry["sku"])] = entry.get("quantity")

    # 哪一軸是顏色（沿用 _build_variants 的關鍵字判斷）；其餘軸用「 / 」併成 size
    color_idx = -1
    for i, label in enumerate(labels):
        if any(kw.lower() in label.lower() for kw in _COLOR_KEYWORDS):
            color_idx = i
            break

    built = []          # [(variant_dict, in_stock)]
    hidden = 0
    for s in skus:
        if not isinstance(s, dict):
            continue
        if s.get("hidden"):
            hidden += 1
            continue

        variant_id = str(s.get("variantId") or "")
        quantity = qty_by_sku.get(variant_id)
        # quantity 只代表「手上有幾個」，不等於不能買：
        # 有 backOrderDeliveryDateId 的店家是取り寄せ／受注生產，quantity 長期為 0 仍可下單
        # （實測 kamiya/20000956 全 12 個 SKU quantity=0，但頁面寫「14営業日以内発送」）。
        # 沒有庫存資料的店家一律當有貨（維持舊行為，寧可讓客人下單再確認）。
        has_backorder = s.get("backOrderDeliveryDateId") is not None
        in_stock = True if quantity is None else ((quantity or 0) > 0 or has_backorder)

        values = [str(v).strip() for v in (s.get("selectorValues") or []) if str(v).strip()]
        if 0 <= color_idx < len(values):
            color = values[color_idx]
            rest = [v for i, v in enumerate(values) if i != color_idx]
        else:
            color = ""
            rest = list(values)
        size = " / ".join(rest)

        price = 0
        raw_price = s.get("taxIncludedPrice")
        if raw_price is not None:
            try:
                price = int(float(raw_price))
            except (TypeError, ValueError):
                price = 0
            if not (_SKU_MIN_PRICE <= price <= _SKU_MAX_PRICE):
                price = 0

        image = ""
        images = s.get("images") or []
        if images and isinstance(images[0], dict):
            image = str(images[0].get("location") or "").strip()

        sku_code = str(s.get("merchantDefinedSkuId") or "").strip() or variant_id
        built.append(({
            "color": color,
            "size": size,
            "sku": sku_code[:80],
            "price": price,      # 0 → shopify_client 用主商品價
            "in_stock": True,
            "image": image,
        }, in_stock))

    if hidden:
        print(f"[Rakuten] SKU JSON：隱藏商品已排除 {hidden} 個")

    in_stock_only = [v for v, ok in built if ok]
    any_in_stock = bool(in_stock_only)

    if any_in_stock:
        variants = in_stock_only
        skipped = len(built) - len(in_stock_only)
        if skipped:
            print(f"[Rakuten] SKU JSON：缺貨變體已排除 {skipped} 個（只保留有庫存）")
    else:
        # 全部判定缺貨 → 不做排除，保留完整選項清單，改由呼叫端把整件標成缺貨。
        # （庫存訊號沒到可以據以刪光選項的可信度，寧可讓客人看得到有哪些款式）
        variants = [v for v, _ in built]

    # 只有「完全沒有可選項目」才回空交給 shopify_client 當單品。
    # 只剩一個有貨的 SKU 時仍然回傳它 —— 24 選 1 的商品若退成無選項單品，
    # 客人看不出自己買到的是哪一款（實測 sidecar/0188583-1 就是這種情況）。
    if not variants or not any(v["color"] or v["size"] for v in variants):
        return [], True, any_in_stock

    return variants, True, any_in_stock


def _parse_sku_options(soup: BeautifulSoup) -> list:
    """
    解析 Rakuten 商品頁的 SKU 選項（色、サイズ等）。

    支援兩種主流模板：
    A. 新版 RMS（2022+）: button.type-sku-button-- 渲染選項，div.display-sku-area 包住
    B. 傳統 select: <select name="inventory_no/item_color/...">

    回傳格式（shopify_client 標準格式 variant list）：
    [
        {"color": "A：白 丸首", "size": "66cm", "sku": "...", "price": 0, "in_stock": True, "image": ""},
        ...
    ]
    空清單代表此商品無 SKU 選項或抓不到 → shopify_client 會降級為單品。

    處理邏輯：
    1. 先試新版 (display-sku-area button)
    2. 找不到時 fallback 到傳統 (<select>)
    3. 抽出所有「選項組」（label + 該組所有值）
    4. 判斷哪一組是 color 維度，其他組視為 size 維度
    5. 多組 size 維度合併（用「/」連接）做笛卡爾積
    6. 產出 color × size 笛卡爾積，全部用主商品價格
    """
    raw_options = _parse_sku_new_rms(soup)
    if not raw_options:
        raw_options = _parse_sku_legacy_select(soup)
    if not raw_options:
        return []

    return _build_variants(raw_options)


def _parse_sku_new_rms(soup: BeautifulSoup) -> list:
    """新版 RMS 模板：button.type-sku-button-- 群組"""
    raw_options: list[tuple[str, list[str]]] = []

    display_area = soup.select_one(".display-sku-area")
    if not display_area:
        return raw_options

    for grp in display_area.select('[class*="padding-bottom-small"]'):
        # 取選項組標籤名稱
        label = ""
        for div in grp.select('[class*="text-display"]'):
            t = div.get_text(strip=True)
            if t and "未選択" not in t and "選択してください" not in t and len(t) <= 20:
                label = t.rstrip("::").strip()
                break

        # 取所有選項按鈕文字
        btn_texts = [
            b.get_text(strip=True)
            for b in grp.select('[class*="type-sku-button"]')
            if b.get_text(strip=True)
        ]

        if btn_texts:
            raw_options.append((label or f"選項{len(raw_options) + 1}", btn_texts))

    return raw_options


def _parse_sku_legacy_select(soup: BeautifulSoup) -> list:
    """傳統 <select> 模板"""
    raw_options: list[tuple[str, list[str]]] = []

    # 樂天傳統 SKU select 的 name 模式
    name_patterns = [
        re.compile(r'inventory_no', re.I),
        re.compile(r'item[_\-]?color', re.I),
        re.compile(r'item[_\-]?size', re.I),
        re.compile(r'sub_pcid', re.I),
        re.compile(r'orderno', re.I),
    ]

    for sel in soup.find_all("select"):
        sel_name = sel.get("name", "")
        if not any(p.search(sel_name) for p in name_patterns):
            continue

        # 取 label：先看前面有沒有 <th>/<dt>/<label>，否則用 select name 推斷
        label = ""
        # 看 parent table row 內的 <th>
        parent_tr = sel.find_parent("tr")
        if parent_tr:
            th = parent_tr.find("th")
            if th:
                label = th.get_text(strip=True).rstrip("::").strip()[:20]
        # 看附近的 label
        if not label:
            for prev in sel.find_all_previous(["label", "dt", "th"], limit=3):
                t = prev.get_text(strip=True)
                if t and len(t) <= 20:
                    label = t.rstrip("::").strip()
                    break
        # name 推斷
        if not label:
            if "color" in sel_name.lower():
                label = "カラー"
            elif "size" in sel_name.lower():
                label = "サイズ"
            else:
                label = "選項"

        # 取所有 <option> 文字（排除「選択してください」之類 placeholder）
        option_values = []
        for opt in sel.find_all("option"):
            v = opt.get("value", "").strip()
            text = opt.get_text(strip=True)
            # 排除 placeholder
            if not v or v == "0" or "選択してください" in text or "選択する" in text or text == "":
                continue
            # 用顯示文字（含色名/尺寸）
            if text:
                option_values.append(text)

        if option_values:
            raw_options.append((label, option_values))

    return raw_options


def _build_variants(raw_options: list) -> list:
    """把 raw_options 拆 color/size 維度後做笛卡爾積，回傳 shopify variant 格式"""
    # ── 判斷哪組是 color 維度 ──
    color_keywords = ["色分類", "カラー", "color", "色"]
    color_idx = -1
    for i, (label, _) in enumerate(raw_options):
        if any(kw.lower() in label.lower() for kw in color_keywords):
            color_idx = i
            break

    # ── 拆 color / size 維度 ──
    if color_idx >= 0:
        color_values = raw_options[color_idx][1]
        size_groups = [opt for i, opt in enumerate(raw_options) if i != color_idx]
    else:
        color_values = [""]
        size_groups = list(raw_options)

    # ── 多組 size 維度做笛卡爾積 ──
    if size_groups:
        size_value_lists = [opt[1] for opt in size_groups]
        size_combinations = [" / ".join(combo) for combo in _cartesian(*size_value_lists)]
    else:
        size_combinations = [""]

    # ── 組成標準 variants ──
    variants = []
    for c in color_values:
        for s in size_combinations:
            sku_parts = [p for p in [c, s] if p]
            sku_raw = "-".join(sku_parts).lower()
            sku = re.sub(r'[^\w\-]+', '-', sku_raw)[:80]
            variants.append({
                "color": c,
                "size": s,
                "sku": sku or "default",
                "price": 0,
                "in_stock": True,
                "image": "",
            })

    return variants


class RakutenMixin:

    async def _scrape_rakuten(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }

        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(url)

                # 樂天頁面是 EUC-JP，手動解碼
                try:
                    html = resp.content.decode("euc-jp", errors="replace")
                except Exception:
                    html = resp.text

                # ── 商品名稱（og:title）──
                title_m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not title_m:
                    title_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html)
                if title_m:
                    title = title_m.group(1).strip()
                    # 去掉「| 店名」或「：店名」suffix（全形/半形冒號都處理）
                    title = re.split(r'[|｜::]\s*\S+(?:楽天|店|ショップ)', title)[0].strip()
                    title = re.sub(r'\s*[-－]\s*楽天市場.*$', '', title).strip()
                    product.title = title

                # ── 価格（複数 pattern 試行）──
                price_patterns = [
                    r'class="price2"[^>]*>\s*([\d,]+)\s*円',
                    r'class="price2"[^>]*>.*?([\d,]+)(?:\s*円|\s*<)',
                    r'itemprop="price"[^>]+content="(\d+)"',
                    r'"price":\s*"?(\d[\d,]+)"?',
                    r'¥\s*([\d,]+)',
                ]
                for pat in price_patterns:
                    m = re.search(pat, html)
                    if m:
                        try:
                            price = int(m.group(1).replace(",", ""))
                            if 100 <= price <= 10_000_000:
                                product.price_jpy = price
                                print(f"[Rakuten DEBUG] 價格 pattern={pat!r} → ¥{price}")
                                break
                        except Exception:
                            continue

                # ── JSON-LD fallback ──
                ld_matches = re.findall(
                    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    html, re.DOTALL
                )
                if not product.price_jpy:
                    for ld_raw in ld_matches:
                        try:
                            ld = json.loads(ld_raw)
                            if isinstance(ld, list):
                                ld = ld[0]
                            offers = ld.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0]
                            price_val = offers.get("price") or offers.get("lowPrice")
                            if price_val:
                                price = int(float(str(price_val)))
                                if 100 <= price <= 10_000_000:
                                    product.price_jpy = price
                                    print(f"[Rakuten DEBUG] JSON-LD 價格 → ¥{price}")
                                    break
                        except Exception:
                            continue

                # ── 主圖（og:image）──
                img_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not img_m:
                    img_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
                if img_m:
                    product.image_url = re.sub(r'\?_ex=\d+x\d+.*$', '', img_m.group(1).strip())

                # ── 描述（og:description）──
                desc_m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not desc_m:
                    desc_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', html)
                if desc_m:
                    product.description = desc_m.group(1).strip()[:500]

                # ── 庫存 ──
                # 樂天：有「カートに入れる」input/button = 有庫存
                # 只有明確找不到購物車 AND 有缺貨文字才判為缺貨
                has_cart = bool(re.search(
                    r'value="[^"]*カートに入れる[^"]*"|買い物かごに入れる|name="cartinsert"',
                    html
                ))
                has_soldout_text = bool(re.search(r'品切れ|売り切れ|SOLD\s*OUT', html))
                if has_cart:
                    product.in_stock = True
                elif has_soldout_text:
                    product.in_stock = False
                else:
                    product.in_stock = True  # 預設有庫存，讓客人下單再確認

                # ── brand（JSON-LD → fallback 店名）──
                for ld_raw in ld_matches:
                    try:
                        ld = json.loads(ld_raw)
                        if isinstance(ld, list):
                            ld = ld[0]
                        brand = ld.get("brand", "")
                        if isinstance(brand, dict):
                            brand = brand.get("name", "")
                        if brand:
                            product.brand = str(brand)
                            break
                    except Exception:
                        continue
                # brand 仍空 → 從 URL 取店家 ID（如 wondergoo）
                if not product.brand:
                    shop_m = re.search(r'item\.rakuten\.co\.jp/([^/]+)/', url)
                    if shop_m:
                        product.brand = shop_m.group(1)

                # ── SKU 選項（色分類、サイズ等）展開為 variant 格式 ──
                # 優先走頁面內嵌的 itemInfoSku JSON：有逐變體價格/庫存/圖，且是真實
                # 存在的組合。抓得到就不必再走 DOM 解析與 driver fallback。
                sku_variants, sku_found, sku_any_stock = _parse_sku_json(html)
                if sku_found:
                    product.variants = sku_variants
                    if not sku_any_stock:
                        product.in_stock = False   # 所有 SKU 都缺貨 → 整件缺貨
                        print("[Rakuten] SKU JSON：所有變體皆缺貨 → 整件標為缺貨")
                    print(f"[Rakuten] SKU JSON 命中 → variants={len(product.variants)}")
                else:
                    soup = BeautifulSoup(html, "html.parser")
                    product.variants = _parse_sku_options(soup)

                # ⚠️ 無條件 fallback：httpx 抓不到 SKU 就用 driver 重抓
                # 樂天新版 RMS 的 SKU 選項是 React 動態渲染，httpx 連 placeholder 都看不到
                # 連標識字串都拿不到，所以無法事先判斷「該不該」用 driver
                # → 簡單的策略：只要 httpx 抓出 0 個 variants，就無條件試 driver 一次
                # 副作用：純單品商品也會多花一次 driver 時間（~10s），但保證有 SKU 的能抓到
                # SKU JSON 命中時不再走 driver：那份資料就是權威來源，
                # 濾完是空的代表「單品」或「整件缺貨」，不是抓不到。
                if not product.variants and not sku_found:
                    print(f"[Rakuten] httpx 抓不到 SKU → 嘗試 driver fallback（驗證 Zeabur IP 是否可拿到 RMS）")
                    try:
                        driver_html = await asyncio.to_thread(self._rakuten_driver_fetch, url)
                        if driver_html:
                            driver_soup = BeautifulSoup(driver_html, "html.parser")
                            product.variants = _parse_sku_options(driver_soup)
                            if product.variants:
                                print(f"[Rakuten] ✓ driver fallback 成功，抓到 {len(product.variants)} 個 variants")
                            else:
                                # driver 也拿不到 → 純單品 OR Zeabur IP 被樂天降級（兩者都可能）
                                # 印出 HTML 標識讓用戶判斷
                                has_sku_marker = any(
                                    m in driver_html
                                    for m in ['display-sku-area', 'type-sku-button', 'inventory_no']
                                )
                                if has_sku_marker:
                                    print(f"[Rakuten] ⚠️ driver 拿到 HTML 含 SKU 標識但仍 parse 不到 → 可能是頁面樣式變了")
                                else:
                                    print(f"[Rakuten] driver 也找不到 SKU 標識 → 純單品商品 OR IP 被降級")
                    except Exception as e:
                        print(f"[Rakuten] driver fallback 失敗: {type(e).__name__}: {e}")

                if product.variants:
                    # 統計
                    colors = set(v["color"] for v in product.variants if v["color"])
                    sizes = set(v["size"] for v in product.variants if v["size"])
                    print(
                        f"[Rakuten] 變體展開: colors={len(colors)} sizes={len(sizes)} "
                        f"→ 共 {len(product.variants)} 個 variants"
                    )

                print(f"[Rakuten] ✅ {product.title[:40]} / ¥{product.price_jpy} / "
                      f"variants={len(product.variants)} / in_stock={product.in_stock}")
                return product

        except Exception as e:
            print(f"[Rakuten] 例外: {type(e).__name__}: {e}，改用通用 Playwright")

        return await self._scrape_with_playwright(url)

    @staticmethod
    def _rakuten_likely_has_sku(html: str) -> bool:
        """
        判斷 HTML 是否「應該有 SKU 選項」但 httpx 沒抓到（被 JS 包起來）

        判斷標準：
        - 含 'display-sku-area' 字串（即使是空的 div）
        - 含 'type-sku-button' 字串
        - 含 'inventory_no' 等傳統 SKU 標識
        - 商品頁含「カラー」「サイズ」「色分類」等 SKU 關鍵字（但要有上下文）
        """
        if not html:
            return False
        sku_markers = [
            'display-sku-area',
            'type-sku-button',
            'inventory_no',
            'sku-area',
            '"skuList"',
        ]
        return any(m in html for m in sku_markers)

    def _rakuten_driver_fetch(self, url: str) -> str:
        """
        用 SeleniumBase UC 抓樂天頁面（JS 渲染後）
        專門給 SKU 選項 fallback 用，最多等 12 秒
        """
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()
            try:
                driver.uc_open_with_reconnect(url, reconnect_time=4)
            except Exception:
                driver.get(url)

            # 評分式等待：等到 SKU 區渲染出來
            best_html = ""
            best_score = 0

            for i in range(6):
                time.sleep(2)
                try:
                    html = driver.page_source
                except Exception:
                    continue

                # 評分：有 type-sku-button 表示 JS 已渲染好
                score = 0
                # 計算實際的 button 數，不只看 class 名
                btn_count = html.count('type-sku-button--')
                if btn_count > 0:
                    score += min(btn_count, 30)  # 最多 30 分（避免被無關內容壓爆）
                if 'display-sku-area' in html:
                    score += 3
                if '色分類' in html or 'カラー' in html:
                    score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                # 抓到足夠多的 button 就提早返回
                if i >= 1 and btn_count >= 2:
                    print(f"[Rakuten][driver] iter={i} btn_count={btn_count} score={score} ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            print(f"[Rakuten][driver] 最佳版本 score={best_score}")
            return best_html

        except Exception as e:
            print(f"[Rakuten][driver] 失敗: {type(e).__name__}: {e}")
            return ""
