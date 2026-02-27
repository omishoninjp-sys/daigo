# GOYOUTATI 代購系統 v3 - 部署指南

## 架構

```
┌────────────────────────────────────────┐
│  Shopify 前端（daigo.liquid）           │
│  客人貼連結 → 預覽商品 → 結帳           │
└──────────────┬─────────────────────────┘
               │ API 呼叫
               ▼
┌────────────────────────────────────────┐
│  Zeabur 後端（FastAPI + Chrome）        │
│                                         │
│  Amazon    → requests（快速，不需瀏覽器）│
│  ZOZOTOWN  → undetected-chromedriver    │
│  其他網站  → Playwright                  │
│                                         │
│  定價計算 → Shopify Admin API 建立商品   │
└────────────────────────────────────────┘
```

**全部跑在 Zeabur 雲端，不需要本機服務。**

## 部署

### 1. Push to GitHub → Zeabur 自動部署

### 2. 設定環境變數

```
SHOPIFY_STORE=goyoutati.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxx
SHOPIFY_API_VERSION=2024-10
DAIGO_COLLECTION_ID=123456789
API_SECRET_KEY=Wakuwaku
ALLOWED_ORIGINS=https://goyoutati.com,https://goyoutati.myshopify.com
MIN_SERVICE_FEE_JPY=300
```

### 3. Shopify 前端不用改

## 各平台爬取方式

| 平台 | 方式 | 速度 |
|------|------|------|
| Amazon.co.jp | requests + BS4 | 1-3 秒 |
| ZOZOTOWN | undetected-chromedriver | 10-15 秒 |
| 樂天/其他 | Playwright | 5-10 秒 |

## 備用：如果 ZOZOTOWN headless 被擋

設定 `ZOZO_SCRAPER_URL` 指向本機 product-fetcher：
```
ZOZO_SCRAPER_URL=https://xxxx.ngrok-free.app
```
