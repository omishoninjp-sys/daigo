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
_BLOCK_MARKERS = (
    "access denied", "403 forbidden", "captcha", "recaptcha", "bot detected",
    "are you a human", "cloudflare", "cf-ray", "akamai", "reference #",
    "attention required", "unusual traffic", "アクセスが拒否",
)

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
                  "block_hint": False, "gone_hint": False})
    except Exception as e:
        print(f"[ScrapeLog] start 失敗（略過）: {type(e).__name__}: {e}")


def note_http(status, body: str = "") -> None:
    """Source 拿到 HTTP 回應時呼叫。body 只用來看特徵，不會被存下來。"""
    try:
        state = _ctx.get()
        if state is None:
            return
        if status is not None:
            state["http_status"] = int(status)
        if body:
            low = body[:4000].lower()
            if any(m in low for m in _BLOCK_MARKERS):
                state["block_hint"] = True
            if any(m.lower() in low for m in _GONE_MARKERS):
                state["gone_hint"] = True
    except Exception as e:
        print(f"[ScrapeLog] note_http 失敗（略過）: {type(e).__name__}: {e}")


def note_source(name: str) -> None:
    """記下實際命中的 Source（httpx / official_api / selenium / playwright）。"""
    try:
        state = _ctx.get()
        if state is not None and name:
            state["source"] = str(name)
    except Exception as e:
        print(f"[ScrapeLog] note_source 失敗（略過）: {type(e).__name__}: {e}")


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
    host = (urlparse(url or "").hostname or "").lower()
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
            "platform_id": platform_id or getattr(product, "platform_id", "") or "",
            "source": state.get("source", ""),
            "ok": ok,
            "failure_kind": "" if ok else classify_failure(
                http_status=http_status, error=error, timed_out=timed_out,
                block_hint=bool(state.get("block_hint")),
                gone_hint=bool(state.get("gone_hint")),
                got_page=got_page,
            ),
            "http_status": http_status,
            "elapsed_ms": int(elapsed_ms) if elapsed_ms is not None else None,
            "error_brief": _error_brief(error),
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
