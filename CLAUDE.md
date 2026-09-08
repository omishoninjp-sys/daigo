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

### 2-1. 🔴 跨層註解不可信，除非附了驗證日期

**後端註解描述前端行為、或前端註解描述後端行為，一律當成「沒有驗證過」。**
要寫這種註解，必須先實際跑過另一層，並在註解裡附上驗證日期。

2026-09-07 的實例：`main.py` 的 `ScrapeResponse.blocked` 上寫著

```python
blocked: bool = False  # ← True 表示此網站被封鎖（前端應顯示錯誤訊息，不要切到「手動填寫」UI）
```

**這句是假的。** 抓線上正在服務的 `assets/daigo.js`（19,659 bytes）全檔搜尋，
`blocked` 出現 **0 次**；實際邏輯是
`if (!data.success || !data.product || !data.product.title) { showManualForm(url); return }`。
也就是四則硬擋訊息（寶可夢卡牌／BEYBLADE／一番賞／抽選販售）**從來沒有顯示給任何客人看過**，
全部被當成「抓取失敗」切到手動填寫表單。

代價不只是一個 bug：那一整天的攔截驗證都建立在「後端回 blocked=true 就等於客人被擋下」
這個錯誤前提上，`/api/scrape` 測出來的 4/6 通過因此**高估了實際效果**。
**註解會變成下一輪推理的前提，錯的註解比沒有註解更貴。**

怎麼寫才算數：

```python
# 前端行為（2026-09-07 抓線上 assets/daigo.js 實測）：
#   blocked=True  → showError(error, handoff_url)，停在輸入步驟
#   blocked=False 且沒有 title → showManualForm()
```

同一條規則反向也成立：`daigo.js` 裡不可以寫「後端會擋掉 X」而沒驗過。
**驗的方法是打一次 API 看回應，或抓一次線上資產看原始碼，不是讀對面的原始碼推論。**

#### 🔴 「拿不到／不支援／會失敗」比「拿得到」更需要證據

**任何否定式斷言都要附上實測日期與實測方式，否則不要寫。**

不對稱在哪裡：

| 斷言 | 寫錯了會怎樣 |
|---|---|
| 「這個拿得到」 | 下一個人去拿，**當場失敗**，五分鐘內就發現 |
| 「這個拿不到」 | 下一個人**根本不會去試** —— 錯誤會活很久，而且沒有任何訊號 |

否定式斷言會讓人**停止嘗試**，所以它自帶一層保護，讓自己不會被推翻。

2026-09-08 的實例：`brake_log.py` 檔頭寫著

```python
#   email、地址同屬受保護的客人資料，一樣拿不到。
```

**這句是猜的。** 真正實測過的只有 `customer { id }`（回 ACCESS_DENIED），
`email` 是從「它們同屬受保護客人資料」推論出來的。當天實際打近 30 天 245 筆訂單：

```
order.email                                 245/245 拿得到
order.clientIp                              245/245 拿得到
customerJourneySummary.customerOrderIndex   245/245 拿得到
```

**`read_customers` 擋的是 `customer` 這個物件，不是訂單自己的那幾個欄位。**
差一點就據此去申請一個根本不需要的受保護資料 scope。

怎麼寫才算數：

```python
# 2026-09-08 實測（近 30 天 245 筆訂單，打正式站）：
#   customer { id }  → ACCESS_DENIED（需要 read_customers）
#   email / clientIp / customerJourneySummary → 245/245 拿得到
# ★ 只驗過 customer{id}，不要據此推論同類欄位 —— 已經錯過一次。
```

**推論不算實測。** 兩個東西「屬於同一類」不代表它們受同一個開關管；
要斷言拿不到，就去拿一次，把錯誤訊息原文貼上來。

同型的舊教訓：〈`SHOPIFY_ACCESS_TOKEN` 沒有 `read_all_orders`〉那條之所以可信，
正是因為它附了實測 —— 一般查詢 497 筆、bulk 也 497 筆、連接器 1,309 筆，
三個數字擺在一起才證明得了「靜默截斷」。

### 3. 線上寫入要分級，不是一律問或一律不問

| 級別 | 例子 | 做法 |
|---|---|---|
| **可逆、且用於止血** | 商品下架／改 DRAFT／停用功能開關 | **可自行執行，但事後立即揭露**（做了什麼、為什麼、怎麼還原） |
| **不可逆** | 刪除商品／訂單／檔案 | **一律先問** |
| **內容變更** | 文章、頁面、theme、SEO 欄位 | **一律先問**（即使只改一句） |
| **`git push`** | 見下 | **看那個 repo 會不會自動部署** |

判準是「錯了能不能還原」，不是「動作大小」。
下架一件商品比刪掉它安全一個數量級 —— 前者一個欄位就改回來，後者救不回來。

**`git push` 不是一律先問，看副作用：**

| repo | 推上去會發生什麼 | 做法 |
|---|---|---|
| `daigo` | GitHub → **Zeabur 自動部署**，push 等同上線 | **先問** |
| `gyt-ops-analysis` 等純文件／分析 repo | 只是存檔，沒有任何自動行為 | **不用問** |

🔴 **規則太寬會產生噪音，而噪音會讓真正該問的時候被忽略。**
每一次「要不要 push 分析文件」的詢問，都在稀釋「要不要部署到正式環境」那一問的份量。
所以判準始終是**這個動作的副作用可不可逆**，不是動作的名字叫什麼。

**止血那一格為什麼可以自己做**：發現線上有東西正在造成傷害時，
先把傷害停掉、再報告，比先報告、等回覆、期間持續受害好。
但**事後必須主動講**，不能等對方問起。

2026-09-07 實例：輪詢誤建 13 件可購買的假商品，我先把它們改成 DRAFT
（止血、可逆）再報告；刪除則等指示才做（不可逆）。

理由：不可逆的錯了要花很大力氣復原。2026-08 的 `gql_nodes` NameError 就是
在 auto mode 下自己引入、自己推上線，等線上建單掛掉才發現 ——
沒有人在 push 之前看過那個 diff。

### 3-1. 🔴 驗證「攔截是否生效」一律用 `/api/scrape`

**絕對不可以用 `/api/create-manual` 或 `/api/create-order` 當探針。**

`/api/scrape` 同樣會跑 `detect_restricted_category`、同樣回 `blocked=true`，
但**永遠不寫入 Shopify**。另外兩支的語義是「建立商品」，
被擋只是它們的失敗路徑之一。

**用可寫入的端點去驗證「一個還沒生效的攔截」，等於在生效前的每一次探測
都執行一次真實寫入。** 這不是機率問題，是必然 ——
輪詢的前提就是攔截還沒生效。

2026-09-07 實例：用 `create-manual` 每 20 秒輪詢等部署生效，
生效前的 13 次探測**各建立了一件真實可購買的商品**，
第 18 次才等到攔截。事後全部刪除，所幸無人下單。

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

## 售價運算

一律呼叫 `pricing.py` 的 `calculate_selling_price()`。
倍率表在 `config.py:PRICING_TIERS`，最低服務費在 `MIN_SERVICE_FEE_JPY`。
**不要自行重算、不要 hardcode 倍率數字、不要在別處重複加價。**
有變體時每個變體用自己的原價各跑一次，不是拿外層已加價的值再乘。

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

> 同一個限制也讓**訂單分析**用這把 token 會靜默拿到截斷的資料，
> 見下面〈`SHOPIFY_ACCESS_TOKEN` 沒有 `read_all_orders`〉。

每次清理前撈近 60 天訂單 → 替涉及商品打上 `已下單` 標籤（永久）→
刪除判斷看 `id 在訂單集合` 或 `帶有該標籤`。

**Fail-closed**：訂單查詢失敗就整輪中止，一件都不刪。
副作用是 token 若失去 `read_orders`，清理會靜默停擺，只能從 log 發現。

**失敗要看兩種，不是只有例外。** `cleanup_old_daigo_products` 的四條中止路徑
（`DAIGO_COLLECTION_ID` 未設定／訂單查詢 fail-closed／分頁重試用盡／cursor 重複）
**全部是 `return` 不是 `raise`**，`_auto_cleanup_loop` 的 `except` 一條都攔不到。
判斷中止一律看回傳值的 `completed` 欄位。2026-08-30 少刪 611 件正是這一類 ——
它在程式碼裡「看起來像正常回傳」，最容易被日後重構漏掉。

**失敗會退避重試，不再等一整天（2026-09-02 起）。**
連續失敗 30分 → 1時 → 2時 → 4時 → 6時（上限），成功就重置回 24 小時節奏。
A 類（例外）與 B 類（`completed=False`）**兩種都會觸發**。
30 分鐘起跳不是保守：`ShopifyClient` 的節流退避是**全域共用**的，cleanup 一輪要打
上千次 API，密集重試會把額度吃光，而同一個容器裡還跑著客人的建單流程 ——
`create_daigo_product` 開始吃 429 才是真正會痛的地方。

**🔴 但「沒有對外訊號」仍然成立。** 失敗只有 `print`，要看得去 Zeabur Runtime Logs
搜 `[AutoCleanup]`（退避中會印「⏳ 連續失敗 N 次，M 分鐘後重試」，恢復會印「↩️ 已恢復」）。
**待辦：等監控信件（`spec-scrape-monitoring.md` 第四節）上線後，把 cleanup 的失敗
接進即時警報，成為那三個條件之外的第 4 條。** 在那之前，改完部署後要自己去
Runtime Logs 確認有 `[Cleanup] 完成：掃描 N 件，刪除 N 件…` 那行。

（中斷本身是安全的：沒有 checkpoint、沒有鎖檔，刪除與標籤都是永久且冪等的，
容器重啟後 60 秒就重跑一輪，只是重複讀取 —— 這也是退避可以放心重跑的前提。）

**刪任何商品之前一定要先查訂單，不可以只看 `已下單` 標籤。**
標籤是 cleanup 執行時才補上去的 —— 沒跑過清理的期間，被下單過的商品身上不會有標籤。
判斷條件永遠是「近 60 天訂單集合 **或** 標籤」，兩個都要查。
2026-08-30 手動清 5 件首頁假商品時，唯一有訂單的正好是最貴的那件（¥97,404，
訂單 GYT20262543）；只憑標籤或憑直覺刪，那筆訂單就再也對不回商品，
查不到是誰買了什麼。

---

## 抓取架構：Platform registry

**Platform registry 的架構與各來源營收佔比搬到**
**[`.claude/rules/scraping-price.md`](.claude/rules/scraping-price.md) 的「架構」小節。**

---

## 🔴 已知雷區（都踩過）

**爬蟲取價與變體的規則搬到 [`.claude/rules/scraping-price.md`](.claude/rules/scraping-price.md)** ——
税込口徑、generic 取價的收集／排除／分級、巢狀價格與千分位 regex、樂天變體、
缺貨判定、笛卡爾積、變體上限。那份只在動 `scrapers/` 時載入。

🔴 **path-scoped rules 由 Read 工具觸發載入，用 bash（`grep`/`cat`/`sed`）讀檔不會觸發。**
全程用 bash 處理的 session 可能整場都不載入規則檔 —— 這兩份拆出去的內容
就等於不存在。要確保載入，動手前先用 Read 讀一次目標檔案。

**`daigo.original_price_jpy` 只在建立商品時寫一次，之後沒有任何機制更新它。**
人工在後台改價只動 variant price，metafield 會永久停在錯的值。所以
**「售價 == 原價 + fee(原價)」只證明沒人改過，不證明原價是對的**，不可當正確性判準；
真正的錯價要靠重跑爬取比對。

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
`/api/create-manual` **不是工作人員用的，是客人用的** —— 爬取失敗時前端
（`daigo.js` 的 Step 1b 手動表單）讓**客人自己**填商品名稱與日幣價格，
`source_url` 帶的是客人原本貼的那條連結，可能是首頁、可能不完整，
之後才在後台補正連結與金額，**商品本身是真的**。
🔴 因為填的人是客人不是工作人員，這條路徑的 `detect_restricted_category`
**必須照擋**（爬取失敗的卡牌會從這裡溜進來），不可以因為「手動」就放寬。

**而且系統沒有記錄商品是怎麼被建立的**：`/api/create-order`（爬取）與
`/api/create-manual`（爬取失敗時客人自己填）走同一支 `create_daigo_product`，tags、metafields
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

## 🔴 會寫檔或寫資料的模組，路徑一律在「呼叫時」解析

**不要在 import 當下就把目的地綁死 —— 那樣測試改不了目的地。**

```python
# ❌ import 就定死。測試在 import 之後才設環境變數，已經來不及
_LOG_DIR_CANDIDATES = [os.environ.get("X_LOG_DIR") or "/data/x_log", "./x_log"]

# ✅ 呼叫時才讀
def _log_dir_candidates():
    return [os.environ.get("X_LOG_DIR") or "/data/x_log", "./x_log"]
```

2026-09-08 的實例：`brake_log` 的紀錄目錄在 import 時算好，於是六支走建單
端點的測試（`verify_blocked_flag` 等）把假商品（「無印良品…」「未知 高爾夫
球桿」之類）寫進**正式的紀錄路徑** `/data/brake_log`。
沒有人會為了跑測試去設環境變數 —— 因為設了也沒用。
後果不是壞掉，是**污染**：第一份 key 分佈裡混進測試資料，
而那份分佈正是要拿來定門檻的東西。

**同一類的錯**（形式不同，病因相同：「測試」「本機」這兩個詞給人的安全感是假的）：

- 2026-09-07 用 `create-manual` 輪詢部署狀態，在正式商店建了 13 件
  可購買的假商品（見〈本機開發〉第 3 條）
- 2026-09-07 `python main.py` 起服務，60 秒後 `_auto_cleanup_loop`
  準備刪正式商品（同上第 1 條）

**判準：這個模組寫出去的東西，測試有沒有辦法把它導到別的地方去？**
沒有就是設計錯了，不是測試該多小心。

適用範圍不只 log：快取檔、匯出檔、資料庫路徑、暫存目錄、上傳目的地 ——
**任何「決定寫到哪裡」的值都算。**

（`scrape_monitor` 目前仍是 import 時算的。沒有一起改是因為它現在沒有這個
需求，改了要重跑它自己的 111 項；動到它的時候順手改掉。）

---

## 🔴 監控要記「選擇前的候選集」，不是「最終選擇」

**只記最終選擇，就永遠看不到「選擇本身選錯了」這一類錯誤。**
最終值看起來永遠是自洽的 —— 它就是程式挑出來的那個。

2026-09-07 的實例（auctions.yahoo 取價）：

| 記什麼 | 看到的 | 結論 |
|---|---|---|
| 只記各規則的最終 `chosen` | `{"R3":3850,"R4":3850,"R5":3850}`，spread **1.0** | 「完全一致，沒問題」 |
| 記候選集 `vals` | `{"R3":[3850,4950],"R5":[3850,4950]}`，spread **1.286** | 頁面同時有現在価格與即決価格，規則取 `min` 挑了買不到的那個 |

同一次爬取、同一份頁面，**差別只在記錄的粒度**。
第一種記法下這個錯誤是隱形的：`ok=True`、五個 `failure_kind` 一個都不命中、
價格也「合理」。而且低估售價對客人有利，**不會有人來反映**。

推廣到其他地方：

- 取價 → 記各規則的候選清單，不是只記選出的那個
- 選 Source（platform.py 逐個試）→ 記每個 Source 的結果，不是只記命中的那個
- 選變體 → 記被排除的變體與原因，不是只記留下來的
- 品類判定 → 記命中的規則名，不是只記 hard/soft

判準：**如果某個欄位的值永遠等於程式的輸出，那它驗不了程式對不對。**
要驗，就得記下「程式在做決定之前看到了什麼」。

（同型的舊教訓：`error_brief` 在 `ok=True` 時被清空，於是成功路徑上的
`note_error` 全部消失 —— MUJI 圖片機制靜默壞掉六週就是這樣沒被發現的。
後來加了 `warnings` 欄位才補起來。）

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

**負向驗證全綠時要先懷疑測試，不是接受結果。** 2026-09-02 對「選擇器優先序反轉」
注入缺陷，第一次跑 37/0 全綠 —— 因為 fixture 兩邊洗完是同一個答案，那條斷言
根本驗不到差異。**fixture 要用兩邊結果確實不同的真實資料。**

**懷疑的第一個對象是 fixture** —— 它是不是讓待驗的差異根本不會出現。
2026-09-03～04 又中三次：注入 cp950 全綠（樣本含「・」，cp950 編不出來→走 UTF-8 退路）／
連續天數的測試組最後一個剛好最嚴重，分不出「取最嚴重」與「取最後一個」／
FakeResp 少一個 cookies 欄位，無關的 AttributeError 混進 error_brief。
**`in`／子字串斷言最危險**，無關的東西混進來就鬆到沒意義；能比對完整字串就不要用 `in`。

**改了新商品的生成邏輯，不會影響已建立的舊商品。** 要一起改得另外跑批次。

---

## Shopify 操作

**Shopify API 的操作規則搬到 [`.claude/rules/shopify-ops.md`](.claude/rules/shopify-ops.md)** ——
站上實際優先於文件、`productSet`／`productDelete`、節流與重試退避與 `idempotent` 取捨、
批次 checkpoint、`products.json` 看不到草稿、API 金鑰三把、Token 與 OAuth。
那份在動任何 `.py` 時載入 —— 一次性掃描腳本也直接用 `ShopifyClient`。

**絕不代為處理憑證。** 使用者貼出 token 時要他立刻撤銷，不要用它做任何事。

### 🔴 GID 只能比數字，不可以比整條字串

**同一個資源在不同查詢裡的 GID 型別前綴不一樣。**

```
channels     → gid://shopify/Channel/112334471402
publications → gid://shopify/Publication/112334471402
```

同一個「線上商店」管道，數字相同、前綴不同，**直接比字串永遠不相等**。
2026-09-08 寫 soft 品類的「不發布到線上商店」時就是這樣：排除清單一個都對不到，
每一件 soft 商品都掉進 fail-closed 分支、一個銷售管道都沒發布。

**是離線測試（假 httpx client）抓到的，讀程式碼看不出來** ——
兩邊都寫著 `id`，型別藏在字串裡面。

比對一律先取最後一段：`str(gid).rsplit("/", 1)[-1]`。

### 🔴 publication 的 `name` 會隨呼叫者的語系變，只能比 `channels` 的 `handle`

2026-09-08 同一天、同一個 publication（id `112334471402`）：

| 呼叫者 | 回傳的 `name` |
|---|---|
| Shopify 連接器（商家身分，店家語系繁中） | 「線上商店」 |
| 本 app 的 token | `"Online Store"` |

比對哪一邊都會在另一邊失效。`channels` 的 `handle`（`online_store`）兩邊都一樣，
而且 `Channel.id` 與 `Publication.id` 是同一個數字（見上一條）。

**失效的方向是最壞的那種：「以為排除了、其實照樣上架」** ——
不該賣的東西會安靜地變成可以結帳，沒有任何錯誤訊息。
所以這類判斷一律 fail-closed：認不出目標管道時整個發布跳過，不是照發全部。

### 🔴 一個字串是另一個的前綴時，天真替換會把「已經對的」改壞

2026-09-08 修加購頁的死連結：

    錯的 handle   goyoutati-安心購
    對的 handle   goyoutati-安心購-安心go      ← 錯的是對的的**前綴**

直接 `body.replace(錯, 對)` 會把**已經正確**的那些也再接一次，
變成 `goyoutati-安心購-安心go-安心go`。搜尋端同樣中招：搜「錯的」
會把「對的」一起數進來，於是「有幾處要修」這個數字本身就是錯的。

替換樣式必須排除「後面接著剩餘部分」的情況：

```python
pat = re.compile(re.escape(OLD) + "(?!" + re.escape(SUFFIX) + ")")
```

**這和 `hoka.com` 誤擋 `hoka.com.tw` 是同一種病**（子字串沒有邊界），
只是發生在**替換**而不是比對。比對錯了會誤擋，替換錯了會**改壞已經對的資料**，
而且改完看起來很像成功。

★ 一律配 CLAUDE.md 的批次改資料演練式一起用，它抓得到這種錯：
  `body.replace(OLD,'') == new.replace(NEW,'')`，
  再加一條「替換後不可以還有未帶後綴的 OLD」。

### 🔴 掃 HTML 內文時，`<script>` 的內容是文字節點，不會被 `<[^>]+>` 剝掉

同一天的第二個教訓。用 `re.sub(r"<[^>]+>", " ", body)` 把標籤拿掉之後，
內嵌 JS 的**原始碼還在**，於是全站搜尋政策字串時，把 `BLOCKED_SITES`
陣列裡的 `'…Mercari、Yahoo 拍賣、樂天等網站…'` 當成**給客人看的文案**報了出來。
客人根本看不到那段 —— 它在 `<script>` 裡。

差別不大但方向很糟：它會讓人去「修」一段其實是程式碼的文字。
先剝內容再剝標籤：

```python
src = STYLE_RE.sub(" ", SCRIPT_RE.sub(" ", body))   # 連內容一起拿掉
text = TAG_RE.sub(" ", src)
```

（實測差異：222 處 vs 225 處、40 份 vs 42 份文件。數字不大，
但那 3 處剛好有一處被排進了「要人工改」的清單。）

### 🔴🔴 body 超過幾千字就不要手抄 —— 先查有沒有「從檔案送出」的路徑

**這是流程問題，不是手滑。** 手抄的錯誤率不會因為小心而降到零，
只會因為量大而必然發生；把內容留在檔案裡、全程不經過對話文字，錯誤率才是真的零。

改文章／頁面之前先確認用哪把 token（2026-09-08 實測）：

| token | 來源 | 對 articles |
|---|---|---|
| `SHOPIFY_ACCESS_TOKEN` | `daigo/.env` | ❌ `{"message":"Access denied for articles field.","code":"ACCESS_DENIED"}` |
| `SHOPIFY_TOKEN`（`shpca_`） | `change/gyt-content-ops/.env` | ✅ 5 個 scope：`read_content`／`write_content`／`read_online_store_pages`／`write_online_store_pages`／`read_translations` |
| Shopify 連接器（`mcp__claude_ai_Shopify__*`） | 商家身分 | ✅ 但 body 必須打進對話＝手抄 |

**文章與頁面的內容改動一律走 `gyt-content-ops` 那把，從本機檔案送。**
連接器沒有「從檔案送」的路：`stagedUploadsCreate` 可以上傳，
但 `bulkOperationRunMutation` 被安全政策擋下
（`{"blocked":true,"matched":"bulkOperationRunMutation","category":"destructive"}`）。

正確的作法是**讀回來 → 在記憶體裡單點替換 → 送回去 → 再讀回來比對**，
一個字都不打（範例見 `scratchpad/audit/edit_article.py` 的 `EDITS` 表）。

#### 附帶規則：真的要逐字送中文時，不要用 `\uXXXX` 逃脫碼，直接寫字元

2026-09-08 六篇文章走連接器手抄（body 合計 135,103 字元，含三次重送實際打了約 228,000 字元），
**五個錯字全部是記錯碼位**，沒有一個是打錯字：

```
瑕 U+7455 → 寫成 U+7635（瘵）   2 處
鋪 U+92EA → 寫成 U+8216（舖）   3 處
梱 U+68B1 → 寫成 U+6885（梅）   1 處
攤 U+6524 → 寫成 U+651E（攞）   1 處
袒 U+8892 → 寫成 U+88AD（袭）   1 處
```

錯字在正式頁面上活了 8–9 分鐘才被回讀抓到。最後一篇改成直接寫中文字元，一次零錯字。
**JSON 字串本來就吃 UTF-8，用逃脫碼沒有任何好處，只是多開一個錯誤來源。**

★ **逐字回讀只證明「送出的跟草稿一致」，證明不了草稿本身是對的。**
草稿如果是腳本從線上原文改出來的，未改動段落就有原文背書；
草稿如果是手寫的，還要另外做字元合理性檢查
（新引入字元、全站語料稀有字、簡繁混用 —— 見 `scratchpad/audit/charcheck.py`）。

### 🔴🔴 `SHOPIFY_ACCESS_TOKEN` 沒有 `read_all_orders`，跨月份訂單分析一律不能用它

**超過 60 天的訂單查詢會靜默只回最近 60 天，不報錯、不警告。**
`orders(query: "created_at:>=…")` 是這樣，**bulk operation 同樣受限**
（試過，一樣只回 60 天），所以「改用 bulk」不是解法。

⚠️ **這件事本來就寫在上面的「訂單保護」裡**（`read_orders` 只看得到近 60 天，
更早要 `read_all_orders`），2026-09-07 還是踩了 —— 因為它被歸在**清理**的脈絡下，
做**訂單分析**時不會讀到那一段。**同一個事實影響兩件事就要寫兩次，
或至少互相指過去。**

2026-09-07 踩了兩次：先用一般查詢拿到 497 筆、再用 bulk 拿到同樣的 497 筆，
兩次都當成「12 個月」在解讀，還據此寫出「這些通路 12 個月 0 成交」的結論。
實際日期範圍是 **2026-07-09 → 2026-09-07**。用 Shopify 連接器（商家身分）
重跑同一支 bulk → **1,309 筆**，與 `cancellation-2026-09` 的 1,302／448 對得上。

那 497 筆「看起來很乾淨」正是它騙人的地方：近 60 天剛好是 BEYBLADE
供給斷掉之後，所以每一個候選網域都是 0 成交。**資料看起來剛好支持你的假設時，
先懷疑資料被截斷了。**

規則：

1. 跨月份的訂單分析**一律走 Shopify 連接器**（`mcp__claude_ai_Shopify__*`，
   商家身分，不受 scope 限制）。
2. 拿到訂單資料後**第一件事是印出日期範圍與月份分布**，再開始算任何比率。
3. 對筆數做 sanity check：與既有分析（`gyt-ops-analysis/cancellation-2026-09`
   的 12 個月 1,302 筆／448 取消）對一次，數量級不對就是資料有問題。
4. 「未取消」≠「收到錢」。這家店手動請款，`EXPIRED` 是授權過期沒請到款。
   算營收要用 `PAID` / `PARTIALLY_PAID` / `(PARTIALLY_)REFUNDED`。

---

## 🔴 本機開發（四條，每一條都已經害人踩過）

### 1. `python main.py` 會刪掉線上商品

啟動後 **60 秒**跑第一次 `_auto_cleanup_loop`，刪除 `DAIGO_COLLECTION_ID` 系列裡
超過 `DAIGO_AUTO_DELETE_DAYS` 天的商品 —— 用的是 `.env` 裡的**正式 token**。
本機只是要測一支 API，卻會動到線上資料。

```bash
DAIGO_COLLECTION_ID="" python main.py     # ← 本機一律這樣起
```

`cleanup_old_daigo_products` 第一行就是 `if not DAIGO_COLLECTION_ID: return`，
fail-closed，log 會印「⚠️ 中止：DAIGO_COLLECTION_ID 未設定，已刪除 0 件」。
2026-09-07 直接 `python main.py` 起過一次，在 60 秒內砍掉行程才沒出事。

### 2. 跑測試要 `PYTHONPATH=.`

`tests/` 底下的測試 `import main` / `from pricing import ...`，
直接 `python tests/xxx.py` 會 `ModuleNotFoundError`。**24 支測試全部如此**，
不是哪一支的問題。

```bash
PYTHONPATH=. python tests/verify_restricted_category.py
```

根目錄有 `conftest.py`，但那只在 pytest 路徑生效（而且本機沒裝 pytest）。

### 3. 🔴🔴 `.env` 是正式 Shopify token —— 本機起服務等於連著正式環境

`.env` 裡的 `SHOPIFY_ACCESS_TOKEN` 是**正式商店的 token**，沒有 staging。
所以本機 `python main.py` 不是「在本機測試」，是**用本機程式碼操作正式資料**。

2026-09-07 一天之內因此發生兩次風險事件：

1. `_auto_cleanup_loop` 在啟動 60 秒後觸發，準備刪除正式商品
   （靠 `DAIGO_COLLECTION_ID=""` 的 fail-closed 擋下，見上面第 1 條）
2. 打 `create-manual` 輪詢部署狀態，**在正式商店建立了 13 件可購買的假商品**

兩次都不是程式碼有 bug，是**「本機」這個詞給人的安全感是假的**。

**動手前先問自己：這支請求會不會寫到正式商店？** 會的話就不要在本機跑，
或先確認有 fail-closed 的護欄。

### 4. 🔴🔴 本機沒有 SeleniumBase —— 本機測 scraper 的結果一律不可信

需要瀏覽器渲染的 scraper（grail／amiami／newbalance／netmall／generic 的
Selenium 分支…）在本機會直接印 `[Driver] seleniumbase 未安裝`，
然後在 **1–3 毫秒**內失敗。那個失敗**看起來跟「網站擋我們」「scraper 壞了」
一模一樣**，只是快得不合理。

**2026-09-07 已經因此誤報過一次**：本機重爬 15 筆得到「7 筆失敗」，
據此差點結論「grail／amiami／newbalance 的 scraper 全壞了」。
改打正式環境後，grail 新舊兩種路徑都正常、newbalance 取到的價與建單原價
完全一致 —— **那 7 筆全是本機環境造成的假象**。

判斷方法：**看耗時**。正常的瀏覽器爬取是 10–50 秒，
本機缺件的失敗是 0.0 秒。看到 0.0s 失敗先想這一條。

要驗證 scraper 只有兩條路：

```bash
# a) 打正式環境（有 SeleniumBase）——/api/scrape 不寫入 Shopify，安全
curl -X POST https://goyoutatidaigo.zeabur.app/api/scrape \
     -H "X-API-Key: $API_SECRET_KEY" -d '{"url":"..."}'

# b) 本機裝 seleniumbase（requirements.txt 裡有）
```

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
