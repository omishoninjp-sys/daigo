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

# 給 backstop：這個標籤在 = 已驗且安全；不在 = 需人工（含沒送到/失敗/待驗）
SAFE_TAG = TAG_OK
ALL_TERMINAL_TAGS = {TAG_OK, TAG_BLOCK, TAG_WARN, TAG_DROP, TAG_FAIL}

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


async def verify_order(order: dict, get_meta, scrape_fn) -> dict:
    """
    逐 line 驗價。get_meta / scrape_fn 由 main.py 注入（async）：
      get_meta(product_id)  -> (source_url, recorded_original_jpy)  取不到回 (None, None)
      scrape_fn(source_url) -> current_original_jpy                 失敗就 raise

    回 {"apply": set(要打的標籤), "ok": bool, "lines": [...]}。

    🔴 fail-closed：任何一 line 的 get_meta / scrape_fn / classify 出錯 → 該 line = 失敗；
       只要有一條不是 OK，整單就不是 OK（apply 不含 TAG_OK）。
       也就是「驗證函式拋錯 → 訂單不會變成 OK」。
    """
    lines, tags = [], set()
    for li in order.get("line_items") or []:
        pid = li.get("product_id")
        selling = li.get("price")
        rec = {"product_id": pid, "title": (li.get("title") or "")[:60], "selling_raw": selling}
        try:
            src, recorded = await get_meta(pid)
            if not src:
                tag, detail = TAG_FAIL, {"reason": "商品缺 source_url metafield"}
            else:
                current = await scrape_fn(src)
                tag, detail = classify(current, selling, recorded)
                detail["source_url"] = src
        except Exception as e:
            tag, detail = TAG_FAIL, {"reason": f"{type(e).__name__}: {e}"}
        rec["tag"] = tag
        rec["detail"] = detail
        lines.append(rec)
        tags.add(tag)

    non_ok = {t for t in tags if t != TAG_OK}
    # 有 line、且沒有任何非 OK 標籤 → 整單 OK；否則套所有非 OK 標籤（不含 OK）
    overall_ok = bool(lines) and not non_ok
    apply = {TAG_OK} if overall_ok else non_ok
    return {"apply": apply, "ok": overall_ok, "lines": lines}
