"""
_download_b64：重構等價性（commit 1）＋ 兩段 profile 重試（commit 2 / Part B，2026-09-13）

commit 1 把 header 與 httpx 呼叫抽到 _IMAGE_HEADER_PROFILES + _fetch_image，行為與今天一致。
commit 2 讓 _download_b64 在 browser 收到 401/403 時換 plain 重試（Dior 圖床：帶瀏覽器 UA
被 Akamai 403、無 UA 走 Cloudflare 200），逾時／例外不重試（MUJI 是 TLS 指紋擋、逾時，
換 UA 無用，重試只會再等一輪）。

兩段驗證都用側錄假 httpx，攔下**實際送出的 url/headers/follow_redirects/timeout**：

  【A 等價】Part B **不改變**的情況仍與今天逐字相同（200 圖、200 非圖、逾時、data:、空、None）——
           與「今天程式碼的逐字拷貝」比對回傳值與送出參數。403 這條 Part B 刻意改，不在等價集。
  【B 重試】Part B 的行為：browser→403→換 plain、browser 成功不重試、逾時不重試（釘住呼叫次數）。

假 httpx 依 header 有無 User-Agent 分辨 profile，可對 browser／plain 回不同結果
（模擬 Dior 真實的 UA 分流）。不連外。
"""
import sys
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


# ── 側錄假 httpx（依 profile 回不同結果）──────────────────────────────
CALLS = []          # 每次 get 記一筆：{profile, client_kwargs, url, headers}
RESP_BY_PROFILE = {}  # {"browser": {...}, "plain": {...}}；值是 {status,content_type,content} 或 {raise}


class _FakeResp:
    def __init__(self, status, content_type, content):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type, "server": "fake"}


class _FakeClient:
    def __init__(self, **kwargs):
        self._client_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        headers = dict(headers or {})
        profile = "browser" if "User-Agent" in headers else "plain"
        # ★ 在回應／拋錯之前先記下實際送出的參數（例外情況也要錄得到）
        CALLS.append({"profile": profile, "client_kwargs": dict(self._client_kwargs),
                      "url": url, "headers": headers})
        resp = RESP_BY_PROFILE[profile]
        if resp.get("raise"):
            raise resp["raise"]
        return _FakeResp(resp["status"], resp["content_type"], resp["content"])


# ── 今天（commit 1 之前）_download_b64 的逐字拷貝 ─────────────────────
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


def _set(browser=None, plain=None):
    """設定各 profile 這輪要回什麼。省略 = 沿用 browser 那份（等價集只需一份）。"""
    global RESP_BY_PROFILE
    RESP_BY_PROFILE = {"browser": browser, "plain": plain if plain is not None else browser}


async def _run(fn, url):
    CALLS.clear()
    ret = await fn(url)
    return ret, list(CALLS)


IMG = {"status": 200, "content_type": "image/jpeg", "content": b"IMGDATA"}
HTML200 = {"status": 200, "content_type": "text/html", "content": b"<html>"}
DENY403 = {"status": 403, "content_type": "text/html", "content": b"denied"}
TIMEOUT = {"raise": httpx.ReadTimeout("timeout"), "status": 0, "content_type": "", "content": b""}


# ══════════════════════════════════════════════════════════════════════
# A. 等價：Part B 不改變的情況仍與今天逐字相同
# ══════════════════════════════════════════════════════════════════════
# 排除 403 —— Part B 刻意在 403 換 plain 重試，那條在 B 段驗。
EQUIV_CASES = [
    ("一般圖片 200 image/jpeg", "https://cdn.example/a.jpg", IMG),
    ("200 但非 image（text/html）→ None", "https://cdn.example/a.jpg", HTML200),
    ("get 拋 ReadTimeout → None（逾時不重試）", "https://cdn.example/a.jpg", TIMEOUT),
    ("data:image 直接回 base64（不打 httpx）", "data:image/png;base64,QUJD", IMG),
    ("空字串 → None（不打 httpx）", "", IMG),
    ("None → None（不打 httpx）", None, IMG),
    ("URL 帶 query/utf8 → Referer 與 url 原樣帶出",
     "https://cdn.example/画像.jpg?sw=1800&x=1",
     {"status": 200, "content_type": "image/jpeg", "content": b"IMG2"}),
]


async def test_equivalence():
    print("\n" + "=" * 74)
    print("A. 等價：Part B 不改變的情況，改後 == 今天（逐字拷貝）")
    print("=" * 74)
    for name, url, resp in EQUIV_CASES:
        print(f"\n【{name}】")
        _set(browser=resp)          # 這些情況只會用到 browser
        new_ret, new_calls = await _run(sc.ShopifyClient._download_b64, url)
        _set(browser=resp)
        old_ret, old_calls = await _run(_download_b64_REFERENCE, url)
        check("回傳值相同", new_ret == old_ret, f"new={new_ret!r} old={old_ret!r}")
        check("★ 送出的 url/headers/client 參數逐一相同", new_calls == old_calls,
              f"\n      new={new_calls}\n      old={old_calls}" if new_calls != old_calls else "")
        if old_calls:
            c = old_calls[0]
            check("  └ follow_redirects=True 且 timeout=15",
                  c["client_kwargs"].get("follow_redirects") is True
                  and c["client_kwargs"].get("timeout") == 15)
            check("  └ browser UA + Referer=url",
                  c["headers"].get("User-Agent", "").startswith("Mozilla/5.0")
                  and c["headers"].get("Referer") == url)
            check("  └ 這些情況只送一次、且沒送 plain（browser 就定案）",
                  len(old_calls) == 1 and old_calls[0]["profile"] == "browser")


# ══════════════════════════════════════════════════════════════════════
# B. 重試：Part B 的行為（釘住呼叫次數，不是只看回傳值）
# ══════════════════════════════════════════════════════════════════════
async def test_retry():
    print("\n" + "=" * 74)
    print("B. 兩段 profile 重試")
    print("=" * 74)
    dl = sc.ShopifyClient._download_b64
    url = "https://www.dior.com/img/Y0000265.jpg?sw=1800"

    print("\n【case 1｜Dior：browser 403 → 換 plain 200 → 回 base64】")
    _set(browser=DENY403, plain=IMG)
    ret, calls = await _run(dl, url)
    check("★ 回 base64（改前：只送 browser→403→None，故此條改前為紅）",
          ret == base64.b64encode(b"IMGDATA").decode(), repr(ret))
    check("★ 呼叫 2 次：browser 後 plain", [c["profile"] for c in calls] == ["browser", "plain"],
          str([c["profile"] for c in calls]))
    if len(calls) == 2:
        check("  └ plain 那次沒有 User-Agent、沒有 Referer",
              "User-Agent" not in calls[1]["headers"] and "Referer" not in calls[1]["headers"],
              str(calls[1]["headers"]))

    print("\n【case 2｜一般站：browser 200 → 不重試】")
    _set(browser=IMG, plain=DENY403)
    ret, calls = await _run(dl, url)
    check("回 base64", ret == base64.b64encode(b"IMGDATA").decode())
    check("★ 只呼叫 1 次（browser 成功，plain 不跑）—— 其他站不多一次請求",
          [c["profile"] for c in calls] == ["browser"], str([c["profile"] for c in calls]))

    print("\n【case 3｜MUJI：browser 逾時 → 不重試】")
    _set(browser=TIMEOUT, plain=IMG)     # plain 就算能成功也不該被呼叫
    ret, calls = await _run(dl, url)
    check("回 None", ret is None, repr(ret))
    check("★★ 只呼叫 1 次（逾時不重試；改前也是 1 次 → 此條改前改後都綠，"
          "釘住『不能因 Part B 變成逾時重試』）",
          len(calls) == 1 and calls[0]["profile"] == "browser",
          str([c["profile"] for c in calls]))

    print("\n【case 4｜Dior 但 plain 也 403 → None（退回 src）】")
    _set(browser=DENY403, plain=DENY403)
    ret, calls = await _run(dl, url)
    check("回 None", ret is None)
    check("呼叫 2 次（都試過了）", len(calls) == 2, str([c["profile"] for c in calls]))

    print("\n【case 5｜404 → 不重試（不是 UA 問題）】")
    _set(browser={"status": 404, "content_type": "text/html", "content": b"nf"}, plain=IMG)
    ret, calls = await _run(dl, url)
    check("回 None", ret is None)
    check("★ 只呼叫 1 次（404 換 UA 無益，不重試）", len(calls) == 1,
          str([c["profile"] for c in calls]))

    print("\n【case 6｜browser 403 → plain 逾時 → None（403 值得試，plain 才逾時）】")
    _set(browser=DENY403, plain=TIMEOUT)
    ret, calls = await _run(dl, url)
    check("回 None", ret is None)
    check("呼叫 2 次", len(calls) == 2, str([c["profile"] for c in calls]))


async def main_():
    print("=" * 74)
    print("_download_b64：等價性 + 兩段 profile 重試")
    print("=" * 74)
    orig = httpx.AsyncClient
    httpx.AsyncClient = _FakeClient
    try:
        await test_equivalence()
        await test_retry()
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
