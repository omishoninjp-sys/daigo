"""
人工接手深連結（handoff_url）
=============================

客人被擋下來、或爬取失敗時，訊息裡都寫著「麻煩用 LINE @544kaytb 傳給我們」。
**但客人要自己去找那個帳號、再自己把網址複製過去。** 這支產生一條可以直接點的
LINE 連結，對話框裡已經帶好他剛剛貼的商品網址。

前端支援情形（2026-09-08 抓線上 assets/daigo.js 實測，31,423 bytes 原始檔）：
    showError(msg, handoffUrl) 會在訊息底下加一條
    「用 LINE 幫我處理這件商品 →」的 <a>，用 createElement + textContent 建，
    **href 只收 https:**（`new URL(...)` 後檢查 protocol）。
  三個呼叫點都已經在讀 data.handoff_url：
    daikoSearch（blocked）／daikoManualOrder（blocked）／daikoCreateOrder（!success）
  🔴 也就是說**前端等這個欄位很久了，後端一直沒送** —— 那條連結從來沒出現過。

★ 為什麼不放進 scrapers/base.py：那份是爬取規則，這支是「客人被擋下來之後
  怎麼接手」的對客流程，跟怎麼抓網頁無關。混在一起下一個人會找不到。
"""
from urllib.parse import quote, urlparse

from config import LINE_OA_ID

# LINE 官方帳號的「開啟對話並帶入訊息」深連結。
#   https://line.me/R/oaMessage/{OA_ID}/?{URL-encoded 訊息}
# 訊息**沒有欄位名稱**，整段就是 query string 本身，所以要整段編碼。
_LINE_OA_MESSAGE = "https://line.me/R/oaMessage/{oa}/?{msg}"

# 訊息長度上限。LINE 對深連結長度沒有公開的硬限制，但瀏覽器與 LINE app
# 對超長 URL 的行為不一致，而客人貼的網址可能帶一長串追蹤參數。
# 超過就截斷 —— 截斷的訊息仍然打得開對話框，客人再補一句就好；
# 連結整條壞掉的話按鈕點下去是死的，那更糟。
_MAX_MESSAGE_CHARS = 900

_DEFAULT_PREFIX = "我想代購這個商品，麻煩幫我看一下："


def _usable(url: str) -> bool:
    """只有真的像商品網址的東西才值得帶進訊息裡。"""
    try:
        u = urlparse((url or "").strip())
    except Exception:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def line_handoff_url(source_url: str, prefix: str = _DEFAULT_PREFIX) -> str | None:
    """
    產生帶好商品連結的 LINE 對話深連結。

    拿不到可用的網址就回 **None** —— 呼叫端把 None 直接放進回應即可，
    前端 `if (handoffUrl)` 會跳過，行為與沒有這個欄位時完全相同。

    ★ 刻意不 raise：這是錦上添花的欄位，壞掉不可以讓整個回應失敗。
    """
    try:
        if not LINE_OA_ID:
            return None
        url = (source_url or "").strip()
        if not _usable(url):
            return None
        msg = f"{prefix}\n{url}" if prefix else url
        if len(msg) > _MAX_MESSAGE_CHARS:
            msg = msg[:_MAX_MESSAGE_CHARS]
        # safe="" —— 冒號、斜線、換行全部要編碼，它們是訊息內容不是網址結構
        return _LINE_OA_MESSAGE.format(oa=quote(LINE_OA_ID, safe="@"),
                                       msg=quote(msg, safe=""))
    except Exception:
        return None
