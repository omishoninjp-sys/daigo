# `theme/` —— 只放「這個 repo 改過的 theme 檔」，不是整份 theme

## 這是刻意的，不是意外

2026-09-08 加入。放在 daigo repo 裡的理由只有一個：
**theme 改動要能被 review。** 沒有 baseline 就沒有 diff，
而「貼一段改好的 JS 給人看」不等於 review —— 看不出動到了什麼、有沒有動到別的。

## 🔴 三件必須知道的事

### 1. 這是**部分快照**，不是整份 theme

線上 MAIN theme（`139858084074`「已更新 Dawn 的副本」）有 250+ 個檔案，
這裡只有兩支：

    assets/daigo.js        代購流程的前端邏輯
    sections/daigo.liquid  代購頁的版面 + BLOCKED_SITES / DENY 兩張清單

**其餘檔案不在這裡，也不該被 push。** 所以 push 一律要帶 `--only`：

    shopify theme push --store fd249b-ba.myshopify.com --theme 139858084074 \
      --path theme --only assets/daigo.js --only sections/daigo.liquid

漏掉 `--only` 會用一份殘缺的 theme 覆蓋線上 —— 那是不可逆的。

### 2. 這裡的內容**會過期**，而且過期沒有任何訊號

有人在 Shopify 後台的 theme 編輯器改了這兩支檔案時，
這裡的副本不會知道。接著任何一次 `theme push` 都會**默默蓋掉那個人的修改**。

**所以：動這兩支之前一律先 pull，不要直接改。**

    shopify theme pull --store fd249b-ba.myshopify.com --theme 139858084074 \
      --path theme --only assets/daigo.js --only sections/daigo.liquid
    git diff        # 有 diff 就代表線上被改過，先弄清楚是誰改的

對得起來的驗證方式（不用 CLI）：Admin API 的 `themeFiles` 會給 `checksumMd5`，
與 `git cat-file -p HEAD:theme/assets/daigo.js | md5sum` 比對。
2026-09-08 建立 baseline 時就是這樣確認的：

    assets/daigo.js        31,423 bytes  md5 8e2291490384adc8f615ecc9b8f5e9d2
    sections/daigo.liquid  66,970 bytes  md5 5a05484ffdf33f977094b0edbecd5991

### 3. 改這裡會**順帶觸發後端部署**

daigo repo 的 `git push` → Zeabur 自動部署。
所以就算只改 `theme/` 底下的檔案，push 上去仍然會讓 API 容器重建、重啟。

不算嚴重（重啟是安全的），但要知道：
**theme 與後端的部署在這個 repo 裡是綁在一起的，而 `shopify theme push` 是分開的一步。**
也就是說 `git push` 之後線上前端**還沒有變** —— 兩個動作都要做。

## `.gitattributes`

`* -text` —— 這個 repo 的 `core.autocrlf=true`，不擋的話 CRLF 會被正規化成 LF，
blob 的 md5 就與線上對不起來，baseline 的意義（逐位元組相同）就沒了。

## 自動檢查

`tests/verify_theme_lists.py`（＋ `tests/verify_theme_lists.js`）驗這兩支檔案的內容，
會跟著 `tests/` 一起跑。**但它驗不到「線上是不是還等於這裡」** —— 那需要連線，
見上面第 2 點與 `backlog.md` 的 GYT-003。
