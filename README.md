# GOYOUTATI 代購系統

讓客人只要貼上日本商品連結，就能自動抓取商品資訊、計算代購售價並直接在 Shopify 結帳。

## 架構總覽

```
┌──────────────────────────────────┐
│  Shopify 前端（page.daiko.liquid） │
│  客人貼連結 → 預覽商品 → 結帳     │
└──────────────┬───────────────────┘
               │ API 呼叫
               ▼
┌──────────────────────────────────┐
│  後端 API（Zeabur / FastAPI）     │
│  /api/scrape     → 爬取商品資訊   │
│  /api/create-order → 建立 Shopify 商品 │
└──────────────┬───────────────────┘
               │ Shopify Admin API
               ▼
┌──────────────────────────────────┐
│  Shopify 商店                     │
│  商品建立在「代購商品」Collection  │
│  客人走正常結帳流程付款           │
└──────────────────────────────────┘
```

## 客人操作流程

1. 進入代購頁面
2. 貼上日本商品 URL
3. 系統自動顯示：商品名稱、圖片、日幣原價、代購售價、台幣參考價
4. 客人點「確認代購・前往結帳」
5. 系統在 Shopify 建立商品 → 導向結帳頁
6. 客人完成付款
7. 你收到訂單通知 → 手動去日本官網購買

---

## 部署步驟

### 1️⃣ Shopify 前置準備

#### 建立「代購商品」Collection
1. Shopify 後台 → Products → Collections → Create collection
2. 名稱：`代購商品`
3. 類型：手動（Manual）
4. 儲存後，從網址列取得 Collection ID（數字部分）

#### 建立 Custom App（取得 API Token）
1. Shopify 後台 → Settings → Apps and sales channels → Develop apps
2. Create an app → 名稱：`代購系統`
3. Configure Admin API scopes，勾選：
   - `write_products`
   - `read_products`
   - `write_inventory`
   - `read_inventory`
4. Install app → 複製 **Admin API access token**

### 2️⃣ 部署後端 API 到 Zeabur

#### 方法 A：Git 部署（推薦）
```bash
# 1. 建立 Git repo
cd daiko-api
git init
git add .
git commit -m "init daiko api"

# 2. 推到 GitHub

# 3. Zeabur 建立新服務 → 選擇 GitHub repo → 自動偵測 Dockerfile
```

#### 方法 B：手動部署
在 Zeabur 建立服務，上傳 `daiko-api` 整個資料夾。

#### 設定環境變數
在 Zeabur 的服務設定中新增以下環境變數：

```
SHOPIFY_STORE=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxx
SHOPIFY_API_VERSION=2024-10
DAIKO_COLLECTION_ID=123456789
API_SECRET_KEY=隨機產生一組密鑰
ALLOWED_ORIGINS=https://your-store.com,https://your-store.myshopify.com
MIN_SERVICE_FEE_JPY=300
```

### 3️⃣ 安裝 Shopify 前端頁面

1. Shopify 後台 → Online Store → Themes → Edit code
2. 在 `Templates` 資料夾點 `Add a new template`
3. 類型選 `page`，名稱填 `daiko`
4. 貼上 `shopify-theme/page.daiko.liquid` 的內容
5. **重要**：修改檔案開頭的 API 設定：
   ```javascript
   const DAIKO_API_BASE = 'https://your-daiko-api.zeabur.app';
   const DAIKO_API_KEY  = '你的 API_SECRET_KEY';
   ```
6. 建立新頁面：Online Store → Pages → Add page
   - 標題：`日本代購`
   - Template：選 `daiko`
7. 將頁面加入導覽選單（Navigation）

---

## 定價邏輯

在 `config.py` 的 `PRICING_TIERS` 設定：

| 日幣原價區間 | 加成倍率 | 範例：原價 ¥10,000 |
|---|---|---|
| ¥0 ~ ¥3,000 | ×1.40 (40%) | — |
| ¥3,001 ~ ¥8,000 | ×1.35 (35%) | — |
| ¥8,001 ~ ¥20,000 | ×1.30 (30%) | 售價 ¥13,000 |
| ¥20,001 ~ ¥50,000 | ×1.25 (25%) | — |
| ¥50,001 以上 | ×1.20 (20%) | — |

最低手續費 ¥300，避免低價商品無利潤。

---

## 支援的網站

### 專用解析器（最佳支援）
- Amazon.co.jp
- 樂天市場
- ZOZOTOWN

### 通用解析（大部分日本網站）
透過 JSON-LD、Open Graph tags、通用 HTML 解析，支援大部分日本品牌官網：
BAPE、Human Made、Onitsuka Tiger、adidas JP、NEIGHBORHOOD 等

---

## API 端點

### `POST /api/scrape`
爬取商品資訊 + 計算售價

```json
// Request
{ "url": "https://www.amazon.co.jp/dp/B0xxxxx" }

// Response
{
  "success": true,
  "product": {
    "title": "商品名稱",
    "price_jpy": 12000,
    "image_url": "https://...",
    "brand": "品牌",
    "source_url": "https://..."
  },
  "pricing": {
    "original_price_jpy": 12000,
    "markup_rate": 1.30,
    "selling_price_jpy": 15600,
    "reference_price_twd": 3276
  }
}
```

### `POST /api/create-order`
建立 Shopify 商品

```json
// Request
{ "url": "https://www.amazon.co.jp/dp/B0xxxxx" }

// Response
{
  "success": true,
  "product_id": 123456789,
  "checkout_url": "https://your-store.com/products/xxx",
  "admin_url": "https://your-store.myshopify.com/admin/products/123456789"
}
```

### `GET /api/rate`
取得目前匯率和費率表

### `GET /api/health`
健康檢查

---

## 未來可擴充

- [ ] 用 Playwright 支援 JavaScript 渲染的頁面
- [ ] 支援選擇尺寸/顏色等 variants
- [ ] LINE 通知整合（有新代購訂單時通知你）
- [ ] 客人可追蹤代購進度
- [ ] 自動翻譯日文商品名稱（接 OpenAI / Claude API）
- [ ] 歷史訂單查詢頁面
