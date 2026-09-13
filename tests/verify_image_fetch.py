"""
_download_b64 重構等價性（commit 1，2026-09-13）

commit 1 把 _download_b64 的 header 與 httpx 呼叫抽到 _IMAGE_HEADER_PROFILES +
_fetch_image，宣稱「行為與今天完全一致」。這支**直接證明等價**，不靠「現有測試維持綠」——
今天已經有四次是測試通過但沒驗到東西（fixture 兩邊洗成同一答案之類）。

做法：用側錄假 httpx 攔下**實際送出去的參數**（AsyncClient 的 timeout/follow_redirects、
get 的 url/headers），同一組輸入分別跑
  · _download_b64_REFERENCE —— 今天（commit 1 之前）那段程式碼的逐字拷貝
  · ShopifyClient._download_b64 —— 重構後的真實方法
逐一比對「回傳值」與「httpx 收到的 url/headers/follow_redirects/timeout」全部相同。
比的是送出去的參數，不是只比回傳值。

不連外：httpx.AsyncClient 全程換成假的。
"""
import sys
import types
import base64
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import shopify_client as sc

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# ── 側錄假 httpx ──────────────────────────────────────────────────────
CALLS = []          # 每次 AsyncClient(...).get(...) 記一筆
RESPONSE = {}        # 這輪要回什麼（status/content_type/content）或 raise


class _FakeResp:
    def __init__(self, status, content_type, content):
        self.status_code = status
        self._content_type = content_type
        self.content = content
        self.headers = {"content-type": content_type, "server": RESPONSE.get("server", "fake")}


class _FakeClient:
    def __init__(self, **kwargs):
        self._client_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        # ★ 在「回應／拋錯」之前先記下實際送出去的參數 —— 例外情況也要錄得到
        CALLS.append({
            "client_kwargs": dict(self._client_kwargs),
            "url": url,
            "headers": dict(headers or {}),
        })
        if RESPONSE.get("raise"):
            raise RESPONSE["raise"]
        return _FakeResp(RESPONSE["status"], RESPONSE["content_type"], RESPONSE["content"])


# ── 今天（commit 1 之前）_download_b64 的逐字拷貝 ─────────────────────
# 只把 headers/timeout/follow_redirects/判斷/print 原樣抄過來；用同一個被 patch 的 httpx。
async def _download_b64_REFERENCE(url):
    if not url or url.startswith("data:image"):
        return url.split(",", 1)[1] if url and "," in url else None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": url,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                return base64.b64encode(r.content).decode()
    except Exception as e:
        print(f"[Shopify] 圖片下載失敗，改用 src: {e}")
    return None


async def _run_one(fn, url, resp):
    """在同一份 RESPONSE 下跑 fn(url)，回 (回傳值, 這次的 CALLS)。"""
    global RESPONSE
    RESPONSE = dict(resp)
    CALLS.clear()
    ret = await fn(url)
    return ret, list(CALLS)


# 測試矩陣：每一項是 (名稱, url, RESPONSE 設定)
CASES = [
    ("一般圖片 200 image/jpeg",
     "https://cdn.example/a.jpg",
     {"status": 200, "content_type": "image/jpeg", "content": b"IMGDATA"}),
    ("200 但非 image（text/html）→ None",
     "https://cdn.example/a.jpg",
     {"status": 200, "content_type": "text/html", "content": b"<html>"}),
    ("403 → None",
     "https://cdn.example/a.jpg",
     {"status": 403, "content_type": "text/html", "content": b"denied"}),
    ("get 拋 ReadTimeout → None（例外分支）",
     "https://cdn.example/a.jpg",
     {"raise": httpx.ReadTimeout("timeout"), "status": 0, "content_type": "", "content": b""}),
    ("data:image 直接回 base64（不打 httpx）",
     "data:image/png;base64,QUJD",
     {"status": 200, "content_type": "image/png", "content": b"x"}),
    ("空字串 → None（不打 httpx）",
     "", {"status": 200, "content_type": "image/jpeg", "content": b"x"}),
    ("None → None（不打 httpx）",
     None, {"status": 200, "content_type": "image/jpeg", "content": b"x"}),
    ("URL 帶 query/utf8 → Referer 與 url 原樣帶出",
     "https://cdn.example/画像.jpg?sw=1800&x=1",
     {"status": 200, "content_type": "image/jpeg", "content": b"IMG2"}),
]


async def main_():
    print("=" * 74)
    print("_download_b64 重構等價性：改前(REFERENCE) vs 改後(真實方法)")
    print("=" * 74)
    orig = httpx.AsyncClient
    httpx.AsyncClient = _FakeClient
    try:
        for name, url, resp in CASES:
            print(f"\n【{name}】")
            new_ret, new_calls = await _run_one(sc.ShopifyClient._download_b64, url, resp)
            old_ret, old_calls = await _run_one(_download_b64_REFERENCE, url, resp)
            check("回傳值相同", new_ret == old_ret, f"new={new_ret!r} old={old_ret!r}")
            check("httpx 呼叫次數相同", len(new_calls) == len(old_calls),
                  f"new={len(new_calls)} old={len(old_calls)}")
            check("★ 送出的 url / headers / client 參數逐一相同",
                  new_calls == old_calls,
                  f"\n      new={new_calls}\n      old={old_calls}" if new_calls != old_calls else "")
            # 針對有打 httpx 的情況，把關鍵欄位挑明講（避免「兩邊都空」的假綠）
            if old_calls:
                c = old_calls[0]
                check("  └ follow_redirects=True", c["client_kwargs"].get("follow_redirects") is True)
                check("  └ timeout=15", c["client_kwargs"].get("timeout") == 15)
                check("  └ headers 有 browser UA + Referer=url",
                      c["headers"].get("User-Agent", "").startswith("Mozilla/5.0")
                      and c["headers"].get("Referer") == url,
                      str(c["headers"]))
                check("  └ 沒有多送 plain profile（commit 1 不重試）", len(old_calls) == 1)
    finally:
        httpx.AsyncClient = orig

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_()))
