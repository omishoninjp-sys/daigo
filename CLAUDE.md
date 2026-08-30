# daigo — 一條連結送到你家

GOYOUTATI 御用達（近江商人株式会社）的日本代購系統。客人貼任何日本商店的商品連結，
系統爬取後在 Shopify 生成商品頁供下單。FastAPI + SeleniumBase，部署在 Zeabur。

Shopify 商店：`fd249b-ba.myshopify.com` / `goyoutati.com`

---

## 用繁體中文回覆

不是日文。程式碼註解也用繁中。

---

## 🔴 工作方式（三條，適用於每一件事）

### 1. 動手前先確認前提成立

**如果發現使用者的假設是錯的，先說，不要照著錯的前提做。**

2026-08 有三次差點為不存在的問題寫程式：
- 「160 件商品沒有 source tag，寫入在漏水」→ 查 `created_at` 發現全部早於 2026-06-11，
  是加這功能之前的存量，不是漏水
- 「`quantity: 0` 代表缺貨要排除」→ 查證發現有 `backOrderDeliveryDateId` 的店家是
  取り寄せ，照樣可下單。照第一版做會讓 19 件商品的選項全部消失
- 「Shopify 變體上限 100，超過要處理」→ 查文件發現是 2048，100 是 2024 年以前的舊值

反例：2026-08 沒先看 log 就批次修好 2,091 件商品的運費區塊，
其中 993 件隨即被 cleanup 刪除，一半白做。**先查再做。**

### 2. 用真實資料驗證，把實際取到的數值貼出來

**「改好了」不算交付，「改好了，這是實際跑出來的數字」才算。**

使用者無法靠讀 200 行 diff 判斷對錯。今天所有被抓到的問題都是靠證據，不是靠讀程式碼：
- 2,064 件的離線演練（確認只有目標段落被動到）
- 7 個誤擋網域的回測（子字串比對的 bug）
- 把測試跑在**壞掉的版本**上先重現，證明測試真的抓得到，再驗修正

所以：印出實際取到的原始數值、跑真實網址、給前後對照。
**樣本要涵蓋要驗的情境** —— 測變體價格就要找真的有多種價格的頁面。

### 3. 不可逆的動作要先問

**`git push`、刪除檔案或資料、批次改線上資料 —— 這三類做之前一定要問。**

其他的自己做，不用逐項確認。

理由：這幾類錯了要花很大力氣復原。2026-08 的 `gql_nodes` NameError 就是
在 auto mode 下自己引入、自己推上線，等線上建單掛掉才發現 ——
沒有人在 push 之前看過那個 diff。

---

## 🔴 費率口徑（最容易寫錯的地方）

**2026/09/01 起最低計費重量由 1kg 提高為 2kg。單價不變，漲的是門檻。**
原因：日本出口端新增爆裂物檢查料金。

代購線國際運費：
- **最低 2kg**，≦2.0kg ¥2,000，每增 0.5kg +¥500（攤平＝¥1,000/kg）
- 含關稅、含台灣配送費
- **依實重計費；材積重在實重 3 倍以內不加收材積費，超過 3 倍才改以材積重計費**
- 絕不可寫成「不收材積費」（無限定）或「一律取大值」

集運線（helpshipping）：NT$220/kg ＋ 理貨費 NT$11/kg，同樣 2kg 起計 → 起跳 NT$440＋理貨費。

**海外線（加拿大／香港／新加坡）不套用 2kg 起計。**

「1kg ¥1,000 / 1.1–1.5kg ¥1,500 / 未滿 1kg 以 1kg 計」是 9/1 前的舊制。
看到這組數字一律要改，除非上下文明確標示為歷史。

二段式收費：商品頁標價只含商品費用，運費於到倉確認實重後另行請款。

---

## 🔴 商品會被自動刪除

`main.py` 的 `_auto_cleanup_loop`：啟動後 60 秒跑第一次，之後每 24 小時一次，
刪除 `DAIGO_COLLECTION_ID` 系列裡超過 `DAIGO_AUTO_DELETE_DAYS`（預設 30）天的商品。

**這是設計不是 bug。** 判斷「某商品為什麼不見了」之前先想到這條。

**要判斷清理有沒有在跑，去看 Zeabur Runtime Logs 搜 `[Cleanup]`，
不要從「舊商品還在」反推。** 2026-08 曾據此誤判「清理從未生效」，
代價是先花 20 分鐘批次修好 2,091 件商品，其中約 993 件隨即被刪。

### 訂單保護

被下單過的商品不論多舊都保留。因為 `read_orders` 只看得到近 60 天訂單
（更早需 `read_all_orders`，要送 Shopify 審核），改用**標籤持久化**：
每次清理前撈近 60 天訂單 → 替涉及商品打上 `已下單` 標籤（永久）→
刪除判斷看 `id 在訂單集合` 或 `帶有該標籤`。

**Fail-closed**：訂單查詢失敗就整輪中止，一件都不刪。
副作用是 token 若失去 `read_orders`，清理會靜默停擺，只能從 log 發現。

**🔴 已知缺口：清理失敗會靜默 24 小時。**
`_auto_cleanup_loop` 的 `except` 之後是 `await asyncio.sleep(24h)`，不是立即重試。
清理每天刪一千多件，失敗時沒有任何對外訊號，只有 Zeabur log 看得出來。
**待辦：等監控信件（`spec-scrape-monitoring.md` 第四節）上線後，把 cleanup 的例外
接進即時警報，成為那三個條件之外的第 4 條。** 在那之前，改完部署後要自己去
Runtime Logs 確認有 `[Cleanup] 完成：掃描 N 件，刪除 N 件…` 那行。

（中斷本身是安全的：沒有 checkpoint、沒有鎖檔，刪除與標籤都是永久且冪等的，
容器重啟後 60 秒就重跑一輪，只是重複讀取。真正的問題只有「例外之後要等一天」。）

**刪任何商品之前一定要先查訂單，不可以只看 `已下單` 標籤。**
標籤是 cleanup 執行時才補上去的 —— 沒跑過清理的期間，被下單過的商品身上不會有標籤。
判斷條件永遠是「近 60 天訂單集合 **或** 標籤」，兩個都要查。
2026-08-30 手動清 5 件首頁假商品時，唯一有訂單的正好是最貴的那件（¥97,404，
訂單 GYT20262543）；只憑標籤或憑直覺刪，那筆訂單就再也對不回商品，
查不到是誰買了什麼。

---

## 抓取架構：Platform registry

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

---

## 🔴 已知雷區（都踩過）

**價格取税込，不是税拔。** SNIDEL 的 JSON-LD `price` 是税拔，要用頁面的
`productNormalPrice` 覆寫。Yahoo 取 `applicablePrice`（税込）不是
`taxExcludedApplicablePrice`。

**巢狀價格欄位不要用「抽字串裡的數字」處理。** Yahoo 的
`individualItemList[].price` 是 dict：`{"applicablePrice":4950,"immediatePrice":4620}`
硬轉字串會變成 `49504620`。真正危險的是 `{990, 890}` → `990890`，
沒超出價格上限，會變成看起來正常的假價直接上架。
`_to_int()` 現在直接拒收 dict/list。

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

**ZOZOTOWN 在 Zeabur 機房 IP 會被 Akamai 擋。** 需要住宅代理或辦公室 IP。

**`/api/scrape` 有快取。** 測試一定要用沒抓過的新連結，或先刪掉舊商品。

**任何子字串比對都要先想清楚會不會誤命中。** 這個專案已經栽過兩次，
形式不同但病因相同：

- **網域比對一律用完整網域或其子網域，絕不可用 `in`。**
  `"t.co" in host` 會命中 `tocco-closet.co.jp`、`golfdigest.co.jp`、`dot-st.com`、
  `newart.co.jp`、`uniformnext.com`、`lilith-soft.com` 等正常商店 ——
  2026-08 寫連結攔截時第一版就是這樣誤擋了 7 家，靠回測才抓到。
- **頁面特徵字比對要加條件，不能只看「有沒有出現」。**
  `"captcha" in html` 會命中每一家 Shopify 商店 —— 正常商品頁內嵌
  `<script id="captcha-bootstrap">`，433KB 的頁面照樣命中。後果是兩邊都錯：
  `generic` 白跑一次 Selenium（慢又常逾時），`scrape_monitor` 把每一家 Shopify
  日本商店的失敗標成 `blocked`，看起來像「該去買住宅代理」。
  現在分強／弱特徵，弱特徵只在頁面小於 50KB 時才算數（真的 challenge 頁都很小）。

**通則：比對前先問「這個字串在正常內容裡會不會自然出現」。**
會的話就要加邊界條件（完整網域、頁面大小、位置），不能只用 `in`。

**Link header 分頁：`rel="next"` 要先切段再取 cursor。**
```python
re.search(r'page_info=([^&>]+).*?rel="next"', link_header)   # ← 錯
```
第 2 頁起 header 是
`<...page_info=PREV>; rel="previous", <...page_info=NEXT>; rel="next"`，
`re.search` 從左邊找，抓到的是 **previous** 的 cursor（base64 解開是
`{"direction":"prev",...}`）。於是在第 1、2 頁之間來回，**永遠不會結束**。
正確做法是先用逗號切段，只看含 `rel="next"` 的那一段 ——
`shopify_client.next_page_info()` 已經封好，一律用它，並加一道 cursor 重複就停的保險。

2026-08-30：一支掃描腳本因此跑了 80 分鐘沒有結束；同一個 regex 當時還在
`/api/admin/cleanup/preview`（不刪東西 → 真無限迴圈）與 `cleanup_old_daigo_products`
（邊掃邊刪，集合縮小才碰巧結束；**某一輪沒東西可刪就會永遠跑下去**）。

**`generic._scrape_with_playwright` 與 `shopify_jp._scrape_shopify_jp` 會互相遞迴。**
Shopify 頁面 → 轉進 Shopify 解析 → 路徑沒有 `/products/` → 退回 generic →
又偵測到 Shopify → 無限循環，每圈重抓一次整頁，直到上層 60 秒逾時。
任何 Shopify 商店的非商品頁連結都會這樣，白佔一個爬取名額整整一分鐘（同時只有 3 個）。
已用 `allow_shopify=False` 旗標切斷 —— **從 shopify_jp 退回 generic 時一定要帶這個旗標。**

**首頁連結會生出「看起來正常、可以下單」的假商品。**
generic 從 og 標籤湊出店名 + 頁面上某件商品的價格，就當成一件商品建出來。
實測把首頁丟給爬蟲，拿到的是**店名**：`decoto.jp/` → 「Decoto(デコット)「ありがとう」をカタチに」¥243；
`fo-online.jp/` → 「子供服・ベビー服 通販のF.O.Online Store」¥8,800。
`detect_invalid_link()` 現在擋首頁與語系首頁（`/zh`、`/en`…），
但**有 query string 一律放行** —— カラーミー 的商品網址是 `/?pid=123456789`，
path 空的卻確實是商品頁。分類頁（`/items?bc=J`）目前擋不掉，仍會生出假商品。

**🔴 但不可以只憑 `source_url` 判定商品是不是假的 —— 手動填寫的商品不適用這條規則。**
`/api/create-manual` 讓工作人員手動建商品，`source_url` 可能是首頁、可能不完整，
之後才在後台補正連結與金額，**商品本身是真的**。

**而且系統沒有記錄商品是怎麼被建立的**：`/api/create-order`（爬取）與
`/api/create-manual`（手動）走同一支 `create_daigo_product`，tags、metafields
完全一樣；連 `source:xxx` 標籤都不能用來分辨 —— 沒帶 `platform_id` 時它會
退而用 `detect_platform(source_url)` 補上。商品刪掉之後 Shopify 也查不到痕跡
（`/products/{id}/events.json` 回 404，全店 Product 事件翻 3,000 筆也沒有）。

2026-08-30 的代價：用「source_url 是首頁」掃出 5 件並刪掉其中 4 件，事後比對
才發現**至少 2 件是手動建的**——「未知 高爾夫球桿 JPX 925 5本套裝 ¥97,404」與
「未知 短褲/童裝 ¥2,217」掛在 Yokumoku（做餅乾的）名下，而實際爬那兩個首頁
只會得到「YOKUMOKU 公式サイト ¥1,998」「ヨックモック公式オンラインショップ ¥650」——
**爬首頁不可能生出高爾夫球桿**，那是人填的。短褲那件已被刪除，救不回來。

所以：
- 判斷商品真假要看**標題／價格與該網域是否相干**，不是只看 source_url 的形狀
- 刪除前一律先查訂單（見上面「訂單保護」），這次唯一有訂單的高爾夫球桿因此逃過一劫
- **已經有來源標記了**（2026-08-30 補的）：metafield `daigo.created_via`，
  `"auto"` = `/api/create-order` 爬取，`"manual"` = `/api/create-manual` 手動填寫，
  `"restored"` = 事後重建的。**兩條路徑都明講**，不可以靠「沒有標記就是自動」推論
  —— 這個欄位之前的舊商品本來就沒有，那樣推論會把全部舊資料誤判成自動。
  用 metafield 不用 tag：tag 會出現在前台，也容易被別的邏輯掃到或被誤刪。
  日後任何「用 source_url 判斷商品品質」的掃描，**先讀這個欄位排除 manual**。

---

## 驗證慣例

**改一個函式的回傳值、參數或行為時，`grep` 函式名把所有呼叫點掃過。**
2026-08-30 兩次同型失誤：

- **`gql_nodes`**：刪掉定義沒檢查 20 行後還有兩處在用，線上 create-order 掛掉
- **cleanup 的 `completed` 欄位**：加了欄位只改 `manual_cleanup`，沒改
  `_auto_cleanup_loop` —— 漏掉的正好是每天在跑、而且前一天真的出事的那條，
  等於那個修正對實際問題完全沒生效

**修了一個 bug 卻沒修到會觸發它的那條路徑，比沒修更糟** —— 因為你以為修好了，
就不會再回頭看它，而問題照樣每天在發生。

**`/code-review` 回報「0 件問題」不等於沒問題。**
它的 verify 階段會把**真實的問題**判成「卻下」，而 CLI 端只拿得到確認清單，
**看不到被駁回的候選、也看不到駁回理由**。
2026-08-30 實例：review 找到「`_graphql` 對 `productSet` 的 5xx 重試會產生重複商品」
—— 那是真的，隨後就改成 `idempotent=False` 了 —— 但它被自己的 verify 駁回，
最終回報 0 件。
**所以跑完 review 一定要自己去 review 頁面看被駁回的那些候選**，
不要只看最後那個數字。

**`py_compile` 通過不算驗證。** 2026-08 的 `gql_nodes` NameError 就是
py_compile 抓不到、只在特定分支才爆的錯：`if color_image_map and gql_nodes:`
會短路，舊解析每個變體 `image: ""` 所以那行從來沒被執行過。

- 改 parser → 拿**真實商品頁**跑，印出實際取到的原始數值
- **樣本要涵蓋要驗的情境**。測變體價格就要找真的有多種價格的頁面，
  不然測了也白測
- 改共用路徑 → 跑 `tests/`，尤其 `verify_create_order.py`
- 修 bug → 先讓測試在**壞掉的版本**上重現，證明測試抓得到，再驗修正
- 批次改線上資料 → 先用公開 `products.json` 離線演練，
  用 `body.replace(OLD,'') == new.replace(NEW,'')` 確認只有目標段落被動到

**改了新商品的生成邏輯，不會影響已建立的舊商品。** 要一起改得另外跑批次。

---

## Shopify 操作

**站上實際永遠優先於文件與記憶。** 判斷某頁是否存在、費率口徑是否一致，
先打網站，不要從 API 資料反推客人看到什麼。

- 商品建立用 GraphQL `productSet`；刪除用 `productDelete`
  （REST 對 >100 變體的商品會 422 拒刪）
- 節流：約 2 req/s。重試走 `ShopifyClient` 的共用退避（最多 5 次，1→2→4→8 秒，
  上限 16，看 `Retry-After`），涵蓋 **429 / THROTTLED / 5xx** 與連線層例外：
  `_graphql()` 內建，REST 分頁用 `_get_with_retry()`。
  其他 GraphQL errors（欄位寫錯、權限不足）**不重試**，重試只會蓋掉真正的錯誤原文。
  - **重試 mutation 的前提是它可以安全重來。** `productDelete` / `tagsAdd` /
    各種查詢都是冪等的，走預設 `idempotent=True`。
    **`create_daigo_product` 的 `productSet` 建立商品用 `idempotent=False`**：
    只重試 429／THROTTLED（Shopify 明確拒收、確定沒執行），5xx 與連線層例外
    一律讓它失敗。理由是代價不對稱 —— 重複建商品是**靜默出錯**，
    沒有人會發現，直到客人買了其中一件而另一件還掛著；建立失敗是**明確的失敗**，
    客人當場看到會再貼一次。**日後新增任何「會產生新東西」的 mutation，
    一律要帶 `idempotent=False`。**
  - 這兩支都是 2026-08-30 才寫的。在那之前**這份文件描述了不存在的機制**：
    「重試要涵蓋 429/THROTTLED/5xx」是從一支一次性腳本抄進來的慣例，正式碼裡沒有。
    跟當時「`detect_invalid_link()` 已經有了」是同一種錯。
    **寫進這份文件之前先確認正式碼裡真的有；沒有的話寫成「還沒有」。**
  （2026-08 批次改 2,086 件時中了一次 503，因為只對 throttle 退避而整件失敗）
- 批次作業一定要有 checkpoint 檔，中斷後可續跑
- storefront 的公開 `products.json` **看不到未上架/草稿商品**
  （實測 Admin API 比 storefront 多 25 件）

### Token

app 是 **Ogura Scraper**（handle `ogura-scraper`），daigo 與 scrapers-monorepo 共用。
另有一把只有 `online_store_pages` / `content` / `translations` 的內容編輯 token，
**沒有任何 products 權限**，別拿去跑商品腳本。

查權限（**路徑不帶 API 版本號**）：
```
https://fd249b-ba.myshopify.com/admin/oauth/access_scopes.json
```

改了 app 的 scope 之後**舊 token 不會自動獲得新權限**，必須重走 OAuth。
redirect 是 `http://localhost:5000/auth/callback`，必須與 app 設定完全一致；
scope 清單是**整份覆蓋**，漏列即失去該權限。

**絕不代為處理憑證。** 使用者貼出 token 時要他立刻撤銷，不要用它做任何事。

---

## 使用者的執行環境：Windows PowerShell 5.1

不是 PowerShell 7，也不是 bash。

| ❌ | ✅ |
|---|---|
| `set VAR=值` | `$env:VAR = "值"` |
| `export VAR=值` | `$env:VAR = "值"` |
| `curl -H ... -d '{"q":"..."}'` | `Invoke-RestMethod -Headers @{} -Body (@{} \| ConvertTo-Json)` |

- `set VAR=值` 在 PowerShell **不報錯也不生效**，靜默失敗
- `curl.exe` 傳 JSON 時引號跳脫會被拆爛。GET 可用 `curl.exe`，帶 body 一律 `Invoke-RestMethod`
- 產出的 `.ps1` **必須 UTF-8 with BOM**，否則 5.1 用 ANSI 解讀，
  中文註解會撞出引號／反斜線，報出完全不相干的語法錯誤
- 執行未簽署腳本：`powershell -ExecutionPolicy Bypass -File .\x.ps1`（只影響單次）

---

## 與使用者協作

使用者（Shan）是工程師本人，也是這家公司的創辦人，聽得懂技術細節，不需要包裝。

- **判斷錯了直接說錯了**，講清楚錯在哪、影響哪些先前結論、哪幾份交付要作廢。不要淡化
- 使用者質疑某個判斷時，**先重驗，不要先辯護**
- 評估的結論是「不要做」也是有價值的結果，不要為了有產出硬改
- 涉及金錢的錯誤要主動指出方向：**低估售價的 bug 不會有人來反映**，
  因為對客人有利
