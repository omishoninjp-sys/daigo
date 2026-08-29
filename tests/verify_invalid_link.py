"""
detect_invalid_link() 的回歸測試（離線，不需要任何憑證）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_invalid_link.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_invalid_link.py`）

★ 這支存在的理由是「子字串誤傷」：
   `"t.co" in host` 會命中 tocco-closet.co.jp、golfdigest.co.jp、dot-st.com、
   newart.co.jp、uniformnext.com、lilith-soft.com 等正常商店。
   第一版就是這樣誤擋了 7 家。C 組把那 7 家釘死成回歸案例，
   任何人把比對改回子字串都會立刻紅燈。

誤擋率回測（拿 Shopify 既有 source_url 當樣本）需要 Shopify 憑證，不放在這裡。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scrapers.base import detect_invalid_link, _host_matches

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── A. 四類都要擋 ──────────────────────────────────────────────────
SHOULD_BLOCK = [
    # 1. 圖片直連
    ("https://shopping.c.yimg.jp/lib/queensshop/yol106.jpg", "圖片副檔名 .jpg"),
    ("https://c.imgz.jp/423/84076423/84076423b_365_d_500.jpg", "imgz.jp"),
    ("https://image.rakuten.co.jp/shop/cabinet/item.png", "主機名第一段 image"),
    ("https://img.example.co.jp/a/b", "主機名第一段 img"),
    ("https://images.example.co.jp/a/b", "主機名第一段 images"),
    ("https://static.zara.net/photos/x", "主機名第一段 static"),
    ("https://assets.adidas.com/images/x", "主機名第一段 assets"),
    ("https://cdn.example.com/x", "主機名第一段 cdn"),
    ("https://cdn.shopify.com/s/files/1/xxx/product.png", "cdn.shopify.com"),
    ("https://cdn.filestackcontent.com/CuBArgW2QGyX8ZACp7kR", "filestackcontent.com"),
    ("https://baseec-img-mng.akamaized.net/images/item/origin/abc", "akamaized.net"),
    ("https://d1234.cloudfront.net/img", "cloudfront.net"),
    ("https://lh3.googleusercontent.com/abc", "googleusercontent.com"),
    ("https://contents.palcloset.jp/static/images/item/1_1.jpg", "路徑副檔名"),
    ("https://example.jp/item/1.WEBP", "副檔名大小寫不敏感"),
    # 2. 搜尋引擎與短網址
    ("https://www.google.com/search?q=zozo", "google.com"),
    ("https://www.google.co.jp/search?q=x", "google.co.jp"),
    ("https://share.google/AB8pYXvTuDWM4lR17", "share.google"),
    ("https://goo.gl/abc", "goo.gl"),
    ("https://www.bing.com/search?q=x", "bing.com"),
    ("https://t.co/XcBymb8X2h", "t.co"),
    ("https://bit.ly/abcdef", "bit.ly"),
    ("https://lin.ee/abcdef", "lin.ee"),
    ("https://reurl.cc/abcdef", "reurl.cc"),
    ("https://pse.is/abcdef", "pse.is"),
    ("https://tinyurl.com/abcdef", "tinyurl.com"),
    # 3. 本站自己
    ("https://goyoutati.com/products/abc", "goyoutati.com"),
    ("https://www.goyoutati.com/pages/abc", "goyoutati.com 子網域"),
    ("https://fd249b-ba.myshopify.com/products/abc", "myshopify.com"),
    # 5. 首頁／語系首頁（不是商品頁，爬了會建出假商品）
    ("https://coldbeer.jp/zh", "語系首頁 /zh"),
    ("https://example.co.jp/", "首頁"),
    ("https://example.co.jp", "首頁（沒有斜線）"),
    ("https://example.co.jp/en", "語系首頁 /en"),
    ("https://example.co.jp/ja/", "語系首頁 /ja/"),
    ("https://example.co.jp/zh-TW", "語系首頁大小寫不敏感"),
    # 4. 結構不成立
    ("ftp://example.com/file.zip", "非 http(s)"),
    ("not a url", "不是網址"),
    ("https://", "沒有 host"),
    ("", "空字串"),
    ("   ", "只有空白"),
]

# ── B. ★ 絕不可被擋：實測被子字串比對誤擋過的 7 家 + 易誤傷網域 ──────
MUST_PASS = [
    "https://tocco-closet.co.jp/products/abc",       # 曾被 "t.co" 誤擋
    "https://www.golfdigest.co.jp/item/123",         # 曾被 "t.co" 誤擋
    "https://www.dot-st.com/stripe/disp/item/1234",  # 曾被 "t.co" 誤擋
    "https://newart.co.jp/shop/item/1",              # 曾被 "t.co" 誤擋
    "https://uniformnext.com/products/x",            # 曾被 "t.co" 誤擋
    "https://lilith-soft.com/product/y",             # 曾被 "t.co" 誤擋
    "https://images-shop.example.jp/item/1",         # 第一段是 images-shop 不是 images
    "https://cdnjapan.co.jp/item/1",                 # 第一段是 cdnjapan 不是 cdn
    "https://mystatic-shop.jp/item/1",
    "https://google.com.tw.example.jp/item/1",       # 不是 google.com 的子網域
    "https://notgoogle.com/item/1",
    "https://mygoo.gl.example.jp/item/1",
    # 真實會用到的商品網址
    "https://item.rakuten.co.jp/shop/code/",
    "https://store.shopping.yahoo.co.jp/queensshop/yol106.html",
    "https://www.amazon.co.jp/dp/B0XXXX",
    "https://zozo.jp/shop/x/goods/12345/",
    "https://jp.mercari.com/item/m12345",
    "https://www.suruga-ya.jp/product/detail/123",
    "https://www.muji.com/jp/ja/store/cmdty/detail/1234",
    # 首頁規則不可誤傷這些
    "https://kawamura-shop.shop-pro.jp/?pid=123456789",  # カラーミー：path 空但有 pid
    "https://example.co.jp/?product_id=55",              # 同上，query 就是商品識別
    "https://example.co.jp/my-product-handle",           # 單段路徑但不是語言代碼
    "https://example.co.jp/jacket",                      # 同上
    "https://www.muji.com/jp/ja/store/cmdty/detail/x",   # 路徑含 ja 但不只一段
]

# ── C. _host_matches 語意 ─────────────────────────────────────────
HOST_CASES = [
    ("t.co", "t.co", True),
    ("api.t.co", "t.co", True),
    ("tocco-closet.co.jp", "t.co", False),
    ("dot-st.com", "t.co", False),
    ("golfdigest.co.jp", "t.co", False),
    ("newart.co.jp", "t.co", False),
    ("uniformnext.com", "t.co", False),
    ("lilith-soft.com", "t.co", False),
    ("google.com", "google.com", True),
    ("www.google.com", "google.com", True),
    ("google.com.tw", "google.com", False),
    ("notgoogle.com", "google.com", False),
    ("mygoo.gl", "goo.gl", False),
    ("GOYOUTATI.COM", "goyoutati.com", True),      # 大小寫
    ("goyoutati.com.", "goyoutati.com", True),     # 結尾的點
    ("", "t.co", False),
    ("t.co", "", False),
]


def main():
    print("=" * 74)
    print("A. 四類非商品頁連結都要擋")
    print("=" * 74)
    for url, label in SHOULD_BLOCK:
        check(f"擋下 {label}", detect_invalid_link(url) is not None, url[:50])

    print("\n" + "=" * 74)
    print("B. ★ 正常商店絕不可被擋（子字串誤傷回歸案例）")
    print("=" * 74)
    for url in MUST_PASS:
        reason = detect_invalid_link(url)
        check(f"放行 {url[:52]}", reason is None, (reason or "")[:40])

    print("\n" + "=" * 74)
    print("C. _host_matches：完整網域或子網域，不可子字串")
    print("=" * 74)
    for host, domain, expect in HOST_CASES:
        got = _host_matches(host, domain)
        check(f"_host_matches({host!r}, {domain!r}) = {expect}", got == expect, f"得到 {got}")

    print("\n" + "=" * 74)
    print("D. 每一類都要有給客人看的繁中說明")
    print("=" * 74)
    for url, label in [("https://x.jp/a.jpg", "圖片"), ("https://t.co/x", "短網址"),
                       ("https://goyoutati.com/products/x", "本站"), ("bad", "結構")]:
        msg = detect_invalid_link(url) or ""
        check(f"{label}類有說明且提示該貼什麼",
              len(msg) > 20 and ("請" in msg), msg[:34])

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
