"""
Amazon brand 抽取的回歸測試（離線，不連外，不需要憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_amazon_brand.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_amazon_brand.py`）

★ 這支存在的理由（2026-09-02 實測 118 件 Amazon 商品，79 件 brand 被污染）：
   舊版是
     el = soup.select_one("#bylineInfo") or soup.select_one(".po-brand .po-break-word")
     b  = re.sub(r'^(ブランド[：:]|Brand[：:]|Visit the |のストアを表示)', '', b)
   兩個問題：
   1. **選擇器優先序反了。** `.po-brand .po-break-word` 是商品規格表的品牌欄，
      本來就乾淨；`#bylineInfo` 是標題底下那行，可能是品牌、賣家店鋪、
      或書籍作者＋格式。優先拿乾淨的那個，不要去洗髒的。
   2. `のストアを表示` 是**後綴**，卻被放進前綴錨定 `^(...)` 的 alternation，
      永遠剝不掉。前後綴一定要拆成兩個 regex。

   Amazon 會依 URL 語系（/-/en/、/-/zh/）回不同語言的 byline，所以同一種東西
   有日／英／簡中三種寫法。下面的 fixture 都是真實頁面上抓到的原始字串。

   brand 錯了會同時進**標題**、**tags** 與 Shopify 的 **vendor** 欄位。

🔴 書籍與音樂一律讓 brand 留空。
   剝掉「形式:単行本」之後剩下的「白浜鴎」仍然不該當品牌；音樂那種
   多人串接（…(アーティスト),…(演奏),…(作曲)）根本不存在可取出的品牌。
   留空 → vendor fallback 成「代購商品」、標題不會有奇怪前綴，
   比填一個錯的好。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bs4 import BeautifulSoup

from scrapers.amazon import _clean_brand, _extract_brand, _BRAND_MAX_LEN

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════
# A. byline 原始字串 → 期望的 brand（全部取自真實頁面）
# ══════════════════════════════════════════════════════════════════
BYLINE_CASES = [
    # ── 賣家店鋪（日文）：舊版的 bug 就是剝不掉這個後綴
    ("日文賣家店鋪", "京セラ(Kyocera)のストアを表示", "京セラ(Kyocera)"),
    ("日文賣家店鋪", "GLIDERのストアを表示", "GLIDER"),
    ("日文賣家店鋪", "タカラトミー(TAKARA TOMY)のストアを表示", "タカラトミー(TAKARA TOMY)"),
    ("日文賣家店鋪", "Amazonベーシック(Amazon Basics)のストアを表示", "Amazonベーシック(Amazon Basics)"),

    # ── 簡中旗艦店：Amazon 對 /-/zh/ 網址回這種
    ("簡中旗艦店", "访问 BURTLE 品牌旗舰店", "BURTLE"),
    ("簡中旗艦店", "访问 コミネ(KOMINE) 品牌旗舰店", "コミネ(KOMINE)"),
    ("簡中旗艦店", "访问 武蔵野ユニフォーム 品牌旗舰店", "武蔵野ユニフォーム"),

    # ── 中文品牌前綴
    ("中文品牌前綴", "品牌：Eye coffret", "Eye coffret"),
    ("中文品牌前綴", "品牌：ワークル", "ワークル"),
    ("中文品牌前綴", "品牌：ノーブランド品", "ノーブランド品"),

    # ── 日文品牌前綴
    ("日文品牌前綴", "ブランド：シマノ", "シマノ"),

    # ── 英文店鋪（舊版本來就處理得了，不可以改壞）
    ("英文店鋪", "Visit the BURTLE Store", "BURTLE"),
    ("英文店鋪", "Visit the Panasonic Store", "Panasonic"),

    # ── 已經乾淨的值要原樣保留
    ("已經乾淨", "Panasonic", "Panasonic"),
    ("已經乾淨", "Dyson(ダイソン)", "Dyson(ダイソン)"),
    ("已經乾淨", "ノーブランド品", "ノーブランド品"),
]

# ══════════════════════════════════════════════════════════════════
# B. 這些不是品牌 → 一律留空
# ══════════════════════════════════════════════════════════════════
NOT_BRAND_CASES = [
    ("書籍作者（日）", "白浜鴎(著)形式:単行本"),
    ("書籍作者（日）", "中村 明日美子(著)形式:コミック"),
    ("書籍作者（簡中）", "白浜鴎(作者)格式：单行本-精装"),
    ("雜誌格式", "Format:Print Magazine"),
    ("音樂演出者（英）", "Patrick Gallois(Artist)Format:Audio CD"),
    ("音樂多人串接（日）",
     "パユ(エマニュエル)&安楽真理子(アーティスト),パユ(エマニュエル)(演奏),"
     "安楽真理子(演奏),ドビュッシー(作曲)&1その他形式:CD"),
    ("音樂多人串接（簡中）",
     "パユ(エマニュエル)&安楽真理子(艺术家),パユ(エマニュエル)(表演者),"
     "安楽真理子(表演者),ドビュッシー(作曲)&1其他格式：CD"),
    ("作曲者單值", "イベール(Composer)"),
    ("指揮者單值", "ガロワ(パトリック)(Conductor)"),
]

# ══════════════════════════════════════════════════════════════════
# C. 選擇器優先序：規格表的品牌欄要贏過 bylineInfo
# ══════════════════════════════════════════════════════════════════
# ★ 這組 fixture 的兩個來源洗完會得到**不同**答案，才驗得到優先序。
#   取自真實頁面：bylineInfo 洗完是 'コミネ(KOMINE)'，po-brand 是 'Komine'。
#   若寫成兩邊洗完同值（例如 '京セラ(Kyocera)のストアを表示' vs '京セラ(Kyocera)'），
#   優先序反轉時測試照樣全綠 —— 那是驗不到東西的假信心，2026-09-02 實際犯過。
HTML_PO_BRAND_WINS = """
<html><body>
  <div id="bylineInfo">访问 コミネ(KOMINE) 品牌旗舰店</div>
  <div class="po-brand"><span class="po-break-word">Komine</span></div>
</body></html>
"""

HTML_ONLY_BYLINE = """
<html><body>
  <div id="bylineInfo">Visit the BURTLE Store</div>
</body></html>
"""

HTML_BOOK = """
<html><body>
  <div id="bylineInfo">白浜鴎(著)形式:単行本</div>
</body></html>
"""

HTML_PO_BRAND_DIRTY = """
<html><body>
  <div class="po-brand"><span class="po-break-word">品牌：umee</span></div>
  <div id="bylineInfo">访问 umee 品牌旗舰店</div>
</body></html>
"""

HTML_NOTHING = "<html><body><p>沒有品牌資訊</p></body></html>"


def brand_of(html):
    return _extract_brand(BeautifulSoup(html, "html.parser"))


def main():
    print("=" * 74)
    print("A. byline 原始字串 → brand（全部取自真實頁面）")
    print("=" * 74)
    for kind, raw, expect in BYLINE_CASES:
        got = _clean_brand(raw)
        check(f"[{kind}] {raw[:40]} -> {expect}", got == expect, f"實際 {got!r}")

    print()
    print("=" * 74)
    print("B. 作者／演出者／格式 —— 不是品牌，一律留空")
    print("=" * 74)
    for kind, raw in NOT_BRAND_CASES:
        got = _clean_brand(raw)
        check(f"[{kind}] {raw[:44]} -> 空字串", got == "", f"實際 {got!r}")

    print()
    print("=" * 74)
    print("C. 選擇器優先序：規格表品牌欄要贏過 bylineInfo")
    print("=" * 74)
    check("有 .po-brand 時用它（不去洗 bylineInfo）",
          brand_of(HTML_PO_BRAND_WINS) == "Komine",
          repr(brand_of(HTML_PO_BRAND_WINS)) + " / 反轉時會是 'コミネ(KOMINE)'")
    check("沒有 .po-brand 才退回 bylineInfo",
          brand_of(HTML_ONLY_BYLINE) == "BURTLE", repr(brand_of(HTML_ONLY_BYLINE)))
    check(".po-brand 本身也要洗（不是無條件信任）",
          brand_of(HTML_PO_BRAND_DIRTY) == "umee", repr(brand_of(HTML_PO_BRAND_DIRTY)))
    check("書籍頁（只有 bylineInfo 且不是品牌）-> 空字串",
          brand_of(HTML_BOOK) == "", repr(brand_of(HTML_BOOK)))
    check("兩個選擇器都沒有 -> 空字串",
          brand_of(HTML_NOTHING) == "", repr(brand_of(HTML_NOTHING)))

    print()
    print("=" * 74)
    print("D. 保險：過長或多值串接 -> 留空")
    print("=" * 74)
    check("超過長度上限 -> 空字串", _clean_brand("A" * (_BRAND_MAX_LEN + 1)) == "",
          f"上限 {_BRAND_MAX_LEN}")
    check("剛好等於上限 -> 保留", _clean_brand("A" * _BRAND_MAX_LEN) != "")
    check("含半形逗號 -> 空字串", _clean_brand("BrandA,BrandB") == "")
    check("含全形逗號 -> 空字串", _clean_brand("BrandA，BrandB") == "")
    check("空輸入 -> 空字串", _clean_brand("") == "")
    check("只有空白 -> 空字串", _clean_brand("   ") == "")
    # ★ 實測 40 個真實頁面，最長的正確品牌是 12 字（KIMURA GLASS），
    #   門檻 40 有 28 字餘裕，保險 0 次誤殺。這條釘住那個餘裕。
    check("真實最長品牌不會被保險殺掉", _clean_brand("KIMURA GLASS") == "KIMURA GLASS")

    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
