---
paths:
  - "scrapers/**"
---

# 爬蟲架構、取價與變體規則

從 CLAUDE.md 的「抓取架構」與「已知雷區」拆出來的，只在動 `scrapers/` 時載入。
每一條都踩過，不是預防性建議。

## 架構

`scrapers/platform.py` 定義 Platform 抽象，一個 Platform 底下可有多個 Source
依序嘗試（官方 API / SSR / Selenium / Playwright 退路）。
`scrapers/__init__.py` 註冊，`LegacyPlatform` 是 catch-all，把 40+ 支舊 Mixin 原樣接住。

已抽出的真 Platform：zozotown、amiami、bookoff、snidel、muji、yahoo_store。

**不要以「把剩下的 Mixin 都抽成 Platform」為目標。** 2026-08 的營收分析顯示
長尾 20+ 支各佔不到 0.5%，抽了沒有回報。抽 Platform 的標準是
「這支能換到結構性覆蓋或穩定性」，不是「還沒抽」。

### 各來源實際營收（2026-08，近 60 天）

| 來源 | 營收佔比 |
|---|---|
| item.rakuten.co.jp | 18.5% |
| jp.mercari.com | 12.6% |
| amazon.co.jp | 10.6% |
| generic（260 個網域的長尾） | 約 40% |
| zozo.jp | 5.4% |
| store.shopping.yahoo.co.jp | 2.9% |

**generic 是長尾不是黑洞。** 260 個網域，最大的只佔 13%。
那正是「一條連結送到你家」的產品定義，槓桿在通用抓取器的成功率，
不在多寫幾支 Platform。

## 取價

**價格取税込，不是税拔。** SNIDEL 的 JSON-LD `price` 是税拔，要用頁面的
`productNormalPrice` 覆寫。Yahoo 取 `applicablePrice`（税込）不是
`taxExcludedApplicablePrice`。

**generic 取價不可以只認「N円(税込)」，更不可以對候選取 `min()`。**
真價常寫成「税込 8,250 円」（税込在前）、`SALE5,500円`、`¥5,500`，都不帶那個樣式；
而帶「税込」的多半是代引手数料／送料／購物袋價，`min()` 保證挑中手續費。
現行是「收集→脈絡排除→分級決策」，取不到可信價回 `None`。**改成 `max()` 更糟**（免運門檻）。

**排除關鍵字要分前後綴，前綴窗口在句界截斷。** 費用類在數字前（「手数料は…330円」），
門檻類在數字後（「8,800円以上」）。不分前後會把「¥5,500 送料無料」的真價殺掉；
不截句界，「送料は500円です。商品代金3,300円」的 3,300 會讀到前一句的「送料」。

**一致性檢查（`max/min`）不可以套在 DOM 選擇器那一級。** 商品頁**合法地**列出相關商品
價格，實測會誤殺 40 個網域裡的 6 個。該級改用「文件順序第一個 + 離群檢查」
（首個與其餘中位數差 5 倍以上才否決）；整頁掃文字的那幾級才用 `max/min`。

**巢狀價格欄位不要用「抽字串裡的數字」處理。** Yahoo 的
`individualItemList[].price` 是 dict：`{"applicablePrice":4950,"immediatePrice":4620}`
硬轉字串會變成 `49504620`。真正危險的是 `{990, 890}` → `990890`，
沒超出價格上限，會變成看起來正常的假價直接上架。
`_to_int()` 現在直接拒收 dict/list。

**同病：金額 regex 的千分位要限定 3 位一組。** `[0-9][0-9,]*` 會把
「1966,1967,1971」這種逗號清單黏成一個數字。用
`(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)`。

## 變體

**樂天官方 API 沒有變體。** 官方文件與 FAQ 明講不提供 SKU 單位價格與色/尺寸。
但商品頁 HTML 內嵌完整 SKU JSON（`sku` / `variantSelectors` /
`variantMappedInventories`），httpx 直接拿得到，含逐變體價格、庫存、圖片。
不要為了「用官方 API 比較穩」而改，那會讓有變體的商品全部退化成單品。

**`quantity == 0` 不等於不能買。** 樂天有 `backOrderDeliveryDateId` 的店家
是取り寄せ／受注生產，庫存長期為 0 仍可下單。判斷條件是
`quantity > 0 OR 有 backOrderDeliveryDateId`。
頁面的 `isSoldOut` 在 SSR 裡全是 false（前端另外打 API 才填），不能用。

**缺貨排除不可以把商品變成零變體。** 兩道保險：全部判定缺貨時不刪選項，
只把整件標 `in_stock=False`；只剩 1 個有貨的 SKU 也保留該變體，
不要退成無選項單品（客人會看不出買到哪一款）。

**笛卡爾積會生出不存在的組合。** 有真實 SKU 清單就用，別自己組合軸。

**變體上限向 Shopify 查（`shop.resourceLimits.maxProductVariants`），不要寫死。**
目前是 2048，不是舊的 100。
