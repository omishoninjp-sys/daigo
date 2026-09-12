"""
通用爬蟲 Mixin
- Playwright / httpx 通用抓取
- JSON-LD、OG tag、generic 解析器
"""
import re
import json
import statistics
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import (ProductInfo, normalize_price,
                           PRICE_MIN_JPY, PRICE_MAX_JPY, price_in_range)


def _note_http(status, body="", final_url=""):
    """回報 HTTP 狀態與最終網址給爬取監控（fail-safe，監控壞掉不影響爬取）。"""
    try:
        import scrape_monitor
        scrape_monitor.note_http(status, body, final_url)
    except Exception:
        pass


# 擋頁特徵。★ 弱特徵只在頁面很小的時候才算數 —— 2026-08-30 實測 coldbeer.jp：
# Shopify 商店的正常頁面內嵌 <script id="captcha-bootstrap">，整頁 433KB 也命中
# "captcha"，於是每一家 Shopify 日本商店都被判定「被擋」，白跑一次 Selenium，
# 最後 60 秒逾時。真正的 challenge 頁都很小。
# （scrape_monitor 的分類端有一份同樣意思的清單，但兩邊刻意不互相 import：
#   監控壞掉不可以影響爬取。）
_BLOCKED_STRONG = ("access denied", "403 forbidden", "bot detected", "are you a human")
_BLOCKED_WEAK = ("robot", "captcha", "recaptcha", "cloudflare", "attention required")
_BLOCKED_WEAK_MAX_BYTES = 50_000


def _has_block_markers(html: str) -> bool:
    """
    只看擋頁**特徵字**，不看「頁面小於 5000 就算被擋」那一條。

    🔴 與 _looks_blocked 的差別就在這裡，不可以拿 _looks_blocked 代替：
      它的第一條是 `len(html) < 5000 → True`，那是給「httpx 抓完之後值不值得
      再花一次 Selenium」用的判斷 —— 放進 Selenium 的輪詢迴圈，等於
      **第一次輪詢一律判定被擋**，會把慢載入的真頁面全部誤殺。
    弱特徵仍然保留 50KB 上限（2026-08-30 的 captcha-bootstrap 教訓）。
    """
    html = html or ""
    low = html.lower()
    if any(kw in low for kw in _BLOCKED_STRONG):
        return True
    return len(html) < _BLOCKED_WEAK_MAX_BYTES and any(kw in low for kw in _BLOCKED_WEAK)


def _looks_blocked(html: str) -> bool:
    """httpx 拿到的內容像不像擋頁 —— 像的話才值得再花一次 Selenium。"""
    html = html or ""
    if len(html) < 5000:
        return True
    low = html.lower()
    if any(kw in low for kw in _BLOCKED_STRONG):
        return True
    return len(html) < _BLOCKED_WEAK_MAX_BYTES and any(kw in low for kw in _BLOCKED_WEAK)


def _note_error(error, where=""):
    """把被吞掉的例外交給監控（fail-safe：監控壞掉不影響爬取）。"""
    try:
        import scrape_monitor
        scrape_monitor.note_error(error, where)
    except Exception:
        pass


def _note_price_candidates(picked: dict, cand_vals: dict = None) -> None:
    """把取價候選交給 scrape_monitor。記錄失敗絕不影響爬取。"""
    try:
        import scrape_monitor
        scrape_monitor.note_price_candidates(picked, cand_vals)
    except Exception:
        pass


def _note_page_settled(size) -> bool:
    """回報「瀏覽器把頁面載完了，多大」，拿回「是不是兩條路都不通」。
    **這邊只有事實，判斷在 scrape_monitor** —— 那需要 httpx 的狀態碼，
    而狀態碼是監控自己記的，爬取這邊從頭到尾不碰它。
    監控爆掉 → False → 頁面照舊往下傳（退回 2026-09-12 之前的行為）。"""
    try:
        import scrape_monitor
        return bool(scrape_monitor.note_page_settled(size))
    except Exception:
        return False


def _note_source(name):
    try:
        import scrape_monitor
        scrape_monitor.note_source(name)
    except Exception:
        pass


class GenericMixin:

    # ============================================================
    # 通用 - httpx（其他日本網站）
    # ============================================================
    async def _scrape_with_playwright(self, url: str, allow_shopify: bool = True) -> ProductInfo:
        """
        allow_shopify=False：不要再轉進 Shopify 專用解析。
        ★ 從 _scrape_shopify_jp 退回來的時候一定要關掉，否則兩支會互相呼叫 ——
          每一圈都重抓一次整頁，直到上層 60 秒逾時（coldbeer.jp/zh 的 timeout
          就是這樣來的，不是網站慢）。
        """
        product = ProductInfo(source_url=url)
        try:
            html = await self._fetch_playwright(url)

            if allow_shopify and ('Shopify.shop' in html or '"shopify"' in html.lower() or 'cdn.shopify.com' in html):
                shopify_product = await self._scrape_shopify_jp(url)
                if shopify_product.title and shopify_product.variants:
                    return shopify_product

            soup = BeautifulSoup(html, "html.parser")

            self._extract_json_ld(soup, product)
            self._extract_og_tags(soup, product)
            _prior_price = product.price_jpy      # 結構化資料（JSON-LD／OG）取到的價
            if not product.title or not product.price_jpy:
                self._extract_generic(soup, product)
            elif _prior_price:
                # ★ 2026-09-07：title 與 price 都已由結構化資料取得時，
                #   原本整條 DOM 取價完全不會跑 —— 而 auctions.yahoo
                #   （現在価格 vs 即決価格）正是走這條，等於最需要對照的
                #   情況反而沒有任何候選資料。這裡**只為記錄**跑一次，
                #   回傳值刻意丟棄，不影響 product.price_jpy。
                try:
                    self._find_price_in_html(soup, _prior=_prior_price)
                except Exception:
                    pass

            # 範圍檢查一律走 base.price_in_range —— 數字只有一個出處
            if product.price_jpy and not price_in_range(product.price_jpy):
                product.price_jpy = None

            if product.image_url and not product.image_url.startswith("http"):
                base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                product.image_url = base + product.image_url

        except Exception as e:
            print(f"[Generic] ❌ 錯誤: {e}")
            _note_error(e, "generic")

        return product

    async def _fetch_playwright(self, url: str) -> str:
        """先用 httpx 快速抓取，若被擋（Access Denied / HTML 太短）自動 fallback 到 Selenium UC"""
        import time as _time
        html = ""
        try:
            async with httpx.AsyncClient(
                timeout=SCRAPE_TIMEOUT,
                follow_redirects=True,
                headers={
                    'User-Agent': USER_AGENT,
                    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                },
            ) as client:
                resp = await client.get(url)
                html = resp.text
                _note_http(resp.status_code, html, str(resp.url))
        except Exception as e:
            print(f"[Generic] httpx 失敗: {e}")
            _note_error(e, "generic:httpx")

        is_blocked = _looks_blocked(html)

        if is_blocked:
            print(f"[Generic] httpx 被擋，改用 Selenium UC: {url}")
            _note_source("generic:selenium")
            html = self._fetch_with_selenium(url)

        return html

    def _fetch_with_selenium(self, url: str) -> str:
        """使用 Selenium UC driver 抓取（與 visvim 相同模式）"""
        import time as _time
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return ""
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
                    prev_len = -1
                    for i in range(6):
                        _time.sleep(2)
                        try:
                            html = driver.page_source
                        except Exception:
                            break
                        if len(html) > 5000:
                            return html
                        # ★ 連兩次長度一樣 = 頁面已經載完，再等也不會變（2026-09-03）。
                        #   本來唯一的提早跳出條件是 >5000，而 Akamai 的擋頁只有幾 KB，
                        #   所以每次都跑滿 6 圈 = 12 秒，全程佔著 _driver_lock。
                        #   實測 dior.com 被擋那幾筆固定 18.3~19.8 秒
                        #   （6 秒 uc_open_with_reconnect + 12 秒輪詢），這一條把它砍到約 10.5 秒。
                        #   ★ 不用字串比對判斷擋頁 —— 見 _has_block_markers 的說明。
                        #     這裡只看「還在不在變」，慢慢渲染的真頁面長度每次都不同，行為不變。
                        if len(html) == prev_len:
                            if self._note_selenium_settled(html):
                                # ★ 2026-09-12：兩條路都不通 → 丟掉擋頁，回空字串。
                                #   不丟的話 og/generic 解析會把擋頁的 <title>
                                #   （dior fashion 是 "Page unavailable"）當成商品標題，
                                #   /api/scrape 回 success=true、前端進預覽頁、還進快取。
                                #   空字串走的是既有失敗路徑：無 title → 前端手動表單。
                                print(f"[Generic] 🚫 軟性擋頁（httpx 被擋 + 瀏覽器只載到 "
                                      f"{len(html)} bytes），放棄這頁: {url[:60]}")
                                return ""
                            return html
                        prev_len = len(html)
                    return html
                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[Generic] Selenium 失敗: {e}")
                    return ""
        return ""

    @staticmethod
    def _note_selenium_settled(html: str) -> bool:
        """
        頁面載完但小於 5000 —— 回報**事實**給監控，判斷不在這裡做。
        回傳「是不是兩條路都不通」（True = 呼叫端要丟掉這頁）。

        ★ 大小交給 scrape_monitor.note_page_settled()：「是不是兩條路都不通」
          要配合 httpx 的狀態碼才判斷得出來，而那個狀態碼是監控自己記的。
          爬取路徑只拿回傳值，判斷仍然只在監控那一處（不另立第二個判斷點）。
          監控爆掉 → False → 頁面照舊往下傳。

        ★ challenge 特徵仍然回報，但**訊息只講它真正驗證到的事**。
          原本這裡寫「Selenium 也被擋」是誇大的 —— 它只驗證了「有沒有
          access denied / captcha 這類字樣」。2026-09-03 實測 dior：
          Selenium 拿到的是 3KB 的 "Page unavailable"，一個特徵字都沒有，
          但同一個網址在住宅 IP 拿得到完整商品頁 —— **確實被擋，只是不自報**。
          訊息與證據不符的話，看 log 的人會做出錯的採購決定。
        """
        both_blocked = False
        try:
            both_blocked = _note_page_settled(len(html or ""))
        except Exception:
            both_blocked = False      # 訊號壞掉 → 退回舊行為，不可以讓抓取多失敗
        try:
            if _has_block_markers(html):
                print("[Generic] ⚠️ Selenium 取得的頁面命中 challenge 特徵")
                _note_error("Selenium 取得的頁面命中 challenge 特徵"
                            "（access denied / captcha / cloudflare 之類）",
                            "Selenium")
        except Exception:
            pass
        return both_blocked

    # ============================================================
    # Extractors（通用解析器）
    # ============================================================
    def _extract_json_ld(self, soup, product: ProductInfo):
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") in ("Product", "IndividualProduct")), data[0] if data else {})
                if data.get("@type") not in ("Product", "IndividualProduct"):
                    if "@graph" in data:
                        for item in data["@graph"]:
                            if item.get("@type") == "Product":
                                data = item
                                break
                    else:
                        continue

                if not product.title:
                    product.title = data.get("name", "")
                if not product.image_url and data.get("image"):
                    img = data["image"]
                    product.image_url = img[0] if isinstance(img, list) else (img.get("url", "") if isinstance(img, dict) else str(img))
                if not product.brand and data.get("brand"):
                    b = data["brand"]
                    product.brand = b.get("name", "") if isinstance(b, dict) else str(b)
                if not product.description:
                    product.description = (data.get("description") or "")[:500]
                if not product.price_jpy:
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        p = normalize_price(price)
                        if price_in_range(p):
                            product.price_jpy = p
            except (json.JSONDecodeError, StopIteration):
                continue

    def _extract_og_tags(self, soup, product: ProductInfo):
        og = {}
        for meta in soup.find_all("meta", property=True):
            og[meta["property"]] = meta.get("content", "")
        if not product.title:
            product.title = og.get("og:title", "")
        if not product.image_url:
            product.image_url = og.get("og:image", "")
        if not product.description:
            product.description = og.get("og:description", "")[:500]
        if not product.price_jpy:
            p = og.get("product:price:amount", "")
            if p:
                product.price_jpy = normalize_price(p)

    def _extract_generic(self, soup, product: ProductInfo):
        if not product.title:
            t = soup.find("title")
            if t:
                product.title = t.get_text(strip=True)
        if not product.image_url:
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if not any(s in src.lower() for s in ["logo", "icon", "banner", "sprite", "blank"]):
                    product.image_url = src
                    break
        # ★ 2026-09-07：不論價格是否已由 JSON-LD／OG 設定，**都要跑一次候選收集**。
        #   原本是 `if not product.price_jpy` 才跑，於是 JSON-LD 先命中的頁面
        #   完全不會經過 R1–R5 —— 而 auctions.yahoo（現在価格 vs 即決価格）
        #   正是走 JSON-LD 那條，等於最需要對照的情況反而沒有資料。
        #   決策仍然不變：已經有價就沿用，DOM 這條只拿來記錄與比對。
        dom_price = self._find_price_in_html(soup, _prior=product.price_jpy)
        if not product.price_jpy:
            product.price_jpy = dom_price

    # ══════════════════════════════════════════════════════════════
    # 取價：候選收集 → 脈絡排除 → 分級決策
    # ══════════════════════════════════════════════════════════════
    # ★ 2026-09-01 重寫。舊版是「『N円(税込)』命中就 return min(候選)」，
    #   但日本電商頁面上帶「税込」的多半**不是商品價**：代引手数料、送料、
    #   購物袋價、免運門檻；而商品本體常寫成 SALE5,500円 / ¥5,500 /
    #   「¥ 756税込」（沒有「円」字），舊 regex 硬性要求 N円…税込，根本進不了候選。
    #   於是 min() 等於「在一堆手續費裡挑最小的那個」。三個實例（都已實測重現）：
    #     chikumeido      候選 [330, 990]        → 取 330（代引手数料），真價 SALE5,500円
    #     okinawa-ichiba  候選 [330, 8800]       → 取 330（代引手数料），真價 ¥756
    #     dior            候選 [330, 440, 14850] → 取 330（購物袋價）
    #   後果是商品以錯價上架，靠人工驗算才發現，改價之後
    #   metafield daigo.original_price_jpy 還是停在錯的值（沒有任何機制更新它）。
    #
    #   🔴 **不可以改成 max()。** 那會抓到免運門檻（¥8,800）與贈品門檻（¥14,850），
    #      方向從少收變成多收 —— 少收有人工驗算擋著，多收會直接變成客訴。
    #      正解是「先把非商品價排掉，再取最小」：排除後剩下的是同一件商品的
    #      定価／SALE 群集，取最小＝取實際售價。
    #
    #   取不到可信價時**寧可回 None**。目前流程每筆都要人工驗算，錯價上架會白白
    #   消耗一次人工檢查；明確失敗讓客人重貼一次，成本更低。

    # 排除關鍵字分「數字之前」與「數字之後」，不可以混成一張表。
    # ★ 位置很重要：「送料無料」常常就印在商品價旁邊，
    #   若不分前後，`¥5,500 送料無料` 會把真正的商品價一起殺掉。
    #   費用類的字幾乎都在數字**之前**（「代引手数料は、一律：330円」），
    #   門檻類的字幾乎都在數字**之後**（「8,800円（税込）以上」）。
    _PRICE_EXCLUDE_BEFORE = (
        "手数料", "手数", "代引", "代金引換", "送料", "配送料", "配送手数",
        "別途", "一律", "ショッピングバッグ", "ラッピング", "包装料", "ギフト包装",
        "キャンセル料", "返品送料", "ポイント", "クーポン", "会費", "年会費",
    )
    _PRICE_EXCLUDE_AFTER = (
        "以上", "未満", "以上で", "分のポイント", "ポイント進呈", "円引き",
    )
    _PRICE_CTX_BEFORE = 24      # 只看數字前這麼多字
    _PRICE_CTX_AFTER = 12       # 只看數字後這麼多字
    # 🔴 不要在這裡寫數字 —— 上下限的唯一出處是 scrapers/base.py 的
    #    PRICE_MIN_JPY / PRICE_MAX_JPY（含選這兩個值的實測依據）。
    #    這兩個名字留著是因為子類別與測試會覆寫／讀它們。
    #    ⚠️ 上限 2026-09-08 由 1,000,000 放寬成 10,000,000（收斂到單一值的結果）。
    _PRICE_MIN = PRICE_MIN_JPY
    _PRICE_MAX = PRICE_MAX_JPY
    # 一致性檢查：**每一個分級都有**，但 R2 與其他級用不同的方法，因為失效模式不同。
    #
    #  · R3/R4/R5 是「整頁掃文字」，同頁的選配、加購、補充包會混進來 →
    #    用 max/min 倍數把離散過大的整組否決。
    #  · R2 是「DOM 價格元素」，一個商品頁常常合法地列出十幾個相關商品的價格，
    #    用 max/min 會把正常頁面整批誤殺（實測 40 個網域裡誤殺 6 個，且準確率反而下降）。
    #    R2 改用**文件順序第一個**（主商品的價格元素幾乎都排在相關商品之前，
    #    這也是改寫前的行為），再加一道離群檢查：第一個若與其餘的中位數差超過
    #    _PRICE_R2_OUTLIER 倍，視為抓錯，往下一級。
    _PRICE_SPREAD_MAX = 20
    _PRICE_R2_OUTLIER = 5
    _PRICE_R5_MAX_DISTINCT = 6

    # 金額樣式：千分位一定是「3 位一組」。
    # ★ 不可以寫成 [0-9][0-9,]* —— 那會把「1966,1967,1971」這種逗號分隔的清單
    #   當成單一數字吃掉，normalize_price 再把逗號拿掉就變成 1966196719711971。
    #   這跟 Yahoo 巢狀價格 {990, 890} → 990890 是同一種病：湊巧落在價格範圍內
    #   就會變成看起來正常的假價直接上架。
    _NUM = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"

    # 前綴窗口要在句界截斷。★ 不截的話「送料は 500円 です。商品代金 3,300円」
    #   裡的 3,300 會讀到前一句的「送料」而被誤殺 —— 排除規則反而變成新的錯價來源。
    _PRICE_SENT_BREAK = ("。．.!！?？|｜/／,、" + chr(10) + chr(13) + chr(9))

    def _price_reject(self, text: str, start: int, end: int) -> str | None:
        """回傳命中的排除關鍵字；沒有命中回 None。只看數字前後的小窗口。"""
        before = text[max(0, start - self._PRICE_CTX_BEFORE):start]
        cut = max((before.rfind(ch) for ch in self._PRICE_SENT_BREAK), default=-1)
        if cut >= 0:
            before = before[cut + 1:]
        after = text[end:end + self._PRICE_CTX_AFTER]
        for kw in self._PRICE_EXCLUDE_BEFORE:
            if kw in before:
                return kw
        for kw in self._PRICE_EXCLUDE_AFTER:
            if kw in after:
                return kw
        return None

    def _price_candidates(self, soup, text: str) -> dict:
        """收集各級候選：{規則: [(值, 排除原因 or None), ...]}。這裡不做決策。"""
        out = {k: [] for k in ("R1", "R2", "R3", "R4", "R5")}

        # ── R1 結構化：itemprop="price" 的 content 屬性（最可信）
        for el in soup.select('[itemprop="price"]'):
            raw = el.get("content") or el.get_text(strip=True)
            p = normalize_price(raw)
            if p:
                t = el.get_text(strip=True)
                out["R1"].append((p, self._price_reject(t, 0, len(t))))

        # ── R2 DOM：class/id 含 price 的元素文字
        seen_el = set()
        for sel in ('[itemprop="price"]', '[class*="price"]', '[class*="Price"]',
                    '[id*="price"]', '[id*="Price"]'):
            for el in soup.select(sel):
                if id(el) in seen_el:
                    continue
                seen_el.add(id(el))
                t = el.get_text(strip=True)
                m = re.search(r'[¥￥]?\s*(' + self._NUM + r')', t)
                if not m:
                    continue
                p = normalize_price(m.group(1))
                if p:
                    out["R2"].append((p, self._price_reject(t, m.start(1), m.end(1))))

        # ── R3 文字：帶「税込」的金額。★「円」設為選擇性 —— okinawa-ichiba 寫成
        #    「¥ 756税込」，舊 regex 要求 N円…税込 就整個漏掉了。
        for m in re.finditer(r'[¥￥]?\s*(' + self._NUM + r')\s*(?:円)?\s*[（(]?\s*税込', text):
            out["R3"].append((normalize_price(m.group(1)),
                              self._price_reject(text, m.start(1), m.end(1))))

        # ── R4 文字：価格類標籤後面接的金額（chikumeido 的 SALE5,500円 走這條）
        for m in re.finditer(
                r'(?:販売価格|本体価格|セール価格|価格|SALE|Sale|税込価格)'
                r'\s*[：:]?\s*[¥￥]?\s*(' + self._NUM + r')', text):
            out["R4"].append((normalize_price(m.group(1)),
                              self._price_reject(text, m.start(1), m.end(1))))

        # ── R5 泛用（最弱，只在前面全空時才會用到）
        for pat in (r'[¥￥]\s*(' + self._NUM + r')',
                    r'(' + self._NUM + r')\s*円'):
            for m in re.finditer(pat, text):
                out["R5"].append((normalize_price(m.group(1)),
                                  self._price_reject(text, m.start(1), m.end(1))))
        return out

    def _find_price_in_html(self, soup, _prior: int | None = None) -> int | None:
        # ── 先移除刪除線元素（原價），避免抓到劃掉的舊價
        for tag in soup.find_all(['del', 's', 'strike']):
            tag.decompose()

        text = soup.get_text()
        cands = self._price_candidates(soup, text)

        # ★ 2026-09-07：五條規則**全部跑完**再決策，不再一命中就 return。
        #   決策順序完全沒變（仍是 R1→R5 取第一個有值的），差別只在
        #   後面幾條規則也會被算出來，好把「各規則各自會選什麼」記進 scrape_monitor。
        #   加這個是因為 auctions.yahoo 那類頁面同時有現在価格與即決価格，
        #   R1 先命中就直接回傳，跨規則從來沒有對照過 —— 挑錯欄位不會讓爬取失敗，
        #   ok=True，而低估售價的錯客人不會來反映。
        #   **這一版只記錄不判斷**，門檻要等真實分佈出來再定。
        log = []
        picked = {}
        cand_vals = {}
        for rule in ("R1", "R2", "R3", "R4", "R5"):
            raw = cands[rule]
            if not raw:
                log.append(f"{rule}=空")
                continue

            kept, dropped = [], []
            for v, reject_kw in raw:
                if v is None:
                    continue
                if not price_in_range(v, self._PRICE_MIN, self._PRICE_MAX):
                    dropped.append(f"{v}→範圍外")
                    continue
                if reject_kw:
                    dropped.append(f"{v}→排除:{reject_kw}")
                    continue
                kept.append(v)

            vals = sorted(set(kept))
            detail = ",".join(dropped + [f"[{v}]" for v in vals]) or "無"
            if not vals:
                log.append(f"{rule}=({detail})→全數排除")
                continue

            if rule == "R2":
                # 文件順序第一個 = 主商品；相關商品排在後面
                chosen = kept[0]
                if len(vals) >= 3:
                    others = [v for v in vals if v != chosen] or vals
                    med = statistics.median(others)
                    if med and max(chosen / med, med / chosen) > self._PRICE_R2_OUTLIER:
                        log.append(f"{rule}=({detail})→首個 {chosen} 離群"
                                   f"（其餘中位數 {med:.0f}）")
                        continue
            else:
                spread = max(vals) / min(vals)
                if spread > self._PRICE_SPREAD_MAX:
                    log.append(f"{rule}=({detail})→一致性不足 max/min={spread:.1f}")
                    continue
                if rule == "R5" and len(vals) > self._PRICE_R5_MAX_DISTINCT:
                    log.append(f"{rule}=({detail})→泛用規則候選過多({len(vals)})")
                    continue
                # 排除後剩下的是同一件商品的定価／SALE 群集，取最小＝實際售價。
                chosen = min(vals)
            log.append(f"{rule}=({detail})✔取 {chosen}")
            picked[rule] = chosen
            # ★ 記整份候選，不只記 chosen。auctions.yahoo 的 R3 是 [3850, 4950]
            #   （現在価格／即決価格），規則取 min=3850 —— 各規則的 chosen 全都是
            #   3850，跨規則離散度是 1.0，只看 chosen 完全看不出問題。
            #   會出事的資訊在**規則內的候選清單**裡。
            cand_vals[rule] = vals

        # ★ _prior 是 JSON-LD／OG 已經取到的價（若有）。把它一起送進候選，
        #   跨規則離散度才涵蓋「結構化資料 vs DOM」這個最常出錯的軸線。
        if _prior:
            picked = dict(picked, PRIOR=int(_prior))
            cand_vals = dict(cand_vals, PRIOR=[int(_prior)])
        _note_price_candidates(picked, cand_vals)

        # 決策：與改版前逐字相同 —— R1→R5 第一個有值的就是答案。
        for rule in ("R1", "R2", "R3", "R4", "R5"):
            if rule in picked and rule != "PRIOR":
                chosen = picked[rule]
                extra = ""
                if len(picked) >= 2:
                    lo, hi = min(picked.values()), max(picked.values())
                    if lo > 0 and hi / lo > 1.0:
                        extra = f"｜跨規則 max/min={hi / lo:.3f}"
                print(f"[Generic] 取價 ¥{chosen:,}（{rule}）｜" + " ".join(log) + extra)
                return chosen

        print("[Generic] ⚠️ 取價失敗（寧可失敗不猜價）｜" + " ".join(log))
        return None
