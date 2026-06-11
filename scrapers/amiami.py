"""
あみあみ (amiami.jp) 商品爬取 Mixin —— 混合版 (v3, 2026-06)

策略：
  1. 樂天 Ichiba API 優先（快、乾淨、無 Chrome；覆蓋新品/預約品）
  2. 樂天查無 → 自動回退 amiami.jp 直爬（SeleniumBase；補中古 -R、未上架品）

對應關係（已實測）：
  amiami.jp scode（GOODS-xxxx / FIGURE-xxxx / RAIL-xxxx …）
    == 樂天 amiami 店 URL slug（小寫）
  ※ 樂天 itemCode 是內部流水號（amiami:13050893），無法由 scode 推算，
    故不能用 itemCode 直查；改用「店內關鍵字搜 scode」：
      shopCode=amiami & keyword={scode}  → 再以 itemUrl slug 比對確認。
  ※ 結尾 -R = amiami 中古品，樂天店不賣 → 必走 fallback。

環境變數（Zeabur）：
  RAKUTEN_APP_ID       樂天 Application ID（UUID）            ← 必填
  RAKUTEN_ACCESS_KEY   樂天 Access Key（pk_...）             ← 必填
  RAKUTEN_REFERER      預設 https://goyoutati.com/（選填）
                       須對應 App 後台 Allowed websites；
                       新 API 真正把關的是 Origin（由此推出），錯誤訊息會誤寫成 Referer。

樂天 API 已知限制（對 amiami 影響小）：
  * 無變體/SKU；availability 只有 0/1（無數量）
  * 無 brand 欄位 → 從標題尾 [メーカー] 抽
  * mediumImageUrls 只有 128px → 去掉 ?_ex= 取原圖

amiami.jp 直爬（fallback）價格陷阱：
  頁面同時有「参考価格」(.selling_price，原価，避開) 與「販売価格」
  (data-item-price / .price，實際售價)。務必只抓販売価格。
"""
import os
import re
import json
import time
import asyncio

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000

# 樂天新版端點（UUID 型 Application ID + Access Key 走這支）
_RAKUTEN_ENDPOINT = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
_RAKUTEN_SHOPCODE = "amiami"
_DEFAULT_REFERER = "https://goyoutati.com/"
_HTTP_TIMEOUT = 20.0


class AmiamiMixin:

    async def _scrape_amiami(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=self._amiami_clean_url(url))

        code = self._amiami_extract_code(url)
        if not code:
            print(f"[Amiami] ❌ 無法從 URL 取得商品代碼: {url}")
            return product
        print(f"[Amiami] 商品代碼: {code}")

        # ── 1. 樂天 API 優先 ──
        got = await self._amiami_try_rakuten(code, product)
        if got and product.is_valid:
            self._amiami_log_ok(product, source="樂天API")
            return product

        # ── 2. fallback：amiami.jp 直爬（只對 amiami.jp 連結；中古 -R / 未上架走這）──
        if "amiami.jp" in (url or ""):
            print(f"[Amiami] ↩️ 樂天查無，改用 amiami.jp 直爬 fallback（scode={code}）")
            await self._amiami_scrape_jp(product.source_url, product)

        if product.is_valid:
            self._amiami_log_ok(product, source="amiami.jp")
        else:
            print(f"[Amiami] ❌ 樂天與 amiami.jp 皆未取得有效資料（scode={code}）")
        return product

    def _amiami_log_ok(self, product: ProductInfo, source: str) -> None:
        title_short = (product.title or "")[:60]
        print(
            f"[Amiami] ✅[{source}] {title_short!r} | ¥{product.price_jpy:,} | "
            f"brand={product.brand!r} | in_stock={product.in_stock} | "
            f"images={1 + len(product.extra_images) if product.image_url else 0}"
        )

    # ═════════════════════════════════════════════════════════════════
    # 1. 樂天 API 路徑
    # ═════════════════════════════════════════════════════════════════
    async def _amiami_try_rakuten(self, code: str, product: ProductInfo) -> bool:
        """成功取得並寫入 product 回 True；查無或無憑證回 False（交給 fallback）。"""
        app_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
        access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
        referer = (os.environ.get("RAKUTEN_REFERER", "").strip() or _DEFAULT_REFERER)
        if not app_id or not access_key:
            print("[Amiami] ⚠️ 缺少 RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY，跳過樂天直接 fallback")
            return False

        # itemCode 無法由 scode 推算 → 用店內關鍵字搜 scode；availability=0 連缺貨也回
        data = await self._amiami_rakuten_call(
            app_id, access_key, referer,
            extra_params={
                "shopCode": _RAKUTEN_SHOPCODE,
                "keyword": code,
                "availability": 0,
                "hits": 10,
            },
        )
        if data is None:
            return False

        item = self._amiami_match_item(data, code)
        if not item:
            print(f"[Amiami] ⚠️ 樂天 amiami 店查無此商品（scode={code}）")
            return False

        try:
            self._amiami_apply_rakuten(item, product)
            return True
        except Exception as e:
            print(f"[Amiami] ❌ 樂天解析錯誤: {type(e).__name__}: {e}")
            return False

    async def _amiami_rakuten_call(
        self, app_id: str, access_key: str, referer: str, extra_params: dict
    ) -> dict | None:
        """
        呼叫樂天 Ichiba Item Search API。回傳 dict；失敗回 None。
        必帶 Referer + Origin（對應 App 後台 Allowed websites）。
        403（Referer 閘道跨節點生效不一致）與 429（流量）自動重試。
        """
        params = {
            "applicationId": app_id,
            "accessKey": access_key,
            "formatVersion": 2,
            **extra_params,
        }
        origin = re.sub(r'(https?://[^/]+).*', r'\1', referer)  # → https://goyoutati.com
        headers = {
            "Referer": referer,
            "Origin": origin,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) goyoutati-daigo/1.0",
        }

        max_attempts = 3
        last_status = None
        last_text = ""
        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    resp = await client.get(_RAKUTEN_ENDPOINT, params=params, headers=headers)
            except Exception as e:
                print(f"[Amiami] ❌ 樂天 API 連線失敗: {type(e).__name__}: {e}")
                return None

            last_status, last_text = resp.status_code, resp.text[:200]

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as e:
                    print(f"[Amiami] ❌ 樂天回傳非 JSON: {e} | {resp.text[:200]}")
                    return None

            if resp.status_code == 404:
                print("[Amiami] ⚠️ 樂天回 404 not_found")
                return {"Items": []}

            # itemCode/keyword 無效等 → 當作查無
            if resp.status_code == 400 and ("itemCode" in resp.text or "wrong_parameter" in resp.text):
                print(f"[Amiami] ⚠️ 樂天 400 wrong_parameter（視為查無）：{resp.text[:150]}")
                return {"Items": []}

            if resp.status_code in (403, 429):
                wait = 1.5
                print(
                    f"[Amiami] ⏳ 樂天 {resp.status_code}（attempt {attempt + 1}/{max_attempts}），"
                    f"等 {wait}s 重試… {resp.text[:120]}"
                )
                await asyncio.sleep(wait)
                continue

            print(f"[Amiami] ❌ 樂天 API HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        if last_status == 403:
            print(
                "[Amiami] ❌ 樂天 403 重試後仍失敗。檢查 Allowed websites 是否含 'goyoutati.com'、"
                f"設定是否已生效。Referer={referer!r}。回應：{last_text}"
            )
        else:
            print(f"[Amiami] ❌ 樂天 API 重試後仍失敗（HTTP {last_status}）：{last_text}")
        return None

    @staticmethod
    def _amiami_match_item(data: dict, code: str) -> dict | None:
        """
        關鍵字搜尋結果中，挑 itemUrl slug 對得上 scode 的那筆。
        相容 formatVersion 1/2、Items/items、Item/item。
        比對：1) slug==scode  2) slug 以 scode 開頭（-s001 變體）  3) 否則 None（不亂配）
        """
        items = data.get("Items")
        if items is None:
            items = data.get("items")
        if not items:
            return None

        def unwrap(entry):
            if isinstance(entry, dict):
                inner = entry.get("Item") or entry.get("item")
                return inner if isinstance(inner, dict) else entry
            return None

        def slug_of(it):
            m = re.search(r'/amiami/([\w\-]+)', str(it.get("itemUrl") or ""))
            return m.group(1).lower() if m else ""

        target = code.lower()
        for entry in items:
            it = unwrap(entry)
            if it and slug_of(it) == target:
                return it
        for entry in items:
            it = unwrap(entry)
            if it and slug_of(it).startswith(target):
                return it
        return None

    def _amiami_apply_rakuten(self, item: dict, product: ProductInfo) -> None:
        raw_name = str(item.get("itemName") or "").strip()
        if raw_name:
            product.title = self._amiami_clean_title(raw_name)
            product.brand = self._amiami_brand_from_title(raw_name)

        price = self._amiami_to_int(item.get("itemPrice"))
        if price:
            product.price_jpy = price

        caption = str(item.get("itemCaption") or "").strip()
        if caption:
            product.description = caption[:1500]

        avail = item.get("availability")
        product.in_stock = (avail == 1 or avail == "1")

        imgs = self._amiami_extract_images(item)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]

    @staticmethod
    def _amiami_extract_images(item: dict) -> list:
        """mediumImageUrls 可能是 str 或 {'imageUrl':...}；去掉 ?_ex=WxH 取原圖。"""
        raw = item.get("mediumImageUrls") or item.get("smallImageUrls") or []
        out, seen = [], set()
        for entry in raw:
            if isinstance(entry, dict):
                u = entry.get("imageUrl") or entry.get("url") or ""
            else:
                u = str(entry or "")
            u = u.strip()
            if not u:
                continue
            u = re.split(r'\?_ex=\d+x\d+', u)[0]
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    # ═════════════════════════════════════════════════════════════════
    # 2. amiami.jp 直爬 fallback（SeleniumBase）
    # ═════════════════════════════════════════════════════════════════
    async def _amiami_scrape_jp(self, url: str, product: ProductInfo) -> None:
        html = await asyncio.to_thread(self._amiami_get_html, url)
        if not html:
            print(f"[Amiami] ❌ amiami.jp HTML 取得失敗: {url}")
            return
        try:
            soup = BeautifulSoup(html, "html.parser")

            ld = self._amiami_find_jsonld(soup)
            if ld:
                self._amiami_apply_jsonld(ld, product)

            if not product.title:
                og_title = soup.find("meta", attrs={"property": "og:title"})
                if og_title and og_title.get("content"):
                    title = og_title["content"].strip()
                    title = re.sub(r'\s*\[[^\]]+\]\s*$', '', title)
                    product.title = title

            price = self._amiami_extract_price(soup, html)
            if price:
                product.price_jpy = price

            product.in_stock = self._amiami_extract_stock(html)
        except Exception as e:
            print(f"[Amiami] ❌ amiami.jp 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    @staticmethod
    def _amiami_find_jsonld(soup: BeautifulSoup) -> dict | None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        return None

    def _amiami_apply_jsonld(self, ld: dict, product: ProductInfo) -> None:
        name = (ld.get("name") or "").strip()
        if name:
            product.title = name

        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str) and brand.strip():
            product.brand = brand.strip()

        desc = (ld.get("description") or "").strip()
        if desc:
            product.description = desc[:1500]

        images = ld.get("image")
        if isinstance(images, str):
            images = [images]
        if isinstance(images, list) and images:
            product.image_url = str(images[0]).strip()
            seen = {product.image_url}
            extra = []
            for img in images[1:]:
                u = str(img).strip()
                if u and u not in seen:
                    seen.add(u)
                    extra.append(u)
                if len(extra) >= 9:
                    break
            product.extra_images = extra

        offers = ld.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            v = self._amiami_to_int(offers.get("price"))
            if v:
                product.price_jpy = v

    def _amiami_extract_price(self, soup: BeautifulSoup, html: str) -> int | None:
        """嚴守販売価格、避開参考価格(.selling_price)。"""
        price_el = soup.select_one('[data-item-price]')
        if price_el:
            v = self._amiami_to_int(price_el.get("data-item-price", ""))
            if v:
                print(f"[Amiami] 價格採用 data-item-price: ¥{v}")
                return v

        price_div = soup.select_one(
            'div.price#detail_detail__item_price, div.price[id*="item_price"]'
        )
        if price_div:
            m = re.search(r'([\d,]+)\s*円', price_div.get_text(" ", strip=True))
            if m:
                v = self._amiami_to_int(m.group(1))
                if v:
                    print(f"[Amiami] 價格採用 .price 區塊: ¥{v}")
                    return v

        m = re.search(
            r'販売価格.*?(?:</[^>]+>\s*){0,3}.*?(\d+%OFF)?\s*([\d,]+)\s*円\s*[（(]\s*税込',
            html, re.DOTALL,
        )
        if m:
            v = self._amiami_to_int(m.group(2))
            if v:
                print(f"[Amiami] 價格採用「販売価格」文字: ¥{v}")
                return v

        print("[Amiami] ⚠️ 找不到販売価格（不採用参考価格）")
        return None

    @staticmethod
    def _amiami_extract_stock(html: str) -> bool:
        out_kw = ["販売停止", "販売終了", "受注終了", "受付終了", "完売", "在庫切れ", "再販未定"]
        in_kw = ["カートに追加", "予約注文", "注文する"]
        for kw in out_kw:
            if kw in html:
                print(f"[Amiami] 庫存: 缺貨（「{kw}」）")
                return False
        for kw in in_kw:
            if kw in html:
                print(f"[Amiami] 庫存: 可下單（「{kw}」）")
                return True
        print("[Amiami] 庫存: 未知，預設可下單")
        return True

    def _amiami_get_html(self, url: str) -> str:
        """SeleniumBase UC 抓 amiami.jp 頁面（fallback 用）。"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()
            try:
                driver.uc_open_with_reconnect(url, reconnect_time=5)
            except Exception:
                driver.get(url)
            time.sleep(2)

            best_html, best_score = "", 0
            for i in range(5):
                time.sleep(1.5)
                try:
                    html = driver.page_source
                except Exception:
                    continue
                score = 0
                if 'application/ld+json' in html: score += 3
                if 'data-item-price' in html: score += 5
                if 'detail_detail__item_price' in html: score += 3
                if 'og:title' in html: score += 2
                if '販売価格' in html: score += 2
                if score > best_score:
                    best_score, best_html = score, html
                if i >= 1 and score >= 13 and len(html) > 30000:
                    print(f"[Amiami][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1
            if best_html and len(best_html) > 10000:
                print(f"[Amiami][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html
            print("[Amiami][fetch] ❌ 取得失敗")
            return ""
        except Exception as e:
            print(f"[Amiami] driver 失敗: {type(e).__name__}: {e}")
            return ""

    # ═════════════════════════════════════════════════════════════════
    # 共用工具
    # ═════════════════════════════════════════════════════════════════
    @staticmethod
    def _amiami_extract_code(url: str) -> str | None:
        m = re.search(r'rakuten\.co\.jp/amiami/([\w\-]+)', url, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r'[?&](?:g|s)code=([\w\-]+)', url, re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _amiami_clean_url(url: str) -> str:
        clean = (url or "").split("#")[0].strip()
        m = re.search(r'(https?://item\.rakuten\.co\.jp/amiami/[\w\-]+)', clean, re.IGNORECASE)
        if m:
            return m.group(1).rstrip("/") + "/"
        base_m = re.match(r'(https?://[^/]+/top/detail/detail)', clean, re.IGNORECASE)
        key_m = re.search(r'(g?s?code)=([\w\-]+)', clean, re.IGNORECASE)
        if base_m and key_m:
            return f"{base_m.group(1)}?{key_m.group(1)}={key_m.group(2)}"
        return clean

    @staticmethod
    def _amiami_clean_title(name: str) -> str:
        t = (name or "").strip()
        t = re.sub(r'(\s*《[^》]*》\s*)+$', '', t).strip()
        t = re.sub(r'\s*\[[^\]]+\]\s*$', '', t).strip()
        return t or (name or "").strip()

    @staticmethod
    def _amiami_brand_from_title(name: str) -> str:
        brackets = re.findall(r'\[([^\]]+)\]', name or "")
        if brackets:
            b = brackets[-1].strip()
            if b and not re.fullmatch(r'\d+', b):
                return b
        return ""

    @staticmethod
    def _amiami_to_int(value) -> int | None:
        if value is None:
            return None
        s = str(value).strip().replace(",", "").replace("，", "").replace("¥", "").replace("円", "")
        if not s:
            return None
        try:
            v = int(float(s))
        except (ValueError, TypeError):
            return None
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
        return None
