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
                  "block_hint": False, "gone_hint": False})
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
                     got_page: bool = False) -> str:
    """
    回傳 blocked / parse_failed / not_found / timeout / other。

    判斷順序有意義：先看逾時（最明確），再看 HTTP 狀態碼，再看內容特徵，
    最後才是「拿到頁面卻抽不出欄位」。
    """
    if timed_out:
        return "timeout"

    err_name = type(error).__name__ if error is not None else ""
    err_text = f"{err_name}: {error}".lower() if error is not None else ""
    if "timeout" in err_text or "timedout" in err_text:
        return "timeout"

    if http_status in (403, 429):
        return "blocked"
    if http_status in (404, 410):
        return "not_found"

    if block_hint:
        return "blocked"
    if gone_hint:
        return "not_found"

    if got_page or http_status == 200:
        return "parse_failed"

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
