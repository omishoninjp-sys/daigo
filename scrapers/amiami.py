"""
あみあみ (amiami.jp) 商品爬取 Mixin —— 樂天 Ichiba API 版 (v2, 2026-06)

改版說明：
- 不再用 SeleniumBase 爬 amiami.jp，改走「樂天 Ichiba Item Search API」。
  好處：純 JSON API、免 Chrome、無 Akamai 封鎖、獨立帳號（與 Yahoo 停權無關）。

對應關係（已實測確認）：
  amiami.jp 的 scode（GOODS-xxxx / FIGURE-xxxx / RAIL-xxxx …）
    == 樂天 amiami 店的 URL slug（小寫）
  例：amiami.jp GOODS-04818580 ↔ item.rakuten.co.jp/amiami/goods-04818580/

  ※ 樂天 API 的 itemCode 是另一組內部流水號（如 amiami:13050893），
    無法由 scode 推算，所以不能用 itemCode 直查。
    改用「店內關鍵字搜尋 scode」：shopCode=amiami & keyword={scode}
    scode 對 amiami 店是唯一的，實測回 count=1，再以 itemUrl slug 比對確認。

需要的環境變數（Zeabur）：
    RAKUTEN_APP_ID       樂天 Application ID（UUID 格式）          ← 必填
    RAKUTEN_ACCESS_KEY   樂天 Access Key                          ← 必填
    RAKUTEN_REFERER      預設 https://goyoutati.com/（選填）
                         必須符合 App 後台「Allowed websites」其中一個網域，
                         否則樂天會回 403 REQUEST_CONTEXT_BODY_HTTP_REFERRER_MISSING

樂天 API 已知限制（對 amiami 影響都很小）：
    * 無變體／SKU 資料（amiami 多為單品公仔模型，無妨；只會拿到代表價）
    * availability 只有 0/1（無庫存數量）
    * 無 brand 欄位 → 從標題尾巴 [メーカー] 抽出
    * mediumImageUrls 只有 128px 縮圖 → 去掉 ?_ex= 後綴取原圖

URL 範例（主要吃 amiami.jp，也能吃樂天 amiami 店連結）：
    https://www.amiami.jp/top/detail/detail?gcode=GOODS-04818580
    https://www.amiami.jp/top/detail/detail?scode=FIGURE-199356
    https://item.rakuten.co.jp/amiami/goods-04818580/
"""
import os
import re
import asyncio

import httpx

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000

# 樂天新版端點（UUID 型 Application ID + Access Key 須走這支）
_RAKUTEN_ENDPOINT = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
_RAKUTEN_SHOPCODE = "amiami"
_DEFAULT_REFERER = "https://goyoutati.com/"
_HTTP_TIMEOUT = 20.0


class AmiamiMixin:

    async def _scrape_amiami(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=self._amiami_clean_url(url))

        # ── 1. 從 URL 取出商品代碼（amiami scode / 樂天 slug）──
        code = self._amiami_extract_code(url)
        if not code:
            print(f"[Amiami] ❌ 無法從 URL 取得商品代碼: {url}")
            return product

        print(f"[Amiami] 商品代碼: {code}（將以店內關鍵字搜尋）")

        # ── 2. 讀取憑證（環境變數）──
        app_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
        access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
        referer = (os.environ.get("RAKUTEN_REFERER", "").strip() or _DEFAULT_REFERER)
        if not app_id or not access_key:
            print("[Amiami] ❌ 缺少 RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY 環境變數，無法呼叫樂天 API")
            return product

        # ── 3. 呼叫樂天 API ──
        # itemCode 是樂天內部流水號、無法由 scode 推算；改用「店內關鍵字搜 scode」。
        # scode（GOODS-xxxx）對 amiami 店是唯一的，實測回傳 count=1。
        # availability=0：連缺貨品也要回，才能正確判斷庫存。
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
            return product

        item = self._amiami_match_item(data, code)
        if not item:
            print(f"[Amiami] ⚠️ 樂天 amiami 店查無此商品（scode={code}），可能未上架樂天")
            return product

        # ── 4. 欄位映射到 ProductInfo ──
        try:
            self._amiami_apply_rakuten(item, product)

            title_short = (product.title or "")[:60]
            if product.is_valid:
                print(
                    f"[Amiami] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"brand={product.brand!r} | in_stock={product.in_stock} | "
                    f"images={1 + len(product.extra_images) if product.image_url else 0}"
                )
            else:
                print(f"[Amiami] ⚠️ 部分資料缺失 ({title_short!r}) | price={product.price_jpy}")
        except Exception as e:
            print(f"[Amiami] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # 樂天 API 呼叫
    # ─────────────────────────────────────────────────────────────────
    async def _amiami_rakuten_call(
        self, app_id: str, access_key: str, referer: str, extra_params: dict
    ) -> dict | None:
        """
        呼叫樂天 Ichiba Item Search API。
        回傳解析後的 dict；失敗回 None。
        Referer 標頭必填（對應 App 後台 Allowed websites），否則 403。
        遇 429（流量過高）自動重試一次。
        """
        params = {
            "applicationId": app_id,
            "accessKey": access_key,
            "formatVersion": 2,
            **extra_params,
        }
        # Origin = referer 的 scheme://host（樂天閘道有時同時檢查 Referer + Origin）
        origin = re.sub(r'(https?://[^/]+).*', r'\1', referer)
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
                # not_found：itemCode 不存在
                print("[Amiami] ⚠️ 樂天回 404 not_found（itemCode 不存在）")
                return {"Items": []}

            # 403（Referer 閘道跨節點生效不一致，會間歇性失敗）與 429（流量）都重試
            if resp.status_code in (403, 429):
                wait = 1.5
                print(
                    f"[Amiami] ⏳ 樂天 {resp.status_code}（attempt {attempt + 1}/{max_attempts}），"
                    f"等 {wait}s 重試… {resp.text[:120]}"
                )
                await asyncio.sleep(wait)
                continue

            # itemCode 無效（多半是該商品未上架樂天 amiami 店）→ 當作查無
            if resp.status_code == 400 and "itemCode" in resp.text:
                print(f"[Amiami] ⚠️ itemCode 無效，此商品可能未上架樂天 amiami 店：{resp.text[:150]}")
                return {"Items": []}

            print(f"[Amiami] ❌ 樂天 API HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        # 重試用盡
        if last_status == 403:
            print(
                "[Amiami] ❌ 樂天 403 Referer 重試後仍失敗。檢查：(1) App 後台 Allowed websites "
                f"確實有 'goyoutati.com'；(2) 設定改完需數分鐘~數十分鐘跨節點生效；"
                f"(3) RAKUTEN_REFERER 是否正確。目前送出 Referer={referer!r}。回應：{last_text}"
            )
        else:
            print(f"[Amiami] ❌ 樂天 API 重試後仍失敗（HTTP {last_status}）：{last_text}")
        return None

    # ─────────────────────────────────────────────────────────────────
    # 回應解析：相容 formatVersion 1/2 與大小寫
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _amiami_match_item(data: dict, code: str) -> dict | None:
        """
        從關鍵字搜尋結果中，挑出 itemUrl slug 對得上 scode 的那筆。
        相容 formatVersion 1/2 與 Items/items、Item/item 大小寫。
        比對策略（由嚴到鬆）：
          1. slug 完全等於 scode（小寫）
          2. slug 以 scode 開頭（處理 goods-xxxx-s001 之類變體後綴）
          3. 都對不上 → 回 None（寧可回報查無，也不亂配錯商品）
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

        # 1. slug 完全相等
        for entry in items:
            it = unwrap(entry)
            if it and slug_of(it) == target:
                return it
        # 2. slug 以 scode 開頭（變體後綴）
        for entry in items:
            it = unwrap(entry)
            if it and slug_of(it).startswith(target):
                return it
        # 3. 對不上 → 視為查無
        return None

    # ─────────────────────────────────────────────────────────────────
    # 欄位映射
    # ─────────────────────────────────────────────────────────────────
    def _amiami_apply_rakuten(self, item: dict, product: ProductInfo) -> None:
        # 標題（去尾巴 《予約》 與 [メーカー]，與舊版風格一致）
        raw_name = str(item.get("itemName") or "").strip()
        if raw_name:
            product.title = self._amiami_clean_title(raw_name)
            # 品牌：標題沒有 brand 欄，從尾巴 [メーカー] 抽
            product.brand = self._amiami_brand_from_title(raw_name)

        # 價格（itemPrice 為含稅售價；taxFlag 0=含稅）
        price = self._amiami_to_int(item.get("itemPrice"))
        if price:
            product.price_jpy = price

        # 描述
        caption = str(item.get("itemCaption") or "").strip()
        if caption:
            product.description = caption[:1500]

        # 庫存（availability：1=可下單 / 0=缺貨）
        avail = item.get("availability")
        product.in_stock = (avail == 1 or avail == "1")

        # 圖片（去掉 ?_ex= 縮圖後綴取原圖）
        imgs = self._amiami_extract_images(item)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]

    # ─────────────────────────────────────────────────────────────────
    # 工具
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _amiami_extract_code(url: str) -> str | None:
        """
        從以下任一種 URL 取出商品代碼：
          amiami.jp ...?gcode=GOODS-04818580
          amiami.jp ...?scode=FIGURE-199356&page=related_item
          item.rakuten.co.jp/amiami/goods-04818580/
        回傳原始大小寫的代碼（建 itemCode 時才轉小寫）。
        """
        # 樂天 amiami 店 URL
        m = re.search(r'rakuten\.co\.jp/amiami/([\w\-]+)', url, re.IGNORECASE)
        if m:
            return m.group(1)
        # amiami.jp scode / gcode
        m = re.search(r'[?&](?:g|s)code=([\w\-]+)', url, re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _amiami_clean_url(url: str) -> str:
        """
        標準化 source_url（沿用舊版邏輯，維持與既有資料的 dedup 一致）：
          amiami.jp → .../top/detail/detail?{gcode|scode}={val}
          樂天 amiami → 標準化為 item.rakuten.co.jp/amiami/{slug}/
        """
        clean = url.split("#")[0].strip()

        # 樂天 amiami 店
        m = re.search(r'(https?://item\.rakuten\.co\.jp/amiami/[\w\-]+)', clean, re.IGNORECASE)
        if m:
            return m.group(1).rstrip("/") + "/"

        # amiami.jp：保留 gcode / scode，去掉追蹤參數
        base_m = re.match(r'(https?://[^/]+/top/detail/detail)', clean, re.IGNORECASE)
        key_m = re.search(r'(g?s?code)=([\w\-]+)', clean, re.IGNORECASE)
        if base_m and key_m:
            return f"{base_m.group(1)}?{key_m.group(1)}={key_m.group(2)}"

        return clean

    @staticmethod
    def _amiami_clean_title(name: str) -> str:
        """去掉尾端 《...予約》 等預約括號與 [メーカー] 標記。"""
        t = name.strip()
        t = re.sub(r'(\s*《[^》]*》\s*)+$', '', t).strip()   # 尾端 《...》
        t = re.sub(r'\s*\[[^\]]+\]\s*$', '', t).strip()     # 尾端 [メーカー]
        return t or name.strip()

    @staticmethod
    def _amiami_brand_from_title(name: str) -> str:
        """品牌＝標題裡最後一組 [メーカー]（半形中括號）。"""
        brackets = re.findall(r'\[([^\]]+)\]', name)
        if brackets:
            b = brackets[-1].strip()
            # 排除明顯非品牌的標記
            if b and not re.fullmatch(r'\d+', b):
                return b
        return ""

    @staticmethod
    def _amiami_extract_images(item: dict) -> list:
        """
        取出圖片並升級為原圖。
        mediumImageUrls 每筆可能是 str（formatVersion=2）或 {"imageUrl": ...}（formatVersion=1）。
        去掉 ?_ex=WxH 後綴即為原圖。
        """
        raw = item.get("mediumImageUrls") or item.get("smallImageUrls") or []
        out = []
        seen = set()
        for entry in raw:
            if isinstance(entry, dict):
                u = entry.get("imageUrl") or entry.get("url") or ""
            else:
                u = str(entry or "")
            u = u.strip()
            if not u:
                continue
            # 去縮圖後綴取原圖：...jpg?_ex=128x128 → ...jpg
            u = re.split(r'\?_ex=\d+x\d+', u)[0]
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

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
