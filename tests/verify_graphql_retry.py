"""
_graphql 的退避重試驗證（429 / THROTTLED / 5xx）
=================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_graphql_retry.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_graphql_retry.py`）

背景：CLAUDE.md 一直寫著「重試要涵蓋 429、THROTTLED 和 5xx」，但 `_graphql`
從來沒實作過 —— 一撞錯就 raise。而清理每輪要跑上千次 GraphQL（查訂單、補標籤、
逐件 productDelete），撞到節流的機率不低，一撞就整輪中止。

這支釘住：
  1. 429 / THROTTLED / 5xx / 連線層例外 → 退避重試
  2. 其他 GraphQL errors（欄位寫錯、權限不足）→ 立刻拋，且**錯誤原文不截斷**
     （原文是唯一能指出「哪個欄位／哪個變體」出問題的線索）
  3. 重試用盡 → 拋出說得出原因的例外，不會假裝成功

不連外：httpx.AsyncClient.post 換成假的。
"""
import sys
import json
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
from shopify_client import ShopifyClient

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


OK_BODY = {"data": {"shop": {"name": "ok"}}}
THROTTLE_BODY = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}
FIELD_ERR_BODY = {"errors": [{"message": "Field 'nope' doesn't exist on type 'Product'",
                              "extensions": {"code": "undefinedField"},
                              "locations": [{"line": 3, "column": 5}]}]}

SLEPT = []


class FakeResp:
    def __init__(self, status, body=None, retry_after=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = {"Retry-After": retry_after} if retry_after else {}
        self.text = json.dumps(self._body, ensure_ascii=False)

    def json(self):
        return self._body


class FakePost:
    """依序回傳 script 裡的東西；FakeResp 直接回，Exception 就 raise。"""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def __call__(self, url, headers=None, json=None):
        self.calls += 1
        item = self.script.pop(0) if self.script else FakeResp(200, OK_BODY)
        if isinstance(item, Exception):
            raise item
        return item


async def fast_sleep(sec):
    SLEPT.append(sec)


async def run(script, idempotent=True):
    """跑一次 _graphql，回傳 (結果或例外, 呼叫次數)。"""
    SLEPT.clear()
    poster = FakePost(script)
    orig_post, orig_sleep = httpx.AsyncClient.post, asyncio.sleep
    httpx.AsyncClient.post = lambda self, url, headers=None, json=None: poster(url, headers, json)
    asyncio.sleep = fast_sleep
    try:
        try:
            out = await ShopifyClient()._graphql("query { shop { name } }",
                                                 idempotent=idempotent)
        except Exception as e:
            out = e
    finally:
        httpx.AsyncClient.post = orig_post
        asyncio.sleep = orig_sleep
    return out, poster.calls


# ─────────────────────────────────────────────────────────────────────
async def test_happy():
    print("\n【1】一次就成功 → 不重試")
    out, calls = await run([FakeResp(200, OK_BODY)])
    check("回傳原本的 data", out == OK_BODY, str(out)[:60])
    check("只送一次", calls == 1, str(calls))
    check("沒有退避等待", SLEPT == [], str(SLEPT))


async def test_http_429():
    print("\n【2】HTTP 429 兩次後成功")
    out, calls = await run([FakeResp(429), FakeResp(429), FakeResp(200, OK_BODY)])
    check("最後成功", out == OK_BODY, str(out)[:40])
    check("送了 3 次", calls == 3, str(calls))
    check("退避 1s、2s", SLEPT == [1.0, 2.0], str(SLEPT))


async def test_throttled_extension():
    print("\n【3】HTTP 200 但 errors 是 THROTTLED → 也要重試")
    out, calls = await run([FakeResp(200, THROTTLE_BODY), FakeResp(200, OK_BODY)])
    check("最後成功", out == OK_BODY, str(out)[:40])
    check("送了 2 次", calls == 2, str(calls))
    check("有退避", SLEPT == [1.0], str(SLEPT))


async def test_5xx():
    print("\n【4】5xx 要重試（2026-08 批次改 2,086 件時中過一次 503）")
    out, calls = await run([FakeResp(503), FakeResp(200, OK_BODY)])
    check("最後成功", out == OK_BODY, str(out)[:40])
    check("送了 2 次", calls == 2, str(calls))


async def test_connection_error():
    print("\n【5】連線層例外（超時／斷線）也要重試")
    out, calls = await run([httpx.ConnectTimeout("timed out"), FakeResp(200, OK_BODY)])
    check("最後成功", out == OK_BODY, str(out)[:40])
    check("送了 2 次", calls == 2, str(calls))


async def test_retry_after():
    print("\n【6】Retry-After 要照做")
    out, calls = await run([FakeResp(429, retry_after="9"), FakeResp(200, OK_BODY)])
    check("第一次退避 ≥ 9s", SLEPT and SLEPT[0] >= 9, str(SLEPT))


async def test_field_error_not_retried():
    print("\n【7】★ 一般 GraphQL errors 不重試，而且原文不可截斷")
    out, calls = await run([FakeResp(200, FIELD_ERR_BODY), FakeResp(200, OK_BODY)])
    check("拋出例外", isinstance(out, Exception), type(out).__name__)
    check("只送一次（沒有白重試）", calls == 1, str(calls))
    check("沒有退避等待", SLEPT == [], str(SLEPT))
    msg = str(out)
    check("錯誤原文完整保留（欄位名）", "doesn't exist on type" in msg, msg[:70])
    check("連 locations 都在（不截斷）", "locations" in msg, msg[-60:])


async def test_non_retryable_http():
    print("\n【8】400/401 這種 HTTP 狀態碼不重試")
    out, calls = await run([FakeResp(400, {"errors": "bad request"}), FakeResp(200, OK_BODY)])
    check("拋出例外", isinstance(out, Exception), type(out).__name__)
    check("只送一次", calls == 1, str(calls))
    check("訊息帶狀態碼與回應內文", "400" in str(out) and "bad request" in str(out),
          str(out)[:70])


async def test_exhausted():
    print("\n【9】一直 503 → 重試用盡要拋，不可以假裝成功")
    out, calls = await run([FakeResp(503)] * 9)
    check("拋出例外", isinstance(out, Exception), type(out).__name__)
    check("送滿 5 次", calls == 5, str(calls))
    check("退避 1/2/4/8", SLEPT == [1.0, 2.0, 4.0, 8.0], str(SLEPT))
    check("訊息說得出是重試用盡", "重試" in str(out) and "503" in str(out), str(out)[:70])


async def test_non_idempotent():
    print("\n【10】★ 非冪等（建立商品）：5xx 與連線層例外一律不重試")
    # 代價不對稱：重複建商品是靜默出錯，沒人會發現，直到客人買了其中一件而
    # 另一件還掛著；建立失敗是明確的失敗，客人當場看到會再貼一次連結。
    out, calls = await run([FakeResp(503), FakeResp(200, OK_BODY)], idempotent=False)
    check("5xx 立刻拋，不重送", isinstance(out, Exception) and calls == 1, f"calls={calls}")
    check("沒有退避等待", SLEPT == [], str(SLEPT))
    check("訊息說得出為什麼不重試",
          "非冪等" in str(out) and "503" in str(out), str(out)[:70])

    out, calls = await run([httpx.ReadTimeout("no response"), FakeResp(200, OK_BODY)],
                           idempotent=False)
    check("連線層例外也不重送（送出去了但不知道有沒有執行）",
          isinstance(out, Exception) and calls == 1, f"calls={calls}")
    check("訊息點出是連線失敗", "連線失敗" in str(out), str(out)[:70])

    # 429 / THROTTLED 是 Shopify 明確拒收、確定沒執行 → 重送安全，仍要重試
    out, calls = await run([FakeResp(429), FakeResp(200, OK_BODY)], idempotent=False)
    check("429 仍然重試（確定沒被執行過）", out == OK_BODY and calls == 2, f"calls={calls}")

    out, calls = await run([FakeResp(200, THROTTLE_BODY), FakeResp(200, OK_BODY)],
                           idempotent=False)
    check("THROTTLED 仍然重試", out == OK_BODY and calls == 2, f"calls={calls}")

    out, calls = await run([FakeResp(200, OK_BODY)], idempotent=False)
    check("正常情況不受影響", out == OK_BODY and calls == 1, f"calls={calls}")


async def test_default_is_idempotent():
    print("\n【11】預設仍是冪等模式（查詢、productDelete、tagsAdd 不受影響）")
    out, calls = await run([FakeResp(503), FakeResp(200, OK_BODY)])
    check("沒帶參數時 5xx 照樣重試", out == OK_BODY and calls == 2, f"calls={calls}")


# ─────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 74)
    print("_graphql 退避重試驗證（假的 httpx，不連外）")
    print("=" * 74)
    await test_happy()
    await test_http_429()
    await test_throttled_extension()
    await test_5xx()
    await test_connection_error()
    await test_retry_after()
    await test_field_error_not_retried()
    await test_non_retryable_http()
    await test_exhausted()
    await test_non_idempotent()
    await test_default_is_idempotent()

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
