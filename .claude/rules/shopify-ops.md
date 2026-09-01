---
paths:
  - "**/*.py"
---

# Shopify 操作

從 CLAUDE.md「Shopify 操作」拆出來的，動任何 `.py` 時載入。
範圍放到全部 Python 檔而不是只有 `shopify_client.py` / `main.py`，
是因為一次性掃描腳本直接呼叫 `ShopifyClient._graphql`，最需要知道
節流退避與 `idempotent` 的取捨，而那些腳本檔名是臨時取的、glob 抓不住；
它們又沒有 code review 也沒有測試，出錯代價最大。
每一條都踩過，不是預防性建議。

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

### API 金鑰（三把，用途不可混用）

| 變數 | Header | 保護什麼 | 可不可以進前端 |
|---|---|---|---|
| `API_SECRET_KEY` | `X-API-Key` | scrape / create-order / create-manual / search / suggest | **已經在前端**（見下） |
| `ADMIN_SECRET_KEY` | `X-Admin-Key` | cleanup、cleanup preview、scrape-log | **絕對不可以** |
| `API_SECRET_KEY_OLD` | `X-API-Key` | ⏳ 輪替過渡用，只認公開端點 | 不需要 |

**公開金鑰等同公開。** 它印在 storefront 那頁的
`window.DAIKO_CONFIG = { api_base, api_key }` 裡，任何人檢視原始碼就看得到。
所以它只能擋隨機流量，**不能當信任邊界** —— 2026-08-30 之前 `/api/admin/cleanup`
（會永久刪商品）與 `/api/admin/scrape-log` 都只靠它把關。
**任何不可逆或會吐資料的端點一律走 `verify_admin_key`。**

**金鑰沒設定時一律拒絕（503）。** 以前預設空字串、Header 預設也是空字串，
變數沒設時「連 header 都不用帶」就會通過，等於整個 API 對外開放。

🔴 **`API_SECRET_KEY_OLD` 是暫時的，輪替完成當天就要刪掉。**
它的用途只有一個：Zeabur 換好新金鑰、但 storefront 頁面還沒改的那段空窗。
舊那把已經公開很久，留著就是繼續開著門。
- 判斷可以移除了沒：改完頁面後看 Zeabur log，不再出現
  `[Auth] ⚠️ 仍有請求在用舊的公開金鑰` 就代表沒有流量在用它
- 移除方式：Zeabur 刪掉 `API_SECRET_KEY_OLD` 這個環境變數（程式碼不用改）
- **admin 金鑰永遠不吃舊值**，過渡機制只作用在公開端點

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
