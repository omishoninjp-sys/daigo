"""
source_url 正規化 —— 重複下單煞車的 key（規則 f）。

**這支只算 key，不判斷、不擋任何東西。** 第一階段只記錄。

規則來源：`gyt-ops-analysis/backlog/TICKET-duplicate-order-brake.md` 附錄 F。
那份是 2026-09-07 用 12 個月完整訂單（Shopify 連接器 bulk，1,309 筆訂單／
2,308 個 line item）裡**730 筆取得到 `daigo.source_url`** 的 line item 實測出來的，
不是憑想像設計的。

規則 f 五步：

  1. 小寫 host、去開頭的 `www.`、scheme 一律當成 https、去掉 fragment
  2. 去掉尾斜線
  3. query string：**只移除已知追蹤參數的白名單**，其餘保留
  4. 對高流量站抽商品 ID 當 key（**只寫有實測依據的**）
  5. 沒有站規則就用第 1–3 步的結果，**不要猜**

🔴 第 3 步為什麼不可以整段砍 query
   資料裡有 8 個站把商品 ID 放在 query string，路徑本身不帶辨識資訊
   （tmrecords / amiami / gundam-base / jfa / gochio / plusmember /
   crux-onlinestore / committee.ibupzdza.homes）。
   amiami 那 9 筆**全部**落在 `path=/top/detail/detail`，實際是 6 件不同商品；
   整段砍會把它們合成 1 個 key，而那個 key 的成交狀況是「4 成交 + 5 未成交」
   —— 煞車看到這種混合值只會得到沒有意義的數字。

🔴 第 4 步為什麼非做不可
   Amazon 在資料裡有 7 種網址形態。同一個 ASIN，桌機版貼 `/dp/`、
   手機 App 版貼 `/gp/aw/d/`，會被算成兩個 key，**各自都達不到門檻**。
   x9 下單、0 成交的寶可夢 MEGA 30週年卡組正是 `/gp/aw/d/` 那一種，
   而原本只認 `/dp/` 的寫法漏掉了 10 筆。

🔴 沒有實測依據的 alias 一條都不要加
   附錄 B 的規則 d（憑想像加的路徑 alias）在 730 筆裡**額外合併了 0 組**：
   樂天 `/sp/`、ZOZO `/sp/` 都沒有任何一件商品同時以兩種形式出現過。
   所以那些 alias 一條都沒寫進來。GRL 的 `/item/{code}` 與
   `/disp/item/{code}` 是 2026-09-07 在站上實際點過、確認指向同一件商品的，
   只有它有依據，所以只有它在。

⚠️ 這裡面唯一「規則 f 這樣寫、但樣本沒驗到」的是 ZOZO 的
   `goods` 與 `goods-sale` 合併 —— 見 `_zozo_id()` 上的註解。
"""
import re
from urllib.parse import urlparse, parse_qsl, urlencode

# 規則版本。**改了正規化行為就要動這個字串**，否則新舊 key 混在同一份紀錄裡
# 算次數會錯（附錄 H 的 `key_rule_version` 欄位就是為了這件事）。
KEY_RULE_VERSION = "f1"


# ─────────────────────────────────────────────────────────────────────
# 第 3 步：追蹤參數白名單
# ─────────────────────────────────────────────────────────────────────
# ★ 白名單，不是黑名單。列在這裡的才砍，沒列到的一律保留 ——
#   保留一個沒用的參數只會多出一個 key（假分裂，會漏擋），
#   砍掉一個其實是商品 ID 的參數會把不同商品合成一個 key（假合併，會誤擋）。
#   兩種錯的代價不對稱，所以往「保留」那邊倒。
_TRACKING_EXACT = frozenset({
    "gclid", "fbclid", "yclid", "_gl", "srsltid", "scid", "rafcid",
    "s-id", "sc2id", "sc_i", "sc_e", "did", "ref", "ref_", "trflg",
    "bkts", "l-id",
})
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking(name: str) -> bool:
    low = (name or "").lower()
    return low in _TRACKING_EXACT or low.startswith(_TRACKING_PREFIXES)


def _clean_query(query: str) -> str:
    """
    只砍白名單裡的追蹤參數，其餘原樣保留，最後**排序**。

    排序的理由：`?a=1&b=2` 與 `?b=2&a=1` 是同一個頁面，不排序會變成兩個 key
    （假分裂）。排序不可能把不同商品合成同一個 key，所以是安全的方向。
    """
    pairs = [(k, v) for k, v in parse_qsl(query or "", keep_blank_values=True)
             if not _is_tracking(k)]
    pairs.sort()
    return urlencode(pairs)


# ─────────────────────────────────────────────────────────────────────
# 共用小工具
# ─────────────────────────────────────────────────────────────────────
def _host_is(host: str, domain: str) -> bool:
    """
    完整網域或其子網域才算命中。

    ★ 絕對不要改成 `domain in host`。`scrapers/base.py` 的 `_host_matches`
      有一模一樣的註解，理由也一樣：子字串比對會讓 "t.co" 命中
      tocco-closet.co.jp。這裡另外寫一份是為了讓這支保持零相依、可離線單測。
    """
    host = (host or "").lower().strip().rstrip(".")
    domain = (domain or "").lower().strip().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


# 語系前綴。只用來剝掉路徑的第一段，不影響其他判斷。
_LANG_SEGMENTS = frozenset({
    "zh", "zh-tw", "zh-cn", "zh-hk", "zh-hant", "zh-hans", "tw", "cn",
    "en", "en-us", "en-gb", "ja", "ja-jp", "jp", "ko", "ko-kr",
    "th", "vi", "id", "fr", "de", "es", "it", "pt", "ru",
})

_ASIN_RE = re.compile(r"^[A-Za-z0-9]{10}$")


# ─────────────────────────────────────────────────────────────────────
# 第 4 步：各站的商品 ID
#
# 每支回傳「商品 ID」或 None。None 就往下掉到第 5 步的通用結果 ——
# **抽不出來就不要硬抽**，硬抽出一個錯的 ID 比沒有 ID 更糟。
# ─────────────────────────────────────────────────────────────────────
def _amazon_id(segs: list[str]) -> str | None:
    """
    /dp/{ASIN}、/gp/product/{ASIN}、/gp/aw/d/{ASIN}，三種都要。

    先剝 `/-/zh/`、`/-/en/` 這種語系前綴（segments 會長成 ['-', 'zh', ...]）。
    `/{日文標題 slug}/dp/{ASIN}/ref=…` 也吃得到，因為是掃描而不是看位置。

    資料裡的 7 種形態與筆數（附錄 D）：
      /dp/{ASIN} 24、/{slug}/dp/{ASIN}/ref= 13、/-/zh/dp/{ASIN} 9、
      /-/zh/gp/aw/d/{ASIN} 9、/-/en/gp/aw/d/{ASIN} 1、/gp/product/{ASIN} 1
    """
    s = list(segs)
    if s and s[0] == "-" and len(s) >= 2:
        s = s[2:]
    for i, seg in enumerate(s):
        low = seg.lower()
        cand = ""
        if low == "dp" and i + 1 < len(s):
            cand = s[i + 1]
        elif (low == "product" and i >= 1 and s[i - 1].lower() == "gp"
              and i + 1 < len(s)):
            cand = s[i + 1]
        elif (low == "d" and i >= 2 and s[i - 2].lower() == "gp"
              and s[i - 1].lower() == "aw" and i + 1 < len(s)):
            cand = s[i + 1]
        if cand and _ASIN_RE.match(cand):
            return cand.upper()
    return None


def _mercari_id(segs: list[str]) -> str | None:
    """
    /item/{id} 與 /shops/product/{id}，先剝語系前綴。

    資料裡：/item/{id} 119 筆、/zh-TW/item/{id} 6 筆、
    /shops/product/{id} 14 筆、/zh-TW/shops/product/{id} 5 筆、/en/… 1 筆。

    ⚠️ Mercari 一物一頁、**售出即 404**，附錄 E 建議把它排除在煞車之外
       （同一個 URL 被重複下單代表兩個人搶同一件二手品，不是「這個商品難買」）。
       那是**第二階段擋不擋**的事，這裡照樣算 key ——
       第一階段要有資料才判斷得出來。用 `is_brake_suitable()` 標記。
    """
    s = list(segs)
    if s and s[0].lower() in _LANG_SEGMENTS:
        s = s[1:]
    if len(s) >= 2 and s[0].lower() == "item":
        return s[1]
    if len(s) >= 3 and s[0].lower() == "shops" and s[1].lower() == "product":
        return s[2]
    return None


def _zozo_id(segs: list[str]) -> str | None:
    """
    /shop/{shop}/goods/{id} → zozo.jp#{shop}/{id}

    🔴 **`goods-sale` 故意不合併進來**（2026-09-08 決定，規則 f 的條文原本寫了
       `goods(-sale)`，這裡刻意不照它）。
       理由：附錄 B 實測 35 個不重複 (shop, id) 裡 **0 個**同時出現兩種路徑，
       所以那條合併在 730 筆樣本上「合併了 0 組」—— 跟規則 d 那些
       **沒被觸發過的 alias 是同一類**，屬於「聽起來合理但沒有實例」。
       同一份附錄的結論就是「不要因為聽起來合理就加 alias」，
       這條不能對自己破例。

       真的遇到同一件商品兩種路徑再加，到時候會有實例可以釘成回歸案例。
       目前 `goods-sale` 會落到第 5 步的通用規則（`matched_site_rule` 為空），
       而 `matched_site_rule` 的空值比例本來就是要盯的指標（附錄 H），
       所以它真的開始出現時看得到。
    """
    if len(segs) >= 4 and segs[0].lower() == "shop" and segs[2].lower() == "goods":
        return f"{segs[1]}/{segs[3]}"
    return None


def _surugaya_id(segs: list[str]) -> str | None:
    """
    /product/detail/{id}。

    `/kaitori/` 是**收購頁**不是販售頁，不算商品，回 None ——
    而且它已經在 `scrapers/base.py:detect_invalid_link()` 被擋在爬取之前，
    正常不會走到這裡。
    """
    if len(segs) >= 3 and segs[0].lower() == "product" and segs[1].lower() == "detail":
        return segs[2]
    return None


def _grail_id(segs: list[str]) -> str | None:
    """
    /item/{code} 與 /disp/item/{code} → 同一個 key。

    ★ 這是規則 d 那批 alias 裡**唯一有實測依據**的一條：
      2026-09-07 在站上實際點過兩種路徑，確認指向同一件商品。
      （附錄 B：/disp/item/ 在資料裡出現 5 次，但沒有任何一件商品同時以
      兩種形式出現，所以 alias 本身沒被樣本觸發過 —— 依據來自站上實測，
      不是來自這 730 筆。）
    """
    if len(segs) >= 2 and segs[0].lower() == "item":
        return segs[1]
    if len(segs) >= 3 and segs[0].lower() == "disp" and segs[1].lower() == "item":
        return segs[2]
    return None


def _takaratomymall_id(segs: list[str]) -> str | None:
    """/shop/g/{code}（資料裡的 key 長成 takaratomymall.jp#g8202701096139）。"""
    if len(segs) >= 3 and segs[0].lower() == "shop" and segs[1].lower() == "g":
        return segs[2]
    return None


def _yahoo_store_id(segs: list[str]) -> str | None:
    """
    store.shopping.yahoo.co.jp/{shop}/{code}。

    尾端的 `.html` 去掉：`{code}.html` 與 `{code}` 是同一件商品，
    去掉只可能減少假分裂，**不可能把兩件不同商品併在一起**（辨識資訊在 code 本身）。
    """
    if len(segs) >= 2:
        code = segs[1]
        if code.lower().endswith(".html"):
            code = code[:-5]
        return f"{segs[0]}/{code}"
    return None


def _paypay_flea_id(segs: list[str]) -> str | None:
    """paypayfleamarket.yahoo.co.jp/item/{id}"""
    if len(segs) >= 2 and segs[0].lower() == "item":
        return segs[1]
    return None


def _amiami_id(params: dict) -> str | None:
    """
    商品 ID 在 query 的 `gcode` 或 `scode`，path 一律是 /top/detail/detail。

    這是附錄 C 那個假合併的正解：9 筆同 path 的資料實際是 6 件不同商品。
    """
    return params.get("gcode") or params.get("scode") or None


def _rakuten_item_id(segs: list[str]) -> str | None:
    """
    item.rakuten.co.jp/{shop}/{itemcode}

    ★ **不處理 `/sp/`**（手機版路徑）。附錄 B 實測：那條 alias 一次都沒被觸發過，
      屬於「聽起來合理但沒有依據」的那一類。要加就要先有同一件商品兩種路徑的實例。
    """
    if len(segs) >= 2:
        return f"{segs[0]}/{segs[1]}"
    return None


# host（用 _host_is 比對）→ (key 前綴, 抽 ID 的函式, 需不需要 query)
# ★ 順序有意義：由具體到一般。store.shopping.yahoo.co.jp 要排在
#   paypayfleamarket 之前沒關係（兩者不互相包含），但 amazon 的
#   amazon.co.jp 與 amazon.jp 是兩個獨立網域，各寫一列。
_SITE_RULES: list[tuple[str, str, str]] = [
    ("amazon.co.jp",                 "amazon.co.jp",                 "amazon"),
    ("amazon.jp",                    "amazon.co.jp",                 "amazon"),
    ("jp.mercari.com",               "jp.mercari.com",               "mercari"),
    ("item.rakuten.co.jp",           "item.rakuten.co.jp",           "rakuten_item"),
    ("suruga-ya.jp",                 "suruga-ya.jp",                 "surugaya"),
    ("zozo.jp",                      "zozo.jp",                      "zozo"),
    ("grail.bz",                     "grail.bz",                     "grail"),
    ("takaratomymall.jp",            "takaratomymall.jp",            "takaratomymall"),
    ("store.shopping.yahoo.co.jp",   "store.shopping.yahoo.co.jp",   "yahoo_store"),
    ("paypayfleamarket.yahoo.co.jp", "paypayfleamarket.yahoo.co.jp", "paypay_flea"),
    ("amiami.jp",                    "amiami.jp",                    "amiami"),
]


def _site_id(rule: str, segs: list[str], params: dict) -> str | None:
    if rule == "amazon":
        return _amazon_id(segs)
    if rule == "mercari":
        return _mercari_id(segs)
    if rule == "rakuten_item":
        return _rakuten_item_id(segs)
    if rule == "surugaya":
        return _surugaya_id(segs)
    if rule == "zozo":
        return _zozo_id(segs)
    if rule == "grail":
        return _grail_id(segs)
    if rule == "takaratomymall":
        return _takaratomymall_id(segs)
    if rule == "yahoo_store":
        return _yahoo_store_id(segs)
    if rule == "paypay_flea":
        return _paypay_flea_id(segs)
    if rule == "amiami":
        return _amiami_id(params)
    return None


# ─────────────────────────────────────────────────────────────────────
# 對外
# ─────────────────────────────────────────────────────────────────────
def normalize(url: str) -> tuple[str, str]:
    """
    回傳 `(norm_key, matched_site_rule)`。

    · `norm_key` —— 空字串代表這條 URL 連 host 都解析不出來，**不要記**
    · `matched_site_rule` —— 命中第 4 步哪一條站規則；沒命中是空字串。
      附錄 H：「沒命中的比例太高就表示站規則要補」，所以這個欄位要一起存。

    ★ 這支不會擲例外。爛輸入回 ("", "")，因為它掛在建單路徑上，
      正規化壞掉不可以讓客人建不了單。
    """
    raw = (url or "").strip()
    if not raw:
        return "", ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return "", ""

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "", ""
    if host.startswith("www."):
        host = host[4:]

    segs = [s for s in (parsed.path or "").split("/") if s]
    params = {k.lower(): v for k, v in parse_qsl(parsed.query or "", keep_blank_values=True)}

    # ── 第 4 步：站規則 ──
    for domain, label, rule in _SITE_RULES:
        if _host_is(host, domain):
            pid = _site_id(rule, segs, params)
            if pid:
                return f"{label}#{pid}", rule
            break   # 命中網域但抽不出 ID → 掉到第 5 步，不要再試別站

    # ── 第 5 步：第 1–3 步的通用結果 ──
    path = "/" + "/".join(segs)
    if path == "/":
        path = ""
    query = _clean_query(parsed.query or "")
    return f"https://{host}{path}" + (f"?{query}" if query else ""), ""


# 附錄 E：URL 結構天生不適合當煞車 key 的站。
#
# 🔴 這只是**標記**，第一階段不影響任何行為。列在這裡的站照樣算 key、照樣記錄，
#    等第一階段的資料累積出來，再決定要不要在第二階段把它們排除。
#
# Mercari：一物一頁、售出即 404。同一個 URL 被重複下單代表兩個人搶同一件二手品，
#          不是「這個商品難買」；而且那個 URL 之後永遠死著，擋它沒有意義。
#          資料裡 145 筆，是最大宗，所以它是不是要排除會實質影響門檻。
_UNSUITABLE_HOSTS = ("jp.mercari.com", "mercari.com")


def is_brake_suitable(url: str) -> bool:
    """這條 URL 的失敗語意適不適合拿來當「這件商品買不到」的證據。"""
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower().rstrip(".")
    except Exception:
        return True
    return not any(_host_is(host, d) for d in _UNSUITABLE_HOSTS)
