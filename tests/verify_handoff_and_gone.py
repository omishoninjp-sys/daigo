"""
批次二的三件事（離線，不連網、不碰 Shopify）
============================================

  A. handoff.line_handoff_url —— 帶好商品連結的 LINE 深連結
  B. 商品頁 404/410 的專屬訊息 + 端點接線
  C. detect_blocked 由 `domain in host` 改成 _host_matches

★ B 為什麼一定要驗到「端點」而不是只驗訊息字串
  訊息寫得再好，走錯 blocked 分支就一個字都不會顯示給客人 ——
  2026-09-08 抓線上 daigo.js 實測：只有 `data.blocked` 那條路會
  showError(error, handoff_url)，blocked=false 一律 showManualForm()。
  所以這裡驗的是 blocked 旗標與 handoff_url，不是「有沒有回錯誤訊息」。

★ C 為什麼要有「反例」那一組
  改成 _host_matches 之後，真的該擋的（www.amazon.com / tw.mercari.com /
  shop.hoka.com / www.buyee.jp）**仍然要擋**。只驗「誤擋消失了」的話，
  把整張清單刪掉也會全綠。
"""
import asyncio
import os
import sys
import tempfile
from urllib.parse import unquote, urlparse, parse_qsl

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DEFAULT_JPY_TO_TWD_RATE", "0.2")
os.environ.setdefault("BRAKE_LOG_DIR", tempfile.mkdtemp(prefix="brake_test_"))

import main as m
import scrape_monitor
from handoff import line_handoff_url, _MAX_MESSAGE_CHARS
from config import LINE_OA_ID
from scrapers.base import detect_blocked, BLOCKED_DOMAINS, MSG_PRODUCT_GONE
from scraper import ProductInfo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + (f"  —— {detail}" if detail and not cond else ""))


# ════════════════════════════════════════════════════════════════════
print("\n【A】line_handoff_url")
# ════════════════════════════════════════════════════════════════════
SRC = "https://zozo.jp/shop/ellnoloset/goods/110151716/"
u = line_handoff_url(SRC)
check("回得出連結", bool(u), repr(u))
check("是 https（前端 showError 只收 https:）", u.startswith("https://"), u)
check("指向 line.me 的 oaMessage 深連結",
      u.startswith(f"https://line.me/R/oaMessage/{LINE_OA_ID}/?"), u)

# 訊息是整段 query string（沒有欄位名），解出來要看得到原網址
qs = u.split("?", 1)[1]
msg = unquote(qs)
check("訊息裡帶著客人貼的原始網址", SRC in msg, msg[:80])
check("訊息有前綴說明（客人不用自己打字）", msg.strip() != SRC, msg[:60])

# 🔴 編碼：網址裡的 : / ? & = 一定要編掉，否則會被當成 line.me 自己的參數
check("冒號有編碼", "%3A" in qs and "://" not in qs, qs[:60])
check("斜線有編碼", "%2F" in qs, qs[:60])
tricky = line_handoff_url("https://ex.jp/a?b=1&c=2#d")
check("query string 的 & 有編碼（不會變成第二個參數）",
      "&" not in tricky.split("?", 1)[1], tricky)
check("整條連結只有一個 ?", tricky.count("?") == 1, tricky)
# 解出來的 query 只能有一個「參數」—— 也就是訊息本身
pairs = parse_qsl(tricky.split("?", 1)[1], keep_blank_values=True)
check("LINE 只會看到一段訊息，不是多個參數", len(pairs) <= 1, str(pairs)[:80])

# 拿不到可用網址 → None（前端 `if (handoffUrl)` 直接跳過）
for bad, why in [("", "空字串"), (None, "None"), ("   ", "空白"),
                 ("javascript:alert(1)", "🔴 javascript:"),
                 ("data:text/html,x", "🔴 data:"),
                 ("ftp://x.jp/a", "非 http(s)"),
                 ("not a url", "不是網址"),
                 ("https://", "沒有主機名")]:
    check(f"拿不到網址就回 None：{why}", line_handoff_url(bad) is None,
          repr(line_handoff_url(bad)))

long_url = "https://ex.jp/item?" + "x" * 5000
lu = line_handoff_url(long_url)
check("超長網址會截斷但仍是可用連結", lu is not None and len(unquote(lu.split("?", 1)[1])) <= _MAX_MESSAGE_CHARS,
      str(len(unquote(lu.split("?", 1)[1])) if lu else None))
check("超長網址的連結仍然是 https", lu.startswith("https://"), str(lu)[:50])


# ════════════════════════════════════════════════════════════════════
print("\n【B】商品頁 404/410")
# ════════════════════════════════════════════════════════════════════
check("訊息說明「再貼一次也一樣」", "重試" in MSG_PRODUCT_GONE or "一樣" in MSG_PRODUCT_GONE,
      MSG_PRODUCT_GONE[:40])
check("訊息給得出下一步", "LINE" in MSG_PRODUCT_GONE and "搜尋" in MSG_PRODUCT_GONE,
      MSG_PRODUCT_GONE[:40])
check("訊息與通用訊息不同",
      MSG_PRODUCT_GONE != "無法從此連結抓取商品資訊")

# ── scrape_monitor.current_state 的行為 ──
scrape_monitor._ctx.set(None)
check("沒有跑過爬取 → current_state() 是 None", scrape_monitor.current_state() is None)
check("沒有狀態時 _scrape_hit_not_found() 是 False", m._scrape_hit_not_found() is False)

scrape_monitor.start("https://zozo.jp/x")
scrape_monitor.note_http(404, "")
check("404 → _scrape_hit_not_found() 是 True", m._scrape_hit_not_found() is True)
st = scrape_monitor.current_state()
check("current_state 回的是 copy（改不到監控自己的狀態）",
      (st.update({"http_status": 999}) or scrape_monitor.current_state()["http_status"]) == 404)

scrape_monitor.start("https://zozo.jp/x")
scrape_monitor.note_http(410, "")
check("410 也算", m._scrape_hit_not_found() is True)

for s in (200, 403, 429, 500, None):
    scrape_monitor.start("https://zozo.jp/x")
    if s is not None:
        scrape_monitor.note_http(s, "")
    check(f"HTTP {s} 不算 404", m._scrape_hit_not_found() is False)

# 🔴 gone_hint（頁面出現「販売終了」之類）刻意不算 —— 範圍大很多、沒有回測語料
scrape_monitor.start("https://ex.jp/x")
scrape_monitor.note_http(200, "この商品は販売終了しました")
check("gone_hint 刻意不觸發專屬訊息（範圍未驗證）",
      m._scrape_hit_not_found() is False,
      f"state={scrape_monitor.current_state()}")


# ── 端點接線 ──
_calls = {"shopify": 0, "seo": 0}


class _FakeShopify:
    async def create_daigo_product(self, **kw):
        _calls["shopify"] += 1
        return {"product_id": 1, "storefront_url": "u", "admin_url": "a"}


async def _fake_seo(**kw):
    _calls["seo"] += 1
    return {"title": "t", "tags": [], "seo_source": "fake"}


def _install(product, http_status=None):
    async def _fake_scrape(url):
        scrape_monitor.start(url)
        if http_status is not None:
            scrape_monitor.note_http(http_status, "")
        return product
    saved = (m.shopify, m.generate_seo_title, m.scrape_with_queue,
             m.cache_get, m.is_no_cache_url)
    m.shopify = _FakeShopify()
    m.generate_seo_title = _fake_seo
    m.scrape_with_queue = _fake_scrape
    m.cache_get = lambda url: None
    m.is_no_cache_url = lambda url: True      # 走「強制重抓」那條，才會有監控狀態
    _calls["shopify"] = _calls["seo"] = 0
    scrape_monitor._ctx.set(None)
    return saved


def _restore(saved):
    (m.shopify, m.generate_seo_title, m.scrape_with_queue,
     m.cache_get, m.is_no_cache_url) = saved


GONE_URL = "https://zozo.jp/sp/shop/ellnoloset/goods/110151716/"
empty = ProductInfo(source_url=GONE_URL)          # 爬不到 → title 空

saved = _install(empty, http_status=404)
r = asyncio.run(m.scrape_product(m.ScrapeRequest(url=GONE_URL)))
_restore(saved)
check("404 /api/scrape：success=False", r.success is False)
check("404 /api/scrape：**blocked=True**（否則客人看不到訊息，只會看到手動表單）",
      r.blocked is True, f"實際 blocked={r.blocked}")
check("404 /api/scrape：用的是專屬訊息", r.error == MSG_PRODUCT_GONE, str(r.error)[:50])
check("404 /api/scrape：有 handoff_url", bool(r.handoff_url), str(r.handoff_url))
check("404 /api/scrape：handoff 帶的是客人那條網址",
      GONE_URL in unquote(r.handoff_url or ""), str(r.handoff_url)[:60])

# 反例：抓不到但**不是** 404（逾時／被擋／解析失敗）→ 維持原本的通用訊息與手動表單
saved = _install(empty, http_status=None)
r2 = asyncio.run(m.scrape_product(m.ScrapeRequest(url="https://ex.jp/item/1")))
_restore(saved)
check("非 404 抓不到：維持通用訊息", r2.error == "無法從此連結抓取商品資訊", str(r2.error)[:40])
check("非 404 抓不到：blocked 維持 False（客人仍可手動填寫）", r2.blocked is False)
check("非 404 抓不到：一樣給 handoff_url", bool(r2.handoff_url))

saved = _install(empty, http_status=403)
r3 = asyncio.run(m.scrape_product(m.ScrapeRequest(url="https://ex.jp/item/1")))
_restore(saved)
check("被擋（403）不可以說成商品下架", r3.error != MSG_PRODUCT_GONE, str(r3.error)[:40])

# /api/create-order 也要有同一道
saved = _install(empty, http_status=404)
r4 = asyncio.run(m.create_order(m.CreateOrderRequest(url=GONE_URL)))
_restore(saved)
check("404 /api/create-order：blocked=True + 專屬訊息",
      r4.success is False and r4.blocked is True and r4.error == MSG_PRODUCT_GONE,
      f"blocked={r4.blocked} error={str(r4.error)[:40]}")
check("404 /api/create-order：沒有建到商品", _calls["shopify"] == 0)
check("404 /api/create-order：有 handoff_url", bool(r4.handoff_url))


# ── handoff_url 要出現在**每一條**失敗路徑 ──
print("\n【B-2】handoff_url 的覆蓋率")
ok_product = ProductInfo(title="無印良品 スタッキングシェルフ", price_jpy=5500,
                         source_url="https://www.muji.com/jp/ja/store/cmdty/detail/1")

CASES = [
    ("封鎖網站", "https://www.amazon.com/dp/B0X"),
    ("非商品頁連結", "https://www.google.com/search?q=x"),
    ("受限通路（網域）", "https://1kuji.com/products/x"),
]
for label, url in CASES:
    saved = _install(ok_product)
    rr = asyncio.run(m.scrape_product(m.ScrapeRequest(url=url)))
    _restore(saved)
    check(f"/api/scrape {label}：blocked=True 且有 handoff_url",
          rr.blocked is True and bool(rr.handoff_url),
          f"blocked={rr.blocked} handoff={rr.handoff_url}")

card = ProductInfo(title="ポケモンカードゲーム 拡張パック", price_jpy=5500,
                   source_url="https://ex.jp/item/1")
saved = _install(card)
rr = asyncio.run(m.scrape_product(m.ScrapeRequest(url="https://ex.jp/item/1")))
_restore(saved)
check("/api/scrape 硬擋品類：有 handoff_url", bool(rr.handoff_url))

sent = ProductInfo(title="なにか", price_jpy=999_999, source_url="https://ex.jp/item/1")
saved = _install(sent)
rr = asyncio.run(m.scrape_product(m.ScrapeRequest(url="https://ex.jp/item/1")))
_restore(saved)
check("/api/scrape 哨兵價格：有 handoff_url", bool(rr.handoff_url))

soft = ProductInfo(title="一番くじ ドラゴンボール", price_jpy=3000,
                   source_url="https://ex.jp/item/1")
saved = _install(soft)
rr = asyncio.run(m.scrape_product(m.ScrapeRequest(url="https://ex.jp/item/1")))
_restore(saved)
check("/api/scrape soft：success=True 但有 handoff_url（只能詢問）",
      rr.success is True and bool(rr.handoff_url) and bool(rr.notice),
      f"success={rr.success} handoff={rr.handoff_url}")

saved = _install(ok_product)
rr = asyncio.run(m.scrape_product(m.ScrapeRequest(url=ok_product.source_url)))
_restore(saved)
check("🔴 一般成功的商品**不給** handoff_url（不需要人工接手）",
      rr.success is True and rr.handoff_url is None, str(rr.handoff_url))

saved = _install(soft)
rr = asyncio.run(m.create_order(m.CreateOrderRequest(url="https://ex.jp/item/1")))
_restore(saved)
check("/api/create-order soft：有 handoff_url", bool(rr.handoff_url) and rr.soft is True)

saved = _install(ok_product)
rr = asyncio.run(m.create_manual_order(m.ManualOrderRequest(
    title="ポケモンカードゲーム 拡張パック", price_jpy=3000,
    source_url="https://ex.jp/item/1")))
_restore(saved)
check("/api/create-manual 硬擋品類：有 handoff_url",
      rr.blocked is True and bool(rr.handoff_url), str(rr.handoff_url))

saved = _install(ok_product)
rr = asyncio.run(m.create_manual_order(m.ManualOrderRequest(
    title="なにか", price_jpy=3000, source_url="")))
_restore(saved)
check("/api/create-manual 沒有 source_url → handoff_url 是 None（沒東西可帶）",
      rr.handoff_url is None, str(rr.handoff_url))


# ════════════════════════════════════════════════════════════════════
print("\n【C】detect_blocked 改用 _host_matches")
# ════════════════════════════════════════════════════════════════════
# C-1 真的該擋的**仍然要擋**（每一個封鎖網域本身、www.、子網域）
for domain in BLOCKED_DOMAINS:
    for h in (domain, "www." + domain, "shop." + domain):
        check(f"仍然擋得下 {h}", detect_blocked(f"https://{h}/item/1") is not None)

# C-2 子字串誤擋要消失。這些網域**都是真實存在的站**，
#     舊寫法（`domain in host`）全部誤擋。
FALSE_POSITIVES = [
    ("amazon.com.tw", "amazon.com"),
    ("www.amazon.com.au", "amazon.com"),
    ("amazon.com.br", "amazon.com"),
    ("hoka.com.tw", "hoka.com"),
    ("www.hoka.com.au", "hoka.com"),
    ("xbuyee.jp", "buyee.jp"),
    ("mybuyee.jp", "buyee.jp"),
    ("notbuyma.com", "buyma.com"),
    ("buyma.com.tw", "buyma.com"),
    ("xbibian.co.jp", "bibian.co.jp"),
    ("nottokukai.com", "tokukai.com"),
]
for host, why in FALSE_POSITIVES:
    check(f"不可誤擋 {host}（舊寫法會被 {why} 命中）",
          detect_blocked(f"https://{host}/item/1") is None,
          (detect_blocked(f"https://{host}/item/1") or "")[:30])

# C-3 正常日本商店照樣放行
for h in ["jp.mercari.com", "www.amazon.co.jp", "item.rakuten.co.jp", "zozo.jp",
          "www.muji.com", "www.uniqlo.com", "store.shopping.yahoo.co.jp",
          "www.dot-st.com", "tocco-closet.co.jp", "golfdigest.co.jp"]:
    check(f"放行 {h}", detect_blocked(f"https://{h}/item/1") is None,
          (detect_blocked(f"https://{h}/item/1") or "")[:30])

# C-4 網址壞掉不可以炸
for bad in ["", "not a url", "https://", "ftp://x", None]:
    try:
        detect_blocked(bad if bad is not None else "")
        check(f"壞網址不會拋例外：{bad!r}", True)
    except Exception as e:
        check(f"壞網址不會拋例外：{bad!r}", False, f"{type(e).__name__}: {e}")


print("\n" + "=" * 62)
print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
if FAIL:
    for n in FAIL:
        print(f"  ❌ {n}")
    sys.exit(1)
print("全部通過")
