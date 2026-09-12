"""
JSON-LD ProductGroup → variants 與主商品價（2026-09-12）

背景：dior.com/ja_jp/beauty 的商品頁把 30 個色號放在 schema.org ProductGroup 的
hasVariant 裡（各自 color/size、offers.price、availability、image、sku），
SSR 寫死在 HTML。generic._extract_json_ld 原本看到 @type != Product 就 continue，
所以 variants=0；價格退回 DOM 規則對整頁候選 [4840, 4950, 5060, 5940, 6270] 取 min，
碰巧對（本品是頁面上最便宜的）。ミス ディオール（¥12,430）旁邊擺著 ¥6,270 的口紅
就會取錯 —— 而且方向是低估。

fixture 全部依 2026-09-12 從 dior.com 原始 HTML 抓下來的真實資料重建
（sku／color／size／price／availability 逐筆對得上），不是憑空造的。

🔴 主商品價規則（釘死）：
  1. 客人貼的網址 == 某變體 offers.url → 那個變體（即使缺貨）
  2. 否則第一個有貨
  3. 全缺貨 → 文件順序第一個（**不是最低**），整件 in_stock=False
缺價變體排除、不繼承主價；全部不能用 → 一個欄位都不動。

不連外。
"""
import os
import sys
import json
import tempfile
import shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="pg_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

from bs4 import BeautifulSoup
import scrape_monitor as sm
from scrapers.base import ProductInfo
from scrapers.generic import GenericMixin

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# ═══════════════════════════════════════════════════════════════════
# fixture：照 dior.com 的 ProductGroup 形狀重建
# ═══════════════════════════════════════════════════════════════════
BASE = "https://www.dior.com/ja_jp/beauty/products"


def variant(sku, price, avail="InStock", color=None, size=None, name=None, slug="X"):
    node = {
        "@type": "Product",
        "name": name,
        "image": f"IMG/{sku}_E01_ZHC.jpg?sw=1800",
        "offers": {
            "@type": "Offer",
            "availability": f"https://schema.org/{avail}" if avail else None,
            "price": price,
            "priceCurrency": "JPY",
            "url": f"{BASE}/{slug}-{sku}.html",
            "itemCondition": "https://schema.org/NewCondition",
        },
        "description": "d",
        "sku": sku,
    }
    if color is not None:
        node["color"] = color
    if size is not None:
        node["size"] = size
    if avail is None:
        del node["offers"]["availability"]
    if price is None:
        del node["offers"]["price"]
    return node


def group(name, gid, variants, varies="color", slug="X"):
    g = {
        "@context": "https://schema.org/", "@type": "ProductGroup",
        "name": name, "productGroupID": gid, "@id": f"{BASE}/{slug}-{gid}.html",
        "brand": {"@type": "Brand", "name": "DIOR"}, "description": f"{name} の説明",
        "hasVariant": [dict(v, name=v["name"] or name) for v in variants],
        "url": f"{BASE}/{slug}-{gid}.html",
        "variesBy": [f"https://schema.org/{varies}"],
    }
    return g


def html_with(ld, og_title="OG タイトル - メイクアップ", extra=""):
    return (f'<html><head><title>T</title>'
            f'<meta property="og:title" content="{og_title}">'
            f'<meta property="og:image" content="https://x/og.jpg">'
            f'<script type="application/ld+json">\n{ld}\n</script>{extra}</head>'
            f'<body>¥ 4,840 ¥ 6,270</body></html>')


# 2026-09-12 實抓：アディクト リップ マキシマイザー Y0319000，30 色，053 缺貨
LIPMAX_ROWS = [
    ("E000001940", "064 ラッキー マホガニー (展開店舗限定色)", "InStock"),
    ("C031900018", "018 インテンス スパイス", "InStock"),
    ("C031900019", "019 シマー ピーチ", "InStock"),
    ("E000001668", "054 オーロラ (サマー コレクション 2026 限定品)", "InStock"),
    ("E000001667", "053 ゴールデン アワー (サマー コレクション 2026 限定品)", "OutOfStock"),
    ("C031900010", "10", "InStock"), ("C031900012", "12", "InStock"),
    ("C031900014", "ロージー(展開店舗限定色)", "InStock"),
    ("E000001576", "252 シュガー クラッシュ (展開店舗限定色)", "InStock"),
    ("C031900007", "7", "InStock"), ("C031900006", "6", "InStock"),
    ("C031900009", "009ホワイト", "InStock"), ("C031900001", "1", "InStock"),
    ("C031900003", "3", "InStock"), ("C031900005", "5", "InStock"),
    ("C031900004", "4", "InStock"), ("C031900040", "040 インテンス ブルーベリー", "InStock"),
    ("E000000671", "212 チュチュ", "InStock"), ("C031900039", "039 インテンス シナモン", "InStock"),
    ("E000000677", "083 スパークリング ローズ (展開店舗限定色) ", "InStock"),
    ("C031900038", "038 ローズ ヌード", "InStock"),
    ("E000001673", "046 サンビーム (サマー コレクション 2026 限定品)", "InStock"),
    ("C031900029", "029 インテンス グレープ", "InStock"),
    ("C031900028", "028 インテンス ディオール 8", "InStock"),
    ("C031900020", "020 マホガニー", "InStock"), ("C031900023", "023 シマー フューシャ", "InStock"),
    ("C031900022", "022 インテンス レッド", "InStock"), ("C031900024", "024 インテンス ブリック", "InStock"),
    ("C031900027", "027 インテンス フィグ", "InStock"), ("C031900026", "026 インテンス モーヴ", "InStock"),
]
LIPMAX = group("ディオール アディクト リップ マキシマイザー", "Y0319000",
               [variant(s, 4840, a, color=c) for s, c, a in LIPMAX_ROWS])

# 2026-09-12 實抓：ミス ディオール オードゥ パルファン Y0000393，size 型，價格不同
MISSDIOR = group("ミス ディオール オードゥ パルファン", "Y0000393",
                 [variant("E000001557", 12430, "InStock", size="30 mL"),
                  variant("E000001555", 18040, "InStock", size="50 mL")], varies="size")

# 2026-09-12 實抓：フォーエヴァー フルイド スキン グロウ Y0000149，11 色全部缺貨
FOREVER_ROWS = ["0W ウォーム", "1.5N ニュートラル", "1CR クール ロージー", "1N ニュートラル",
                "1W ウォーム", "0CR クール ロージー", "2N ニュートラル", "0N ニュートラル",
                "0.5N ニュートラル", "00 ニュートラル", "3N ニュートラル"]
FOREVER = group("ディオールスキン フォーエヴァー フルイド スキン グロウ", "Y0000149",
                [variant(f"E00000{1360 + i}", 8030, "OutOfStock", color=c)
                 for i, c in enumerate(FOREVER_ROWS)])


def run(ld_obj_or_str, url=f"{BASE}/X-Y0319000.html", og_title="OG タイトル - メイクアップ",
        extra="", with_ctx=True):
    ld = ld_obj_or_str if isinstance(ld_obj_or_str, str) else json.dumps(ld_obj_or_str, ensure_ascii=False)
    if with_ctx:
        sm.start(url)
    eng = GenericMixin()
    product = ProductInfo(source_url=url)
    soup = BeautifulSoup(html_with(ld, og_title, extra), "html.parser")
    eng._extract_json_ld(soup, product)
    eng._extract_og_tags(soup, product)
    errs = " | ".join((sm._ctx.get() or {}).get("errors") or []) if with_ctx else ""
    return product, errs


# ═══════════════════════════════════════════════════════════════════
def test_color_group():
    print()
    print("【1】color 型：リップ マキシマイザー 30 色（053 缺貨）")
    p, errs = run(LIPMAX)
    check("★ variants = 30", len(p.variants) == 30, str(len(p.variants)))
    check("有貨 29／缺貨 1", sum(v["in_stock"] for v in p.variants) == 29)
    oos = [v for v in p.variants if not v["in_stock"]]
    check("缺貨的是 053 ゴールデン アワー（E000001667）",
          len(oos) == 1 and oos[0]["sku"] == "E000001667", str(oos[:1]))
    check("★ 主價 ¥4,840，取第一個有貨（064＝hasVariant[0]）", p.price_jpy == 4840, str(p.price_jpy))
    check("★ 標題取 ProductGroup.name，不是 og:title 那串「新作/限定…- メイクアップ」",
          p.title == "ディオール アディクト リップ マキシマイザー", p.title)
    check("每個變體都有自己的 price", all(v["price"] == 4840 for v in p.variants))
    check("每個變體都有自己的圖", len({v["image"] for v in p.variants}) == 30)
    check("color 欄位是色號名", p.variants[1]["color"] == "018 インテンス スパイス", p.variants[1]["color"])
    check("size 欄位空", all(v["size"] == "" for v in p.variants))
    check("整件 in_stock=True（有任一變體有貨）", p.in_stock is True)
    check("品牌 DIOR", p.brand == "DIOR", p.brand)
    check("圖片取主價那個變體的圖", "E000001940" in p.image_url, p.image_url)
    check("★ warnings 留下證據（部署後看線上紀錄用）",
          "ProductGroup: 30 變體" in errs and "取第一個有貨" in errs, errs[:120])


def test_size_group_prices_differ():
    print()
    print("【2】size 型：ミス ディオール 30mL ¥12,430 / 50mL ¥18,040 —— 各自的價")
    p, errs = run(MISSDIOR, url=f"{BASE}/X-Y0000393.html")
    check("variants = 2", len(p.variants) == 2)
    check("★ 30mL 12430 / 50mL 18040，各自的價不是繼承的",
          [(v["size"], v["price"]) for v in p.variants] == [("30 mL", 12430), ("50 mL", 18040)],
          str([(v["size"], v["price"]) for v in p.variants]))
    check("★ 主價 ¥12,430（第一個有貨 = 頁面預設的 30mL）", p.price_jpy == 12430, str(p.price_jpy))
    check("color 欄位空、size 有值", p.variants[0]["color"] == "" and p.variants[0]["size"] == "30 mL")


def test_url_selects_variant():
    print()
    print("【3】★ 規則 1：客人貼的是變體網址 → 取那個變體的價")
    p, _ = run(MISSDIOR, url=f"{BASE}/X-E000001555.html")
    check("★ 貼 50mL 的網址 → 主價 ¥18,040", p.price_jpy == 18040, str(p.price_jpy))
    # 網址帶 query／fragment／percent-encoding 也要對得上
    p, _ = run(MISSDIOR, url=f"{BASE}/X-E000001555.html?utm=x#top")
    check("網址帶 query/fragment 仍對得上", p.price_jpy == 18040, str(p.price_jpy))
    # 指到缺貨變體：取它的價，整件 in_stock 仍看全部
    p, _ = run(LIPMAX, url=f"{BASE}/X-E000001667.html")
    check("★ 貼缺貨變體的網址 → 仍取它的價（¥4,840），不跳到別的", p.price_jpy == 4840)
    check("整件 in_stock 仍為 True（其他色有貨）", p.in_stock is True)
    # 母頁網址（不等於任何變體 url）→ 規則 2
    p, _ = run(MISSDIOR, url=f"{BASE}/X-Y0000393.html")
    check("母頁網址 → 規則 2（第一個有貨）", p.price_jpy == 12430)


def test_all_out_of_stock():
    print()
    print("【4】★ 規則 3：全缺貨 → 取文件順序第一個，不取最低；不可以變成零變體")
    p, errs = run(FOREVER, url=f"{BASE}/X-Y0000149.html")
    check("★ 11 個變體全部保留（不可以退成單品）", len(p.variants) == 11, str(len(p.variants)))
    check("★ 整件 in_stock=False", p.in_stock is False)
    check("主價 ¥8,030", p.price_jpy == 8030)
    check("warnings 寫明「全缺貨→第一個」", "全缺貨→第一個" in errs, errs[:120])

    # 價格不同的 size 型全缺貨：第一個是貴的那個 → 取第一個（18040），不是最低（12430）
    g = group("X", "G1", [variant("A", 18040, "OutOfStock", size="50 mL"),
                          variant("B", 12430, "OutOfStock", size="30 mL")], varies="size")
    p, _ = run(g, url=f"{BASE}/X-G1.html")
    check("★ size 型全缺貨、第一個較貴 → 取第一個 ¥18,040（不是最低 ¥12,430）",
          p.price_jpy == 18040, str(p.price_jpy))
    check("兩個變體各自的價保留", [v["price"] for v in p.variants] == [18040, 12430])


def test_empty_has_variant():
    print()
    print("【5】ProductGroup 存在但 hasVariant 是空陣列 → 一個欄位都不動，退回 og")
    g = group("空的", "G2", [])
    p, errs = run(g, url=f"{BASE}/X-G2.html")
    check("★ variants 空", p.variants == [])
    check("★ price 不動（None）", p.price_jpy is None, str(p.price_jpy))
    check("★ 標題退回 og:title（ProductGroup.name 不拿來用）", p.title == "OG タイトル - メイクアップ", p.title)
    check("warnings 說明「hasVariant 為空」", "hasVariant 為空" in errs, errs[:100])
    # hasVariant 不是 list（壞資料）也一樣
    g["hasVariant"] = "oops"
    p, _ = run(g, url=f"{BASE}/X-G2.html")
    check("hasVariant 不是 list 也不炸、不動", p.variants == [] and p.price_jpy is None)


def test_variant_missing_price():
    print()
    print("【6】★ 某一筆缺 offers.price → 排除那筆，不繼承主價")
    g = group("缺價", "G3", [variant("A", None, "InStock", color="赤"),      # 第一個有貨但缺價
                            variant("B", 5000, "InStock", color="青"),
                            variant("C", 5500, "OutOfStock", color="緑")])
    p, errs = run(g, url=f"{BASE}/X-G3.html")
    check("★ 缺價的 A 被排除，剩 2 個", [v["sku"] for v in p.variants] == ["B", "C"],
          str([v["sku"] for v in p.variants]))
    check("★ 沒有任何變體的 price 是 None／0（不繼承）",
          all(v["price"] and v["price"] > 0 for v in p.variants))
    check("★ 主價取下一個有貨的 B ¥5,000（不是 A）", p.price_jpy == 5000, str(p.price_jpy))
    check("warnings 記下「缺價排除 1」", "缺價排除 1" in errs, errs[:120])
    # 價格超出合理範圍也算缺價（price_in_range 是唯一的數值出處）
    g = group("超界", "G4", [variant("A", 5, "InStock", color="赤"),
                            variant("B", 5000, "InStock", color="青")])
    p, _ = run(g, url=f"{BASE}/X-G4.html")
    check("¥5 超出下限 → 當缺價排除", [v["sku"] for v in p.variants] == ["B"])
    # 全部缺價 → 退回一般解析
    g = group("全缺價", "G5", [variant("A", None, "InStock", color="赤")])
    p, errs = run(g, url=f"{BASE}/X-G5.html")
    check("★ 全部缺價 → variants 空、price None、標題退回 og",
          p.variants == [] and p.price_jpy is None and p.title.startswith("OG"), f"{p.variants} {p.price_jpy} {p.title}")
    check("warnings 說明「全部不能用」", "全部不能用" in errs, errs[:120])


def test_price_formats_and_availability():
    print()
    print("【7】價格格式與 availability 口徑")
    g = group("格式", "G6", [variant("A", "4840.00", "InStock", color="赤"),
                            variant("B", "5,060", "InStock", color="青"),
                            variant("C", 6270, None, color="緑"),            # 沒寫 availability
                            variant("D", 7000, "PreOrder", color="黄")])     # 非 InStock
    p, _ = run(g, url=f"{BASE}/X-G6.html")
    check("★ \"4840.00\" → 4840（不是 484000）", p.variants[0]["price"] == 4840, str(p.variants[0]["price"]))
    check("\"5,060\" → 5060", p.variants[1]["price"] == 5060)
    check("沒寫 availability → 視為有貨（對齊 jsonld.py）", p.variants[2]["in_stock"] is True)
    check("PreOrder → 視為缺貨（對齊 jsonld.py：非 InStock 即缺貨）", p.variants[3]["in_stock"] is False)


def test_variant_without_option():
    print()
    print("【8】沒有 color／size 的變體：name 與群組名不同就拿 name，否則排除")
    g = group("本体", "G7", [variant("A", 3000, "InStock", name="本体 詰め替え"),
                            variant("B", 3500, "InStock", name="本体")])       # name == 群組名
    p, errs = run(g, url=f"{BASE}/X-G7.html")
    check("A 的 name 當 color", p.variants and p.variants[0]["color"] == "本体 詰め替え", str(p.variants[:1]))
    check("★ B 沒有可用選項 → 排除", len(p.variants) == 1)
    check("warnings 記下「缺選項排除 1」", "缺選項排除 1" in errs, errs[:120])


def test_broken_json_and_plain_product():
    print()
    print("【9】壞掉的 ProductGroup 不炸；既有 Product 型行為不變")
    broken = '{"@type":"ProductGroup","name":"壞", "hasVariant":[{"@type":"Product",]'
    p, errs = run(broken, url=f"{BASE}/X-G8.html")
    check("★ JSON 壞掉 → 不炸，退回 og", p.title.startswith("OG") and p.variants == [])
    check("★ 留痕「含 ProductGroup 但解析失敗」", "ProductGroup 但解析失敗" in errs, errs[:120])

    plain = {"@context": "http://schema.org/", "@type": "Product", "sku": "S2610ODSL_M69E",
             "name": "【日本限定】Lady Dior Cosmos ジップ カードホルダー",
             "brand": {"@type": "Thing", "name": "Dior"}, "image": "https://x/a.jpg",
             "offers": [{"@type": "Offer", "availability": "http://schema.org/InStock",
                         "price": "119000.00", "priceCurrency": "JPY"}]}
    p, _ = run(plain, url="https://www.dior.com/ja_jp/fashion/products/S2610ODSL_M69E")
    check("Product 型：標題／價格照舊（¥119,000）",
          p.title.startswith("【日本限定】") and p.price_jpy == 119000, f"{p.title[:20]} {p.price_jpy}")
    check("Product 型：variants 仍為空", p.variants == [])

    # ProductGroup 放在 list／@graph 裡也找得到
    p, _ = run([{"@type": "BreadcrumbList"}, LIPMAX], url=f"{BASE}/X-Y0319000.html")
    check("ProductGroup 在 list 裡也找得到", len(p.variants) == 30)
    p, _ = run({"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, MISSDIOR]},
               url=f"{BASE}/X-Y0000393.html")
    check("ProductGroup 在 @graph 裡也找得到", len(p.variants) == 2)


def test_axis_declared():
    print()
    print("【11】★ variesBy → axis_declared 標記：站方宣告的軸集合 == 實際填值的欄位集合才信")
    # color 單軸（Dior 現況）
    p, errs = run(LIPMAX)
    check("★ variesBy=[color]、變體都填 color → 每個變體 axis_declared=True",
          len(p.variants) == 30 and all(v.get("axis_declared") is True for v in p.variants))
    check("warnings 寫明軸", "軸=color（宣告）" in errs, errs[:140])
    # size 單軸
    p, _ = run(MISSDIOR, url=f"{BASE}/X-Y0000393.html")
    check("variesBy=[size]、變體都填 size → 標記", all(v.get("axis_declared") for v in p.variants))
    # 雙軸：每個變體 color+size 都有
    both = group("雙軸", "G9", [variant("A", 3000, "InStock", color="赤", size="S"),
                               variant("B", 3000, "InStock", color="赤", size="M")], varies="color")
    both["variesBy"] = ["https://schema.org/color", "https://schema.org/size"]
    p, errs = run(both, url=f"{BASE}/X-G9.html")
    check("★ variesBy=[color,size]、變體兩欄都有 → 標記", all(v.get("axis_declared") for v in p.variants))
    check("warnings 寫「軸=color+size（宣告）」", "軸=color+size（宣告）" in errs, errs[:140])
    # 宣告一軸、實際兩欄都有值 → 不符 → 不標
    half = group("半套", "G10", [variant("A", 3000, "InStock", color="赤", size="S")], varies="color")
    p, errs = run(half, url=f"{BASE}/X-G10.html")
    check("★ variesBy 只宣告 color、變體卻 color+size 都有 → 不標（退回 regex）",
          p.variants and not any(v.get("axis_declared") for v in p.variants), str(p.variants[:1]))
    check("warnings 說明不符", "宣告與實際不符" in errs, errs[:160])
    # 宣告兩軸、實際只填一欄 → 不符 → 不標
    half2 = group("半套2", "G11", [variant("A", 3000, "InStock", color="赤")], varies="color")
    half2["variesBy"] = ["https://schema.org/color", "https://schema.org/size"]
    p, _ = run(half2, url=f"{BASE}/X-G11.html")
    check("variesBy 宣告 color+size、變體只有 color → 不標", not any(v.get("axis_declared") for v in p.variants))
    # 沒有 variesBy → 不標
    nov = group("無宣告", "G12", [variant("A", 3000, "InStock", color="赤")])
    del nov["variesBy"]
    p, errs = run(nov, url=f"{BASE}/X-G12.html")
    check("★ 沒有 variesBy → 不標，走 regex", not any(v.get("axis_declared") for v in p.variants))
    check("warnings 寫「軸=color（未宣告）」", "軸=color（未宣告）" in errs, errs[:140])
    # variesBy 含 color/size 以外的軸 → 不標 + 留痕
    mat = group("材質", "G13", [variant("A", 3000, "InStock", color="赤")], varies="color")
    mat["variesBy"] = ["https://schema.org/color", "https://schema.org/material"]
    p, errs = run(mat, url=f"{BASE}/X-G13.html")
    check("★ variesBy 含 material → 不標（我們沒讀那個軸，不能說宣告對得上）",
          not any(v.get("axis_declared") for v in p.variants))
    check("warnings 記「未支援軸 material」", "未支援軸 material" in errs, errs[:160])
    # variesBy 寫法容錯：不帶網域、大小寫
    loose = group("寫法", "G14", [variant("A", 3000, "InStock", color="赤")], varies="color")
    loose["variesBy"] = ["Color"]
    p, _ = run(loose, url=f"{BASE}/X-G14.html")
    check("variesBy=['Color']（不帶網域、大寫）也認得", all(v.get("axis_declared") for v in p.variants))
    loose["variesBy"] = "https://schema.org/color"          # 單一字串不是 list
    p, _ = run(loose, url=f"{BASE}/X-G14.html")
    check("variesBy 是單一字串也認得", all(v.get("axis_declared") for v in p.variants))
    # name 當 color 的變體（沒有 color/size 欄）→ 不是站方宣告的欄位 → 不標
    nm = group("本体", "G15", [variant("A", 3000, "InStock", name="本体 詰め替え")], varies="color")
    p, _ = run(nm, url=f"{BASE}/X-G15.html")
    check("★ 值是從 name 借來的（變體沒有 color 欄）→ 不標", not any(v.get("axis_declared") for v in p.variants))


def test_no_ctx_failsafe():
    print()
    print("【10】沒有監控 ctx 時解析照常（note_error 是 fail-safe）")
    sm._ctx.set(None)
    p, _ = run(LIPMAX, with_ctx=False)
    check("variants 30、價 4840", len(p.variants) == 30 and p.price_jpy == 4840)


def main_():
    print("=" * 74)
    print("JSON-LD ProductGroup → variants 與主商品價")
    print("=" * 74)
    test_color_group()
    test_size_group_prices_differ()
    test_url_selects_variant()
    test_all_out_of_stock()
    test_empty_has_variant()
    test_variant_missing_price()
    test_price_formats_and_availability()
    test_variant_without_option()
    test_broken_json_and_plain_product()
    test_axis_declared()
    test_no_ctx_failsafe()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main_()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
