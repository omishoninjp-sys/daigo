"""
A' 請款前重驗價（2026-09-14）。

Shopify orders/create webhook 收單 → 重抓 source_url 拿現成本 → 比對售價 → 打標。
店家手動請款，所以標籤是請款前的人工檢查點。

🔴 fail-closed 核心：**只有『價格驗證:OK』代表「已驗且可直接請款」**。
   其他狀態（需確認／毛利偏低／降價／失敗／待驗／完全沒標籤）一律請款前人工確認。
   驗證過程任何一步出錯 → 那條 line 記『失敗』→ 整單不會變 OK。

這支只做「純計算 + 編排」，不直接打 Shopify（I/O 由 main.py 注入 get_meta / scrape_fn），
所以 verify_hmac / classify / verify_order 都能離線測。Shopify 讀寫在 main.py。
"""
import hmac
import hashlib
import base64
import re

# ── 標籤（固定命名，設計成日後每日摘要可直接聚合；不要改字面）──────────
TAG_PREFIX  = "價格驗證"
TAG_PENDING = f"{TAG_PREFIX}:待驗"      # webhook 一收到就打 → fail-closed 起點
TAG_OK      = f"{TAG_PREFIX}:OK"        # ★ 唯一「可直接請款」
TAG_BLOCK   = f"{TAG_PREFIX}:需確認"    # 現成本 ≥ 售價 → 毛利歸零/轉負，請款前必須人工
TAG_WARN    = f"{TAG_PREFIX}:毛利偏低"  # 現成本 ≥ 售價 × 0.9
TAG_DROP    = f"{TAG_PREFIX}:降價"      # 現成本 ≤ 記錄原價 × 0.8（②，客人多付，退差價候選）
TAG_FAIL    = f"{TAG_PREFIX}:失敗"      # 爬取/驗證出錯 → 當要人工（**不是 OK**）
TAG_NA      = f"{TAG_PREFIX}:不適用"    # 整單沒有任何代購 line → 這個檢查沒有東西可驗（見下）

# ── 給 backstop：這兩個標籤任一個在 = 請款前不需要人工看價；都不在 = 需人工 ──
#
# 🔴 兩個都「安全」，但安全的理由**不同**，改 backstop 邏輯前要分清楚：
#
#   OK     = 驗過沒問題。有代購 line，重抓現成本比對過售價，毛利還在。
#   不適用 = 沒有東西可驗。整單一條代購 line 都沒有 —— 常態商品（店內定價，
#            沒有日本採購成本會漂移）、安心GO 加購（固定價）、差額／代付這類
#            工作人員事後填的自訂項目。這個檢查對它們沒有任何可比的數字。
#
#   所以「不適用」不是「驗過」，是「這張單不歸這個檢查管」。它能放行是因為
#   本檢查要防的風險（日本原價漲了、售價沒跟上）在這些 line 上不存在，
#   不是因為有人看過它們的價格。把不適用當成「驗過」去推論別的事（例如
#   「不適用的單毛利都正常」）是錯的。
#
#   2026-09-15 實測近 60 天 472 筆：全代購 277、全常態 160、混單 35。
#   在這之前全常態單會被打「失敗」永遠留在 backstop 清單。
SAFE_TAGS = {TAG_OK, TAG_NA}
SAFE_TAG = TAG_OK        # 舊名，只剩測試在用；backstop 一律看 SAFE_TAGS
ALL_TERMINAL_TAGS = {TAG_OK, TAG_BLOCK, TAG_WARN, TAG_DROP, TAG_FAIL, TAG_NA}

# 代購品標題前綴：create_daigo_product 生成的，用來認「product 已刪除的 line 原本是不是代購品」
DAIGO_TITLE_PREFIX = "日本代購｜"

WARN_RATIO = 0.9    # 售價的九成 → 毛利剩不到一半
DROP_RATIO = 0.8    # 現成本掉到記錄原價八成以下 → ②


def verify_hmac(body: bytes, header_hmac: str, secret: str) -> bool:
    """
    Shopify webhook HMAC-SHA256 驗簽（base64）。

    🔴 secret 為空 → 一律 False。**絕不可以「沒有 secret 就跳過驗簽當通過」**，
       那等於對任何人開放偽造。header 為空同樣 False。
    """
    if not secret or not header_hmac:
        return False
    try:
        digest = hmac.new(secret.encode("utf-8"), body or b"", hashlib.sha256).digest()
    except Exception:
        return False
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, str(header_hmac))


def _to_int(v):
    """'17160' / '17160.00' / 17160 → 17160；取不到 → None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[^0-9.]", "", str(v))
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def classify(current_cost, selling, recorded):
    """
    回 (tag, detail)。比「現成本」與「售價」，門檻綁毛利、不是固定百分比。

    🔴 為什麼不是固定百分比：
      售價 = 原價 + fee(原價)，fee 依級距是原價的 15–25%（<5k→1.25 … >30k→1.15）。
      同樣是漲 10%，在 fee 25% 的低價品還有毛利、在 fee 15% 的高價品可能已經虧本。
      固定 % 門檻對低價品太鬆、對高價品太緊。改用「現成本 vs 售價」直接量到
      毛利被吃掉多少，門檻就自動隨級距浮動：
        現成本 ≥ 售價        → 需確認（毛利歸零或轉負）
        現成本 ≥ 售價 × 0.9  → 毛利偏低（剩不到一半）
        現成本 ≤ 記錄原價×0.8 → 降價（②，客人多付，退差價候選；先只打標不擋）
        其餘                 → OK
      （實例：14300→現15400、售17160：現<售 → OK；3916→現7700、售4895：現>售 → 需確認）

    現成本或售價取不到（None/<=0）→ 失敗（當要人工，**不是 OK**）。
    """
    cc, sp, rc = _to_int(current_cost), _to_int(selling), _to_int(recorded)
    detail = {"current_cost": cc, "selling": sp, "recorded": rc}
    if not cc or cc <= 0 or not sp or sp <= 0:
        return TAG_FAIL, detail
    if cc >= sp:
        return TAG_BLOCK, detail
    if cc >= sp * WARN_RATIO:
        return TAG_WARN, detail
    if rc and rc > 0 and cc <= rc * DROP_RATIO:
        return TAG_DROP, detail
    return TAG_OK, detail


def _is_daigo_title(title) -> bool:
    return str(title or "").startswith(DAIGO_TITLE_PREFIX)


async def verify_order(order: dict, get_meta, scrape_fn) -> dict:
    """
    逐 line 驗價。order 的形狀 = Shopify REST orders/{id}.json（也就是 webhook payload）：
      line_items[].product_id  已刪商品與自訂項目都是 null（REST 實測兩者完全一樣）
      line_items[].price       售價
      line_items[].title
    get_meta / scrape_fn 由 main.py 注入（async）：
      get_meta(product_id) -> {"found": bool,        product 查得到
                               "daigo": bool,        有任何 daigo.* metafield（= 代購品）
                               "source_url": str|None, "recorded": int|None}
      scrape_fn(source_url) -> current_original_jpy   失敗就 raise

    回 {"apply": set(要打的標籤), "ok": bool, "lines": [...]}。

    逐 line 判定：
      product_id 為 null
        ├─ 標題「日本代購｜」開頭 → 失敗（代購品已刪除，source_url 跟著沒了，無法重驗）
        └─ 否則                 → 不適用（差額／代付這類自訂項目，工作人員自己填的金額）
      product 查不到（建單後才被刪）→ 失敗（fail-closed：分不出是不是代購品）
      product 存在、完全沒有 daigo.* → 不適用（常態商品／安心GO，沒有日本成本會漂移）
      有 daigo.* 但沒 source_url     → 失敗
      有 source_url                  → 重抓現成本、classify

    🔴 「已刪除」與「自訂項目」在 REST 裡長得一模一樣（product_id=null,
       product_exists=false），唯一的結構性訊號是標題前綴 —— 那是本系統自己生成的。
       誤判方向是 fail-closed 的：自訂項目取名帶了前綴 → 多一個失敗（吵但安全）；
       要漏掉，得有人把代購品的前綴改掉（2026-09-15 近 60 天 0 筆）。

    整單判定**只看非「不適用」的 line**：
      一條都沒有 → {不適用}；全 OK → {OK}；否則 → 那些非 OK 標籤。
      不適用永遠不會和別的標籤一起出現在訂單上（逐 line 的不適用仍寫進明細）。
      混單（代購 + 安心GO／自訂項目）因此只由代購 line 決定 —— 2026-09-15 近 60 天 35 筆。

    🔴 fail-closed：任何一 line 的 get_meta / scrape_fn / classify 出錯 → 該 line = 失敗；
       只要有一條代購 line 不是 OK，整單就不是 OK（apply 不含 TAG_OK）。
       也就是「驗證函式拋錯 → 訂單不會變成 OK」。
    """
    lines = []
    for li in order.get("line_items") or []:
        pid = li.get("product_id")
        selling = li.get("price")
        title = li.get("title") or ""
        rec = {"product_id": pid, "title": title[:60], "selling_raw": selling}
        try:
            if not pid:
                if _is_daigo_title(title):
                    tag, detail = TAG_FAIL, {"reason": "代購品已刪除（product_id 為 null），無法重驗"}
                else:
                    tag, detail = TAG_NA, {"reason": "自訂項目（差額／代付等，無商品）"}
            else:
                meta = await get_meta(pid)
                if not meta.get("found"):
                    tag, detail = TAG_FAIL, {"reason": "product 查不到（建單後被刪？）"}
                elif not meta.get("daigo"):
                    tag, detail = TAG_NA, {"reason": "常態商品（沒有 daigo.* metafield）"}
                elif not meta.get("source_url"):
                    tag, detail = TAG_FAIL, {"reason": "商品缺 source_url metafield"}
                else:
                    src = meta["source_url"]
                    current = await scrape_fn(src)
                    tag, detail = classify(current, selling, meta.get("recorded"))
                    detail["source_url"] = src
        except Exception as e:
            tag, detail = TAG_FAIL, {"reason": f"{type(e).__name__}: {e}"}
        rec["tag"] = tag
        rec["detail"] = detail
        lines.append(rec)

    # 整單只看代購 line（不適用的 line 不參與判定）
    verified = {r["tag"] for r in lines if r["tag"] != TAG_NA}
    if lines and not verified:
        # ok=False 是刻意的：ok 的語意是「驗過沒問題」，不適用沒有驗過任何東西。
        # 要判斷「請款前需不需要人工」看 apply ∩ SAFE_TAGS，不要看 ok。
        return {"apply": {TAG_NA}, "ok": False, "na": True, "lines": lines}
    non_ok = {t for t in verified if t != TAG_OK}
    # 有代購 line、且沒有任何非 OK 標籤 → 整單 OK；否則套所有非 OK 標籤（不含 OK）
    overall_ok = bool(verified) and not non_ok
    apply = {TAG_OK} if overall_ok else non_ok
    return {"apply": apply, "ok": overall_ok, "lines": lines}
