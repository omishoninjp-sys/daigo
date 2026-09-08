# backlog

還沒做、但已經知道成因與影響的事。每一條都要寫「怎麼發現的」與「不做會怎樣」，
沒有這兩項的條目不要進來 —— 那種條目日後沒有人判斷得出該不該做。

---

## GYT-001　`.claude/hooks/guard.py`：兩個誤擋

**狀態**：待評估　**開立**：2026-09-08　**優先度**：低（有 workaround，但會偷走時間）

🔴 **守門員是安全控制，改它要另外評估，不可以順手修。**
它的整個設計前提是「壞掉時 fail-closed、很吵但看得見」（見 `guard.py` 檔頭）。
任何修改都可能把 fail-closed 變成 fail-open，而那正是它要防的東西。
所以這張單只記錄現象，**不附修法**。

### (a) `sys.stdin.read()` 沒有指定 UTF-8，中文命令會被 cp950 解成亂碼

`guard.py:88` 是裸的 `sys.stdin.read()`。Windows 上 `sys.stdin` 的預設編碼是
cp950，而 hook 收到的 payload 是 UTF-8 JSON。中文夠多的時候，某個 UTF-8
位元組序列會被 cp950 解成引號或反斜線之類的字元，把 JSON 打斷：

```
[guard] 守門員自己壞了（JSONDecodeError: Expecting ',' delimiter:
        line 1 column 1768 (char 1767)）—— fail-closed，
        所有 Bash/PowerShell 命令都會被擋，直到 .claude/hooks/ 修好。
```

**怎麼發現的**：2026-09-08 跑一支含中文 `print()` 的分析腳本時觸發。
同一個 session 裡更早、更短的中文命令都正常 —— **命中位置取決於內容，
所以是間歇性的**，不是「有中文就會壞」。

**不做會怎樣**：偶發地把整個 session 的 Bash/PowerShell 全部鎖住，
而訊息說的是「守門員自己壞了」，看起來像 hook 檔案有語法錯誤，
會往錯的方向查。**workaround**：中文寫進腳本檔，命令列只留 ASCII。

### (b) `python -m py_compile <file.py>` 被判成「批次改線上資料」

`guard_impl._check_segment` 對 `_RUNNERS` 裡的命令，會把命令列上所有 `.py`
路徑的**檔案內容**讀進來掃 `_BULK_CODE`。於是：

```
$ python -m py_compile main.py shopify_client.py
[guard] 擋下含 `productDelete` 的程式碼 —— 那會批次改線上資料。
```

`shopify_client.py` 裡本來就有 `productDelete` / `productSet` 字樣，
但 `py_compile` **只是編譯，不會執行任何一行**。

**怎麼發現的**：2026-09-08 想用 py_compile 做語法檢查時被擋。

**不做會怎樣**：語法檢查這條路被封死。
**workaround**：改跑 `tests/` 底下的測試 —— `_is_test_path()` 對 `tests/`
有豁免，而那些測試會 import `main`，語法錯誤照樣會爆出來。
（CLAUDE.md 本來就寫著「`py_compile` 通過不算驗證」，所以這個 workaround
其實是比較對的做法。）

---

## GYT-002　前端／後端對不上的三件事（要動 theme）

**狀態**：待排　**開立**：2026-09-08　**歸屬**：併進 ZOZO 404 那批

全部以 2026-09-08 抓線上
`//goyoutati.com/cdn/shop/t/4/assets/daigo.js?v=150301092339533680921788760972`
（20,802 bytes）實測為準。

### (a) 後端從來沒有回過 `handoff_url`

`daigo.js` 有三處讀 `data.handoff_url`，`showError(msg, handoffUrl)` 會在訊息
下面加一條「用 LINE 幫我處理這件商品 →」的連結（只認 https）。
但 `main.py` 的 `ScrapeResponse` / `CreateOrderResponse` **都沒有這個欄位**，
所以那個按鈕從來沒出現過。

**不做會怎樣**：每一則硬擋／哨兵訊息都叫客人「用 LINE 傳給我們」，
卻沒有可以點的連結 —— 客人要自己去找 LINE ID。

### (b) soft 品類在 `/api/create-manual` 走 `alert()`，不是 `showError`

`daikoManualOrder` 先看 `data.blocked`（soft 回 `false`），再走
`if(!data.success){ alert(data.error||"建立商品失敗") }`。
`daikoCreateOrder` 那條則是 `showError`（內嵌顯示）。訊息都會到，樣式不一致。

要一致得讓前端認 `soft` 這個欄位 —— 那是 theme 改動，2026-09-08 那次刻意沒動。

### (c) `Shop` 銷售管道仍然會發布 soft 商品

2026-09-08 決議**維持現狀，只排除線上商店**。記在這裡是為了日後有人問
「soft 商品到底能不能被買到」時，看得到這是決定不是遺漏。
實測：soft 商品 `onlineStoreUrl=null`、storefront 404，但仍發布到
Shop / Inbox / TikTok / Google & YouTube / Facebook & Instagram / Collective。

---

## GYT-003　`shopify theme push` 直接改線上，沒有任何攔截

**狀態**：待評估（不做）　**開立**：2026-09-08　**優先度**：看 theme 改動頻率

### 現況

`shopify theme push --theme 139858084074` **直接寫線上 MAIN theme**：

- 沒有預覽、沒有 staging、沒有審核
- 不像 daigo repo 的 `git push` 還會經過 Zeabur 的建置（雖然那個也不會擋你）
- 錯了要靠 Shopify 後台的 theme 版本記錄回滾，而那個記錄只留最近幾次

**性質與 `git push origin main`（daigo）同一類：一個指令、不可逆、影響線上客人。**
而 `guard.py` 目前擋得住後者、擋不住前者 —— 那張清單裡沒有 `shopify`。

漏掉 `--only` 的情況更糟：`theme/` 只是**部分快照**（兩支檔案），
不帶 `--only` 會用一份殘缺的 theme 覆蓋整個線上版面。

### 怎麼發現的

2026-09-08 做批次二的 theme 改動時。當時只有兩支檔案、一次性，
所以是人工小心處理的（先 pull baseline、對 checksum、再 push）。

### 不做會怎樣

現在不會怎樣 —— theme 改動很少，而且每次都會走 review。
**風險是頻率上升之後**：習慣了就會有人直接 `shopify theme push` 不 pull，
蓋掉別人在後台改的東西（見 `theme/README.md` 第 2 點：過期**沒有任何訊號**）。

### 要做的話

把 `shopify` 加進 `guard_impl.py` 的確認清單，命中 `theme push` 就擋。
🔴 但那是動守門員，跟 GYT-001 一樣要另外評估 ——
   而且擋太多會讓人習慣繞過，那比沒有更糟（`guard_impl.py` 檔頭自己寫的）。
   **判準應該是「theme 改動變成常態」再做，不是現在。**

### 順帶

`theme/` 在 daigo repo 裡 → 改 theme 也會觸發 Zeabur 重新部署後端。
不算問題（重啟是安全的），但要知道 `git push` 之後線上前端**還沒變**，
`shopify theme push` 是分開的第二步。兩個動作都要做。
