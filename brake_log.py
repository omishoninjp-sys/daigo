"""
重複下單煞車 —— 第一階段（**只記錄，不擋任何東西**）。

工單：`gyt-ops-analysis/backlog/TICKET-duplicate-order-brake.md`
（含 2026-09-07 的附錄，730 筆 line item 的規則評估）。

同 C-1（`TICKET-generic-price-accuracy.md` 的 `price_spread`）的做法：
先讓系統記錄，累積一兩週真實分佈之後再決定門檻。**這支不做任何判斷。**

兩條紀錄線，缺一不可：

  ① 建單時（`note_created`）—— `/api/create-order`、`/api/create-manual` 成功
     建出商品時寫一筆。這是**分母**：包含那些從來沒有變成訂單的商品。
     只有這裡看得到 `created_via=manual`。

  ② 每日掃訂單（`scan_orders`）—— 才有結果（作廢／收款／哪位客人）。
     `cancelled_at` / `displayFinancialStatus` / 客人 只存在於訂單上，
     建單當下不可能知道。

🔴 ② 的 `source_url` 從哪裡來，決定這件事成不成立
   優先讀 line item 的 `_daigo_source_url` property（**下單當下就寫進訂單，
   商品被 auto-cleanup 刪掉也還在**）；讀不到才退回商品的 `daigo.source_url`
   metafield。後者會在商品被刪掉的那一刻消失 ——
   2026-09-07 的回溯分析涵蓋率只有 31.6%、2026-06 那批 BEYBLADE 只剩 6.6%，
   就是因為當時只有 metafield 這一條路。`source_from` 欄位記的就是這一筆走了哪條，
   metafield 的比例應該隨著時間掉到 0。

🔴 這份紀錄裡的商品標題可能含客人自己打的字（`/api/create-manual` 是**客人**在填）
   同 `gyt-ops-analysis/.gitignore` 的理由：**JSONL 一律不進版控**，
   也不要貼進任何對外文件。
"""
import os
import json
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta

import source_key

# ★ 與 scrape_monitor 分開放，兩者的保留策略與敏感度不同。
#
# ★ 環境變數在**呼叫時**才讀，不在 import 當下讀死 ——
#   測試要能在 import 之後才把目錄導到暫存區。
#   （scrape_monitor 是在 import 當下算的，所以它做不到這件事；
#     不去改它是因為那支現在沒有這個需求，改了要重跑它自己的 111 項。）
def _log_dir_candidates() -> list:
    return [
        os.environ.get("BRAKE_LOG_DIR", "").strip() or "/data/brake_log",
        "./brake_log",
    ]

_log_dir: str = ""

# 🔴🔴 `SHOPIFY_ACCESS_TOKEN` 沒有 `read_all_orders`，超過 60 天的訂單查詢
#     **會靜默只回最近 60 天**，不報錯、不警告（bulk operation 一樣受限）。
#     2026-09-07 就是這樣拿到 497 筆當成 12 個月在解讀。
#     所以這裡硬上限 60 天，超過就夾住並印出來 ——
#     煞車只需要近 30 天，這個限制對第一階段沒有影響，但**不可以讓日後有人
#     傳 days=365 進來然後以為自己拿到了一年的資料**。
MAX_SCAN_DAYS = 60

# 「收到錢」的口徑（CLAUDE.md〈跨月份訂單分析〉第 4 條）：
# 這家店手動請款，`EXPIRED` 是授權過期沒請到款，不算成交。
_PAID_STATUSES = frozenset({"PAID", "PARTIALLY_PAID", "REFUNDED", "PARTIALLY_REFUNDED"})


def _pick_dir() -> str:
    global _log_dir
    if _log_dir:
        return _log_dir
    for path in _log_dir_candidates():
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".probe")
            with open(probe, "a", encoding="utf-8"):
                pass
            os.remove(probe)
            _log_dir = path
            print(f"[Brake] 紀錄目錄：{path}")
            return _log_dir
        except Exception:
            continue
    _log_dir = "."
    print("[Brake] ⚠️ 取不到可寫目錄，退回目前工作目錄")
    return _log_dir


def log_dir() -> str:
    return _pick_dir()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _append(prefix: str, entry: dict) -> None:
    path = os.path.join(_pick_dir(), f"{prefix}-{entry['ts'][:10]}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# 附錄 H 要的 `customer_id`：**這一版故意不記，但不是因為拿不到。**
#
#   `orders { nodes { customer { id } } }` 確實拿不到 —— 2026-09-08 實測回
#     "Access denied for customer field. Required access: `read_customers`
#      access scope."（extensions.code = ACCESS_DENIED）
#   `/admin/oauth/access_scopes.json` 也確認這把 token 沒有 read_customers。
#
#   🔴 但**不要因此以為客人身分完全拿不到**。同一天實測近 30 天 245 筆訂單：
#        order.email                                 245/245 拿得到
#        order.clientIp                              245/245 拿得到
#        customerJourneySummary.customerOrderIndex    245/245 拿得到
#      read_customers 擋的是 `customer` 這個物件，不是訂單自己的這幾個欄位。
#      （這段原本寫著「email、地址一樣拿不到」，那句話是錯的，當天量完就改掉 ——
#        錯的註解會變成下一輪推理的前提，比沒有註解更貴。）
#
#   這一版仍然不記，理由是**能不落地的個資就不要落地**：
#   第一階段先看時間分佈（first_seen / last_seen / span_days）分不分得開
#   「一個人重試 14 次」與「14 個人各試一次」——
#   14 次分散 10 天與 14 次集中 20 分鐘，形狀完全不同。
#   分得開就不必記任何身分；分不開再依序加 customerOrderIndex（無個資）、
#   sha256(email)（只存雜湊）。評估與數字全部在
#   `gyt-ops-analysis/backlog/TICKET-brake-distinct-customers.md`。
#
#   在那之前這裡記 `distinct_orders`（同一張訂單買兩件 ≠ 兩次下單）。
#   ⚠️ 它是「不重複客人數」的**上界**，不是同一件事，不要當成同一件事解讀。


def _hash_customer(gid: str) -> str:
    """保留給日後真的拿得到客人識別碼的時候；目前恆為空字串（見上面）。"""
    gid = (gid or "").strip()
    if not gid:
        return ""
    return hashlib.sha256(gid.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────
# ① 建單時
# ─────────────────────────────────────────────────────────────────────
def note_created(source_url: str, product_id=None, created_via: str = "",
                 title: str = "") -> dict:
    """
    建單成功時記一筆。**回傳算出來的 key 供呼叫端 log，但不影響任何行為。**

    ★ 全程 fail-safe：紀錄壞掉不可以讓客人建不了單。
      這條路徑上的例外會讓 `/api/create-order` 整支失敗，
      而它的正職是幫客人建商品，不是寫紀錄。
    """
    out = {"norm_key": "", "matched_site_rule": ""}
    try:
        raw = (source_url or "").strip()
        if not raw:
            # 手動建單常常沒有 source_url（附錄 E）。第一階段先只記錄有 URL 的。
            return out
        key, rule = source_key.normalize(raw)
        if not key:
            return out
        out = {"norm_key": key, "matched_site_rule": rule}
        _append("created", {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "norm_key": key,
            # 🔴 附錄 H：**不要只記 norm_key。** 規則錯了要能重算 ——
            #    2026-09-07 那份五種規則的比較做得出來，正是因為原始 URL 還在。
            "raw_url": raw,
            "key_rule_version": source_key.KEY_RULE_VERSION,
            "matched_site_rule": rule,
            "brake_suitable": source_key.is_brake_suitable(raw),
            "product_id": product_id,
            "created_via": created_via,
            # 假合併偵測要用：同一個 key 底下出現 >1 種標題就要人看一眼
            # （附錄 C：多數是 SEO 標題把同一件商品寫成兩種說法，
            #   但 amiami 那一類是真的把不同商品併在一起）。
            "title": (title or "")[:80],
        })
    except Exception as e:
        print(f"[Brake] ⚠️ 建單紀錄寫入失敗（略過，不影響建單）: {type(e).__name__}: {e}")
    return out


# ─────────────────────────────────────────────────────────────────────
# ② 每日掃訂單
# ─────────────────────────────────────────────────────────────────────
def _rows_from_orders(orders: list) -> list[dict]:
    """把訂單攤平成一行一個 line item。純函式，離線可測。"""
    rows = []
    for o in orders:
        # customer 這把 token 讀不到（ACCESS_DENIED），恆為空字串。見檔案上方。
        cust = _hash_customer("")
        for li in ((o.get("lineItems") or {}).get("nodes") or []):
            attrs = {a.get("key"): a.get("value")
                     for a in (li.get("customAttributes") or []) if a.get("key")}
            prop_url = (attrs.get("_daigo_source_url") or "").strip()
            meta = ((li.get("product") or {}).get("metafield") or {})
            meta_url = (meta.get("value") or "").strip()
            url = prop_url or meta_url
            source_from = "property" if prop_url else ("metafield" if meta_url else "")
            key, rule = source_key.normalize(url)
            rows.append({
                "order_name": o.get("name") or "",
                "created_at": o.get("createdAt") or "",
                "cancelled_at": o.get("cancelledAt") or "",
                # 🔴 附錄「注意」：客人自己反悔取消 ≠ 我們採購失敗。
                #    目前 448 筆取消 100% 是店員執行，`cancelReason` 多半會是
                #    OTHER/STAFF，**分不出來**。先記著，第二階段定門檻時
                #    要嘛靠取消備註、要嘛另加欄位 —— 沒分開就會把「客人反悔」
                #    算成「買不到」。
                "cancel_reason": o.get("cancelReason") or "",
                "financial_status": o.get("displayFinancialStatus") or "",
                "customer_hash": cust,
                "quantity": li.get("quantity") or 0,
                "title": (li.get("title") or "")[:80],
                "raw_url": url,
                "source_from": source_from,
                "norm_key": key,
                "key_rule_version": source_key.KEY_RULE_VERSION,
                "matched_site_rule": rule,
                "brake_suitable": source_key.is_brake_suitable(url) if url else True,
            })
    return rows


def build_summary(rows: list[dict], days: int) -> dict:
    """
    近 N 天的 key 分佈。**只描述，不判斷。**

    工單第一階段要看的：次數的直方圖、首次到末次的時間跨度、涉及幾位不重複客人。
    門檻等這份分佈長出來再定 —— 如果 N=2 就涵蓋 8 成，門檻可以很低；
    長尾很平就要靠「時間窗 + 次數」兩個條件。
    """
    total = len(rows)
    with_url = [r for r in rows if r["raw_url"]]
    keyed = [r for r in with_url if r["norm_key"]]

    source_from: dict[str, int] = {}
    site_rule: dict[str, int] = {}
    for r in rows:
        source_from[r["source_from"] or "none"] = source_from.get(r["source_from"] or "none", 0) + 1
    for r in keyed:
        label = r["matched_site_rule"] or "(通用規則)"
        site_rule[label] = site_rule.get(label, 0) + 1

    groups: dict[str, list[dict]] = {}
    for r in keyed:
        groups.setdefault(r["norm_key"], []).append(r)

    repeats = []
    for key, rs in groups.items():
        if len(rs) < 2:
            continue
        stamps = sorted(x["created_at"] for x in rs if x["created_at"])
        span = ""
        if len(stamps) >= 2:
            try:
                a = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
                b = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
                span = round((b - a).total_seconds() / 86400, 1)
            except Exception:
                span = ""
        titles = sorted({x["title"] for x in rs if x["title"]})
        repeats.append({
            "key": key,
            "count": len(rs),
            # 一個人重試 N 次 vs N 個人各試一次 —— 兩者該有不同門檻
            # ⚠️ 這是「不重複客人數」的上界，不是不重複客人數本身 ——
            #    這把 token 沒有 read_customers，拿不到客人識別碼（見檔案上方）。
            "distinct_orders": len({x["order_name"] for x in rs}),
            "cancelled": sum(1 for x in rs if x["cancelled_at"]),
            "paid": sum(1 for x in rs if x["financial_status"] in _PAID_STATUSES),
            "first_seen": stamps[0][:10] if stamps else "",
            "last_seen": stamps[-1][:10] if stamps else "",
            "span_days": span,
            "brake_suitable": all(x["brake_suitable"] for x in rs),
            "matched_site_rule": rs[0]["matched_site_rule"],
            # >1 個標題 = 要人看一眼是不是假合併（附錄 C）
            "titles": titles[:5],
            "sample_url": rs[0]["raw_url"],
        })
    repeats.sort(key=lambda x: (-x["count"], x["key"]))

    histogram: dict[str, int] = {}
    for g in groups.values():
        histogram[str(len(g))] = histogram.get(str(len(g)), 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": days,
        "key_rule_version": source_key.KEY_RULE_VERSION,
        "line_items": total,
        "with_source_url": len(with_url),
        "coverage_pct": round(100.0 * len(with_url) / total, 1) if total else 0.0,
        # metafield 的比例應該隨時間掉到 0；掉不下去就表示 line item property
        # 沒有真的寫進訂單，那是要修的事。
        "source_from": source_from,
        "matched_site_rule": site_rule,
        "keys_total": len(groups),
        "keys_repeated": len(repeats),
        "histogram": dict(sorted(histogram.items(), key=lambda kv: int(kv[0]))),
        "repeat_keys": repeats[:60],
        # ★ 第一階段只描述分佈，門檻等資料。這裡**故意不算**「≥N 次就該擋」。
        "note": "第一階段：只記錄，不擋任何東西。N 值等一到兩週分佈再定。",
    }


async def scan_orders(shopify, days: int = 30, write: bool = True) -> dict:
    """
    掃近 N 天訂單 → 攤平成 line item → 算 key → 寫快照 + 回摘要。

    `shopify` 是 `ShopifyClient` 實例（由呼叫端傳進來，避免這支去碰憑證）。
    """
    if days > MAX_SCAN_DAYS:
        print(f"[Brake] ⚠️ days={days} 超過 {MAX_SCAN_DAYS}，夾成 {MAX_SCAN_DAYS} "
              f"—— token 沒有 read_all_orders，再多也只會靜默回 60 天")
        days = MAX_SCAN_DAYS
    days = max(1, days)

    orders = await shopify.fetch_orders_for_brake(days=days)
    rows = _rows_from_orders(orders)
    summary = build_summary(rows, days)

    if write:
        try:
            day = _today()
            path = os.path.join(_pick_dir(), f"orders-{day}.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            with open(os.path.join(_pick_dir(), f"summary-{day}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Brake] ⚠️ 快照寫入失敗（摘要仍然回傳）: {type(e).__name__}: {e}")

    print(f"[Brake] 掃描完成：近 {days} 天 {len(orders)} 筆訂單／"
          f"{summary['line_items']} 個 line item，"
          f"有 source_url {summary['with_source_url']}（{summary['coverage_pct']}%，"
          f"property {summary['source_from'].get('property', 0)}／"
          f"metafield {summary['source_from'].get('metafield', 0)}），"
          f"key {summary['keys_total']} 個，其中 ≥2 次的 {summary['keys_repeated']} 個")
    return summary


def read_summary(day: str = "") -> dict:
    """讀某天寫下的摘要快照；沒有就回空 dict。"""
    try:
        path = os.path.join(_pick_dir(), f"summary-{day or _today()}.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Brake] 讀取摘要失敗: {type(e).__name__}: {e}")
        return {}


def _seconds_until(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_scan_loop(shopify, enabled: bool, hour_utc: int, days: int):
    """
    每天固定時刻掃一次。

    ★ 這支**只讀訂單、只寫本機檔案**，不寫 Shopify、不寄信、不擋任何東西。
      一天 3 頁左右的查詢，對節流沒有實際影響。
    ★ 迴圈本身永遠不能死掉 —— 死了就沒有任何人會發現紀錄停了
      （同 scrape_digest.daily_digest_loop 的理由）。
    """
    if not enabled:
        print("[Brake] ⏸️ BRAKE_LOG_ENABLED=false，每日訂單掃描未啟用")
        return
    print(f"[Brake] ✅ 每日訂單掃描已啟用（每天 {hour_utc % 24:02d}:00 UTC，近 {days} 天）")
    while True:
        try:
            await asyncio.sleep(_seconds_until(hour_utc))
            await scan_orders(shopify, days=days)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Brake] ❌ 迴圈例外（繼續）: {type(e).__name__}: {e}")
            await asyncio.sleep(300)
