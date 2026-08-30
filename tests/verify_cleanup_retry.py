"""
自動清理的分頁重試與「中止 vs 完成」驗證
==========================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_cleanup_retry.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_cleanup_retry.py`）

2026-08-30 線上實況：自動清理刪到第 262 件之後停住，611 件該刪的留在站上，
而 log 印的是一行看起來完全正常的「[Cleanup] 完成」。原因是分頁請求只要非 200
就 `break`，沒有重試、也沒有標成不完整 —— **唯一的觀測管道在給假訊號。**

這支釘住三件事：
  1. 分頁遇到 429/5xx 會退避重試，重試成功就照常把整輪跑完
  2. 重試用盡時印的是「中止」不是「完成」，而且已刪除的數字要對
  3. 回傳值帶 completed 欄位（之後接即時警報直接用這個）

不連外：httpx 與 _graphql 都換成假的，一件真商品都不會被刪。
"""
import sys
import asyncio
import io as _io
import contextlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import shopify_client
from shopify_client import ShopifyClient

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# ─────────────────────────────────────────────────────────────────────
# 假的 HTTP 回應與商品分頁
# ─────────────────────────────────────────────────────────────────────
OLD = "2026-01-01T00:00:00Z"          # 遠早於 cutoff，一定會被刪

PAGE1 = [{"id": 101, "title": "舊商品 A", "created_at": OLD, "status": "active", "tags": "daigo"},
         {"id": 102, "title": "舊商品 B", "created_at": OLD, "status": "active", "tags": "daigo"},
         {"id": 103, "title": "舊商品 C", "created_at": OLD, "status": "active", "tags": "daigo"}]
PAGE2 = [{"id": 201, "title": "舊商品 D", "created_at": OLD, "status": "active", "tags": "daigo"},
         {"id": 202, "title": "舊商品 E", "created_at": OLD, "status": "active", "tags": "daigo"}]

NEXT_LINK = ('<https://x/admin/api/2024-10/products.json?limit=250&page_info=PAGE2>; rel="next"')


class FakeResp:
    def __init__(self, status, products=None, link="", retry_after=""):
        self.status_code = status
        self._products = products or []
        self.headers = {}
        if link:
            self.headers["Link"] = link
        if retry_after:
            self.headers["Retry-After"] = retry_after
        self.text = "fake"

    def json(self):
        return {"products": self._products}


class FakeGet:
    """
    模擬 products.json 的兩頁分頁；第 2 頁前 fail_times 次回 fail_status。
    """
    def __init__(self, fail_times=0, fail_status=429, retry_after=""):
        self.fail_times = fail_times
        self.fail_status = fail_status
        self.retry_after = retry_after
        self.calls = []

    async def __call__(self, url, headers=None, params=None):
        params = params or {}
        page2 = params.get("page_info") == "PAGE2"
        self.calls.append("p2" if page2 else "p1")
        if page2:
            if self.fail_times > 0:
                self.fail_times -= 1
                return FakeResp(self.fail_status, retry_after=self.retry_after)
            return FakeResp(200, PAGE2)
        return FakeResp(200, PAGE1, link=NEXT_LINK)


async def fake_graphql(self, query, variables=None):
    """訂單查詢／標籤查詢／刪除三種都回最單純的成功結果。"""
    if "orders(" in query:
        return {"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": []}}}
    if "nodes(ids:" in query:
        return {"data": {"nodes": []}}
    if "productDelete" in query:
        pid = (variables or {}).get("input", {}).get("id", "")
        DELETED.append(int(pid.split("/")[-1]))
        return {"data": {"productDelete": {"deletedProductId": pid, "userErrors": []}}}
    raise AssertionError(f"沒預期到的 GraphQL: {query[:60]}")


DELETED = []
SLEPT = []


async def fast_sleep(sec):
    SLEPT.append(sec)          # 退避秒數記下來，但不真的等


async def run_cleanup(fail_times=0, fail_status=429, retry_after=""):
    """跑一輪假的清理，回傳 (結果 dict, 印出來的字串, 這輪刪掉的 id)。"""
    DELETED.clear()
    SLEPT.clear()
    getter = FakeGet(fail_times, fail_status, retry_after)

    orig_get, orig_gql, orig_sleep = httpx.AsyncClient.get, ShopifyClient._graphql, asyncio.sleep
    orig_cid = shopify_client.DAIGO_COLLECTION_ID
    httpx.AsyncClient.get = lambda self, url, headers=None, params=None: getter(url, headers, params)
    ShopifyClient._graphql = fake_graphql
    asyncio.sleep = fast_sleep
    shopify_client.DAIGO_COLLECTION_ID = "999"
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = await ShopifyClient().cleanup_old_daigo_products(days=30)
    finally:
        httpx.AsyncClient.get = orig_get
        ShopifyClient._graphql = orig_gql
        asyncio.sleep = orig_sleep
        shopify_client.DAIGO_COLLECTION_ID = orig_cid
    return result, buf.getvalue(), list(DELETED)


# ─────────────────────────────────────────────────────────────────────
async def test_happy_path():
    print("\n【1】兩頁都正常 → 完成，五件都刪掉")
    r, out, deleted = await run_cleanup()
    check("completed=True", r.get("completed") is True, str(r.get("completed")))
    check("刪除 5 件", r["deleted_count"] == 5, str(r["deleted_count"]))
    check("兩頁的商品都刪到", sorted(deleted) == [101, 102, 103, 201, 202], str(sorted(deleted)))
    check("log 印「完成」", "[Cleanup] 完成：" in out, out.strip().splitlines()[-1][:60])
    check("log 沒有「中止」", "中止" not in out)


async def test_retry_then_success():
    print("\n【2】第 2 頁先回兩次 429 → 退避重試後成功，整輪照樣跑完")
    r, out, deleted = await run_cleanup(fail_times=2)
    check("有印重試訊息", "後重試" in out,
          next((l for l in out.splitlines() if "重試" in l), "")[:60])
    check("重試 2 次（退避 1s、2s）", SLEPT[:2] == [1.0, 2.0], str(SLEPT[:4]))
    check("completed=True（重試成功不算中止）", r.get("completed") is True)
    check("五件都還是刪到了", sorted(deleted) == [101, 102, 103, 201, 202], str(sorted(deleted)))
    check("log 印「完成」", "[Cleanup] 完成：" in out)


async def test_retry_honours_retry_after():
    print("\n【3】429 帶 Retry-After → 至少等那麼久")
    r, out, _ = await run_cleanup(fail_times=1, retry_after="7")
    check("第一次退避 ≥ Retry-After(7s)", SLEPT and SLEPT[0] >= 7, str(SLEPT[:2]))


async def test_exhausted_is_abort_not_complete():
    print("\n【4】★ 第 2 頁一直 429 → 必須是「中止」，不可以是「完成」")
    r, out, deleted = await run_cleanup(fail_times=99)
    check("completed=False", r.get("completed") is False, str(r.get("completed")))
    check("log 印「中止」", "[Cleanup] ⚠️ 中止：" in out,
          next((l for l in out.splitlines() if "中止" in l), "")[:70])
    check("★ log 不可以出現「完成」（假訊號比沒訊號更糟）",
          "完成" not in out, out[-200:])
    check("中止訊息指出是第幾頁", "第 2 頁" in r.get("incomplete_reason", ""), r.get("incomplete_reason", ""))
    check("中止訊息帶狀態碼", "429" in r.get("incomplete_reason", ""), r.get("incomplete_reason", ""))
    check("已刪除的數字是對的（第 1 頁那 3 件）", r["deleted_count"] == 3, str(r["deleted_count"]))
    check("真的只刪了那 3 件", sorted(deleted) == [101, 102, 103], str(sorted(deleted)))
    check("log 講得出已刪除幾件", "已刪除 3 件" in out)
    check("log 講得出還沒處理完", "剩餘未處理" in out)
    check("中止原因也進 errors", any("第 2 頁" in e for e in r["errors"]), str(r["errors"])[:80])
    check("重試用盡＝試滿 5 次", SLEPT.count(1.0) + SLEPT.count(2.0) + SLEPT.count(4.0)
          + SLEPT.count(8.0) >= 4, str(SLEPT))


async def test_non_retryable_status():
    print("\n【5】404 這種重試也沒用的狀態碼 → 不重試，直接中止")
    r, out, deleted = await run_cleanup(fail_times=99, fail_status=404)
    check("completed=False", r.get("completed") is False)
    check("沒有退避等待（不浪費 5 次）", SLEPT == [] or all(s == 0 for s in SLEPT), str(SLEPT))
    check("中止原因帶 404", "404" in r.get("incomplete_reason", ""), r.get("incomplete_reason", ""))
    check("已刪除 3 件仍然正確", r["deleted_count"] == 3, str(r["deleted_count"]))


async def test_5xx_also_retried():
    print("\n【6】503 也要重試（規格：429、THROTTLED 和 5xx）")
    r, out, deleted = await run_cleanup(fail_times=1, fail_status=503)
    check("重試後成功", r.get("completed") is True and r["deleted_count"] == 5,
          f'completed={r.get("completed")} deleted={r["deleted_count"]}')
    check("有印重試訊息且標出 503", "HTTP 503" in out,
          next((l for l in out.splitlines() if "503" in l), "")[:60])


class _StopLoop(BaseException):
    """跳出 while True 用。繼承 BaseException，才不會被迴圈自己的 except Exception 吃掉。"""


async def _drive_auto_loop(result: dict) -> str:
    """
    真的跑一次 main._auto_cleanup_loop，回傳它印出來的東西。

    ★ 故意驅動「正式的那個迴圈」而不是某個抽出來的 helper —— 這樣同一支測試在
      **未修正的版本**上也跑得起來，才能先重現「中止卻印完成」再驗修正。
    asyncio.sleep 換成假的：第 1 次是啟動前的 60 秒（直接放行），第 2 次是跑完
    一輪後的 24 小時（丟 _StopLoop 跳出）。
    """
    import main as m

    async def fake_cleanup(days=30):
        return result

    n = {"sleep": 0}

    async def fake_sleep(sec):
        n["sleep"] += 1
        if n["sleep"] >= 2:
            raise _StopLoop

    orig_cleanup = m.shopify.cleanup_old_daigo_products
    orig_sleep = asyncio.sleep
    m.shopify.cleanup_old_daigo_products = fake_cleanup
    asyncio.sleep = fake_sleep
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                await m._auto_cleanup_loop()
            except _StopLoop:
                pass
    finally:
        m.shopify.cleanup_old_daigo_products = orig_cleanup
        asyncio.sleep = orig_sleep
    return buf.getvalue()


ABORTED = {"deleted_count": 87, "skipped_count": 240, "protected_count": 3,
           "error_count": 1, "errors": ["分頁在第 3 頁失敗（HTTP 429）"],
           "deleted_ids": [], "cutoff_date": "",
           "completed": False, "incomplete_reason": "分頁在第 3 頁失敗（HTTP 429）"}

FINISHED = {"deleted_count": 12, "skipped_count": 5, "protected_count": 1,
            "error_count": 0, "errors": [], "deleted_ids": [], "cutoff_date": "",
            "completed": True, "incomplete_reason": ""}


async def test_auto_loop_log():
    print(chr(10) + "【7】★ 每天在跑的 _auto_cleanup_loop 也不可以無條件印「完成」")
    # cleanup_old_daigo_products 的四條中止路徑全是 return 不是 raise，迴圈的
    # except 一條都攔不到。每天跑的是這支、不是 /api/admin/cleanup —— 這裡印錯，
    # 唯一的觀測管道就在給假訊號（2026-08-30 少刪 611 件就是這樣沒被發現的）。
    out = await _drive_auto_loop(ABORTED)
    check("中止時印「⚠️ 中止」", "[AutoCleanup] ⚠️ 中止：" in out,
          next((l for l in out.splitlines() if "中止" in l), "")[:70])
    check("★ 中止時不可以印「✅ 完成」（未修版本會在這裡紅燈）",
          "[AutoCleanup] ✅ 完成" not in out,
          next((l for l in out.splitlines() if "✅ 完成" in l), "")[:70])
    check("印得出中止原因", "第 3 頁" in out, out[-90:])
    check("印得出已刪除幾件與剩餘未處理",
          "已刪除 87 件" in out and "剩餘未處理" in out)

    # 反向：正常跑完那條不可以被改成一律印中止
    out = await _drive_auto_loop(FINISHED)
    check("跑完時印「✅ 完成」", "[AutoCleanup] ✅ 完成：" in out, out.strip()[-60:])
    check("跑完時不出現「中止」", "中止" not in out)


# ─────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 74)
    print("自動清理：分頁重試與中止訊息驗證（全程用假的 httpx / GraphQL）")
    print("=" * 74)
    await test_happy_path()
    await test_retry_then_success()
    await test_retry_honours_retry_after()
    await test_exhausted_is_abort_not_complete()
    await test_non_retryable_status()
    await test_5xx_also_retried()
    await test_auto_loop_log()

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
