"""
generic._find_price_in_html() 的取價回歸測試（離線，不連外，不需要憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 testserify_generic_price.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_generic_price.py`）

★ 這支存在的理由（2026-09-01 的實際事故）：
   舊版是「『N円(税込)』命中就 return min(候選)」。但日本電商頁面上帶「税込」的
   多半**不是商品價** —— 代引手数料、送料、購物袋價、免運門檻；而商品本體常寫成
   SALE5,500円 / ¥5,500 /「税込 8,250 円」/「¥ 756税込」，舊 regex 硬性要求
   「N円…税込」全都進不了候選。於是 min() 等於「在一堆手續費裡挑最小的那個」。

   線上實際發生（三個網域寫出同一個 ¥330，都是代引手数料級距）：
     chikumeido.com      取到 ¥330（代引手数料），真價 ¥5,500
     okinawa-ichiba.net  取到 ¥330（代引手数料），真價 ¥756
     dior.com            取到 ¥330（購物袋價），  真價 ¥5,720
     suqqu.com           取到 ¥550（送料），      真價 ¥8,250
   suqqu 那筆最貴：9 件 SUQQU 彩妝以 ¥850 上架賣出，實際每件要付 ¥8,250。
   **少收不會有客人來反映**，是靠事後比對 metafield 才發現的。

   下面每個 fixture 都是從真實頁面萃取的最小重現，期望值寫死。
   任何人把取價改回「整頁掃税込取 min」都會立刻紅燈。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bs4 import BeautifulSoup

from scraper import Scraper

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail else ""))


_S = Scraper()


def price_of(html):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        v = _S._find_price_in_html(BeautifulSoup(html, "html.parser"))
    return v, buf.getvalue().strip()


# ══════════════════════════════════════════════════════════════════
# A. 四個線上實際踩到的網域（期望值寫死）
# ══════════════════════════════════════════════════════════════════

CHIKUMEIDO = """
<html><body>
  <div class="price">価格</div>
  <p>価格 6,300円 SALE5,500円</p>
  <div class="shop-info">
    <p>【送料】990円（税込）（一配送先当たり）</p>
    <p>お支払い方法 ■代金引換 代引手数料、別途330円(税込)必要です。</p>
  </div>
</body></html>
"""

OKINAWA = """
<html><body>
  <span class="cart_price pc">合計&yen; 0</span>
  <p class="sale_price text-primary">&yen; 756税込</p>
  <span class="price02_default">&yen; 756</span>
  <div class="guide">
    <p>※代引き手数料は、一律：330円（税込）いただきます。</p>
    <p>1回のお買い物の合計金額が 8,800円（税込） 以上の場合は送料無料となります。</p>
  </div>
</body></html>
"""

DIOR = """
<html><body>
  <span class="product-price">&yen;5,720</span>
  <div class="services">
    <p>ショッピングバッグを330円(税込)でご購入いただけます。</p>
    <p>※代金引換をご利用の場合、手数料が別途440円(税込)かかります。</p>
    <p>14,850円(税込)以上ご購入でプレゼント</p>
  </div>
</body></html>
"""

SUQQU = """
<html><body>
  <div class="product-price">税込 8,250 円</div>
  <p>送料全国一律550円（税込）、11,000円（税込）以上で無料</p>
</body></html>
"""

CASES = [
    ("chikumeido：代引手数料 ¥330 / 送料 ¥990 / 真價 SALE5,500円", CHIKUMEIDO, 5500),
    ("okinawa-ichiba：代引手数料 ¥330 / 免運門檻 ¥8,800 / 真價 ¥756", OKINAWA, 756),
    ("dior：購物袋 ¥330 / 代引 ¥440 / 贈品門檻 ¥14,850 / 真價 ¥5,720", DIOR, 5720),
    ("suqqu：送料 ¥550 / 免運門檻 ¥11,000 / 真價 ¥8,250（税込在數字前）", SUQQU, 8250),
]


# ══════════════════════════════════════════════════════════════════
# B. 排除規則的位置敏感度
# ══════════════════════════════════════════════════════════════════
# 「送料無料」常常就印在商品價旁邊。排除關鍵字若不分數字前後，
# 這種正常頁面的真價會被一起殺掉。

SOURYOU_MUKEN = """
<html><body>
  <span class="price">&yen;5,500</span> 送料無料
</body></html>
"""

# 反過來：費用類的字在數字**之前**，必須排除
SOURYOU_FEE = """
<html><body>
  <p>送料は 500円 です。</p>
  <p>商品代金 3,300円</p>
</body></html>
"""


# ══════════════════════════════════════════════════════════════════
# C. 逗號清單不可以被黏成一個假價
# ══════════════════════════════════════════════════════════════════
# 跟 Yahoo 巢狀價格 {990, 890} -> 990890 是同一種病：
# [0-9][0-9,]* 會把「1966,1967,1971」當成單一數字吃掉。

COMMA_LIST = """
<html><body>
  <p>創業年表：1966,1967,1971,1972 の記録</p>
  <span class="price">&yen;4,400</span>
</body></html>
"""


def main():
    print("=" * 74)
    print("A. 線上實際踩到的四個網域（期望值寫死）")
    print("=" * 74)
    for name, html, expect in CASES:
        got, log = price_of(html)
        check(f"{name} -> {expect}", got == expect, f"實際 {got}｜{log[:110]}")

    print()
    print("=" * 74)
    print("B. 排除關鍵字要看位置（前綴才是費用，後綴的『送料無料』不算）")
    print("=" * 74)
    got, log = price_of(SOURYOU_MUKEN)
    check("商品價旁邊寫『送料無料』不可以把真價殺掉 -> 5500", got == 5500, f"實際 {got}")
    got, log = price_of(SOURYOU_FEE)
    check("『送料は 500円』要被排除，取商品代金 3300", got == 3300, f"實際 {got}｜{log[:110]}")

    print()
    print("=" * 74)
    print("C. 逗號清單不可以黏成假價")
    print("=" * 74)
    got, log = price_of(COMMA_LIST)
    check("『1966,1967,1971,1972』不可以變成一個數字 -> 取 4400", got == 4400, f"實際 {got}")
    check("結果不可以是黏起來的巨數", got is None or got < 1_000_000, f"實際 {got}")

    print()
    print("=" * 74)
    print("D. 取不到可信價時要回 None，不可以猜")
    print("=" * 74)
    got, log = price_of("<html><body><p>この商品は現在お取り扱いしておりません。</p></body></html>")
    check("完全沒有金額的頁面 -> None", got is None, f"實際 {got}")
    got, log = price_of("<html><body><p>代引手数料 330円(税込)</p></body></html>")
    check("整頁只有手續費 -> None（不可以拿手續費當商品價）", got is None, f"實際 {got}｜{log[:110]}")

    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
