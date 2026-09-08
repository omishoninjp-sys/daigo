"""
source_key.normalize() 的回歸測試（離線，不需要任何憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_source_key.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_source_key.py`）

★ 這支存在的理由是**兩個方向相反的錯**，來自 2026-09-07 用 730 筆真實
  line item 做的規則評估（TICKET-duplicate-order-brake.md 附錄 C／D）：

    假合併 —— 不同商品被算成同一個 key → 會**誤擋**買得到的東西
              病灶：整段砍 query。8 個站把商品 ID 放在那裡。
    假分裂 —— 同一商品被算成多個 key → 會**漏擋**
              病灶：Amazon 7 種網址形態、Mercari 語系前綴。

  兩種錯的來源不同，修法也不同，所以 A 組與 B 組都要綠才算對。
  只修一邊會得到一個「看起來收斂得很漂亮」但擋錯東西的 key。

★ 這裡的 URL 形態全部來自那份分析的實際樣本（筆數寫在各組註解裡），
  不是想像出來的。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from urllib.parse import urlparse

import source_key
from source_key import normalize, is_brake_suitable, KEY_RULE_VERSION

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def key(url):
    return normalize(url)[0]


def rule(url):
    return normalize(url)[1]


def naive_strip_query(url):
    """規則 b/c/d 的做法：整段砍 query。拿來當對照組，證明它會產生假合併。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"https://{host}{(p.path or '').rstrip('/')}"


# ─────────────────────────────────────────────────────────────────────
# A. 假合併：整段砍 query 會把不同商品併成一個 key
# ─────────────────────────────────────────────────────────────────────
# 附錄 C：8 個站把商品 ID 放在 query string，路徑本身不帶辨識資訊。
# amiami 那 9 筆**全部**落在 path=/top/detail/detail，實際是 6 件不同商品。
AMIAMI = [
    "https://www.amiami.jp/top/detail/detail?gcode=GOODS-04809438",
    "https://www.amiami.jp/top/detail/detail?gcode=GOODS-04812345",
    "https://www.amiami.jp/top/detail/detail?gcode=GOODS-04899999",
    "https://www.amiami.jp/top/detail/detail?scode=SCODE-12345",
]

# 其餘 7 個沒有站規則的 query-based 站：靠「保留非追蹤參數」撐住
QUERY_BASED = [
    ("https://www.tmrecords.shop/?id=1001", "https://www.tmrecords.shop/?id=1002", "tmrecords（id，8 筆）"),
    ("https://www.gundam-base.net/?path=/a/1", "https://www.gundam-base.net/?path=/a/2", "gundam-base（path，3 筆）"),
    ("https://official-store.jfa.jp/?id=55", "https://official-store.jfa.jp/?id=56", "jfa（id，3 筆）"),
    ("https://gochio.kyoto.jp/?product_id=7", "https://gochio.kyoto.jp/?product_id=8", "gochio（product_id，2 筆）"),
    ("https://store.plusmember.jp/?product_id=1", "https://store.plusmember.jp/?product_id=2", "plusmember（product_id）"),
]


# ─────────────────────────────────────────────────────────────────────
# B. 假分裂：同一件商品的多種網址要收斂成同一個 key
# ─────────────────────────────────────────────────────────────────────
# 附錄 D：Amazon 在資料裡有 7 種形態，同一個 ASIN 桌機貼 /dp/、手機 App 貼
# /gp/aw/d/ 會被算成兩個 key，各自都達不到門檻。x9 成交 0 的寶可夢 MEGA
# 卡組正是 /gp/aw/d/ 那一種，原本只認 /dp/ 的規則漏掉 10 筆。
ASIN = "B0GXCM4BCZ"
AMAZON_FORMS = [
    (f"https://www.amazon.co.jp/dp/{ASIN}", "/dp/{ASIN}（24 筆）"),
    (f"https://www.amazon.co.jp/ポケモンカードゲーム-MEGA/dp/{ASIN}/ref=sr_1_3?keywords=x", "/{slug}/dp/{ASIN}/ref=（13 筆）"),
    (f"https://www.amazon.co.jp/-/zh/dp/{ASIN}", "/-/zh/dp/{ASIN}（9 筆）"),
    (f"https://www.amazon.co.jp/-/zh/gp/aw/d/{ASIN}/ref=ox_sc_act_title_1", "/-/zh/gp/aw/d/{ASIN}（9 筆，手機 App）"),
    (f"https://www.amazon.co.jp/-/en/gp/aw/d/{ASIN}", "/-/en/gp/aw/d/{ASIN}（1 筆）"),
    (f"https://www.amazon.co.jp/gp/product/{ASIN}", "/gp/product/{ASIN}（1 筆）"),
    (f"https://amazon.co.jp/dp/{ASIN}?th=1&psc=1", "不帶 www.＋殘留參數"),
]

# 附錄 D：/item/{id} 119 筆、/zh-TW/item/{id} 6 筆、
#         /shops/product/{id} 14 筆、/zh-TW/shops/product/{id} 5 筆、/en/… 1 筆
MERCARI_ITEM = [
    ("https://jp.mercari.com/item/m12345678901", "/item/{id}（119 筆）"),
    ("https://jp.mercari.com/zh-TW/item/m12345678901", "/zh-TW/ 前綴（6 筆）"),
    ("https://jp.mercari.com/en/item/m12345678901", "/en/ 前綴（1 筆）"),
]
MERCARI_SHOPS = [
    ("https://jp.mercari.com/shops/product/abcdef123", "/shops/product/{id}（14 筆）"),
    ("https://jp.mercari.com/zh-TW/shops/product/abcdef123", "/zh-TW/shops/…（5 筆）"),
]

# 附錄 B：GRL 的兩種路徑是 2026-09-07 **在站上實際點過**確認同一件商品的，
#         這是整批 alias 裡唯一有依據的一條。
GRAIL = [
    ("https://www.grail.bz/item/ABC123", "/item/{code}"),
    ("https://www.grail.bz/disp/item/ABC123", "/disp/item/{code}（5 筆）"),
]


# ─────────────────────────────────────────────────────────────────────
# C. 追蹤參數白名單（第 3 步）
# ─────────────────────────────────────────────────────────────────────
# 附錄 B：規則 b 相對 a 少掉的 19 個 key 全部來自追蹤參數。
TRACKING = [
    ("https://example.jp/item/1?utm_source=google&utm_medium=cpc", "utm_*"),
    ("https://example.jp/item/1?gclid=xyz", "gclid"),
    ("https://example.jp/item/1?fbclid=xyz", "fbclid"),
    ("https://example.jp/item/1?_gl=1*abc", "_gl"),
    ("https://example.jp/item/1?s-id=ph_pc_itemname", "s-id（樂天）"),
    ("https://example.jp/item/1?rafcid=wsc_i_is_123", "rafcid（樂天）"),
    ("https://example.jp/item/1?sc_i=shp_pc_search", "sc_i"),
    ("https://example.jp/item/1?did=xyz", "did（ZOZO）"),
    ("https://example.jp/item/1?ref=nav_logo", "ref"),
    ("https://example.jp/item/1?l-id=top_normal_thumb", "l-id"),
    ("https://example.jp/item/1?srsltid=abc", "srsltid"),
    ("https://example.jp/item/1?trflg=1&bkts=a", "trflg / bkts"),
]

# 站規則（第 4 步）
SITE_RULES = [
    ("https://item.rakuten.co.jp/book/18719874/", "item.rakuten.co.jp#book/18719874", "rakuten_item"),
    ("https://www.suruga-ya.jp/product/detail/892621178", "suruga-ya.jp#892621178", "surugaya"),
    ("https://zozo.jp/shop/beams/goods/12345678/", "zozo.jp#beams/12345678", "zozo"),
    ("https://www.takaratomymall.jp/shop/g/g8202701096139/", "takaratomymall.jp#g8202701096139", "takaratomymall"),
    ("https://store.shopping.yahoo.co.jp/auc-toy/abc123.html", "store.shopping.yahoo.co.jp#auc-toy/abc123", "yahoo_store"),
    ("https://store.shopping.yahoo.co.jp/auc-toy/abc123", "store.shopping.yahoo.co.jp#auc-toy/abc123", "yahoo_store"),
    ("https://paypayfleamarket.yahoo.co.jp/item/z1234567890", "paypayfleamarket.yahoo.co.jp#z1234567890", "paypay_flea"),
]

# 第 1、2 步：host 小寫／去 www／去尾斜線／去 fragment／scheme 一律 https
SHAPE = [
    ("https://www.example.jp/item/1", "https://example.jp/item/1", "去 www."),
    ("https://EXAMPLE.JP/item/1", "https://example.jp/item/1", "host 小寫"),
    ("http://example.jp/item/1", "https://example.jp/item/1", "scheme 統一"),
    ("https://example.jp/item/1/", "https://example.jp/item/1", "去尾斜線"),
    ("https://example.jp/item/1#reviews", "https://example.jp/item/1", "去 fragment"),
    ("https://example.jp/", "https://example.jp", "只有斜線"),
    ("https://example.jp./item/1", "https://example.jp/item/1", "FQDN 尾點"),
]

# ★ 刻意**不**做的合併（附錄 B：規則 d 的 alias 一條都沒被觸發過）
DELIBERATELY_SPLIT = [
    ("https://item.rakuten.co.jp/book/18719874/",
     "https://item.rakuten.co.jp/sp/book/18719874/",
     "樂天 /sp/ —— 沒有任何一件商品同時以兩種形式出現過，不加"),
    # 2026-09-08：規則 f 的條文寫了 goods(-sale)，但實測 35 個 (shop, id)
    # 有 0 個同時出現兩種路徑 —— 跟規則 d 那些沒被觸發的 alias 同類，所以拿掉。
    # 真遇到實例再加。這條斷言就是防止有人「看起來合理」又把它加回去。
    ("https://zozo.jp/shop/beams/goods/12345678/",
     "https://zozo.jp/shop/beams/goods-sale/12345678/",
     "ZOZO goods-sale —— 樣本裡 0 個實例，不合併"),
]


def main():
    print("=" * 74)
    print(f"規則版本 {KEY_RULE_VERSION}")
    print("=" * 74)

    print("\n" + "=" * 74)
    print("A1. 🔴 假合併：amiami 同一個 path 底下的不同商品必須是不同 key")
    print("=" * 74)
    keys = {key(u) for u in AMIAMI}
    naive = {naive_strip_query(u) for u in AMIAMI}
    check(f"規則 f：{len(AMIAMI)} 筆 → {len(keys)} 個 key", len(keys) == len(AMIAMI),
          str(sorted(keys)))
    check("對照組：整段砍 query 會併成 1 個（這正是要修的病）",
          len(naive) == 1, str(sorted(naive)))
    check("gcode 抽出來當 key", key(AMIAMI[0]) == "amiami.jp#GOODS-04809438",
          key(AMIAMI[0]))
    check("scode 也認得", key(AMIAMI[3]) == "amiami.jp#SCODE-12345", key(AMIAMI[3]))
    check("matched_site_rule = amiami", rule(AMIAMI[0]) == "amiami", rule(AMIAMI[0]))
    check("amiami 的追蹤參數不影響 key",
          key("https://www.amiami.jp/top/detail/detail?gcode=GOODS-04809438&utm_source=x")
          == "amiami.jp#GOODS-04809438")

    print("\n" + "=" * 74)
    print("A2. 🔴 假合併：沒有站規則的 query-based 站，ID 參數一定要保留")
    print("=" * 74)
    for u1, u2, label in QUERY_BASED:
        check(f"{label}：兩件商品兩個 key", key(u1) != key(u2), f"{key(u1)} / {key(u2)}")
        check(f"{label}：對照組整段砍會併成一個",
              naive_strip_query(u1) == naive_strip_query(u2))

    print("\n" + "=" * 74)
    print("B1. 🔴 假分裂：Amazon 7 種網址形態要收斂成同一個 ASIN")
    print("=" * 74)
    got = {}
    for u, label in AMAZON_FORMS:
        k = key(u)
        got.setdefault(k, []).append(label)
        check(f"{label} → amazon.co.jp#{ASIN}", k == f"amazon.co.jp#{ASIN}", k)
    check(f"★ {len(AMAZON_FORMS)} 種形態收斂成 1 個 key", len(got) == 1,
          f"{len(got)} 個：{list(got)}")
    check("matched_site_rule = amazon", rule(AMAZON_FORMS[0][0]) == "amazon")
    check("amazon.jp 也算同一個站",
          key(f"https://www.amazon.jp/dp/{ASIN}") == f"amazon.co.jp#{ASIN}")
    check("ASIN 大小寫統一", key(f"https://www.amazon.co.jp/dp/{ASIN.lower()}")
          == f"amazon.co.jp#{ASIN}")
    check("抽不出 ASIN 就退回通用規則，不硬抽",
          key("https://www.amazon.co.jp/s?k=beyblade").startswith("https://amazon.co.jp/s"),
          key("https://www.amazon.co.jp/s?k=beyblade"))

    print("\n" + "=" * 74)
    print("B2. 假分裂：Mercari 語系前綴")
    print("=" * 74)
    ks = {key(u) for u, _ in MERCARI_ITEM}
    check("/item/ 三種語系 → 1 個 key", len(ks) == 1, str(ks))
    check("key 形狀", key(MERCARI_ITEM[0][0]) == "jp.mercari.com#m12345678901")
    ks2 = {key(u) for u, _ in MERCARI_SHOPS}
    check("/shops/product/ 兩種語系 → 1 個 key", len(ks2) == 1, str(ks2))

    print("\n" + "=" * 74)
    print("B3. 假分裂：GRL 的兩種路徑（整批 alias 裡唯一有實測依據的一條）")
    print("=" * 74)
    ks = {key(u) for u, _ in GRAIL}
    check("/item/ 與 /disp/item/ → 1 個 key", len(ks) == 1, str(ks))
    check("key 形狀", key(GRAIL[0][0]) == "grail.bz#ABC123")

    print("\n" + "=" * 74)
    print("C. 追蹤參數白名單：列到的才砍，沒列到的一律保留")
    print("=" * 74)
    base = "https://example.jp/item/1"
    for u, label in TRACKING:
        check(f"砍掉 {label}", key(u) == base, key(u))
    check("★ 非追蹤參數保留",
          key("https://example.jp/item/1?pid=123") == "https://example.jp/item/1?pid=123",
          key("https://example.jp/item/1?pid=123"))
    check("★ 追蹤與非追蹤混在一起：只砍追蹤的",
          key("https://example.jp/?utm_source=g&pid=123&s-id=x") == "https://example.jp?pid=123",
          key("https://example.jp/?utm_source=g&pid=123&s-id=x"))
    check("參數順序不同 → 同一個 key（排序）",
          key("https://example.jp/i?a=1&b=2") == key("https://example.jp/i?b=2&a=1"),
          key("https://example.jp/i?b=2&a=1"))

    print("\n" + "=" * 74)
    print("D. 站規則的 key 形狀")
    print("=" * 74)
    for u, expect, r in SITE_RULES:
        k, got_rule = normalize(u)
        check(f"{u[:52]} → {expect}", k == expect, k)
        check(f"  matched_site_rule = {r}", got_rule == r, got_rule)
    check("駿河屋買取頁抽不出商品 ID（也已在 detect_invalid_link 擋掉）",
          rule("https://www.suruga-ya.jp/kaitori/kaitori_detail/1") == "",
          key("https://www.suruga-ya.jp/kaitori/kaitori_detail/1"))

    print("\n" + "=" * 74)
    print("E. 第 1、2 步：host／scheme／尾斜線／fragment")
    print("=" * 74)
    for u, expect, label in SHAPE:
        check(f"{label}", key(u) == expect, key(u))

    print("\n" + "=" * 74)
    print("F. ★ 刻意不加的 alias（沒有實測依據就不要加）")
    print("=" * 74)
    for u1, u2, label in DELIBERATELY_SPLIT:
        check(label, key(u1) != key(u2), f"{key(u1)} / {key(u2)}")

    print("\n" + "=" * 74)
    print("G. 爛輸入不可以擲例外（這支掛在建單路徑上）")
    print("=" * 74)
    for bad in ["", "   ", "not a url", "ftp://example.jp/x", "https://", "http:///x", None]:
        try:
            k, r = normalize(bad)
            ok = isinstance(k, str) and isinstance(r, str)
        except Exception as e:
            ok = False
            k = f"{type(e).__name__}: {e}"
        check(f"normalize({bad!r}) 不擲例外", ok, str(k)[:40])
    check("空字串輸入 → 空 key（不要記）", normalize("")[0] == "")
    check("沒有 host → 空 key", normalize("not a url")[0] == "")

    print("\n" + "=" * 74)
    print("H. is_brake_suitable：附錄 E 的標記（只標記，第一階段不影響行為）")
    print("=" * 74)
    check("Mercari 標成不適合（售出即 404，失敗語意是「被別人買走」）",
          is_brake_suitable("https://jp.mercari.com/item/m123") is False)
    check("Mercari 語系網址一樣",
          is_brake_suitable("https://jp.mercari.com/zh-TW/item/m123") is False)
    check("一般商店是適合的",
          is_brake_suitable("https://item.rakuten.co.jp/book/18719874/") is True)
    check("爛輸入不擲例外", is_brake_suitable("not a url") is True)
    check("★ 但 Mercari 照樣算得出 key（第一階段要有資料才判斷得出來）",
          key("https://jp.mercari.com/item/m123") == "jp.mercari.com#m123")

    print("\n" + "=" * 74)
    print("I. 規則版本要跟著行為走")
    print("=" * 74)
    check("KEY_RULE_VERSION 有值", bool(source_key.KEY_RULE_VERSION), KEY_RULE_VERSION)
    check("白名單沒有把 'id' 之類的商品參數列進去",
          not {"id", "pid", "product_id", "gcode", "scode", "path", "itemcode"}
          & set(source_key._TRACKING_EXACT))

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
