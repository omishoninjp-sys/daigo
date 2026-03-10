# GOYOUTATI — 即時購買通知 部署說明

## 架構概覽

```
Shopify 前端  →  fetch()  →  Zeabur API  →  Shopify Admin API
                              (快取 2 分鐘)
```

---

## Step 1：Shopify Admin API Token

1. Shopify 後台 → **Settings → Apps and sales channels → Develop apps**
2. 建立 App，命名如 `live-ticker`
3. Admin API 權限只需開：`read_orders`
4. 安裝 App，複製 **Admin API access token**（`shpat_xxxx`）

---

## Step 2：Zeabur 部署後端

### 方法：上傳 `main.py` + `requirements.txt` 到新 Zeabur Service

環境變數設定（Zeabur Dashboard → Variables）：

| 變數名 | 範例值 |
|--------|--------|
| `SHOPIFY_STORE_DOMAIN` | `goyoutati.myshopify.com` |
| `SHOPIFY_ADMIN_TOKEN` | `shpat_xxxxxxxxxxxxxxxx` |
| `ALLOWED_ORIGIN` | `https://www.goyoutati.com` |

部署後取得網址，例如：`https://live-ticker.zeabur.app`

驗證：瀏覽 `https://live-ticker.zeabur.app/api/recent-orders`
應回傳：
```json
{
  "orders": [
    { "flag": "🇹🇼", "region": "台北市", "product": "YOKUMOKU...", "time": "3 分鐘前" }
  ],
  "cached": false
}
```

---

## Step 3：Shopify Theme 設定

### 3-1. 上傳 Snippet

把 `live-order-ticker.liquid` 上傳到：
**Online Store → Themes → Edit Code → snippets/**

### 3-2. 新增 Theme Setting（讓你能填入 API 網址）

在 `config/settings_schema.json` 找到合適位置加入：

```json
{
  "name": "Live Ticker",
  "settings": [
    {
      "type": "text",
      "id": "live_ticker_api_url",
      "label": "即時訂單 API 網址",
      "placeholder": "https://live-ticker.zeabur.app/api/recent-orders"
    }
  ]
}
```

### 3-3. 填入 API 網址

**Customize → Theme Settings → Live Ticker** → 貼上你的 Zeabur API 網址

### 3-4. 找到原本的靜態數字區塊並替換

在你現在放那個 2500件 / 4.9★ 區塊的 section 檔案裡，
把那段 HTML 整個刪除，換成：

```liquid
{% render 'live-order-ticker' %}
```

---

## 注意事項

- **訂單隱私**：只顯示城市（不顯示姓名、地址、金額）
- **快取**：後端快取 2 分鐘，前端每 2 分鐘重抓一次，不會打爆 Shopify rate limit
- **失敗處理**：API 失敗時前端靜默，不會跳錯或破版
- **手機版**：字體自動縮小，LIVE 標籤在手機上隱藏以省空間
