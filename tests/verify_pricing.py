"""
calculate_selling_price() 的定價回歸測試（離線，不需要任何憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 testserify_pricing.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_pricing.py`）

★ 這支存在的理由是「測試自己算期望值等於沒測」：
   tests/verify_auth_and_manual_price.py 的期望值是呼叫
   calculate_selling_price() 產生的，所以定價算錯時它照樣全綠 ——
   它證明的是「兩條路徑一致」，不是「算出來的數字對」。

   **這支的期望值一律寫死。**
   不可以 import PRICING_TIERS、不可以呼叫 calculate_selling_price() 來湊期望值，
   否則就退化成上面那種自己驗自己的測試。改了倍率表要人工來這裡改數字，
   那個「被迫停下來想一下」正是這支的價值。

擋得住的兩種錯（2026-09-01 各真實發生過一次）：
  1. 浮點截斷：int(10000 * (1.22 - 1)) = 2199 而不是 2200，
     因為 1.22 - 1 在浮點數是 0.21999999999999997。¥1–¥200,000 之間
     有 5.4% 的原價會少收 1 圓，而且「少收」不會有客人來反映。
  2. 級距表上限太低：上限寫 999999 時，原價一超過 ¥999,999 就掉出整張表，
     改套 pricing.py 的預設 1.30 —— ¥1,000,000 會從 ¥1,150,000 變成 ¥1,300,000。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 匯率固定住，避免 calculate_selling_price() 去打線上匯率 API（要在 import config 之前設）。
# 只影響 reference_price_twd，不影響售價。
os.environ.setdefault("DEFAULT_JPY_TO_TWD_RATE", "0.2")

from pricing import calculate_selling_price

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── 期望值：全部寫死，不得由程式算出來 ────────────────────────────────
# (原價, 倍率, 服務費, 售價, 這一筆在守什麼)
CASES = [
    (        500, 1.25,       300,       800, "最低服務費 ¥300 生效（¥500×0.25=¥125 不足額）"),
    (      5_000, 1.25,     1_250,     6_250, "第 1 段 1.25 上緣"),
    (      5_001, 1.22,     1_100,     6_101, "第 2 段 1.22 下緣"),
    (     10_000, 1.22,     2_200,    12_200, "★ 浮點截斷：算成 2,199 就是錯的"),
    (     30_000, 1.18,     5_400,    35_400, "★ 浮點截斷：算成 5,399 就是錯的"),
    (    999_999, 1.15,   149_999, 1_149_998, "第 5 段內，舊上限 999999 的最後一筆"),
    (  1_000_000, 1.15,   150_000, 1_150_000, "★ 級距上限：掉出表套 1.30 會變 ¥1,300,000"),
    (  5_000_000, 1.15,   750_000, 5_750_000, "★ 級距上限：掉出表套 1.30 會變 ¥6,500,000"),
]


def main():
    print("=" * 74)
    print("A. 售價（期望值寫死）")
    print("=" * 74)
    for original, _rate, _fee, expect_sell, why in CASES:
        got = calculate_selling_price(original)["selling_price_jpy"]
        check(f"¥{original:,} → ¥{expect_sell:,}　（{why}）",
              got == expect_sell, f"實際 ¥{got:,}")

    print()
    print("=" * 74)
    print("B. 服務費與倍率（同一筆算錯時指出是哪一半錯）")
    print("=" * 74)
    for original, expect_rate, expect_fee, _sell, _why in CASES:
        r = calculate_selling_price(original)
        check(f"¥{original:,} 服務費 = ¥{expect_fee:,}",
              r["service_fee_jpy"] == expect_fee, f"實際 ¥{r['service_fee_jpy']:,}")
        check(f"¥{original:,} 倍率 = {expect_rate}",
              r["markup_rate"] == expect_rate, f"實際 {r['markup_rate']}")

    print()
    print("=" * 74)
    print("C. 售價 = 原價 + 服務費（回傳的三個欄位要自洽）")
    print("=" * 74)
    for original, _rate, _fee, _sell, _why in CASES:
        r = calculate_selling_price(original)
        check(f"¥{original:,}：原價 + 服務費 = 售價",
              r["original_price_jpy"] + r["service_fee_jpy"] == r["selling_price_jpy"],
              f"{r['original_price_jpy']:,} + {r['service_fee_jpy']:,} = {r['selling_price_jpy']:,}")

    print()
    print("=" * 74)
    print("D. 台幣參考價（固定匯率 0.2，只是顯示用，不影響售價）")
    print("=" * 74)
    check("¥6,250 → 約 NT$1,250", calculate_selling_price(5_000)["reference_price_twd"] == 1_250,
          f"實際 {calculate_selling_price(5_000)['reference_price_twd']}")

    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
