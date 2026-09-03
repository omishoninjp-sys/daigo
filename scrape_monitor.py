"""
爬取監控紀錄（spec-scrape-monitoring.md 第一～三節）
====================================================
目的：generic 通用抓取器佔營收約 40%、涵蓋 260 個網域，但失敗的爬取不留痕跡，
分母不存在。這支把每次爬取（成功與失敗都要）寫成 append-only JSONL。

**目前是上線步驟第 1 步：只記錄，不寄信。** 摘要信與即時警報（規格第四、五節）
還沒接，等跑兩天、確認分類準不準再開。

存哪裡（規格第三節：不開新的資料庫服務）：
  優先 /data/scrape_log/YYYY-MM-DD.jsonl（Zeabur Volume，持久）
  沒掛 Volume 就退回 ./scrape_log/YYYY-MM-DD.jsonl（可跑，重部署會清空）
一天一檔，之後寄完摘要可直接刪當日檔。

**fail-safe 是硬性要求**：記錄失敗絕不能影響爬取本身。所有對外函式都整段包
try/except，出錯 print 一行就繼續，永遠不 raise。

不記客人身分／IP／session。url 只留 path，不留 query string（可能含個資）。

main.py 用法：
    import scrape_monitor
    scrape_monitor.start(url)                       # 爬取前
    ...
    scrape_monitor.record(url, product=p, elapsed_ms=n)          # 成功/失敗都呼叫
    scrape_monitor.record(url, error=e, elapsed_ms=n, timed_out=True)

Source 端（選填，能提供就提供，classification 會更準）：
    scrape_monitor.note_http(resp.status_code, resp.text)
    scrape_monitor.note_source("YahooStoreHttpxSource")
    scrape_monitor.note_gone()        # 確定查無／下架（API 回 200 空清單也算）
    scrape_monitor.note_page_settled(len(html))   # 瀏覽器把頁面載完了，多大
"""
import os
import re
import json
import contextvars
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

# 每次爬取的暫存狀態。放「可變 dict」而不是每次 set()，因為 asyncio.wait_for 會把
# coroutine 包成 Task、複製一份 context —— 複製的是 dict 的參照，所以 Source 在
# 子 task 裡改 dict，外層 record() 讀得到；若在子 task 裡 set() 則外層讀不到。
_ctx: contextvars.ContextVar = contextvars.ContextVar("scrape_monitor_ctx", default=None)

_LOG_DIR_CANDIDATES = [
    os.environ.get("SCRAPE_LOG_DIR", "").strip() or "/data/scrape_log",
    "./scrape_log",
]

_log_dir: str = ""

# 被擋的內容特徵（規格第二節：Akamai / Cloudflare / reCAPTCHA）
#
# ★ 分成「強」「弱」兩組，弱的只有在頁面很小的時候才算數。
#   2026-08-30 實測 coldbeer.jp：Shopify 商店的正常頁面內嵌
#   <script id="captcha-bootstrap">，整頁 433KB 也照樣命中 "captcha"。
#   照舊寫法，每一家 Shopify 日本商店只要抓失敗就會被分類成 blocked，
#   然後我們去買住宅代理解一個根本不存在的問題。
#   真正的擋頁（Akamai / Cloudflare / reCAPTCHA challenge）都是很小的一頁，
#   所以弱特徵加一道大小門檻，強特徵（明講被拒絕的字樣）才不看大小。
_BLOCK_MARKERS_STRONG = (
    "access denied", "403 forbidden", "bot detected", "are you a human",
    "attention required", "unusual traffic", "アクセスが拒否",
)
_BLOCK_MARKERS_WEAK = (
    "captcha", "recaptcha", "cloudflare", "cf-ray", "akamai", "reference #",
)
_WEAK_MARKER_MAX_BYTES = 50_000

# 明確下架/完售的頁面字樣（規格第二節：not_found 不只看 404）
_GONE_MARKERS = (
    "販売終了", "販売を終了", "取扱終了", "お取り扱いを終了", "掲載終了",
    "この商品は現在お取り扱いできません", "商品が見つかりません",
    "ページが見つかりません", "お探しのページは見つかりません",
    "sold out", "product not found", "page not found",
)


# ─────────────────────────────────────────────────────────────────────
# 每次爬取的狀態
# ─────────────────────────────────────────────────────────────────────
def start(url: str) -> None:
    """爬取開始前呼叫，建立本次爬取的暫存狀態。"""
    try:
        _ctx.set({"url": url, "http_status": None, "source": "",
                  "platform_id": "", "errors": [], "redirect_to": "",
                  "block_hint": False, "gone_hint": False,
                  "error_kinds": []})
    except Exception as e:
        print(f"[ScrapeLog] start 失敗（略過）: {type(e).__name__}: {e}")


def note_http(status, body: str = "", final_url: str = "") -> None:
    """
    Source 拿到 HTTP 回應時呼叫。body 只用來看特徵，不會被存下來。

    final_url 是跟隨轉址後的最終網址（httpx 的 resp.url）。轉到別的主機通常代表
    我們根本沒抓到商品頁 —— order.mandarake.co.jp 的每個路徑都 302 到
    www.mandarake.co.jp 首頁，抓回來的是 200 的公司首頁，看起來像「解析失敗」，
    其實是被轉走。這件事不記下來，光看紀錄永遠判斷不出來。
    """
    try:
        state = _ctx.get()
        if state is None:
            return
        if status is not None:
            state["http_status"] = int(status)
        if final_url:
            src_host = _domain(state.get("url", ""))
            dst_host = _domain(final_url)
            if dst_host and src_host and dst_host != src_host:
                state["redirect_to"] = dst_host
        if body:
            low = body[:8000].lower()
            strong = any(m in low for m in _BLOCK_MARKERS_STRONG)
            weak = (len(body) < _WEAK_MARKER_MAX_BYTES
                    and any(m in low for m in _BLOCK_MARKERS_WEAK))
            if strong or weak:
                state["block_hint"] = True
            if any(m.lower() in low for m in _GONE_MARKERS):
                state["gone_hint"] = True
    except Exception as e:
        print(f"[ScrapeLog] note_http 失敗（略過）: {type(e).__name__}: {e}")


def note_gone() -> None:
    """
    Source 確定「這件商品不存在／已下架」時呼叫。

    ★ 為什麼需要這支（2026-09-02，GU）：
      note_http() 的 gone 判定是掃**頁面內容**有沒有「販売終了」「商品が見つかりません」
      那類字樣。但有些站的內部 API 對查無商品是回 **HTTP 200 + 空清單**，
      JSON 裡一個字都沒有 —— GU 的 `result.items: []` 就是這樣。
      於是 classify_failure 走到「got_page or http_status == 200」那行，
      判成 parse_failed（＝我們的解析壞了，該去修 parser），
      實際上是 not_found（＝商品下架，沒東西可修）。
      這兩種的處置完全相反，混在一起摘要就會指錯方向。

    ★ 不新增 classify_failure 的參數 —— gone_hint 本來就是為這件事存在的，
      而且它在判斷順序上**排在 http_status == 200 之前**，設了就會贏。

    只設旗標，不寫訊息；原因請 Source 自己另外呼叫 note_error()，
    兩件事分開才看得出「分類」與「說明」各自從哪裡來。
    """
    try:
        state = _ctx.get()
        if state is not None:
            state["gone_hint"] = True
    except Exception as e:
        print(f"[ScrapeLog] note_gone 失敗（略過）: {type(e).__name__}: {e}")


# 🔴 兩份清單，各自回答不同的問題（2026-09-03）。爬取端的那一份在
#   scrapers/platform.BLOCKED_HTTP_STATUS —— **刻意不 import**：
#   監控壞掉不可以影響爬取（同 _BLOCKED_STRONG 那組的理由）。
#   改成用一支測試釘住兩邊一致，分岔就會紅。
#
# A「這次失敗是被擋造成的嗎」→ failure_kind 用這份
_BLOCKED_HTTP_STATUS = (401, 403, 429)

# B「要不要買住宅代理」→ note_page_settled 用這份
#   ★ 429 刻意不在裡面：那是節流，重試就會過，買代理沒有用。
#     這份必須是 A 的子集 —— 測試有釘。
_PROXY_NEEDED_HTTP_STATUS = (401, 403)
_SETTLED_SMALL_BYTES = 5000


def note_page_settled(size) -> None:
    """
    瀏覽器那條路把頁面載完了，內容有多大。**呼叫端只回報事實，判斷在這裡做。**

    ★ 為什麼判斷放這裡：爬取路徑（scrapers/generic.py）不需要知道 httpx
      拿到什麼狀態碼 —— 那是監控自己用 note_http 記下來的。兩個訊號在這裡
      合流，爬取那邊仍然不知道監控的存在，監控壞掉也影響不到抓取。

    🔴 判準是**結構**不是字串（2026-09-03）：
      httpx 拿 401/403 **且** 瀏覽器載完仍不到 5KB → 兩條路都不通。

      實測 dior.com：Zeabur 上 httpx 403、Selenium 拿到約 3KB 的
      "Page unavailable"；同一個網址在住宅 IP 拿得到 1.1MB 的完整商品頁
      （JSON-LD 有 ¥540,000）。**那確實是被擋，但擋頁不自報身分** ——
      access denied / captcha / cloudflare 一個字樣都沒有。
      所以不能靠特徵字：枚舉軟性擋頁的說法救得了 dior，救不了下一家。
      （同一個病早上才踩過：generic 取價用 min() 挑候選，換一家就崩。）

    ★ 這是訊號不是控制流：failure_kind 本來就會因為 403 判成 blocked，
      這裡只是把「值不值得買住宅代理」這個問題回答清楚 ——
      httpx 被擋但瀏覽器過得去的網域，買了代理也沒有多賺。
    """
    try:
        state = _ctx.get()
        if state is None:
            return
        try:
            n = int(size)
        except (TypeError, ValueError):
            return
        if n >= _SETTLED_SMALL_BYTES:
            return                      # 瀏覽器拿得到內容 → 不是兩邊都不通
        status = state.get("http_status")
        if status not in _PROXY_NEEDED_HTTP_STATUS:
            return                      # 沒有被擋的狀態碼 → 頁面小只是頁面小
        note_error(f"兩條路都不通：httpx HTTP {status}，瀏覽器載完仍只有 "
                   f"{n / 1024:.1f}KB —— 這個網域要住宅代理才抓得到", "Blocked")
        print(f"[ScrapeLog] 🔴 兩條路都不通（httpx {status} + 瀏覽器 {n} bytes）"
              f"—— 這個網域需要住宅代理")
    except Exception as e:
        print(f"[ScrapeLog] note_page_settled 失敗（略過）: {type(e).__name__}: {e}")


def note_source(name: str) -> None:
    """記下實際命中的 Source（httpx / official_api / selenium / playwright）。"""
    try:
        state = _ctx.get()
        if state is not None and name:
            state["source"] = str(name)
    except Exception as e:
        print(f"[ScrapeLog] note_source 失敗（略過）: {type(e).__name__}: {e}")


def note_platform(platform_id: str) -> None:
    """
    記下這次走到哪支 Platform。

    ★ 不可以只從 ProductInfo 上拿：timeout 與例外路徑根本沒有 product，
      那正是最需要知道「是哪支在爬」的時候（2026-08-30 的 coldbeer.jp 那筆
      就是 60 秒逾時、platform_id 空白）。
    """
    try:
        state = _ctx.get()
        if state is not None and platform_id:
            state["platform_id"] = str(platform_id)
    except Exception as e:
        print(f"[ScrapeLog] note_platform 失敗（略過）: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────
# note_error 當下就把分類算好，不要事後拿字串去猜
# ─────────────────────────────────────────────────────────────────────
# 🔴 為什麼分類要在這裡做，而不是在 classify_failure 裡比對訊息（2026-09-03）
#   Source 一律用 return None 表示失敗，網路層的原因只能靠 note_error 補回來
#   （見 .claude/rules/scraping-price.md）。那些原因**進不了 failure_kind**：
#   classify_failure 只看 record() 傳進來的 error 參數，而那個參數是 None。
#   於是「httpx 逾時但最後整條失敗」的紀錄，error_brief 寫「逾時」，
#   failure_kind 卻是 other —— 而摘要與警報看的是 failure_kind。
#
#   把分類放在 note_error 當下有三個好處：
#     · 字串比對只存在**一個地方**，不會散到 classify_failure 去
#     · 例外物件用**型別名**精確比對（ASCII、不是子字串猜）
#     · classify_failure 維持純函式，它那 11 條分類表測試一條都不用改
#
# ★ 字串比對用 startswith 錨定開頭，比對的是**我們自己產生的** brief
#   （scrapers/platform.py 的三個 helper）。措辭一改分類就會靜默失效，
#   所以有一支測試把兩邊綁死：期望值由 http_fail_brief(403) 現算出來。
_NOTE_KIND_PREFIX = (
    ("被擋：HTTP ", "blocked"),
    ("頁面不存在：HTTP ", "not_found"),
    ("逾時（", "timeout"),
)

# 例外物件走型別名，不碰訊息內容
_TIMEOUT_TYPES = (
    "TimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout",
    "WriteTimeout", "PoolTimeout",
)


def _note_kind(error, brief: str) -> str:
    """
    這一則 note_error 對應到哪個 failure_kind；對不上回空字串。

    ★ 刻意**不映射**的幾類：
      連線失敗（ConnectError）  多半是網址錯或站掛了，不是被擋也不是逾時
      非 200（500 之類）        站的暫時性錯誤，映到哪一類都不對
      引擎沒有 X()              那是能力警告，不是這次失敗的原因
      未設 YAHOO_APP_ID         同上，是設定問題
      硬要給它們一個分類，只會讓摘要指向錯的地方。
    """
    try:
        if not isinstance(error, str):
            return "timeout" if type(error).__name__ in _TIMEOUT_TYPES else ""
        for prefix, kind in _NOTE_KIND_PREFIX:
            if brief.startswith(prefix):
                return kind
    except Exception:
        pass
    return ""


def note_error(error, where: str = "") -> None:
    """
    Source 內部把例外吞掉時，把失敗原因交給監控。

    ★ 這是 error_brief 全空的根因：Platform.fetch 對每個 Source 都
      try/except + print，然後回傳一個空的 ProductInfo，上層看起來是「成功回傳」，
      record() 收到 error=None，於是 14 筆失敗沒有一筆說得出原因。
    只留最後 3 個，join 起來仍受 200 字上限限制。
    """
    try:
        state = _ctx.get()
        if state is None:
            return
        brief = error if isinstance(error, str) else _error_brief(error)
        brief = (brief or "").replace("\n", " ").strip()
        if not brief:
            return
        # ★ 一定要在加上 where 前綴**之前**算分類 —— 前綴會讓 startswith 失效
        kind = _note_kind(error, brief)
        if kind:
            kinds = state.setdefault("error_kinds", [])
            if kind not in kinds:
                kinds.append(kind)
        if where:
            brief = f"{where}: {brief}"
        errs = state.setdefault("errors", [])
        if brief not in errs:
            errs.append(brief)
        del errs[:-3]
    except Exception as e:
        print(f"[ScrapeLog] note_error 失敗（略過）: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────
# 失敗分類（規格第二節：在寫入時就分好，不要事後看字串猜）
# ─────────────────────────────────────────────────────────────────────
def classify_failure(http_status=None, error=None, timed_out: bool = False,
                     block_hint: bool = False, gone_hint: bool = False,
                     got_page: bool = False, noted_kinds=()) -> str:
    """
    回傳 blocked / parse_failed / not_found / timeout / other。

    判斷順序有意義：先看逾時（最明確），再看 HTTP 狀態碼，再看內容特徵，
    最後才是「拿到頁面卻抽不出欄位」。

    noted_kinds 是 Source 透過 note_error 補回來的分類（state["error_kinds"]）。
    ★ 它**只在既有邏輯什麼都判斷不出來時**才會被用到 —— 見函式最後。
      這樣可以證明：對所有原本不是 other 的輸入，行為一個字都沒變。
    """
    if timed_out:
        return "timeout"

    err_name = type(error).__name__ if error is not None else ""
    err_text = f"{err_name}: {error}".lower() if error is not None else ""
    if "timeout" in err_text or "timedout" in err_text:
        return "timeout"

    # ★ 2026-09-03：401 本來不在這裡，於是「被擋」會落到下面的 got_page
    #   變成 parse_failed（＝我們的解析壞了），查的人會往完全錯的方向去。
    #   http_fail_brief 與 note_page_settled 兩邊本來就把 401 當被擋，
    #   只有這裡沒有 —— 三處對同一件事有兩種認定，那是 bug 不是設計。
    if http_status in _BLOCKED_HTTP_STATUS:
        return "blocked"
    if http_status in (404, 410):
        return "not_found"

    if block_hint:
        return "blocked"
    if gone_hint:
        return "not_found"

    if got_page or http_status == 200:
        return "parse_failed"

    # ★ 走到這裡代表既有訊號什麼都判斷不出來（本來一律回 other），
    #   才輪到 Source 自己 note 出來的分類。**gate 放在最後一行**是刻意的：
    #   上面每個分支都是 return，新程式碼碰不到它們，所以
    #   「原本不是 other 的輸入，結果完全不變」是可以窮舉證明的。
    # ★ 取**最嚴重**的，不取最後一個：note_error 只留最後 3 則，
    #   最後一則往往是最弱的那層退路（generic Playwright），資訊量最低。
    #   順序抄本函式自己的（403/429 先於 404/410），處置成本也同向 ——
    #   被擋要花錢買代理，逾時只要觀察。
    # ★ 整段包 try：noted_kinds 是輔助資訊，壞掉不可以害**整筆紀錄**寫不出來。
    #   `in` 對非序列會拋 TypeError，而它會一路穿透到 record() 的外層 except ——
    #   跟 2026-09-02 _safe_price 那次一模一樣：為了兩個診斷欄位賠掉整筆，
    #   而失敗路徑正是最需要紀錄的時候。
    try:
        for kind in ("blocked", "not_found", "timeout"):
            if kind in (noted_kinds or ()):
                return kind
    except Exception:
        pass

    # 🔴 已知的誤標風險（2026-09-03，這次刻意不修）
    #   「httpx 逾時 → 退路用瀏覽器抓到頁 → 解析失敗」會落到這裡並讀到
    #   timeout，但真正該修的是解析。原因是 got_page 目前只看 http_status，
    #   而 Selenium 成功抓到 HTML **不會**設 http_status
    #   （MUJI 那 4 筆 http_status 全是 None 就是證據）。
    #   要根治得讓 Selenium 那條也回報「我拿到頁面了」，那是另一個改動；
    #   在那之前，timeout 在這條路徑上的意思是「過程中有逾時」而不是
    #   「這次失敗是逾時造成的」。
    return "other"


# ─────────────────────────────────────────────────────────────────────
# 寫入
# ─────────────────────────────────────────────────────────────────────
def _pick_dir() -> str:
    global _log_dir
    if _log_dir:
        return _log_dir
    for path in _LOG_DIR_CANDIDATES:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".probe")
            with open(probe, "a", encoding="utf-8"):
                pass
            os.remove(probe)
            _log_dir = path
            print(f"[ScrapeLog] 紀錄目錄：{path}")
            return _log_dir
        except Exception:
            continue
    _log_dir = "."
    print("[ScrapeLog] ⚠️ 取不到可寫目錄，退回目前工作目錄")
    return _log_dir


def _domain(url: str) -> str:
    """
    正規化網域：小寫、去 www.、去尾端的點。

    尾端點（jp.mercari.com.）是合法的 FQDN 寫法，指的是同一個站，不去掉就會在
    統計裡多出一列。開頭是點或中間有空 label 的壞主機名**故意保持原樣** ——
    那是客人貼錯的證據，安靜地正規化成 mercari.com 只會讓人再也查不出原因；
    這種連結該由 detect_invalid_link() 擋在爬取之前。
    """
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _url_path(url: str) -> str:
    """只留 path，不留 query string（規格：可能含個資或 session）。"""
    try:
        return (urlparse(url or "").path or "")[:300]
    except Exception:
        return ""


def _error_brief(error) -> str:
    """例外類型 + 訊息前 200 字，不存完整 traceback。"""
    if error is None:
        return ""
    try:
        msg = str(error).replace("\n", " ").strip()
    except Exception:
        msg = ""
    return f"{type(error).__name__}: {msg}"[:200]


def _failure_brief(error, product, state) -> str:
    """
    失敗原因，依可信度排序取第一個拿得到的：

      1. 上層真的收到的例外
      2. Source 吞掉、透過 note_error() 補回來的例外
      3. 被轉址到別的主機（看起來像解析失敗，其實沒抓到商品頁）
      4. 什麼例外都沒有 —— 那就講清楚「哪個欄位沒抓到」，
         parse_failed 要修的正是這個

    永遠 200 字以內，永遠不含 traceback（規格第一節）。
    """
    if error is not None:
        return _error_brief(error)

    noted = " | ".join(state.get("errors") or [])
    if noted:
        return noted[:200]

    redirect_to = state.get("redirect_to") or ""
    if redirect_to:
        return f"Redirected: {_domain(state.get('url', ''))} → {redirect_to}"[:200]

    title = getattr(product, "title", "") if product is not None else ""
    price = getattr(product, "price_jpy", None) if product is not None else None
    img = getattr(product, "image_url", "") if product is not None else ""
    return (f"NoFields: 抽不到必要欄位（title={'有' if title else '無'}, "
            f"price={'有' if price else '無'}, image={'有' if img else '無'}）")[:200]


def _warnings_brief(state) -> str:
    """成功筆的警告摘要：Source 退路、降級、圖片沒抓到之類。取不到回空字串。"""
    try:
        noted = " | ".join(state.get("errors") or [])
        return noted.replace(chr(10), " ").strip()[:200]
    except Exception:
        return ""


def _safe_price(product):
    """
    取 product.price_jpy 轉 int；取不到或不合理一律回 None。

    ★ 取值**連同 getattr 一起**包在 try 裡。`getattr(o, k, default)` 的預設值
      只吃 AttributeError —— 屬性是 property 且內部拋別的例外時會直接穿透，
      被 record() 的外層 except 接住，**整筆紀錄就沒了**。
      而 timeout／例外路徑正是最需要紀錄的時候，不可以為了兩個內容欄位
      把整筆賠掉。
    ★ 不寫 0：0 與「沒有」意義不同，混在一起之後
      「同網域商品價格全部相同」那類掃描會被一堆 0 污染。
    """
    try:
        v = getattr(product, "price_jpy", None)
        if v is None or isinstance(v, bool):
            return None
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None


def _safe_brand(product):
    """取 product.brand 轉字串；空值回 None。限長避免異常長的值撐大 JSONL。"""
    try:
        v = getattr(product, "brand", None)
        if not v or not isinstance(v, str):
            return None
        s = v.replace(chr(10), " ").replace(chr(13), " ").strip()
        return s[:80] or None
    except Exception:
        return None


def record(url: str, product=None, error=None, elapsed_ms=None,
           timed_out: bool = False, platform_id: str = "") -> None:
    """
    爬取結束時記一筆（成功與失敗都要）。永遠不 raise。

    ok 的定義沿用 ProductInfo.is_valid（有標題且有正價）—— 與 /api/scrape
    對客人回「成功」的判準一致，不然算出來的成功率跟客人的體感對不上。
    """
    try:
        state = _ctx.get() or {}
        http_status = state.get("http_status")
        got_page = http_status is not None

        ok = bool(product is not None and getattr(product, "is_valid", False))
        if not ok and product is not None and getattr(product, "title", ""):
            # 有標題沒價格也算失敗（客人下不了單），但這屬於解析問題
            got_page = True

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "domain": _domain(url),
            # platform_id 三段退路：呼叫端明講 → product 上的 → Platform 自己回報的。
            # 最後那段是 timeout／例外路徑唯一拿得到的來源。
            "platform_id": (platform_id
                            or getattr(product, "platform_id", "")
                            or state.get("platform_id", "")
                            or ""),
            "source": state.get("source", ""),
            "ok": ok,
            "failure_kind": "" if ok else classify_failure(
                http_status=http_status, error=error, timed_out=timed_out,
                block_hint=bool(state.get("block_hint")),
                gone_hint=bool(state.get("gone_hint")),
                got_page=got_page,
                # ★ Source 吞掉的失敗原因（note_error 當下就分好類了）。
                #   只在上面全部判斷不出來時才會被採用。
                noted_kinds=state.get("error_kinds") or (),
            ),
            # ── 抓到的內容本身（規格第一節只記「爬取過程」，記不到「值對不對」）
            # ★ 2026-09-02 加。今天三個實際損失（brand 污染 67% 持續三個月、
            #   取價抓到代引手数料、metafield 原價是垃圾值）全部是 ok=True 的
            #   成功爬取，五個 failure_kind 一個都涵蓋不到，而這份 log 當時
            #   連價格都沒記 —— 所有調查只能繞道 Shopify Admin API。
            #   有了這兩欄，「同一網域的商品價格全部相同」這類掃描才做得起來
            #   （suqqu 9 件全 ¥550 就是這樣挖出來的）。
            # ★ timeout 與例外路徑沒有 product，這時兩欄寫 null ——
            #   **不可以因為取不到就整筆不寫**，那正是最需要紀錄的時候。
            "price_jpy": _safe_price(product),
            "brand": _safe_brand(product),
            "http_status": http_status,
            "elapsed_ms": int(elapsed_ms) if elapsed_ms is not None else None,
            "error_brief": "" if ok else _failure_brief(error, product, state),
            # ── 成功但走得很勉強：退路、降級、圖片沒抓到之類 ──────────────
            # ★ 2026-09-02 加。error_brief 在 ok=True 時被強制清空，於是
            #   **成功路徑上的 note_error 全部被丟掉** —— 掃過 24 個呼叫點，
            #   有 14 個命中後最終仍可能成功：
            #     platform.py:91   每個 Source 失敗都 note_error，只要後面
            #                      任一個成功，前面全部消失
            #     uniqlo.py        四層 fallback，前三層的原因（含 API 403
            #                      機房 IP 被擋）只要第四層成功就查不到
            #     amazon.py:146    短連結抽不到 ASIN，改用轉址後網址繼續跑
            #     jsonld.py:270-302 圖片 base64 沒抓到，但商品本身有效
            #   MUJI 那件事（圖片機制靜默消失六週）就是這一類。
            #
            # ★ 只在 ok=True 時寫，與 error_brief **互斥** ——
            #   兩者同一份 state["errors"]，都寫會讓同一句話出現兩次。
            #   警告只在成功筆有意義：失敗筆該看的是 error_brief。
            # ★ 限長 200 字，比照 error_brief。note_error 的 del errs[:-3]
            #   已經有天然上限（最多 3 條），限長是第二道。
            #   這是給人看的診斷摘要，不是完整紀錄。
            "warnings": _warnings_brief(state) if ok else "",
            "url_path": _url_path(url),
        }

        day = entry["ts"][:10]
        path = os.path.join(_pick_dir(), f"{day}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        tag = "✅" if ok else f"❌ {entry['failure_kind']}"
        print(f"[ScrapeLog] {tag} {entry['domain']} "
              f"platform={entry['platform_id'] or '-'} source={entry['source'] or '-'} "
              f"http={entry['http_status']} {entry['elapsed_ms']}ms")
    except Exception as e:
        # fail-safe：記錄壞掉不能影響爬取
        print(f"[ScrapeLog] ⚠️ 寫入失敗（略過，不影響爬取）: {type(e).__name__}: {e}")


def read_day(day: str = "") -> list:
    """讀某天的紀錄（給之後的摘要用；壞行直接跳過）。day 格式 YYYY-MM-DD。"""
    out = []
    try:
        day = day or datetime.now(timezone.utc).date().isoformat()
        path = os.path.join(_pick_dir(), f"{day}.jsonl")
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"[ScrapeLog] 讀取失敗: {type(e).__name__}: {e}")
    return out


# ─────────────────────────────────────────────────────────────────────
# 匯出用（/api/admin/scrape-log）
# 路徑規則（一天一檔、目錄怎麼選）只有這支模組知道，所以讀檔的入口留在這裡，
# 不要讓 main.py 自己去拼路徑。
# ─────────────────────────────────────────────────────────────────────
def log_dir() -> str:
    """回傳目前實際在用的紀錄目錄（/data/scrape_log 或退路）。"""
    try:
        return _pick_dir()
    except Exception:
        return ""


def recent_days(days: int = 2) -> list:
    """最近 N 天的日期字串（UTC，新到舊，含今天）。檔名用 UTC 日期，這裡也要用。"""
    try:
        n = max(1, int(days))
    except Exception:
        n = 1
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n)]


def days_back_from(day: str, days: int = 2) -> list:
    """
    從**指定的某一天**往回數 N 天（新到舊，含 day 本身）。

    ★ 與 recent_days() 的差別只有錨點，但那個差別會讓統計整個歸零：
      recent_days() 一律錨在「現在」。每日摘要報告的是**前一個 UTC 日**，
      拿 recent_days 去算「連續 N 天失敗」就是錨錯 ——
      排程在 01:00 UTC 跑，那時「今天」才過了一小時、通常一筆失敗都還沒有，
      failure_streaks 的第一個集合就是空集合，迴圈立刻 break，
      **整份 streaks 是空的，一個標記都不會出現**（2026-09-03 實測）。
    day 解析不出來時回空清單（沒有標記），不要退回 recent_days ——
    那等於把剛修好的錯又放回去。
    """
    try:
        n = max(1, int(days))
    except Exception:
        n = 1
    try:
        base = datetime.strptime((day or "").strip(), "%Y-%m-%d").date()
    except Exception:
        print(f"[ScrapeLog] days_back_from: day 解析失敗（回空清單）: {day!r}")
        return []
    return [(base - timedelta(days=i)).isoformat() for i in range(n)]


def read_raw(day: str = "") -> str:
    """
    讀某天的原始 JSONL 文字（匯出用）。

    與 read_day() 的差別：這支不解析、不丟壞行 —— 匯出要的是檔案裡實際長什麼樣，
    壞行本身也是要看的資訊。檔案不存在回空字串。
    """
    try:
        day = day or datetime.now(timezone.utc).date().isoformat()
        path = os.path.join(_pick_dir(), f"{day}.jsonl")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        print(f"[ScrapeLog] 讀取失敗: {type(e).__name__}: {e}")
        return ""
