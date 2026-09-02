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
from scrapers.base import ProductInfo, normalize_price


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
            if not product.title or not product.price_jpy:
                self._extract_generic(soup, product)

            if product.price_jpy and (product.price_jpy < 100 or product.price_jpy > 1000000):
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
                            self._note_selenium_settled(html)
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
    def _note_selenium_settled(html: str) -> None:
        """
        頁面載完但小於 5000 —— 只在**命中擋頁特徵**時留一句話。

        ★ 這是訊號不是控制流：不論記不記，上面都照樣回傳 html。
          它要回答的問題是「httpx 被擋但瀏覽器過得去」還是「兩條路都被擋」——
          後者才需要花錢買住宅代理，前者不用。
          dior.com 兩種都出現過（同一天有 4 筆 http=403 卻 ok=True，
          也有 5 筆連 Selenium 都拿不到內容），分不出來就會買錯東西。
        """
        try:
            if _has_block_markers(html):
                print("[Generic] ⚠️ Selenium 取得的頁面命中擋頁特徵 —— "
                      "httpx 與瀏覽器兩條路都不通")
                _note_error("Selenium 也被擋（httpx 與瀏覽器兩條路都不通，"
                            "需要住宅代理）", "Selenium")
        except Exception:
            pass          # 訊號壞掉不可以影響抓取結果

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
                        if p and 100 <= p <= 1000000:
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
        if not product.price_jpy:
            product.price_jpy = self._find_price_in_html(soup)

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
    _PRICE_MIN = 100
    _PRICE_MAX = 1_000_000
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

    def _find_price_in_html(self, soup) -> int | None:
        # ── 先移除刪除線元素（原價），避免抓到劃掉的舊價
        for tag in soup.find_all(['del', 's', 'strike']):
            tag.decompose()

        text = soup.get_text()
        cands = self._price_candidates(soup, text)

        log = []
        for rule in ("R1", "R2", "R3", "R4", "R5"):
            raw = cands[rule]
            if not raw:
                log.append(f"{rule}=空")
                continue

            kept, dropped = [], []
            for v, reject_kw in raw:
                if v is None:
                    continue
                if not (self._PRICE_MIN <= v <= self._PRICE_MAX):
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
            print(f"[Generic] 取價 ¥{chosen:,}（{rule}）｜" + " ".join(log))
            return chosen

        print("[Generic] ⚠️ 取價失敗（寧可失敗不猜價）｜" + " ".join(log))
        return None
