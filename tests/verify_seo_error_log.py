"""
SEO 錯誤 log 不可以外洩金鑰的回歸測試（離線，不連外，不需要憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_seo_error_log.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_seo_error_log.py`）

★ 這支存在的理由（2026-09-02）：
   `_call_chatgpt` 收到非 200 時原本印的是 `resp.text[:200]` —— 無條件把
   回應主體前 200 字元寫進 Zeabur Runtime Log。OpenAI 的 401 回應
   `error.message` 裡會回帶遮蔽過的金鑰（`sk-proj-****…`），於是那串就
   進了 log。查 SEO 降級問題時實際印出來過一次。

   注意「只取 error.message」**不足以解決問題** —— 金鑰就在 message 裡面。
   必須再對訊息做一次 `sk-…` 遮蔽。這支測試釘的就是這件事。

   通則：**任何把外部 API 回應寫進 log 的地方，都要先問「這段內容裡會不會
   有憑證」。** 對方今天不放，不代表改版後不放。

判準：`_safe_api_error()` 的輸出**不可以**出現
   · `sk-` 開頭的字串
   · 連續 8 個以上的星號（遮蔽形式本身也算洩漏長度資訊）
   · 32 碼以上的連續 base62 片段
"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from seo_title import _safe_api_error

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail else ""))


# 金鑰形態偵測（不含反斜線跳脫，避免這個檔案自己踩到編碼／跳脫問題）
LEAK_PATTERNS = {
    "sk- 開頭": "sk-",
    "連續遮蔽星號(>=8)": "[" + chr(42) + "]{8,}",
    "疑似金鑰片段(>=32碼)": "[A-Za-z0-9_-]{32,}",
}


def leaks(text):
    """回傳命中的洩漏形態名稱清單。"""
    return [name for name, pat in LEAK_PATTERNS.items() if re.search(pat, text)]


class FakeResp:
    """最小化的 httpx.Response 替身。payload=None 代表回應不是 JSON。"""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


# ── A. OpenAI 401 的真實回應格式（error.message 內含遮蔽金鑰）────────
_MASKED = "sk-proj-" + chr(42) * 140
OPENAI_401 = FakeResp(401, {
    "error": {
        "message": ("Incorrect API key provided: " + _MASKED +
                    ". You can find your API key at "
                    "https://platform.openai.com/account/api-keys."),
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    }
}, text='{"error": {"message": "Incorrect API key provided: ' + _MASKED + '"}}')

# ── B. 回應不是 JSON（Cloudflare 擋頁、502 HTML …）──────────────────
NOT_JSON = FakeResp(502, None, text="<html><title>502 Bad Gateway</title></html>")

# ── C. 是 JSON 但沒有 error.message ───────────────────────────────
NO_MESSAGE = [
    ("空 dict", FakeResp(500, {})),
    ("有 error 但無 message", FakeResp(500, {"error": {"type": "server_error"}})),
    ("error 是 None", FakeResp(500, {"error": None})),
    ("message 是空字串", FakeResp(500, {"error": {"message": "   "}})),
    ("message 不是字串", FakeResp(500, {"error": {"message": 12345}})),
]

# ── D. error.message 超過 200 字元 ────────────────────────────────
LONG_MSG = FakeResp(429, {
    "error": {"message": "Rate limit reached for gpt-4o-mini. " + ("詳細說明。" * 80)}
})

# ── E. 正常的錯誤訊息要原樣保留（不可以過度遮蔽）────────────────────
CLEAN_MSG = FakeResp(429, {
    "error": {"message": "Rate limit reached for gpt-4o-mini in organization on tokens per min."}
})


def main():
    print("=" * 74)
    print("A. OpenAI 401 真實格式：error.message 內含遮蔽金鑰")
    print("=" * 74)
    out = _safe_api_error(OPENAI_401)
    hits = leaks(out)
    check("輸出不含任何金鑰形態", not hits, f"命中 {hits}" if hits else out[:66])
    check("仍看得出是金鑰問題（保留 Incorrect API key provided）",
          "Incorrect API key provided" in out, out[:66])
    check("遮蔽標記有出現", "[已遮蔽]" in out, out[:66])
    print(f"     實際輸出：{out}")

    print()
    print("=" * 74)
    print("B. 回應不是 JSON")
    print("=" * 74)
    out = _safe_api_error(NOT_JSON)
    check("回固定字串，不 fallback 回印 body", out == "(回應非 JSON，內容不記錄)", out)
    check("輸出不含原始 body 片段", "502 Bad Gateway" not in out, out)

    print()
    print("=" * 74)
    print("C. 是 JSON 但沒有可用的 error.message")
    print("=" * 74)
    for label, resp in NO_MESSAGE:
        out = _safe_api_error(resp)
        check(f"[{label}] 回固定字串",
              out == "(回應無 error.message，內容不記錄)", out)

    print()
    print("=" * 74)
    print("D. error.message 超過 200 字元要截斷")
    print("=" * 74)
    out = _safe_api_error(LONG_MSG)
    check("輸出長度 <= 200", len(out) <= 200, f"實際 {len(out)}")
    check("開頭仍是可讀的錯誤原因", out.startswith("Rate limit reached"), out[:50])

    print()
    print("=" * 74)
    print("E. 沒有金鑰的正常訊息不可以被過度遮蔽")
    print("=" * 74)
    out = _safe_api_error(CLEAN_MSG)
    check("原樣保留", out == CLEAN_MSG.json()["error"]["message"], out[:66])

    print()
    print("=" * 74)
    print("F. 所有情境的輸出一律不得含金鑰形態")
    print("=" * 74)
    every = [("401", OPENAI_401), ("非 JSON", NOT_JSON), ("長訊息", LONG_MSG),
             ("正常訊息", CLEAN_MSG)] + NO_MESSAGE
    for label, resp in every:
        out = _safe_api_error(resp)
        hits = leaks(out)
        check(f"[{label}] 無洩漏", not hits, f"命中 {hits}" if hits else "")

    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
