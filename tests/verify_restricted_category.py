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
]
for t in HARD_TITLES:
    r = detect_restricted_category(t, "")
    check(f"硬擋：{t[:24]}", r is not None and r[0] == "hard", f"實際回傳 {r}")

# 🔴 2026-09-08 由 hard 改判 soft 的兩條。**這兩條原本在 HARD_TITLES 裡**，
#    移過來不是因為規則失效，是因為精確率不夠格硬擋：
#      航海王卡牌 61.5%、一番賞 66.7%（三分之一其實買得到）。
#    soft 的處置是「商品頁照建、不上架」，錯判成本從「買不到」降成「多問一句」。
SOFT_TITLES = [
    "ONE PIECE カードゲーム 新時代の主役",
    "一番くじ ドラゴンボール",
]
for t in SOFT_TITLES:
    r = detect_restricted_category(t, "")
    check(f"軟擋：{t[:24]}", r is not None and r[0] == "soft", f"實際回傳 {r}")

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
# ⚠️ 2026-09-08 起命中的結果是 **soft**（商品照建、不上架），不是 hard。
#    豁免那幾條**仍然是 None** —— 二手現貨是真的買得到，連 soft 都不該掛。
#    這一組的斷言故意分成兩種期望值（"soft" / None），
#    不可以簡化成布林 —— 那樣就分不出「改成 soft」和「規則整條失效」。
ICHIBAN_CASES = [
    ("一番くじ ドラゴンボール", "https://1kuji.com/lineup/xxx", "soft", "官方通路 → 軟擋"),
    ("一番くじ ドラゴンボール", "", "soft", "沒有網址 → 判斷不出來，照軟擋"),
    ("一番賞 弗利倫 公仔 C賞 D賞", "https://jp.mercari.com/item/m123", None, "Mercari 現貨"),
    ("一番賞 まとめ賣", "https://www.mercari.com/jp/items/m1", None, "mercari.com 子網域"),
    ("一番くじ ワンピース", "https://paypayfleamarket.yahoo.co.jp/item/z1", None, "PayPay フリマ"),
    # ⚠️ Yahoo 拍賣已從 _SECONDHAND_HOSTS 移除：純競標與即決網址相同，分不出來，
    #    留在豁免清單會讓純競標頁被放行。即決 scraper 完成後再改回 None。
    ("一番くじ 景品", "https://auctions.yahoo.co.jp/jp/auction/x1", "soft",
     "Yahoo 拍賣 → 不豁免（競標與即決分不出來）"),
    ("一番くじ フィギュア", "https://www.suruga-ya.jp/product/detail/123", None, "駿河屋"),
    ("一番くじ 景品", "https://netmall.hardoff.co.jp/product/1", None, "ハードオフ"),
    # ★ 子字串誤命中的反例：網域裡有 mercari 字樣但不是 mercari
    ("一番くじ 景品", "https://mercari-fan.example.jp/item/1", "soft", "假 mercari 網域 → 照軟擋"),
]
for t, u, want, why in ICHIBAN_CASES:
    r = detect_restricted_category(t, u)
    got = r[0] if r else None
    check(f"一番賞：{why}", got == want, f"期望 {want}，實際回傳 {r}")

# ── 3-2. 一番賞仍然**不可以**是 hard（回歸保險）──
# 只驗「不是 None」會讓「改回 hard」也通過，那就等於沒驗到這次的改動。
r = detect_restricted_category("一番くじ ドラゴンボール", "https://1kuji.com/lineup/x")
check("一番賞：不可以再是 hard", r is not None and r[0] != "hard", f"實際回傳 {r}")
r = detect_restricted_category("ONE PIECE カードゲーム 新時代の主役", "")
check("航海王卡牌：不可以再是 hard", r is not None and r[0] != "hard", f"實際回傳 {r}")

# ── 3-3. 純網域硬擋不受影響：1kuji.com 走的是 detect_restricted_host ──
# 🔴 標題規則改成 soft **不可以**連帶把整域硬擋一起放掉 ——
#    1kuji.com 整站 100% 一番賞、12 個月請到款 0 元，那個判斷是另一支函式。
from scrapers.base import detect_restricted_host as _drh
r = _drh("https://1kuji.com/lineup/xxx")
check("1kuji.com 仍然是純網域 hard", r is not None and r[0] == "hard", f"實際回傳 {r}")
r = _drh("https://jp.mercari.com/item/m123")
check("Mercari 不在純網域硬擋表", r is None, f"實際回傳 {r}")

# ── 3-4. 二手豁免適用於**每一條標題關鍵字規則**（2026-09-09）──
#
# 🔴 這一組釘的是 2026-09-07 的實際事故：豁免機制當天就寫好了，
#    但只套在一番賞一條上。46 小時後客人在 Mercari 的寶可夢卡牌商品頁上
#    收到「請改貼 Mercari 上的現貨連結」—— 叫他去 Mercari，然後擋掉 Mercari。
#    jp.mercari.com/item/m26961089339（¥8,999），六分鐘內重試五次。
#
# ★ 三個方向都要驗，缺一個這組就沒有意義：
#     二手網域 + 命中 → 放行（新行為）
#     官方網域 + 命中 → 照擋（回歸；只驗第一項的話，把規則整條刪掉也會過）
#     沒有網址     + 命中 → 照擋（判斷不出來就不放行，同一番賞）
POKE_CARD = "ポケモンカードゲーム 拡張パック メガブレイブ BOX シュリンク付き"
OP_CARD = "ONE PIECEカードゲーム 世界最強の男 BOX"
CHUSEN_T = "【抽選販売】ポケモンセンターオリジナル ぬいぐるみ"
CHUSEN_ONLY = "抽選販売 スニーカー 27cm"        # 不含卡牌字樣，單驗抽選那條
SOFTWARN = "予約 フィギュア 2026年12月お届け予定"

SECOND_EXEMPT_CASES = [
    # (標題, 網址, 期望, 說明)
    (POKE_CARD, "https://jp.mercari.com/item/m26961089339", None,
     "寶可夢卡牌 @ Mercari（事故原案）"),
    (POKE_CARD, "https://jp.mercari.com/zh-TW/item/m26961089339", None,
     "寶可夢卡牌 @ Mercari /zh-TW 語系前綴（客人第 1–4 次試的形式）"),
    (POKE_CARD, "https://www.suruga-ya.jp/product/detail/220304023", None,
     "寶可夢卡牌 @ 駿河屋"),
    (POKE_CARD, "https://paypayfleamarket.yahoo.co.jp/item/z1", None,
     "寶可夢卡牌 @ PayPay フリマ"),
    (OP_CARD, "https://jp.mercari.com/item/m1", None, "航海王卡牌 @ Mercari"),
    (OP_CARD, "https://www.suruga-ya.jp/product/detail/1", None, "航海王卡牌 @ 駿河屋"),
    (CHUSEN_T, "https://jp.mercari.com/item/m2", None, "抽選（卡牌）@ Mercari"),
    (CHUSEN_ONLY, "https://jp.mercari.com/item/m3", None, "抽選（非卡牌）@ Mercari"),
    (SOFTWARN, "https://jp.mercari.com/item/m4", None, "予約／お届け予定 @ Mercari"),

    # 回歸：官方通路一件都不可以放掉
    (POKE_CARD, "https://www.pokemoncenter-online.com/x", "hard",
     "回歸：寶可夢卡牌 @ Pokémon Center 仍 hard"),
    (POKE_CARD, "https://www.amazon.co.jp/dp/B0XXXX", "hard",
     "回歸：寶可夢卡牌 @ Amazon 仍 hard"),
    (POKE_CARD, "https://www.30th.pokemon-card.com/x", "hard",
     "回歸：寶可夢卡牌 @ 30th 官方站仍 hard"),
    (OP_CARD, "https://p-bandai.jp/item/x", "soft",
     "回歸：航海王卡牌 @ P-Bandai 仍 soft"),
    (CHUSEN_ONLY, "https://takaratomymall.jp/x", "hard",
     "回歸：抽選 @ 官方商城仍 hard"),
    (SOFTWARN, "https://www.rakuten.co.jp/x", "soft",
     "回歸：予約 @ 一般通路仍 soft"),

    # 回歸：沒有網址（手動建單）判斷不出來 → 照擋
    (POKE_CARD, "", "hard", "無網址：寶可夢卡牌照 hard"),
    (OP_CARD, "", "soft", "無網址：航海王卡牌照 soft"),
    (CHUSEN_ONLY, "", "hard", "無網址：抽選照 hard"),
    (SOFTWARN, "", "soft", "無網址：予約照 soft"),

    # 子字串誤命中的反例：網域含 mercari / suruga 字樣但不是那些站
    (POKE_CARD, "https://mercari-fan.example.jp/item/1", "hard",
     "假 mercari 網域 → 不豁免"),
    (POKE_CARD, "https://suruga-ya.jp.evil.example.com/x", "hard",
     "假 suruga-ya 網域 → 不豁免"),
]
for t, u, want, why in SECOND_EXEMPT_CASES:
    r = detect_restricted_category(t, u)
    got = r[0] if r else None
    check(f"二手豁免：{why}", got == want, f"期望 {want}，實際回傳 {r}")

# ★ 語系前綴的兩種寫法必須得到**同一個結果，而且是放行** ——
#   客人第五次就是把 /zh-TW 拿掉再試一次，以為是網址格式的問題。
#
# 🔴 只斷言 `_a == _b` 是**空的斷言**：豁免整條拿掉時兩邊同樣是 hard，仍然相等。
#    2026-09-09 的缺陷注入抓到這一點 —— 注入「寶可夢那條拿掉豁免」時，
#    這條照樣綠。所以一定要連值一起釘死。
#    （CLAUDE.md：負向驗證全綠時先懷疑測試，fixture 要讓待驗的差異真的出現。）
_a = detect_restricted_category(POKE_CARD, "https://jp.mercari.com/item/m26961089339")
_b = detect_restricted_category(POKE_CARD, "https://jp.mercari.com/zh-TW/item/m26961089339")
check("二手豁免：/zh-TW 與無語系前綴結果一致，且都放行",
      _a is None and _b is None, f"{_a} vs {_b}")

# ★ 網域類規則不受豁免影響（它們本來就碰不到二手網域，這裡釘住不要被順手改壞）
r = detect_restricted_category("ベイブレードX BX-46 ランダムブースター",
                               "https://takaratomymall.jp/x")
check("網域規則：BEYBLADE 官方商城仍 hard", r is not None and r[0] == "hard", f"實際 {r}")
r = detect_restricted_category("ぬいぐるみ 受注 ピカチュウ",
                               "https://www.pokemoncenter-online.com/x")
check("網域規則：Pokémon Center 受注仍 soft", r is not None and r[0] == "soft", f"實際 {r}")

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
    # ⚠️ 這條 2026-09-08 之後改由 _SOFT_HOSTS 接手 → 回 soft，仍然**不是 hard**。
    #    這裡驗的是「型番規則不可以跨網域生效」，那個結論沒變。
    ("日本足球協會 - UX-00 サムライセイバー5-60K 日本代表足球",
     "https://official-store.jfa.jp/item/1", False,
     "JFA 聯名，型番中但 host 不符 → 不可 hard（改由 soft 網域規則接）"),
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

# ── 7. 2026-09-08 新增的兩條 soft 規則 ──
#
# official-store.jfa.jp：12 個月 23 個 line item **全部 VOIDED 且已取消**、
#   請到款 0 元。這 23 件實際上是 BEYBLADE X 聯名（UX-00 サムライセイバー），
#   但 SEO 標題寫成「日本足球協會 運動器材」，**關鍵字一條都不命中**，
#   而 _BEYBLADE_MODEL 只在 takaratomymall.jp 生效 → 只有網域擋得住。
JFA = "https://official-store.jfa.jp/item/1"
SOFT_HOST_CASES = [
    ("日本足球協會 運動器材 - UX-00 サムライセイバー5-60K 足球運動器材", JFA, "soft",
     "真實標題（型番在，但關鍵字全不中）"),
    ("日本代表 レプリカユニフォーム 2026", JFA, "soft",
     "同網域的一般周邊 → 也是 soft（整域規則）"),
    ("日本代表 レプリカユニフォーム 2026", "https://item.rakuten.co.jp/x/1", None,
     "🔴 換個網域就不可以命中"),
    ("なにかの商品", "https://official-store.jfa.jp.evil.example.com/x", None,
     "後綴偽裝網域 → 不可命中"),
    ("なにかの商品", "https://shop.official-store.jfa.jp/x", "soft", "子網域 → 命中"),
]
for t, u, want, why in SOFT_HOST_CASES:
    r = detect_restricted_category(t, u)
    check(f"JFA：{why}", (r[0] if r else None) == want, f"期望 {want}，實際回傳 {r}")

# Pokémon Center 的單獨「受注」。既有的 _SOFT_WARNING 只認「受注生産／受注販売」，
# 而該站的 SEO 標題是單獨的「受注」。12 個月 4 筆＝2 PAID／2 VOIDED（50%）。
POKECEN = "https://www.pokemoncenter-online.com/?p=1"
JUCHU_CASES = [
    ("寶可夢中心 皮卡丘 毛絨玩具 - 受注皮卡丘毛絨玩具", POKECEN, "soft", "真實標題（單獨受注）"),
    ("寶可夢中心 皮卡丘 玩偶 - 受注 皮卡丘玩偶・公仔", POKECEN, "soft", "真實標題（受注＋空白）"),
    ("寶可夢中心 比卡超 毛絨玩具", POKECEN, None,
     "🔴 同網域但沒有受注 → 不可命中（這是有成交的一般周邊）"),
    ("寶可夢中心 桌遊 - 寶可夢探險盒 01 桌遊", POKECEN, None, "一般商品 → 放行"),
    ("なにか 受注 グッズ", "https://item.rakuten.co.jp/x/1", None,
     "🔴 單獨『受注』只在 Pokémon Center 生效，別站不可命中"),
    # 這條在別站要命中，但靠的是既有的 _SOFT_WARNING（受注生産），不是新規則
    ("なにか 受注生産 グッズ", "https://item.rakuten.co.jp/x/1", "soft",
     "受注『生産』→ 既有 soft 規則照舊"),
]
for t, u, want, why in JUCHU_CASES:
    r = detect_restricted_category(t, u)
    check(f"受注：{why}", (r[0] if r else None) == want, f"期望 {want}，實際回傳 {r}")

# 🔴 hard 不可以被新規則波及：卡牌在 Pokémon Center 上仍然是 hard
r = detect_restricted_category("寶可夢 卡牌擴充包 - 受注 MEGA 擴張包", POKECEN)
check("hard 優先於 soft：卡牌＋受注 → 仍是 hard",
      r is not None and r[0] == "hard", f"實際回傳 {r}")
r = detect_restricted_category("【抽選販売】一番くじ 限定", "")
check("hard 優先於 soft：抽選販売＋一番賞 → 仍是 hard",
      r is not None and r[0] == "hard", f"實際回傳 {r}")


# ────────────────────────────────────────────────────────────────────
# 一之三、哨兵價格（detect_sentinel_price）
# ────────────────────────────────────────────────────────────────────
print("\n【一之三】哨兵價格")

from scrapers.base import detect_sentinel_price

# 命中：全庫 1,526 件與 12 個月 2,301 個 line item 裡，
# 除了 dot-st 的 4 件 ¥999,999 之外一件都沒有（見 base.py 的表）
for v in (0, 1, 99_999, 999_999, 1_000_000, 9_999_999):
    r = detect_sentinel_price(v)
    check(f"哨兵命中 ¥{v:,}", r is not None and r[0] == "hard", f"實際回傳 {r}")
    if r:
        check(f"哨兵訊息帶出金額 ¥{v:,}", f"{v:,}" in r[1], f"實際 {r[1][:60]}")

# 🔴 最重要的一組：真實成交價不可以被當成哨兵
#    ¥9,999 曾被列入候選清單，回測發現全庫 9 件、12 個月 3 個 line item
#    全部 PAID 未取消（GYT20262587 一張單三台 GBA：9,999 / 10,000 / 9,999），
#    所以**刻意排除**。這條掉了就代表有人把它加回去了。
NOT_SENTINEL = [
    (9_999,   "Mercari 心理定價，12 個月 3 筆實際成交"),
    (10_000,  "同一張訂單裡的鄰居"),
    (972_612, "★ ASUS Ascent GX10：真實最高價，與 ¥999,999 只差 2.8%"),
    (110,     "全庫最低價（郵票）"),
    (100,     "使用者指定不可納入 —— animate ¥176、郵票 ¥110 都在附近"),
    (415_000, "AIRBOW 音響（ippinkan.jp，走 generic）"),
    (440_000, "CITIZEN 腕錶"),
    (99_998,  "只差 1 圓就不是魔術值"),
    (1_000_001, "只差 1 圓"),
    (999_999.5, "非整數 → 不是那幾個整數魔術值"),
]
for v, why in NOT_SENTINEL:
    check(f"不可誤擋 ¥{v:,}：{why}", detect_sentinel_price(v) is None,
          f"實際回傳 {detect_sentinel_price(v)}")

# None 是「沒抓到價格」，不是哨兵 —— 那條路由呼叫端原本的檢查處理
check("None 不算哨兵（那是抓不到價格）", detect_sentinel_price(None) is None)
check("非數字不算哨兵", detect_sentinel_price("999999") is None)
check("布林不算哨兵", detect_sentinel_price(True) is None and detect_sentinel_price(False) is None)


# ────────────────────────────────────────────────────────────────────
# 一之四、價格範圍收斂成單一數值來源
# ────────────────────────────────────────────────────────────────────
print("\n【一之四】價格範圍單一來源")

from scrapers import base as _b
from scrapers.generic import GenericMixin as _G
from scrapers import amiami as _amiami, animate as _animate, rakuten as _rakuten
from scrapers import platform_zozotown as _zozo

check("generic 的 _PRICE_MIN 指向 base", _G._PRICE_MIN is _b.PRICE_MIN_JPY)
check("generic 的 _PRICE_MAX 指向 base", _G._PRICE_MAX is _b.PRICE_MAX_JPY)
check("amiami 的上限指向 base", _amiami._MAX_PRICE == _b.PRICE_MAX_JPY)
check("animate 的上限指向 base", _animate._MAX_PRICE == _b.PRICE_MAX_JPY)
check("rakuten SKU 的上限指向 base", _rakuten._SKU_MAX_PRICE == _b.PRICE_MAX_JPY)
check("ZOZO 的下限保留自己的 50", _zozo._MIN_PRICE == _b.PRICE_MIN_JPY_YAHOO == 50)
check("三處上限已經一致",
      _G._PRICE_MAX == _amiami._MAX_PRICE == _rakuten._SKU_MAX_PRICE,
      f"{_G._PRICE_MAX} / {_amiami._MAX_PRICE} / {_rakuten._SKU_MAX_PRICE}")

check("price_in_range：全庫最低 ¥110 收得下", _b.price_in_range(110) is True)
check("price_in_range：¥99 在下限外", _b.price_in_range(99) is False)
check("price_in_range：ASUS ¥972,612 收得下", _b.price_in_range(972_612) is True)
check("price_in_range：黏起來的巨數 49504620 擋掉",
      _b.price_in_range(49_504_620) is False)
check("price_in_range：None 是 False", _b.price_in_range(None) is False)
check("price_in_range：True 不可以被當成 1", _b.price_in_range(True) is False)


# ────────────────────────────────────────────────────────────────────
# 二、端點接線
# ────────────────────────────────────────────────────────────────────
print("\n【二】三個端點的接線")

CARD_TITLE = "ポケモンカードゲーム 拡張パック 熱風のアリーナ"
CARD_URL = "https://www.pokemoncenter-online.com/?p=4521329421162"
OK_TITLE = "無印良品 スタッキングシェルフ・オーク材"
OK_URL = "https://www.muji.com/jp/ja/store/cmdty/detail/4550344932803"

_calls = {"shopify": 0, "seo": 0}


_last_kw = {}


class _FakeShopify:
    async def create_daigo_product(self, **kw):
        _calls["shopify"] += 1
        _last_kw.clear()
        _last_kw.update(kw)
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
#   ⚠️ 一番賞 2026-09-08 起是 soft：商品**會**建立，但 success=False、不上架。
saved = _install(card)
res = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title="一番くじ 鬼滅の刃", price_jpy=3000, source_url="")))
_restore(saved)
check("/api/create-manual 無 source_url 仍攔得住（soft）",
      res.success is False and res.soft is True, f"實際 error={res.error}")
check("/api/create-manual soft：商品有建立", _calls["shopify"] == 1)
check("/api/create-manual soft：沒有上架到線上商店",
      _last_kw.get("publish_online_store") is False, f"實際 {_last_kw.get('publish_online_store')}")
check("/api/create-manual soft：不回 checkout_url（那條連結是 404）",
      res.checkout_url is None, f"實際 {res.checkout_url}")
check("/api/create-manual soft：回得了 product_id 給後台", res.product_id == 1)

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

# ── 軟擋接線（2026-09-08 起真的有行為，之前 soft 算出來就被丟掉）──
#
# 🔴 這一段是這次改動的核心。舊版斷言是「三個端點都放行」，
#    那是「soft 沒有被使用」的行為，不是 soft 的正確行為。
soft = ProductInfo(title="【予約】フィギュア 2026年3月発売予定", price_jpy=8800, source_url=OK_URL)

# /api/scrape：**不擋**。這支不寫入任何東西，擋了客人連預覽都看不到，
# 而 soft 的定義就是「商品照建」。判定結果放進 notice 欄位帶出來。
saved = _install(soft)
r1 = asyncio.run(m.scrape_product(m.ScrapeRequest(url=OK_URL)))
_restore(saved)
check("soft /api/scrape 仍然放行", r1.success is True and r1.blocked is False,
      f"實際 error={r1.error}")
check("soft /api/scrape 回得了商品資料", r1.product is not None and r1.pricing is not None)
check("soft /api/scrape 把判定放進 notice",
      r1.notice is not None and "預購" in r1.notice, f"實際 notice={r1.notice}")

# /api/create-order：商品要建立，但不上架，且 success=False（前端會顯示訊息）
saved = _install(soft)
r2 = asyncio.run(m.create_order(m.CreateOrderRequest(url=OK_URL)))
_restore(saved)
check("soft /api/create-order：商品**有**建立", _calls["shopify"] == 1)
check("soft /api/create-order：publish_online_store=False",
      _last_kw.get("publish_online_store") is False,
      f"實際 {_last_kw.get('publish_online_store')}")
check("soft /api/create-order：success=False（客人不能自己結帳）", r2.success is False)
check("soft /api/create-order：soft=True", r2.soft is True)
check("soft /api/create-order：blocked 保持 False（商品確實建了）", r2.blocked is False)
check("soft /api/create-order：不回 checkout_url", r2.checkout_url is None)
check("soft /api/create-order：error 是那段說明",
      r2.error is not None and "預購" in r2.error, f"實際 {r2.error}")

# /api/create-manual：同上
saved = _install(soft)
r3 = asyncio.run(m.create_manual_order(
    m.ManualOrderRequest(title=soft.title, price_jpy=8800, source_url=OK_URL)))
_restore(saved)
check("soft /api/create-manual：商品**有**建立", _calls["shopify"] == 1)
check("soft /api/create-manual：publish_online_store=False",
      _last_kw.get("publish_online_store") is False)
check("soft /api/create-manual：success=False + soft=True",
      r3.success is False and r3.soft is True, f"實際 error={r3.error}")

# ★ 反例：正常商品一定要 publish_online_store=True，
#   不然這個旗標接錯方向也會全綠
saved = _install(ok)
r4 = asyncio.run(m.create_order(m.CreateOrderRequest(url=OK_URL)))
_restore(saved)
check("正常商品：publish_online_store=True",
      _last_kw.get("publish_online_store") is True,
      f"實際 {_last_kw.get('publish_online_store')}")
check("正常商品：success=True 且回得了 checkout_url",
      r4.success is True and r4.checkout_url == "u")
check("正常商品：soft=False", r4.soft is False)


# ────────────────────────────────────────────────────────────────────
# 三、哨兵價格的端點接線（三支都要過）
# ────────────────────────────────────────────────────────────────────
print("\n【三】哨兵價格的端點接線")

SENT_URL = "https://www.dot-st.com/classicalelf/disp/item/293463/"
SENT_TITLE = "Classical Elf 再入荷 JaVa ジャバコラボ 綿100%刺繡半袖T恤"
sent = ProductInfo(title=SENT_TITLE, price_jpy=999_999, source_url=SENT_URL)

saved = _install(sent)
s1 = asyncio.run(m.scrape_product(m.ScrapeRequest(url=SENT_URL)))
_restore(saved)
check("哨兵 /api/scrape：success=False", s1.success is False)
check("哨兵 /api/scrape：blocked=True", s1.blocked is True)
check("哨兵 /api/scrape：不回商品資料", s1.product is None and s1.pricing is None)
check("哨兵 /api/scrape：訊息帶出金額",
      s1.error is not None and "999,999" in s1.error, f"實際 {s1.error}")

saved = _install(sent)
s2 = asyncio.run(m.create_order(m.CreateOrderRequest(url=SENT_URL)))
_restore(saved)
check("哨兵 /api/create-order：success=False + blocked=True",
      s2.success is False and s2.blocked is True)
check("哨兵 /api/create-order：**沒有建到商品**", _calls["shopify"] == 0,
      f"create_daigo_product 被呼叫了 {_calls['shopify']} 次")
check("哨兵 /api/create-order：連 SEO 都沒去打", _calls["seo"] == 0)

# 手動表單：客人自己填 999999 一樣要擋。檢查的是**原價**不是前端算的售價。
saved = _install(ok)
s3 = asyncio.run(m.create_manual_order(m.ManualOrderRequest(
    title="なにかの商品", price_jpy=1_149_998, original_price_jpy=999_999,
    source_url=SENT_URL)))
_restore(saved)
check("哨兵 /api/create-manual：success=False + blocked=True",
      s3.success is False and s3.blocked is True, f"實際 {s3.error}")
check("哨兵 /api/create-manual：沒有建到商品", _calls["shopify"] == 0)

# 沒填 original_price_jpy 時，原價會退回 price_jpy —— 那條路也要擋
saved = _install(ok)
s4 = asyncio.run(m.create_manual_order(m.ManualOrderRequest(
    title="なにかの商品", price_jpy=999_999, source_url=SENT_URL)))
_restore(saved)
check("哨兵 /api/create-manual：沒填原價時退回 price_jpy 也擋得住",
      s4.success is False and s4.blocked is True, f"實際 {s4.error}")
check("哨兵 /api/create-manual：沒有建到商品（退回路徑）", _calls["shopify"] == 0)

# 🔴 回歸：真實成交價一定要走得完全程，三支都是
asus = ProductInfo(title="ASUS Ascent GX10 迷你電腦 AI開發者用", price_jpy=972_612,
                   source_url="https://www.amazon.co.jp/dp/B0ASUS")
saved = _install(asus)
a1 = asyncio.run(m.scrape_product(m.ScrapeRequest(url=asus.source_url)))
_restore(saved)
check("★ ASUS ¥972,612 /api/scrape 放行",
      a1.success is True and a1.blocked is False, f"實際 {a1.error}")
saved = _install(asus)
a2 = asyncio.run(m.create_order(m.CreateOrderRequest(url=asus.source_url)))
_restore(saved)
check("★ ASUS ¥972,612 /api/create-order 建得出商品",
      a2.success is True and _calls["shopify"] == 1, f"實際 {a2.error}")

gba = ProductInfo(title="Game Boy Advance SP 星光金 限定版", price_jpy=9_999,
                  source_url="https://jp.mercari.com/item/m29885564646")
saved = _install(gba)
g1 = asyncio.run(m.create_order(m.CreateOrderRequest(url=gba.source_url)))
_restore(saved)
check("★ Mercari GBA ¥9,999 建得出商品（12 個月實際成交過）",
      g1.success is True and _calls["shopify"] == 1, f"實際 {g1.error}")


# ────────────────────────────────────────────────────────────────────
# 四、_publish_to_all_channels 的排除邏輯（離線，假 httpx client）
# ────────────────────────────────────────────────────────────────────
print("\n【四】銷售管道排除")

from shopify_client import ShopifyClient


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class _FakeClient:
    """依 query 內容回不同的假結果，並記下 publishablePublish 送出的 input。"""

    def __init__(self, channels_ok=True, channel_handle="online_store"):
        self.channels_ok = channels_ok
        self.channel_handle = channel_handle
        self.published = None
        self.publish_called = 0

    async def post(self, url, headers=None, json=None):
        q = (json or {}).get("query", "")
        if "channels(" in q:
            if not self.channels_ok:
                return _FakeResp({}, status=500)
            return _FakeResp({"data": {"channels": {"edges": [
                {"node": {"id": "gid://shopify/Channel/111", "handle": self.channel_handle}},
                {"node": {"id": "gid://shopify/Channel/222", "handle": "shop-72"}},
            ]}}})
        if "publications(" in q:
            return _FakeResp({"data": {"publications": {"edges": [
                # ★ 名稱刻意用繁中「線上商店」—— 這家店的 Admin 語系就是這樣，
                #   用 "Online Store" 比對名稱會一個都對不到
                {"node": {"id": "gid://shopify/Publication/111", "name": "線上商店"}},
                {"node": {"id": "gid://shopify/Publication/222", "name": "Shop"}},
                {"node": {"id": "gid://shopify/Publication/333", "name": "Inbox"}},
            ]}}})
        if "publishablePublish" in q:
            self.publish_called += 1
            self.published = [x["publicationId"]
                              for x in (json.get("variables") or {}).get("input", [])]
            return _FakeResp({"data": {"publishablePublish": {"userErrors": []}}})
        return _FakeResp({})


def _run_publish(fake, publish_online_store):
    import contextlib
    import httpx as _httpx

    @contextlib.asynccontextmanager
    async def _ac(*a, **kw):
        yield fake

    saved = _httpx.AsyncClient
    _httpx.AsyncClient = _ac
    try:
        c = ShopifyClient.__new__(ShopifyClient)
        c.graphql_url = "https://x/admin/api/2024-10/graphql.json"
        c.headers = {}
        asyncio.run(c._publish_to_all_channels(
            123, publish_online_store=publish_online_store))
    finally:
        _httpx.AsyncClient = saved


f = _FakeClient()
_run_publish(f, True)
check("一般商品：三個管道全發布", f.published is not None and len(f.published) == 3,
      f"實際 {f.published}")
check("一般商品：線上商店有在裡面",
      f.published is not None and "gid://shopify/Publication/111" in f.published)

f = _FakeClient()
_run_publish(f, False)
check("soft：線上商店被排除",
      f.published is not None and "gid://shopify/Publication/111" not in f.published,
      f"實際 {f.published}")
check("soft：其他管道照發", f.published is not None and len(f.published) == 2,
      f"實際 {f.published}")

# 🔴 fail-closed：認不出線上商店時，**整個發布跳過**，不是照發全部
f = _FakeClient(channels_ok=False)
_run_publish(f, False)
check("soft fail-closed：channels 查不到 → 一個都不發", f.publish_called == 0,
      f"publishablePublish 被呼叫了 {f.publish_called} 次")

f = _FakeClient(channel_handle="not_online_store")
_run_publish(f, False)
check("soft fail-closed：認不出 online_store handle → 一個都不發", f.publish_called == 0,
      f"publishablePublish 被呼叫了 {f.publish_called} 次")

# 反例：channels 掛掉時，**一般商品**不可以被連累
f = _FakeClient(channels_ok=False)
_run_publish(f, True)
check("channels 掛掉不影響一般商品發布", f.publish_called == 1 and len(f.published) == 3,
      f"實際 called={f.publish_called} {f.published}")


print(f"\n{'=' * 60}")
print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
if FAIL:
    for n in FAIL:
        print(f"  ❌ {n}")
    sys.exit(1)
print("全部通過")
