# daigo 品類攔截 — 接入說明

> ## ⚠️ 已作廢，保留僅供追溯
>
> **一、接入步驟已完成。** 本文描述的貼上步驟已於 2026-09-07 commit
> `c21ebc6` 完成，`scrapers/base.py` 與 `main.py` 都已進版控，
> 而且線上版本比本文所述**更進一步**（BEYBLADE 改為純網域判斷、
> 一番賞加了二手平台豁免、抽選補了繁中變體）。**照本文再貼一次會覆蓋掉那些修正。**
>
> **二、下面「24%／30%」那組數字已被更完整的分析取代。**
> 那是近 50 筆訂單的短窗抽樣。完整的 12 個月分析在
> `gyt-ops-analysis/cancellation-2026-09/`，母體是 1,302 筆訂單、
> 2,290 筆 line item，結論與本文不同也更可靠：
>
> - 限量搶購品類取消率 **85.1%**（其餘代購 13.8%）
> - 全部 448 筆取消**都是員工手動取消**，這家店沒有客人自助取消的機制
> - 「快速取消＝我方拒單」不成立：205 筆快速取消裡**沒有任何一筆發生退款**
>
> 引用取消率時請以 `cancellation-2026-09/` 為準，不要引用本文的數字。

## 為什麼

近 50 筆訂單（2026-08-29 ~ 09-04）作廢 **12 筆＝24%**，其中：

| 品類 | 筆數 | 金額 |
|---|---|---|
| 寶可夢卡牌 | 10 | ¥131,293 |
| BEYBLADE X | 1 | ¥9,149 |
| 海賊王卡牌 | 1 | ¥14,054 |

多數在下單後 **20–95 秒**內被取消。六個月累計作廢 ¥5,555,371＝gross 的 30%。

系統目前不執行「不做卡牌／陀螺」這個決定，訂單照樣進來再人工砍掉。

---

## 步驟 1：`scrapers/base.py`

把 `daigo_restricted_category.py` 的內容**整段貼到檔案最後面**
（`detect_invalid_link` 之後）。開頭的 `import re` 如果檔案上方已經有，
可以省略。

---

## 步驟 2：`main.py` — `/api/scrape`

找到（約第 484 行）：

```python
        product: ProductInfo = await scrape_with_queue(url)
        if not product.title:
            return ScrapeResponse(
                success=False,
                error="無法從此連結抓取商品資訊",
                queue_info={"active": _active_count, "waiting": _queue_count},
            )
        pricing = calculate_selling_price(product.price_jpy) if product.price_jpy else None
```

在 `pricing = ...` 那行**之前**插入：

```python
        # ★ 品類攔截：要有 title 才判斷得出來，所以擺在 scrape 之後
        #   （detect_blocked 是網址層級，擋不掉「同網域但特定商品」）
        from scrapers.base import detect_restricted_category
        restricted = detect_restricted_category(product.title, url)
        if restricted and restricted[0] == "hard":
            print(f"[API] 🚫 受限品類: {product.title[:60]}")
            return ScrapeResponse(
                success=False,
                blocked=True,          # 前端已支援：顯示訊息、不切手動表單
                error=restricted[1],
                queue_info={"active": _active_count, "waiting": _queue_count},
            )
```

---

## 步驟 3：`main.py` — `/api/create-order`

找到（約第 546 行）：

```python
        if not product.price_jpy:
            return CreateOrderResponse(success=False, error="無法偵測到商品價格")
        pricing = calculate_selling_price(product.price_jpy)
```

在 `pricing = ...` **之前**插入：

```python
        # ★ 同上。這裡是最後一道 —— cache 命中時不會重跑 /api/scrape，
        #   所以不能只靠上面那道。
        from scrapers.base import detect_restricted_category
        restricted = detect_restricted_category(product.title, url)
        if restricted and restricted[0] == "hard":
            print(f"[API] 🚫 受限品類（建單嘗試）: {product.title[:60]}")
            return CreateOrderResponse(
                success=False,
                blocked=True,
                error=restricted[1],
            )
```

---

## 步驟 4：`main.py` — `/api/create-manual`

手動表單的標題是客人自己打的，一樣要擋（否則爬取失敗的卡牌會從這裡溜進來）。

在 `create_manual_order` 裡，現有的 `detect_blocked(req.source_url...)`
區塊**之後**插入：

```python
        from scrapers.base import detect_restricted_category
        restricted = detect_restricted_category(req.title, req.source_url or "")
        if restricted and restricted[0] == "hard":
            print(f"[API] 🚫 受限品類（手動建單）: {req.title[:60]}")
            return CreateOrderResponse(
                success=False,
                blocked=True,
                error=restricted[1],
            )
```

---

## 前端要不要改？

**硬擋不用改。** `ScrapeResponse.blocked` / `CreateOrderResponse.blocked`
這兩個欄位本來就存在，前端已經會顯示 `error` 訊息而不切到手動表單
（就是 tw.mercari 攔截器用的同一條路）。

**軟擋（預購／數量限定）目前不會顯示。** 它需要在前端多接一個欄位，
是獨立的一步。現在 `detect_restricted_category` 回傳 `("soft", ...)`
時三個端點都放行，行為跟現在完全一樣，不會有副作用。

---

## 上線後怎麼驗

Zeabur log 搜 `受限品類`，一週後看兩個數字：

1. 被擋下的次數 → 這就是原本會變成作廢訂單的量
2. 作廢率是否從 24% 下降

如果出現誤擋（客訴說某個正常商品被拒），把標題貼給我，我調正規式。
規則寫在 `base.py` 一個地方，改一處就好。
