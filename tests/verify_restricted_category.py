"""
驗證受限品類攔截（detect_restricted_category ＋ 三個端點的接線）
================================================================
離線測試，不打網路、不碰 Shopify。

★ 為什麼不只測 detect_restricted_category 本身
  規則寫對、但沒接到「真的會走到的那條路徑」，比沒寫更糟 —— CLAUDE.md
  的 gql_nodes 與 cleanup `completed` 兩次都是這個形狀。所以這裡直接呼叫
  main.scrape_product / main.create_order / main.create_manual_order 三支
  端點函式，並且把 shopify.create_daigo_product 換成「一被呼叫就算失敗」的
  假物件 —— 驗的是「有沒有真的走到建商品那一步」，不是「有沒有回錯誤訊息」。

★ /api/create-order 那條特別重要
  cache 命中時不會重跑 /api/scrape，所以擋不擋得住完全靠它自己那一道。
  這裡刻意讓 cache_get 回傳商品（模擬命中），確認 cache 路徑也擋得下來。
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 匯率固定住，避免 calculate_selling_price() 去打線上匯率 API（要在 import config 之前設）
os.environ.setdefault("DEFAULT_JPY_TO_TWD_RATE", "0.2")

# ★ 建單端點會呼叫 brake_log.note_created 寫紀錄。測試用的是假商品，
#   **不可以寫進正式的紀錄目錄** —— 否則第一份 key 分佈裡會混進
#   「無印良品」之類的測試資料。導到暫存區。
import os as _os, tempfile as _tf
_os.environ.setdefault("BRAKE_LOG_DIR", _tf.mkdtemp(prefix="brake_test_"))

import main as m
from scrapers.base import detect_restricted_category
from scraper import ProductInfo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + (f"  —— {detail}" if detail and not cond else ""))


# ────────────────────────────────────────────────────────────────────
# 一、規則本身
# ────────────────────────────────────────────────────────────────────
print("\n【一】detect_restricted_category 規則")

# 真實作廢訂單的標題（2026-08-29 ~ 09-04）
HARD_TITLES = [
    "ポケモンカードゲーム スカーレット&バイオレット 拡張パック",
    "ポケカ 強化拡張パック 熱風のアリーナ BOX",
    "【抽選販売】ポケモンセンター オリジナル",
    "ONE PIECE カードゲーム 新時代の主役",
    "一番くじ ドラゴンボール",
]
for t in HARD_TITLES:
    r = detect_restricted_category(t, "")
    check(f"硬擋：{t[:24]}", r is not None and r[0] == "hard", f"實際回傳 {r}")

# 正常商品不可以被擋（含刻意挑的相鄰詞）
CLEAN_TITLES = [
    "ユニクロ ヒートテッククルーネックT",
    "ポケモン ぬいぐるみ ピカチュウ",              # 寶可夢周邊，不是卡牌
    "ポケモンセンター オリジナル マグカップ",
    "無印良品 スタッキングシェルフ",
    "ミズノ ゴルフクラブ JPX 925",
    "SEIKO 腕時計 メンズ",
    "ワンピース フィギュア ルフィ",                # 航海王公仔，不是卡牌
    "任天堂 Nintendo Switch 2 本体",
]
for t in CLEAN_TITLES:
    r = detect_restricted_category(t, "https://example.jp/item/1")
    check(f"放行：{t[:24]}", r is None, f"實際回傳 {r}")

# 軟擋：回 soft，端點應放行
r = detect_restricted_category("【予約】フィギュア 2026年3月発売予定", "")
check("軟擋回 soft", r is not None and r[0] == "soft", f"實際回傳 {r}")
check("空標題回 None", detect_restricted_category("", "https://x.jp/") is None)


# ────────────────────────────────────────────────────────────────────
# 一之二、2026-09-05 的四項修正，各自要有正反樣本
#
# ★ fixture 一定要「只含待驗的那個差異」。上一版 28 條全過是假象：
#   驗方向 bug 的樣本同時含 30th CELEBRATION，靠字面命中蓋住了方向錯誤。
#   所以下面驗方向的樣本**刻意不含任何週年字樣**。
# ────────────────────────────────────────────────────────────────────
print("\n【一之二】四項修正")

# ── 1. 寶可夢中文規則的方向 ──
DIRECTION_CASES = [
    # (標題, 期望硬擋, 說明)
    ("寶可夢 卡片擴充包 - 卡牌 MEGA 擴充包 風暴綠寶石", True, "寶可夢在前（舊版漏掉的真實標題）"),
    ("寶可夢 卡牌擴充包 - 卡牌 MEGA 擴張包 風暴翡翠", True, "寶可夢在前＋擴張包"),
    ("拡張パック ポケモン カードゲーム", True, "擴充包在前（舊版唯一認得的方向）"),
    ("寶可夢中心 比卡超 毛絨玩具", False, "只有寶可夢，沒有擴充包 → 周邊，不擋"),
    ("卡牌 MEGA 擴充包 風暴綠寶石", False, "只有擴充包，沒有寶可夢 → 別家 TCG，不擋"),
]
for t, want, why in DIRECTION_CASES:
    r = detect_restricted_category(t, "")
    got = r is not None and r[0] == "hard"
    check(f"方向：{why}", got == want, f"實際回傳 {r}")

# ── 2. 30th CELEBRATION 要有寶可夢上下文 ──
ANNIV_CASES = [
    ("寶可夢 擴展包 - 卡牌擴展包「30th CELEBRATION」", "", True, "有寶可夢 → 擋"),
    ("MEGA 30th CELEBRATION 慶祝商品", "https://item.rakuten.co.jp/x/1", False,
     "沒有寶可夢上下文 → 不擋"),
    ("SEIKO 腕時計 30th CELEBRATION 記念モデル", "", False, "SEIKO 週年錶 → 不擋"),
    ("ちいかわ 30周年慶典 マグカップ", "", False, "別的 IP 的週年商品 → 不擋"),
    ("寶可夢 卡牌遊戲 - MEGA 30週年慶祝 高級套組", "", True, "寶可夢＋週年慶祝 → 擋"),
]
for t, u, want, why in ANNIV_CASES:
    r = detect_restricted_category(t, u)
    got = r is not None and r[0] == "hard"
    check(f"週年：{why}", got == want, f"實際回傳 {r}")

# ── 3. 一番賞：二手平台豁免 ──
ICHIBAN_CASES = [
    ("一番くじ ドラゴンボール", "https://1kuji.com/lineup/xxx", True, "官方通路 → 擋"),
    ("一番くじ ドラゴンボール", "", True, "沒有網址 → 判斷不出來，照擋"),
    ("一番賞 弗利倫 公仔 C賞 D賞", "https://jp.mercari.com/item/m123", False, "Mercari 現貨"),
    ("一番賞 まとめ賣", "https://www.mercari.com/jp/items/m1", False, "mercari.com 子網域"),
    ("一番くじ ワンピース", "https://paypayfleamarket.yahoo.co.jp/item/z1", False, "PayPay フリマ"),
    # ⚠️ Yahoo 拍賣已從 _SECONDHAND_HOSTS 移除：純競標與即決網址相同，分不出來，
    #    留在豁免清單會讓純競標頁被放行。即決 scraper 完成後再改回 False。
    ("一番くじ 景品", "https://auctions.yahoo.co.jp/jp/auction/x1", True,
     "Yahoo 拍賣 → 不豁免（競標與即決分不出來）"),
    ("一番くじ フィギュア", "https://www.suruga-ya.jp/product/detail/123", False, "駿河屋"),
    ("一番くじ 景品", "https://netmall.hardoff.co.jp/product/1", False, "ハードオフ"),
    # ★ 子字串誤命中的反例：網域裡有 mercari 字樣但不是 mercari
    ("一番くじ 景品", "https://mercari-fan.example.jp/item/1", True, "假 mercari 網域 → 照擋"),
]
for t, u, want, why in ICHIBAN_CASES:
    r = detect_restricted_category(t, u)
    got = r is not None and r[0] == "hard"
    check(f"一番賞：{why}", got == want, f"實際回傳 {r}")

# ── 4. BEYBLADE 只擋官網 ──
BEY_CASES = [
    ("ベイブレードX BX-35 スターター", "https://beyblade.takaratomy.co.jp/item/1", True,
     "官方專門站 → 擋"),
    ("何かの商品", "https://shop.beyblade.takaratomy.co.jp/x", True, "官網子網域 → 擋"),
    # 🔴 這條是最重要的反例：takaratomy.co.jp 有專屬 scraper、賣其他玩具
    ("トミカ ミニカー ランボルギーニ", "https://www.takaratomy.co.jp/products/tomica/1",
     False, "takaratomy.co.jp 本體 → 絕不可誤傷"),
    ("ベイブレードX UX-21 セット", "https://www.takaratomy.co.jp/products/x/1", False,
     "官方主站的 BEYBLADE → 新規則不擋（標題關鍵字已移除）"),
    ("BEYBLADE X BX-46 戰鬥入門套件", "https://www.amazon.co.jp/dp/B0XXXX", False,
     "Amazon 的 BEYBLADE → 買得到，不擋"),
    ("BEYBLADE X UX-21 玩具", "https://item.rakuten.co.jp/shop/ux21/", False,
     "樂天的 BEYBLADE → 買得到，不擋"),
    ("戰鬥陀螺 BEYBLADE X", "", False, "沒有網址 → 純網域規則不作用"),
    # 子字串誤命中的反例
    ("何かの商品", "https://beyblade.takaratomy.co.jp.evil.example.com/x", False,
     "後綴偽裝網域 → 不可命中"),
]
for t, u, want, why in BEY_CASES:
    r = detect_restricted_category(t, u)
    got = r is not None and r[0] == "hard"
    check(f"BEYBLADE：{why}", got == want, f"實際回傳 {r}")

# ── 5. 抽選販售的繁中變體（純 bug）──
# 日文是「抽選販売」，SEO 標題寫的是繁中「抽選販售」，兩個是不同的字。
MALL = "https://takaratomymall.jp/shop/g/g4904810000000/"
CHUSEN_CASES = [
    ("塔卡拉托米 玩具 - 抽選販售 BEYBLADE X CX-18 隨機增強包｜takaratomymall.jp", "", True,
     "使用者指定的那筆（純標題，無網址）"),
    ("日本代購｜塔卡拉托米 玩具 - 抽選販售 BEYBLADE X CX-18 隨機增強包｜タカラトミー", MALL, True,
     "同一筆帶真實網址"),
    ("【抽選販売】ポケモンセンター オリジナル", "", True, "日文原本那個仍要中"),
    ("限定商品 抽選販賣 受付中", "", True, "抽選販賣"),
    ("抽選受理 のお知らせ", "", True, "抽選受理"),
    ("商品 抽選預約 開始", "", True, "抽選預約"),
    ("CX-17 隨機增強器 Vol.10", "", False, "隨機增強『器』不在字表裡 → 不擋"),
    ("普通の抽選会場までの地図", "", False, "只有『抽選』兩個字 → 不擋"),
]
for t, u, want, why in CHUSEN_CASES:
    r = detect_restricted_category(t, u)
    got = r is not None and r[0] == "hard"
    check(f"抽選：{why}", got == want, f"實際回傳 {r}")

# ── 6. takaratomymall.jp：host ＋ 型番 才擋 ──
# 🔴 這一組的重點是**同網域的正常玩具不可以被波及**。
MALL_CASES = [
    ("塔卡拉托米 模型 - BEYBLADE X UX-21 赫爾斯納札德模型套裝", MALL, True, "型番命中 → 擋"),
    ("塔卡拉托米 模型 - 限定 BEYBLADE X CX-00 模型套裝", MALL, True, "CX-00 → 擋"),
    ("タカラトミー 玩具 - 購物車玩具", MALL, False,
     "🔴 同網域但沒有型番（站上真實商品）→ 絕不可擋"),
    ("トミカ No.104 ランボルギーニ", MALL, False, "🔴 トミカ → 絕不可擋"),
    ("リカちゃん人形 セット", MALL, False, "莉卡娃娃 → 不擋"),
    ("プラレール 車両セット", MALL, False, "普樂路路 → 不擋"),
    ("BEYBLADE X UX-21 玩具", "https://item.rakuten.co.jp/x/ux21/", False,
     "型番中但不是官方商城 → 放行"),
    ("BEYBLADE X BX-46 戰鬥入門套件", "https://www.amazon.co.jp/dp/B0X", False,
     "Amazon → 放行"),
    ("日本足球協會 - UX-00 サムライセイバー5-60K 日本代表足球",
     "https://official-store.jfa.jp/item/1", False,
     "JFA 聯名，型番中但 host 不符 → 放行（站上真實商品）"),
    ("ベイブレードX UX-21", "https://takaratomymall.jp.evil.example.com/x", False,
     "後綴偽裝網域 → 不可命中"),
    # 邊界：CJK 緊貼型番（\b 會失效的那種寫法）
    ("塔卡拉托米 限定BX-46 套組", MALL, True, "CJK 緊貼型番也要中"),
    ("ABX-46 なにか", MALL, False, "前面接英數 → 不是型番"),
    ("UX-1 なにか", MALL, False, "只有一位數 → 不是型番"),
    ("UX-123 なにか", MALL, False, "三位數 → 不是型番"),
]
for t, u, want, why in MALL_CASES:
    r = detect_restricted_category(t, u)
    got = r is not None and r[0] == "hard"
    check(f"商城：{why}", got == want, f"實際回傳 {r}")


# ────────────────────────────────────────────────────────────────────
# 二、端點接線
# ────────────────────────────────────────────────────────────────────
print("\n【二】三個端點的接線")

CARD_TITLE = "ポケモンカードゲーム 拡張パック 熱風のアリーナ"
CARD_URL = "https://www.pokemoncenter-online.com/?p=4521329421162"
OK_TITLE = "無印良品 スタッキングシェルフ・オーク材"
OK_URL = "https://www.muji.com/jp/ja/store/cmdty/detail/4550344932803"

_calls = {"shopify": 0, "seo": 0}


class _FakeShopify:
    async def create_daigo_product(self, **kw):
        _calls["shopify"] += 1
        return {"product_id": 1, "storefront_url": "u", "admin_url": "a"}


async def _fake_seo(**kw):
    _calls["seo"] += 1
    return {"title": "t", "tags": [], "seo_source": "fake"}


def _install(product):
    """把 main 的外部相依全部換掉，回傳被換掉的原值。"""
    async def _fake_scrape(url):
        return product
    saved = (m.shopify, m.generate_seo_title, m.scrape_with_queue, m.cache_get, m.is_no_cache_url)
    m.shopify = _FakeShopify()
    m.generate_seo_title = _fake_seo
    m.scrape_with_queue = _fake_scrape
    m.cache_get = lambda url: product          # ★ 模擬 cache 命中
    m.is_no_cache_url = lambda url: False
    _calls["shopify"] = _calls["seo"] = 0
    return saved


def _restore(saved):
    (m.shopify, m.generate_seo_title, m.scrape_with_queue,
     m.cache_get, m.is_no_cache_url) = saved


card = ProductInfo(title=CARD_TITLE, price_jpy=5500, source_url=CARD_URL)
ok = ProductInfo(title=OK_TITLE, price_jpy=5500, source_url=OK_URL)

# ── /api/scrape ──
saved = _install(card)
res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=CARD_URL)))
_restore(saved)
check("/api/scrape 擋下卡牌：success=False", res.success is False)
check("/api/scrape 擋下卡牌：blocked=True", res.blocked is True)
check("/api/scrape 擋下卡牌：說明講了原因",
      res.error is not None and "抽選" in res.error, f"實際 error={res.error}")
check("/api/scrape 擋下卡牌：不回商品資料", res.product is None and res.pricing is None)

saved = _install(ok)
res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=OK_URL)))
_restore(saved)
check("/api/scrape 放行正常商品", res.success is True and res.blocked is False,
      f"實際 error={res.error}")

# ── /api/create-order（cache 命中路徑）──
saved = _install(card)
res = asyncio.run(m.create_order(m.CreateOrderRequest(url=CARD_URL)))
_restore(saved)
check("/api/create-order 擋下卡牌：success=False", res.success is False)
check("/api/create-order 擋下卡牌：blocked=True", res.blocked is True)
check("/api/create-order 擋下卡牌：**沒有建到商品**", _calls["shopify"] == 0,
      f"create_daigo_product 被呼叫了 {_calls['shopify']} 次")
check("/api/create-order 擋下卡牌：連 SEO 都沒去打", _calls["seo"] == 0)

saved = _install(ok)
res = asyncio.run(m.create_order(m.CreateOrderRequest(url=OK_URL)))
_restore(saved)
check("/api/create-order 放行正常商品且有建商品",
      res.success is True and _calls["shopify"] == 1, f"實際 error={res.error}")

# ★ title_override 不可以拿來繞過：擋的是爬到的 product.title
saved = _install(card)
res = asyncio.run(m.create_order(m.CreateOrderRequest(url=CARD_URL, title_override="普通の雑貨")))
_restore(saved)
check("/api/create-order 改標題也繞不過", res.success is False and _calls["shopify"] == 0)

# ── /api/create-manual ──
saved = _install(card)
res = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title=CARD_TITLE, price_jpy=5500, source_url="")))
_restore(saved)
check("/api/create-manual 擋下卡牌：success=False", res.success is False)
check("/api/create-manual 擋下卡牌：blocked=True", res.blocked is True)
check("/api/create-manual 擋下卡牌：沒有建到商品", _calls["shopify"] == 0)

# ★ 沒有 source_url 也要擋得住（那道 import 不能借用 detect_blocked 區塊裡的）
saved = _install(card)
res = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title="一番くじ 鬼滅の刃", price_jpy=3000, source_url="")))
_restore(saved)
check("/api/create-manual 無 source_url 仍擋得住", res.success is False and res.blocked is True,
      f"實際 error={res.error}")

# ★ 二手平台豁免要能走到端點：同一個標題換成 Mercari 連結就該放行
saved = _install(ok)
res = asyncio.run(m.create_manual_order(m.ManualOrderRequest(
    title="一番賞 弗利倫 公仔 C賞 D賞", price_jpy=3000,
    source_url="https://jp.mercari.com/item/m12345")))
_restore(saved)
check("/api/create-manual 一番賞＋Mercari 連結放行",
      res.success is True and _calls["shopify"] == 1, f"實際 error={res.error}")

# ★ BEYBLADE 官網走 /api/scrape 要擋得下來（純網域規則，標題無關）
bey = ProductInfo(title="ベイブレードX UX-21 セット", price_jpy=3000,
                  source_url="https://beyblade.takaratomy.co.jp/product/ux21")
saved = _install(bey)
res = asyncio.run(m.scrape_product(m.ScrapeRequest(url=bey.source_url)))
_restore(saved)
check("/api/scrape 擋下 BEYBLADE 官網", res.success is False and res.blocked is True,
      f"實際 error={res.error}")

# ★ 同一件商品在 Amazon 上要放行（這正是 2026-07-22 那張成交訂單的形狀）
bey_amazon = ProductInfo(title="BEYBLADE X BX-46 戰鬥入門套件", price_jpy=3000,
                         source_url="https://www.amazon.co.jp/dp/B0XXXX")
saved = _install(bey_amazon)
res = asyncio.run(m.create_order(m.CreateOrderRequest(url=bey_amazon.source_url)))
_restore(saved)
check("/api/create-order 放行 Amazon 的 BEYBLADE",
      res.success is True and _calls["shopify"] == 1, f"實際 error={res.error}")

saved = _install(ok)
res = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title=OK_TITLE, price_jpy=5500, source_url=OK_URL)))
_restore(saved)
check("/api/create-manual 放行正常商品", res.success is True and _calls["shopify"] == 1,
      f"實際 error={res.error}")

# ── 軟擋在三個端點都必須放行（行為與接之前完全一樣）──
soft = ProductInfo(title="【予約】フィギュア 2026年3月発売予定", price_jpy=8800, source_url=OK_URL)
saved = _install(soft)
r1 = asyncio.run(m.scrape_product(m.ScrapeRequest(url=OK_URL)))
r2 = asyncio.run(m.create_order(m.CreateOrderRequest(url=OK_URL)))
r3 = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title=soft.title, price_jpy=8800, source_url=OK_URL)))
_restore(saved)
check("軟擋在三個端點都放行", r1.success and r2.success and r3.success,
      f"scrape={r1.error} order={r2.error} manual={r3.error}")


print(f"\n{'=' * 60}")
print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
if FAIL:
    for n in FAIL:
        print(f"  ❌ {n}")
    sys.exit(1)
print("全部通過")
