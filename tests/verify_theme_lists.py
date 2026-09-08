"""
theme 的兩張封鎖清單（離線，只讀 theme/ 底下的檔案）
====================================================

驗的是 sections/daigo.liquid 裡的兩張表 —— 它們**不是同一回事**：

  BLOCKED_SITES  14 條 regex。包住 window.daikoSearch，命中就 alert 並 return。
                 **這是真閘門。**
  DENY           14 個網域字串。只給 pickUrl() 用，決定 ?url= 要不要自動帶入。
                 **不是閘門** —— 缺一筆的後果是「自動填了，按查詢才被 alert 罵」。

以及 assets/daigo.js 的 daikoManualOrder：soft 品類回的是
blocked=false + success=false，唯一會走到的就是 `if (!data.success)`。
那一行 2026-09-08 之前是 alert()，而 **alert 顯示不了 handoff_url**。

★ 這支用 Python 的 re 跑那些 regex。可行的前提是這幾條只用到
  `(?:...)`、`[^...]`、`(?!...)` 這種兩邊語意一致的構造，所以下面有一條
  斷言把「出現 JS 專屬語法」擋掉（尤其 lookbehind `(?<`）——
  真的出現了就代表不能再用 Python 驗，要改跑 tests/verify_theme_lists.js。
  node 裝得到的話會**另外**跑那支（JS 語意才是線上實際行為）。
"""
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEME = os.path.join(ROOT, "theme")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name + (f"  —— {detail}" if detail and not cond else ""))


def read(rel):
    with open(os.path.join(THEME, rel), encoding="utf-8", newline="") as f:
        return f.read()


js = read(os.path.join("assets", "daigo.js"))
liq = read(os.path.join("sections", "daigo.liquid"))


# ── A. daikoManualOrder ────────────────────────────────────────────
print("\n【A】daikoManualOrder 的 !success 分支")

# 🔴 一定要先切出函式本體。daikoCreateOrder 裡有一模一樣的三行，
#    整檔搜尋會命中它 —— 2026-09-08 缺陷注入就是這樣抓到斷言本身是假的：
#    把 manual 這邊的 backToInput() 拿掉，整檔搜尋照樣全綠。
i0 = js.find("async function daikoManualOrder")
i1 = js.find("async function daikoCreateOrder")
check("切得出 daikoManualOrder 的本體", 0 < i0 < i1, f"{i0}/{i1}")
manual = js[i0:i1]

check("!success 不再用 alert",
      re.search(r"if \(!data\.success\) \{ alert\(", manual) is None)
check("!success 改用 showError 並帶 handoff_url",
      re.search(r"if \(!data\.success\) \{[\s\S]{0,120}?"
                r"showError\(data\.error \|\| '建立商品失敗', data\.handoff_url\);",
                manual) is not None)
# backToInput 不是可有可無：#daiko-error 在 step-input 裡，手動步驟時
# step-input 是隱藏的 —— 少了它訊息會畫在看不見的容器裡（檔案自己的註解寫過）。
check("showError 之前有 backToInput（否則訊息畫在隱藏的容器裡）",
      re.search(r"if \(!data\.success\) \{\s*backToInput\(\);", manual) is not None)
check("blocked 分支沒有被動到",
      js.count("showError(data.error || '這個連結目前不開放代購。', data.handoff_url)") == 2,
      str(js.count("showError(data.error || '這個連結目前不開放代購。', data.handoff_url)")))
check("handoff_url 的呼叫點從 3 個變成 4 個",
      js.count(", data.handoff_url)") == 4, str(js.count(", data.handoff_url)")))
check("純本機驗證仍然用 alert（那些沒有後端訊息可顯示）",
      "alert('請填寫商品名稱')" in manual and "alert('請填寫正確的日幣價格')" in manual)


# ── B. 兩張清單 ────────────────────────────────────────────────────
print("\n【B】BLOCKED_SITES（閘門）與 DENY（自動帶入）")

pat_src = re.findall(r"pattern:\s*/((?:\\.|\[[^\]]*\]|[^/\\])+)/i", liq)
check("BLOCKED_SITES 有 14 條", len(pat_src) == 14, str(len(pat_src)))

# 🔴 這條在守「Python 驗得準嗎」這件事本身
for p in pat_src:
    check(f"regex 不含 JS 專屬語法：{p[:34]}",
          "(?<" not in p and "\\p{" not in p and "\\k<" not in p, p)

PATS = [re.compile(p, re.I) for p in pat_src]

deny_raw = re.search(r"var DENY = \[(.*?)\];", liq, re.S).group(1)
DENY = re.findall(r"'([^']+)'", deny_raw)
check("DENY 有 14 個（原本 11 個）", len(DENY) == 14, str(len(DENY)))
for d in ("jpgoodbuy.com", "buyma.com", "buyee.jp"):
    check(f"DENY 補上了 {d}", d in DENY)

# 後端那張表是唯一真相，兩邊要對得起來
from scrapers.base import BLOCKED_DOMAINS
check("DENY 與後端 BLOCKED_DOMAINS 完全一致",
      sorted(DENY) == sorted(BLOCKED_DOMAINS), f"前端多/少：{set(DENY) ^ set(BLOCKED_DOMAINS)}")


def gated(url):
    return any(p.search(url) for p in PATS)


def denied(url):
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or "").lower()
    h = h[4:] if h.startswith("www.") else h
    return any(h == d or h.endswith("." + d) for d in DENY)


# ── C. 閘門：該擋的仍然要擋 ────────────────────────────────────────
print("\n【C】對照組：仍然要擋")
MUST_BLOCK = [
    "https://www.hoka.com/en/us/x", "https://hoka.com/x", "https://shop.hoka.com/x",
    "https://www.buyee.jp/item/1", "https://buyee.jp/item/1",
    "https://www.buyma.com/item/1", "https://buyma.com/item/1",
    "https://www.amazon.com/dp/B0X", "https://amazon.com/dp/B0X",
    "https://tw.mercari.com/item/1", "https://jpgoodbuy.com/x",
    "https://www.zenmarket.jp/x", "https://dokodemo.world/x",
    "https://duty-free-japan.jp/x", "https://bibian.co.jp/x",
    "https://tokukai.com/x", "https://letao.com.tw/x",
    "https://daigobang.com/x", "https://go1buy1.com/x",
]
for u in MUST_BLOCK:
    check(f"閘門仍然擋 {u}", gated(u))
    check(f"DENY 也不自動帶入 {u}", denied(u))

# ── D. 子字串誤擋要消失 ────────────────────────────────────────────
print("\n【D】誤擋要消失（左右兩種）")
MUST_PASS = [
    ("https://hoka.com.tw/x", "HOKA 台灣官網（右邊）"),
    ("https://www.hoka.com.au/x", "HOKA 澳洲官網（右邊）"),
    ("https://xbuyee.jp/x", "🔴 左邊 —— 只加右邊守衛完全擋不掉"),
    ("https://mybuyee.jp/x", "🔴 左邊"),
    ("https://notbuyma.com/x", "🔴 左邊"),
    ("https://buyma.com.tw/x", "右邊"),
    ("https://notamazon.com/x", "🔴 左邊（amazon 原本只有右邊守衛）"),
    ("https://amazon.com.tw/dp/1", "右邊（amazon 原本就守得住）"),
    ("https://www.amazon.co.jp/dp/1", "🔴 Amazon JP 絕不可擋"),
    ("https://jp.mercari.com/item/m1", "Mercari JP"),
    ("https://item.rakuten.co.jp/x/1", "樂天"),
    ("https://zozo.jp/shop/x/goods/1", "ZOZO"),
    ("https://www.dot-st.com/x", "dot-st"),
]
for u, why in MUST_PASS:
    check(f"閘門放行 {u}（{why}）", not gated(u))
    check(f"DENY 允許自動帶入 {u}", not denied(u))


# ── E. node 在的話，用 JS 語意再跑一次（線上實際行為）──────────────
print("\n【E】node（JS 語意，權威）")
node = shutil.which("node")
if not node:
    print("⚠️  找不到 node，跳過 —— 上面是 Python re 的結果，"
          "與 JS 的差異由【B】那組斷言擋住")
else:
    p = subprocess.run([node, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "verify_theme_lists.js")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [l for l in (p.stdout or "").splitlines() if l.strip()][-1:]
    check("tests/verify_theme_lists.js 全綠", p.returncode == 0,
          (p.stdout or "")[-400:] + (p.stderr or "")[-200:])
    print("   " + (tail[0] if tail else ""))


print("\n" + "=" * 62)
print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
if FAIL:
    for n in FAIL:
        print(f"  ❌ {n}")
    sys.exit(1)
print("全部通過")
