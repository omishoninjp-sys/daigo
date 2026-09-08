"""
驗證 blocked 旗標與純網域硬擋（2026-09-07）
================================================================
離線測試，不打網路、不碰 Shopify。

★ 為什麼要單獨一支
  2026-09-07 查出兩個「規則對、但客人看不到」的洞：

  1. **純網域規則擺錯位置。** `detect_restricted_category` 開頭是
     `if not title: return None`，而 `/api/scrape` 的順序是
     爬取 →「爬不出 title 就 return」→ 品類判斷。
     beyblade.takaratomy.co.jp 這種爬不動的官方站（實測逾時 60 秒）
     永遠走不到品類判斷，線上回的是 blocked=false ＋「無法抓取商品資訊」。
     → 純網域判斷必須擺在**爬取之前**，這支測的就是那個位置。

  2. **blocked=false 等於沒攔截。** 前端（線上 assets/daigo.js）的邏輯是
     `if (!success || !product?.title) showManualForm()`，
     所以任何 blocked=false 的失敗都會把客人丟到手動填寫表單，
     說明訊息一個字都不會顯示。`detect_invalid_link` 的四則訊息
     （圖片直連／搜尋結果／短網址／本站自己）就是這樣從來沒出現過 ——
     generic-longtail 裡的 Cdn.filestackcontent 就是圖床網址被建成商品。

  這支測「回傳的 blocked 值」而不只是「有沒有回錯誤」，因為
  **blocked 才是前端拿來決定要不要顯示訊息的欄位**。
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DEFAULT_JPY_TO_TWD_RATE", "0.2")

# ★ 建單端點會呼叫 brake_log.note_created 寫紀錄。測試用的是假商品，
#   **不可以寫進正式的紀錄目錄** —— 否則第一份 key 分佈裡會混進
#   「無印良品」之類的測試資料。導到暫存區。
import os as _os, tempfile as _tf
_os.environ.setdefault("BRAKE_LOG_DIR", _tf.mkdtemp(prefix="brake_test_"))

import main as m
from scrapers.base import detect_restricted_host
from scraper import ProductInfo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + (f"  —— {detail}" if detail and not cond else ""))


# ────────────────────────────────────────────────────────────────────
print("\n【一】detect_restricted_host：純網域，不看 title")

HOST_HARD = [
    ("https://1kuji.com/products/bleach8", "一番賞"),
    ("https://on-line.1kuji.com/anything", "一番賞"),          # 子網域也要中
    ("https://30th.pokemon-card.com/product/m6a", "寶可夢"),
    ("https://beyblade.takaratomy.co.jp/products/ux-21/", "BEYBLADE"),
]
for u, word in HOST_HARD:
    r = detect_restricted_host(u)
    check(f"擋下 {u[8:46]}", r is not None and r[0] == "hard", f"實際 {r}")
    check(f"　訊息講了原因（含「{word}」）",
          r is not None and word in r[1], f"實際 {r[1][:40] if r else None}")

# 🔴 這些**不可以**被誤擋
HOST_OK = [
    "https://www.takaratomy.co.jp/products/tomica/",   # 本體賣其他玩具，有專屬 scraper
    "https://takaratomymall.jp/shop/g/g4904810931478/",  # 維持型番條件，不整域擋
    "https://www.pokemoncenter-online.com/9900000008109.html",  # 12 個月唯一成交是遊戲軟體
    "https://www.pokemon-card.com/products/",          # 只擋 30th 子網域
    "https://www.suruga-ya.jp/product/detail/185191748",
    "https://order.mandarake.co.jp/order/detailPage/item?itemCode=1",
    "https://jp.mercari.com/item/m123",
]
for u in HOST_OK:
    check(f"不誤擋 {u[8:52]}", detect_restricted_host(u) is None,
          f"實際 {detect_restricted_host(u)}")

check("網址是空的就不判斷（手動建單常常沒填）", detect_restricted_host("") is None)
check("壞網址不炸", detect_restricted_host("not a url") is None)

# ────────────────────────────────────────────────────────────────────
print("\n【二】手動路徑：客人打中文泛稱繞不過網域")

from scrapers.base import detect_restricted_category

BYPASS = [
    ("寶可夢卡盒", "https://30th.pokemon-card.com/product/m6a"),
    ("卡盒 一盒", "https://30th.pokemon-card.com/product/m6a"),
    ("BLEACH 公仔", "https://1kuji.com/products/bleach8"),
    ("陀螺", "https://beyblade.takaratomy.co.jp/products/ux-21/"),
]
for t, u in BYPASS:
    by_title = detect_restricted_category(t, u)
    by_host = detect_restricted_host(u)
    check(f"「{t}」＋官方網址 → 網域擋得住", by_host is not None and by_host[0] == "hard",
          f"title 規則={by_title}，host 規則={by_host}")

# ────────────────────────────────────────────────────────────────────
print("\n【三】三個端點：blocked 旗標")

_calls = {"shopify": 0, "seo": 0}


class _FakeShopify:
    async def create_daigo_product(self, **kw):
        _calls["shopify"] += 1
        return {"product_id": 1, "storefront_url": "u", "admin_url": "a"}


async def _fake_seo(**kw):
    _calls["seo"] += 1
    return {"title": "t", "tags": [], "seo_source": "fake"}


def _install(product):
    async def _fake_scrape(url):
        if product is None:
            raise AssertionError("不該走到爬取：純網域硬擋要擋在爬取之前")
        return product
    saved = (m.shopify, m.generate_seo_title, m.scrape_with_queue, m.cache_get, m.is_no_cache_url)
    m.shopify = _FakeShopify()
    m.generate_seo_title = _fake_seo
    m.scrape_with_queue = _fake_scrape
    m.cache_get = lambda url: product
    m.is_no_cache_url = lambda url: False
    _calls["shopify"] = _calls["seo"] = 0
    return saved


def _restore(saved):
    (m.shopify, m.generate_seo_title, m.scrape_with_queue,
     m.cache_get, m.is_no_cache_url) = saved


HOST_URL = "https://beyblade.takaratomy.co.jp/products/ux-21/"

# ★ product=None → 一旦走到爬取就 AssertionError，證明真的擋在爬取之前
saved = _install(None)
res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=HOST_URL)))
_restore(saved)
check("/api/scrape 純網域：blocked=True", res.blocked is True, f"實際 {res.blocked}")
check("/api/scrape 純網域：success=False", res.success is False)
check("/api/scrape 純網域：**沒有去爬**（擋在爬取之前）", "BEYBLADE" in (res.error or ""),
      f"實際 error={res.error}")

saved = _install(None)
res = asyncio.run(m.create_order(m.CreateOrderRequest(url=HOST_URL)))
_restore(saved)
check("/api/create-order 純網域：blocked=True", res.blocked is True)
check("/api/create-order 純網域：沒有建到商品", _calls["shopify"] == 0)

saved = _install(None)
res = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title="陀螺", price_jpy=3000, source_url=HOST_URL)))
_restore(saved)
check("/api/create-manual 純網域＋中文泛稱標題：blocked=True", res.blocked is True,
      f"實際 blocked={res.blocked} error={res.error}")
check("/api/create-manual 純網域：沒有建到商品", _calls["shopify"] == 0)

# ────────────────────────────────────────────────────────────────────
print("\n【四】detect_invalid_link 的四則訊息也要 blocked=True")

INVALID = [
    ("圖片直連", "https://cdn.shopify.com/s/files/1/x/y.jpg"),
    ("搜尋結果", "https://www.google.com/search?q=beyblade"),
    ("短網址", "https://bit.ly/abc123"),
    ("本站自己", "https://goyoutati.com/products/some-item"),
    ("社群平台", "https://www.facebook.com/share/r/abc"),
    # 駿河屋的買取（收購）頁 —— 不是販售頁，履約在物理上不可能。2026-09-08 加
    ("駿河屋買取頁", "https://www.suruga-ya.jp/kaitori/kaitori_detail/123456789"),
]
for label, u in INVALID:
    saved = _install(None)
    res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=u)))
    _restore(saved)
    check(f"/api/scrape {label}：blocked=True", res.blocked is True,
          f"實際 blocked={res.blocked}")
    check(f"/api/scrape {label}：有給說明", bool(res.error))

    saved = _install(None)
    res = asyncio.run(m.create_order(m.CreateOrderRequest(url=u)))
    _restore(saved)
    check(f"/api/create-order {label}：blocked=True", res.blocked is True)
    check(f"/api/create-order {label}：沒有建到商品", _calls["shopify"] == 0)

# ────────────────────────────────────────────────────────────────────
print("\n【五】正常商品不受影響")

OK_URL = "https://www.muji.com/jp/ja/store/cmdty/detail/4550344932803"
ok = ProductInfo(title="無印良品 スタッキングシェルフ・オーク材", price_jpy=5500, source_url=OK_URL)

saved = _install(ok)
res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=OK_URL)))
_restore(saved)
check("/api/scrape 正常商品：success=True 且 blocked=False",
      res.success is True and res.blocked is False, f"實際 error={res.error}")

saved = _install(ok)
res = asyncio.run(m.create_order(m.CreateOrderRequest(url=OK_URL)))
_restore(saved)
check("/api/create-order 正常商品：有建到商品且 blocked=False",
      res.success is True and res.blocked is False and _calls["shopify"] == 1,
      f"實際 error={res.error}")

# 二手平台上的一番賞仍然放行（mandarake 是 2026-09-07 新加的）
SH = ProductInfo(title="一番くじ BLEACH A賞 フィギュア", price_jpy=4200,
                 source_url="https://order.mandarake.co.jp/order/detailPage/item?itemCode=1")
saved = _install(SH)
res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=SH.source_url)))
_restore(saved)
check("mandarake 上的一番賞放行（二手豁免）",
      res.success is True and res.blocked is False, f"實際 error={res.error}")

# ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 74)
print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
print("=" * 74)
if FAIL:
    for f in FAIL:
        print("  ❌", f)
    sys.exit(1)
print("全部通過")
