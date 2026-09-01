"""
掃描整個 repo，找出 Big5 誤讀造成的編碼損毀（離線，不連外，不需要憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_no_mojibake.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_no_mojibake.py`）

★ 這支存在的理由（2026-03-11 真的發生過，2026-09-01 才查出來）：
   commit 39792b9 把 config.py 的中文註解整檔毀掉 —— UTF-8 位元組被當 Big5 讀、
   Big5 沒有對應碼位的變成字面 `?`、再存回 UTF-8。**不可逆**：原文只能靠 git
   歷史撈回（本案從 d146775 撈到），沒進版控的部分就永遠沒了。
   同一個 commit 裡 scrapers/zozotown.py 的中文完好，所以不是 git、不是
   core.autocrlf、也不是任何批次腳本 —— 是編輯器用 CP950 開檔又存回去。
   成因在編輯器設定，程式碼擋不住，只能靠這支在 CI 把結果攔下來。

判準兩條（實測：8/9 真實損毀行命中，全 repo 誤報 0）：
   A 極短行：`?` 緊鄰 CJK，且非 ASCII 字元**全部**在 mojibake 字元表內，且 <= 4 個
             → 抓「# ?舐?」「# ?祈」這種只剩一兩個字的殘骸
   B 一般行：`?` 緊鄰 CJK >= 2 次，且表內率 >= 0.5，且非 ASCII >= 5 個

   mojibake 字元表是算出來的不是手寫的：UTF-8 中日文的位元組空間
   （lead E3–E9、continuation 80–BF）所有 2-byte 組合，逐一用 cp950 解碼。
   含跨字邊界的組合 —— 少了那些就抓不到「舐」「摰」「雿」。

🔴 已知限制：`# 摰`（單一字元、且沒有 `?`）**抓不到**。
   它在字元統計上與正常的「# 成交」「# 版型」完全無法區分。
   所以本測試綠燈的意思是「沒有整句損毀」，不是「保證一個字都沒壞」。
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail else ""))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIR = {".venv", "__pycache__", ".git", "node_modules", "downloaded_files", ".pytest_cache"}
EXT = {".py", ".md", ".ps1", ".json", ".txt", ".csv", ".html", ".liquid",
       ".yml", ".yaml", ".cfg", ".toml", ".js"}
# ★ 本檔自己帶著損毀樣本當負向測資，掃描時要跳過，否則永遠紅燈
SELF = os.path.basename(__file__)


def _build_alphabet():
    """UTF-8 中日文位元組空間的所有 2-byte 組合用 cp950 解出來的字。"""
    alpha = set()
    lead = range(0xE3, 0xEA)
    cont = range(0x80, 0xC0)
    for a in list(lead) + list(cont):
        for b in list(cont) + list(lead):
            try:
                ch = bytes([a, b]).decode("cp950")
            except (UnicodeDecodeError, LookupError):
                continue
            if ch and ord(ch) > 0x2000:
                alpha.add(ch)
    return alpha


ALPHA = _build_alphabet()

# `?` 緊鄰 CJK。★ 用 chr() 組字元類別，不寫跳脫序列 —— 這個檔案本身就是在講
#   編碼問題，原始碼裡不要再出現任何需要二次解讀的東西。
_CJK = "[" + chr(0x3040) + "-" + chr(0x9FFF) + chr(0xFF00) + "-" + chr(0xFF60) + "]"
_QNEXT = re.compile(_CJK + "[?]|[?]" + _CJK)


def line_verdict(line):
    """回傳 (是否判定為損毀, 表內率, ?鄰CJK次數, 非ASCII字數)。"""
    na = [c for c in line if ord(c) > 127]
    if not na:
        return False, 0.0, 0, 0
    ratio = len([c for c in na if c in ALPHA]) / len(na)
    q = len(_QNEXT.findall(line))
    short_hit = q >= 1 and ratio >= 1.0 and len(na) <= 4
    long_hit = q >= 2 and ratio >= 0.5 and len(na) >= 5
    return (short_hit or long_hit), ratio, q, len(na)


def scan_text(text):
    out = []
    for i, line in enumerate(text.split(chr(10)), 1):
        bad, ratio, q, n = line_verdict(line)
        if bad:
            out.append((i, line.rstrip()[:96], round(ratio, 2), q, n))
    return out


def scan_repo():
    damaged, not_utf8, n = {}, [], 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in EXT or fn == SELF:
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT)
            n += 1
            try:
                raw = open(p, "rb").read()
            except OSError:
                continue
            if bytes([0]) in raw[:4096]:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                not_utf8.append((rel, str(e)[:60]))
                continue
            hits = scan_text(text)
            if hits:
                damaged[rel] = hits
    return damaged, not_utf8, n


# ── 負向樣本：39792b9:config.py 的真實內容，一字不改 ────────────────
BROKEN = [
    ("一般行", "GOYOUTATI 隞?頃蝟餌絞 (DAIGO) - 閮剖?瑼?"),
    ("一般行", "# ZOZOTOWN 憭?祈嚗憛恬??嚗?"),
    ("一般行", "# 隞??嚗OZOTOWN ?剁??交雿? IP 蝜? Akamai IP 靽∟亳瑼Ｘ嚗?"),
    ("一般行", "# OpenAI嚗EO 璅?蝧餉陌?剁?"),
    ("一般行", "# 敹怠?嚗?嚗?30 ??嚗?撠?銴??"),
    ("極短行", "# ?舐?"),
    ("極短行", "# ?祈"),
    ("極短行", "# 雿萇?"),
]

# ── 不可以誤報：都是本 repo 真實出現過、且曾經誤報過的內容 ──────────
CLEAN = [
    ("═ 分隔線", "    # " + chr(0x2550) * 60),
    ("─ 分隔線", "# " + chr(0x2500) * 40),
    ("還原後的原註解", "# 代理（ZOZOTOWN 用，日本住宅 IP 繞過 Akamai IP 信譽檢查）"),
    ("還原後的原註解", "# 快取（秒）— 30 分鐘，減少重複爬取"),
    ("句末問號", "# 這個字串在正常內容裡會不會自然出現? 會的話就要加邊界條件"),
    ("regex 量詞緊鄰日文", "desc = re.sub(r'" + chr(0x203B) + "?ご予約期間', '', desc)"),
    ("regex 非捕獲群組", "re.sub(r'(?:また、)?いかなる理由があっても', '', desc)"),
    ("網址參數", "        # " + chr(0x2550) * 2 + " 方法2：?currency=JPY " + chr(0x2550) * 2),
    ("f-string 含問號", 'f"variant ¥{p}(税抜?) → 頁面 ¥{q}(税込), "'),
    ("中日混排", '"上衣": [("上衣", "トップス"), ("T恤", "Tシャツ")],'),
    ("emoji 標題", "# 🔴 已知雷區（都踩過）"),
    ("兩字短註解", "# 成交"),
]


def main():
    print("=" * 74)
    print("A. 負向樣本：真實損毀行要被抓到（含極短行）")
    print("=" * 74)
    for kind, line in BROKEN:
        bad, r, q, n = line_verdict(line)
        check(f"[{kind}] {line[:40]}", bad, f"表內率={r:.2f} ?鄰CJK={q} n={n}")

    print()
    print("=" * 74)
    print("B. 正常內容不可以誤報")
    print("=" * 74)
    for kind, line in CLEAN:
        bad, r, q, n = line_verdict(line)
        check(f"[{kind}] {line[:40]}", not bad, f"表內率={r:.2f} ?鄰CJK={q} n={n}")

    print()
    print("=" * 74)
    print("C. 全 repo 掃描")
    print("=" * 74)
    damaged, not_utf8, n = scan_repo()
    print(f"  掃描 {n} 個檔案（跳過本檔，它帶著損毀樣本）")
    for rel, err in not_utf8:
        print(f"    [非 UTF-8] {rel}  {err}")
    for rel, hits in damaged.items():
        print(f"    [損毀] {rel}")
        for ln, txt, r, q, cnt in hits[:10]:
            print(f"        L{ln}  表內率={r} ?鄰CJK={q} n={cnt}")
            print(f"            {txt}")
    check("所有檔案都是合法 UTF-8", not not_utf8, f"{len(not_utf8)} 個不是")
    check("沒有任何檔案帶 Big5 誤讀特徵", not damaged, f"{len(damaged)} 個檔案受損")

    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
