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

from scrapers.base import (detect_invalid_link, _host_matches,
                          _MSG_NON_SHOP, _NON_SHOP_HOSTS)

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


# ── D. 明顯不是商店：社群／通訊／協作／影音（2026-09-03）──────────────
#
# 7 天 535 筆裡有 2 筆真的發生：facebook.com/share/r/… 與
# evolutivelabs.slack.com/archives/…，兩筆都排進每日摘要的「需要處理」區，
# 但沒有人該去修 facebook.com 的解析器。
NON_SHOP_BLOCK = [
    # 實際發生過的兩筆
    ("https://www.facebook.com/share/r/19fZKBcKTy/", "facebook（實際案例）"),
    ("https://evolutivelabs.slack.com/archives/D0A9323B718/p1788227919356269",
     "slack 子網域（實際案例）"),
    # 清單其餘各一
    ("https://www.instagram.com/p/ABC123/", "instagram"),
    ("https://www.threads.net/@someone/post/x", "threads"),
    ("https://twitter.com/user/status/1", "twitter"),
    ("https://x.com/user/status/1", "x.com"),
    ("https://discord.com/channels/1/2", "discord"),
    ("https://www.notion.so/workspace/Page-abc", "notion"),
    ("https://github.com/user/repo", "github"),
    ("https://www.youtube.com/watch?v=abc", "youtube"),
    ("https://youtu.be/abc", "youtu.be"),
    ("https://www.tiktok.com/@u/video/1", "tiktok"),
    ("https://www.pinterest.com/pin/1/", "pinterest"),
    ("https://www.reddit.com/r/x/comments/1/", "reddit"),
    ("https://www.linkedin.com/in/someone", "linkedin"),
    # 子網域一律命中
    ("https://m.facebook.com/share/r/x/", "facebook 子網域"),
    ("https://gaming.youtube.com/watch?v=x", "youtube 子網域"),
]

# ★ 這幾個刻意**不**放進名單，貼進來要照常放行
NON_SHOP_MUST_PASS = [
    ("https://shopping.line.me/item/1", "line.me：LINE GIFT／ショッピング 是真的商店"),
    ("https://note.com/someone/n/nabc123", "note.com：日本 note 有賣數位商品"),
    # 名稱含關鍵字但完全無關的正常商店
    ("https://x-girl.jp/products/12345", "x-girl.jp 不可以被 x.com 命中"),
    ("https://slackline-shop.jp/item/1", "slackline-shop.jp 不可以被 slack.com 命中"),
    ("https://github-shop.example.jp/p/1", "github-shop 不可以被 github.com 命中"),
    ("https://www.reddit-store.jp/item/1", "reddit-store 不可以被 reddit.com 命中"),
    ("https://youtube-goods.jp/products/1", "youtube-goods 不可以被 youtube.com 命中"),
]

# ── E. ★ 260 個真實 generic 網域的回測（誤擋率必須是 0）────────────────
#
# 樣本來源：Shopify 既有商品的 daigo.source_url，是客人真的貼過、
# 而且真的建成商品的網址。CLAUDE.md 記著 "t.co" 用子字串比對誤擋 7 家的教訓，
# 這一組就是那條規則的常設保險。
#
# ★ 斷言是「不可以被**新規則**擋」，不是「不可以被擋」——
#   其中有些本來就會被既有規則擋（圖床、首頁），那是既有行為不是本次範圍。
REAL_GENERIC_URLS = [
    "https://store.kiseki-products.jp/products/%E4%BA%88%E7%B4%84-27%E5%B9%",
    "https://cdn.filestackcontent.com/CuBArgW2QGyX8ZACp7kR",
    "https://www.yodobashi.com/product/100000001009336462/",
    "https://store.shopping.yahoo.co.jp/golkin/btg-cb027.html?sc_i=shopping",
    "https://woodarbre.com/items/690dea7a27d5b2b901cf9f42",
    "https://yokumoku.co.jp/products/",
    "https://tokushima-shikki.com/shop/products/detail/112",
    "https://www.maruyamanori.com/c/matcha_n/831176-L292",
    "https://www.suruga-ya.jp/product/detail/892621178",
    "https://tokyocrafts.jp/products/products-161-tenbishelter_gray",
    "https://www.digimart.net/cat13/shop5000/DS09782098/",
    "https://www.a-hatoya.com/products/detail/76",
    "https://www.eteweb.com/items/721198",
    "https://www.arknets.co.jp/g/gMS-1-SHORT-NAPPAm-sblk/",
    "https://cybex-japan.com/collections/car-seat/products/solution-g2?vari",
    "https://salomon.jp/products/x-ultra-5-mid-wide-gore-tex-l477554",
    "https://darumashouten.jp/bt-ac1154-78-35set/",
    "https://www.lilith-soft.com/store/product/LGD-74124/3045",
    "https://www.exseal.jp/products/detail/395",
    "https://kurand.jp/products/juufuuteiraden",
    "https://www.tmrecords.shop/shopdetail/000000000032/",
    "https://tsuentea.com/products/%E6%8A%B9%E8%8C%B6-%E5%A4%AA%E9%96%A4%E3",
    "https://sugihokowakenomikoto.jimdoweb.com/%E5%BE%A1%E5%AE%88%E3%82%8A-",
    "https://store.seibulions.jp/shop/g/gL020903",
    "https://www.ghibli-museum-shop.jp/i/MDG-TOSH-178",
    "https://shop.shiki.jp/%e3%82%aa%e3%83%9a%e3%83%a9%e5%ba%a7%e3%81%ae%e6",
    "https://chikumeido.com/web_shop/%e7%85%a4%e7%ab%b9%e3%80%80%e8%8c%b6%e",
    "https://comicomi-studio.com/goods/detail/228817",
    "https://store.bluebottlecoffee.jp/products/g227156-pre?utm_source=pop&",
    "https://subu2016-onlinestore.com/products/subu-outline-beige?variant=5",
    "https://remu2024.official.ec/items/149088277",
    "https://chaho-yutoha.com/products/matcha-ryosui?variant=45695943606527",
    "https://www.rinnoji.or.jp/amulet/taiyuuin/item03.html",
    "https://gochio.kyoto.jp/shop/products/detail.php?product_id=5",
    "https://ujimatcha.base.shop/items/10370944",
    "https://goyoutati.com/products/%E6%97%A5%E6%9C%AC%E4%BB%A3%E8%B3%BC-%E",
    "https://www.maruyamacoffee.com/ec/products/detail/2146",
    "https://kenko.morinagamilk.co.jp/Form/Product/ProductDetail.aspx?shop=",
    "https://tsuruta-hachimangu.com/342/",
    "https://ec.treasure-f.com/item/1113000389460423",
    "https://okinawa-ichiba.net/products/detail/2423",
    "https://okunishirokuhoen.jp/product/matcha_keiun/",
    "https://stripe-club.com/brand/earth1999/item/1001M26D0030?areaid=eb011",
    "https://shop.shunsho.co.jp/Form/Product/ProductDetail.aspx?shop=0&pid=",
    "https://shopping.bookoff.co.jp/used/0017127470",
    "https://www.duffy-cos.com/product/5250",
    "https://www.tocco-closet.co.jp/SHOP/186-205637.html",
    "https://caseplay.shop/products/sp_sp_cl11c0001d001883-27_op2315?varian",
    "https://seishou.base.shop/items/152717570?fbclid=IwRlRTSATj3lVwZG9mBWZ",
    "https://www.nissin.com/jp/product/items/13761/",
    "https://www.itohkyuemon.co.jp/c/sweets/094053",
    "https://lifetunes-mall.jp/shop/shop/g/gL20260218/",
    "https://store.alpen-group.jp/Form/Product/ProductDetail.aspx?shop=0&pi",
    "https://shop.hanshintigers.jp/goods/index.html?ggcd=39071&cid=replica",
    "https://tokichi.jp/products/mc5?variant=50795149426901",
    "https://www.dot-st.com/classicalelf/disp/item/293463/",
    "https://auctions.yahoo.co.jp/jp/auction/g1238499348",
    "https://paypayfleamarket.yahoo.co.jp/item/z631751208",
    "https://store.plusmember.jp/equallove/products/detail.php?product_id=1",
    "https://fujitaka-japan.com/zh-CHT/product/646688/",
    "https://workman.jp/shop/g/g2300055104066/",
    "https://www.cospa.com/cospa/detail/id/00000141554",
    "https://official-store.jfa.jp/goods_list.php?keyword=UX-00",
    "https://www.suqqu.com/ja/categories/colorMakeup/eyes/p/4973167049938",
    "https://www.amiami.com/cn/detail/?scode=LTD-FIG-10697",
    "https://www.costco.co.jp/Seasonal/1-DAY-ACUVUE-DEFINE-30-pack/p/666100",
    "https://sagyougi.net/products/ho-v1217",
    "https://shop.akachan.jp/shop/g/g264320001/",
    "https://www.nissen.co.jp/item/CDY0524B0011",
    "https://hakuchikudo.jp/collections/men/products/alumihoneycomb",
    "https://item.fril.jp/f126f8b57a1d7bc8f9f26fd8524ca141",
    "https://www.ebisato.shop/shopdetail/000000000324/",
    "https://1kuji.com/products/gintama22",
    "https://www.uniformnext.com/work-uniform/product/03-ac2041/",
    "https://japan.calvinklein.com/shop/item/40432MF?colorCode=RRF&shopCode",
    "https://shop.tushima-jinja.jp/products/detail/49",
    "https://shop.afternoon-tea.net/shop/g/gJQ90-26100214/",
    "https://www.shorakuen.com/product-page/utopian-luckey-cat-tea-pot",
    "https://zh-tw.sarutahiko.jp/products/m-ec?variant=42083834658869",
    "https://www.ebay.com/itm/398114535035?_skw=adidas+BAPE+Teamgeist&itmme",
    "https://www.gundam-base.net/products/details.php?path=01_6730",
    "https://www.skechers.jp/%E3%82%A6%E3%82%A9%E3%83%BC%E3%82%BF%E3%83%BC%",
    "https://www.google.com/url?q=https://tohoentertainmentonline.com/shop/",
    "https://share.google/AB8pYXvTuDWM4lR17",
    "https://fo-online.jp/items?bc=J",
    "https://www.syusendo-horiichi.co.jp/SHOP/22919.html",
    "https://www.abc-mart.net/shop/g/g6104240001043/",
    "https://www.kixdutyfree.jp/tw/velo-mighty-peppermint-intense-240690001",
    "https://store.toei-anim.co.jp/shop/g/g26PC12614",
    "https://shop.elleair.co.jp/collections/menstrual_products-slim/product",
    "https://store.wacoal.jp/disp/01_PTJ432.html",
    "https://store-jp.nintendo.com/item/goods/BH_NSJ_8_BZAD",
    "https://www.cledepeau-beaute.com/jp/products/makeup/face/primers/45142",
    "https://www.honeys-onlineshop.com/shop/g/g619013011632/",
    "https://on-line.1kuji.com/Form/Product/ProductDetail.aspx?pid=sap_0000",
    "https://www.matsukiyococokara-online.com/store/catalog/product/view/id",
    "https://gelatopique.com/Form/Product/ProductDetail.aspx?shop=0&pid=PSG",
    "https://www.sondersable.com/products/sheer-embroidered-lace-voluminous",
    "https://oofos.jp/collections/mens-thong/products/original-black",
    "https://store.world.co.jp/brand/right-on/item/BD11425S0033",
    "https://www.post.japanpost.jp/enjoy/culture/stamp/frame/detail.php?id=",
    "https://shop.golfdigest.co.jp/used/f/dmg_5003046263",
    "https://www.enskyshop.com/products/detail/28625",
    "https://www.yasaburo.com/products/%e7%99%bd%e7%ab%b9%e8%8c%b6%e7%ad%8c",
    "https://vvstore.jp/products/detail/5065538",
    "https://7net.omni7.jp/detail/1301600296",
    "https://conanplaza.com/i/55053",
    "https://www.shop.carp.co.jp/shop/i91032.html",
    "https://minne.com/items/2952642",
    "https://mashstylelab.jp/sanriohouse/Form/Product/ProductDetail.aspx?sh",
    "https://www.ralphlauren.co.jp/%E3%83%AA%E3%83%9F%E3%83%86%E3%83%83%E3%",
    "https://www.sunlemon-original.jp/prod/tatton/s_nihonzaru/",
    "https://mpglobal.donki.com/ec-web/m/gd?gId=I2024041904001?lan=zh-tw",
    "https://ec.snowpeak.co.jp/item/SNP0126A0085?clr_id=101",
    "https://webshop.montbell.jp/goods/disp.php?product_id=1130713&top_sk=1",
    "https://www.on.com/ja-jp/products/running-t-paf-u-1ug1008/unisex/gale-",
    "https://p-bandai.com/tw/item/N2643872003",
    "https://www.plazastyle.com/contents/spongebob2026/?via_source=top_bann",
    "https://www.goldwin.co.jp/ap/item/i/m/NTJ32636R#&gid=1&pid=5",
    "https://www.pokemon-card.com/ex/m5/",
    "https://kyocera-ffg-tools.com/product/vest-ac2094k/",
    "https://www.sodastream.jp/products/detail/453",
    "https://kuchoufuku.com/set/AC1154set/?c=53#zaikoinfo",
    "https://www.kao-kirei.com/ja/item/kbb/curel/4901301238825/?tw=kbb",
    "https://fukushimahachimangu.or.jp/jyuyosho/20260601-15997/",
    "https://www.24028-net.jp/item/205207594.html",
    "https://shop.ligneroset.jp/products/togo_1?variant=36218496581783",
    "https://www.camera-ohnuki.com/products/%e3%82%ad%e3%83%a4%e3%83%8e%e3%",
    "https://militical.base.shop/items/94586518",
    "https://vgiftshop.base.shop/items/152373504",
    "https://www.dreampocket-webshop.jp/c/lepetitprince/lepetit_apparel/lep",
    "https://www.1999.co.jp/11360760",
    "https://www.burnedestrose.com/shop/g/g200826251000141100/?ismodesmartp",
    "https://homemadejp.stores.jp/items/69674212597398157d8ecf7b",
    "https://decoto.jp/",
    "https://www.a-golf.net/c/01/42/100/029-19-warbirdset",
    "https://webshop.self.co.jp/shop/goods/search.aspx?tree=&search=x&keywo",
    "https://www.hmv.co.jp/artist_MARQUEE%E7%B7%A8%E9%9B%86%E9%83%A8_000000",
    "https://www.mtgec.jp/shop/pages/refa_dryer_smart_w.aspx",
    "https://www.chanel.com/jp/makeup/p/158432/rouge-coco-hydra-gloss-hydra",
    "https://www.lunasol-official.com/categories/pointmake/eye-shadow/p/497",
    "https://scratch.dmm.com/kuji/shikiokuri/",
    "https://www.tfmmall.com/products/suqqu-blurring-color-blush",
    "https://edepart.sogo-seibu.jp/item/002228/00100134973167012093",
    "https://baycrews.jp/item/detail/js-relume/bag/26092463000130?q_sclrcd=",
    "https://www.mychoice-mylife.com/item/MCML-charity-kumamoto-BXL/#ItemDe",
    "https://marche.airdo.jp/Form/Product/ProductDetail.aspx?shop=0&pid=406",
    "https://shop.mu-mo.net/avx/sv/item1?jsiteid=mumo&seq_exhibit_id=399581",
    "https://isehan-beni-shop.com/?pid=189184971",
    "https://www.daikokudrug-taiwan.com/zh-TW/products/m81055695",
    "https://runnet.jp/project/shop/mtfujim/2026en-shop/",
    "https://nerugoo.jp/products/nerugoo-medical",
    "https://www.netsea.jp/shop/3018/N00546287",
    "https://store.universal-music.co.jp/products/dsku16174/?utm_source=Ori",
    "https://fu-a.info/onlineshop/150599479",
    "https://groovegarage.supersale.jp/items/99107668",
    "https://shop.shogyokuen.co.jp/?pid=139596957",
    "https://sakatamatabei.com/product/asahi/",
    "https://vvshop.com.tw/product/%E3%80%90%E6%97%A5%E6%9C%AC%E7%9B%B4%E9%",
    "https://recolte.official.ec/items/118093902",
    "https://kentex-shop.com/frieren2026/index_en.html",
    "https://petlovers.shop-pro.jp/?pid=179155230",
    "https://www.e-cha.co.jp/c/ocha/nihoncha/matcha-genmaicha-houjicha/matc",
    "https://www.krf.co.jp/SHOP/K001.html",
    "https://petico.legend-walker.com/product/icoplus/",
    "https://mall.shopro.co.jp/c/item/kotoyama-ex/KTY26027",
    "https://pwstore.easy-myshop.jp/c-item-detail?ic=26ilf-008",
    "https://plus-kb.com/collections/home-care-for-rabbits/products/soq?var",
    "https://orgel-gallery.jp/products/mm801-aip?variant=39518517002420",
    "https://www.letao.jp/category/GIFT/K065.html",
    "https://hoshinohana.shop-pro.jp/?pid=136407117",
    "https://www.newart.co.jp/95482.html",
    "https://www.zerofighter555.com/cathand/detail-627189.html",
    "https://www.cute-sales.com/%E3%82%AD%E3%83%A5%E3%83%BC%E3%83%88%E8%B2%",
    "https://tsuchiya-kaban.jp/products/otona-randsel-ft-001",
    "https://arabica.com/product/colombia-quindio-santa-monica/",
    "https://www.getchu.com/item/1363566/?srsltid=AfmBOoq72ncXPmYJzMv375D7g",
    "https://shopping.tbs.co.jp/tbs/product/P2123128",
    "https://gs.abc-mart.net/shop/g/g6996950001013/",
    "https://subaruya.com/ygtia_s2/",
    "https://www.logos.ne.jp/products/info/11719",
    "https://www.viviennewestwood.com/ja-jp/women/accessories/wallets-and-p",
    "https://t.co/XcBymb8X2h",
    "https://www.book61.co.jp/book.php/N94155",
    "https://raffle-kuji.jp/lotteries/1355",
    "https://www.bandai.co.jp/catalog/item.php?jan_cd=4582769993848000",
    "https://www.superdelivery.com/p/r/pd_p/13043862/",
    "https://geo-online.co.jp/store_info/item/5196217/?clk=store_info_searc",
    "https://www.dior.com/ja_jp/beauty/products/%E3%83%87%E3%82%A3%E3%82%AA",
    "https://panasonic.jp/face/products/EH-SR86.html",
    "https://www.paqtomog.com/shop/g/g2130/",
    "https://gomexus.jp/product/th90/",
    "https://yasutomisake.base.shop/items/51340313",
    "https://ifing-beauty.com/products/tokio-ie-1?variant=52516860526901",
    "https://prtimes.jp/main/html/rd/p/000002411.000046210.html",
    "https://ilu.booth.pm/items/8622457",
    "https://hikotakegu.stores.jp/items/668626abe8c4f8002c008891",
    "https://shop.goyokikiya.jp/zh-tw/shop/detail/rakuten/?scd=zononetshop&",
    "https://sesamestreetmarket.jp/Form/Product/ProductDetail.aspx?shop=0&p",
    "https://www.mansaw.net/c/wallet/bil-wallet/m00000418",
    "https://eshop.fujitv.co.jp/c/g_anime/B007096/33046",
    "https://goods.mobilitystation.jp/items/68c27ec96270d02300c40038",
    "https://tohoentertainmentonline.com/shop/g/gTASG03806a/",
    "https://christophernemeth.co/products/tshirt-printed-cotton100-jersey-",
    "https://zhtw.seisukeknife.com/products/kei-kobayashi-r2-sg2-gyuto-japa",
    "https://m.qoo10.jp/g/1194260777",
    "https://www.30th.pokemon-card.com/product/m6a",
    "https://www.cross-country.com.tw/SalePage/Index/5872773",
    "https://www.shopthermos.jp/shop/g/g300067941FG0/?utm_campaign=facebook",
    "https://shop.sanrio.co.jp/item/detail/1_1_2607014842_1/KT_BLACK/-",
    "https://www.peachjohn.co.jp/shop/g/g10264820164/",
    "https://pellicule.jp/products/ribbon-brilliant-denim",
    "https://a-onstore.jp/item/item-1000235803/?srsltid=AfmBOoqSs6FrPGFKipg",
    "https://bape.com/products/1j22-110-010",
    "https://yamashinseikyo.com/products/detail.php?product_id=658",
    "https://www.sino-kyoto.shop/smartphone/detail.html?id=000000000224&cat",
    "https://hcj.jp/snk/home.html",
    "https://www.stokke.com/JPN/ja-jp/%E3%83%8F%E3%82%A4%E3%83%81%E3%82%A7%",
    "https://y-3.com/item/104426322.html",
    "https://www.nanamica.com/item/8955/",
    "https://chiikawamogumogu.shop/products/chgs-0340",
    "https://www.cledepeau-beaute.com.tw/%E6%81%86%E6%BD%A4%E7%B5%B2%E7%B7%",
    "https://www.zsports.shop/shopdetail/000000000253/",
    "https://www.san-ei-boeki.co.jp/character/pkp/pkp01/11121/",
    "https://brownieonline.jp/products/detail/1548",
    "https://azzurronero.jp/goods_detail.php?id=2447",
    "https://www.sanrio.co.jp/news/goods/pc-ap-atarikuji-20260723/",
    "https://www.bleubleuet.jp/shop/g/g040130043000264/",
    "https://store.persica.jp/collections/asahi-deck/products/asahi-deck-wh",
    "https://www.gazaihanbai.jp/products/detail/product_id/77283.html",
    "https://www.rmkrmk.com/ja/categories/skincare/UVcare/p/4973167529928",
    "https://brand.shiseido.co.jp/colorglow-enhancer.html",
    "https://usagi-online.com/brand/sanriohouse/item/SRO0126F0003",
    "https://www.id-official.com/products/protect-u-ultralight",
    "https://shop.hushtug.net/zh/collections/all-handbag/products/bag-draws",
    "https://www.curtain-damashii.com/item/storagebox_browndust01/",
    "https://www.fujisan.co.jp/product/1281710346/b/list/",
    "https://info.nikkeibp.co.jp/media/NPC/sales/pc21dvd26/",
    "https://www.nike.com/jp/t/%E3%82%A8%E3%82%A2-%E3%82%B8%E3%83%A7%E3%83%",
    "https://jalplaza-airport.jalux.com/product/detail/9200907242667/",
    "https://shop.collabocafe.tokyo/products/%E5%8F%97%E6%B3%A8%E5%95%86%E5",
    "https://www.junonline.jp/rope-picnic/product/jacket-outerwear/jacket/G",
    "https://shop.chocolate-inc.com/products/snsn040-3?fbclid=PAVERFWATxBg5",
    "http://www.yokumoku.jp",
    "https://onlinestore.nepenthes.co.jp/products/ventisei-h-d-track-pant-1",
    "https://www.kapital-webshop.jp/category/MENSALL/K2603KN052.html",
    "https://kuji.kingrecords.co.jp/lotteries/soubirthday2026",
    "https://hobby.ec.volks.co.jp/item/4535123850325.html",
    "https://shop.holbein.co.jp/collections/watercolors-artists-watercolor-",
    "https://www.onitsukatiger.com/jp/ja-jp/product/mexico-66-deluxe/1181a5",
    "https://www.fukuoka-anpanman.jp/buy/goods/k0lqgj7h42o526fw.html",
    "https://store.anpanman.jp/products/a25x0059",
    "https://www.ichiranstore.com/shop/g/g9699004/",
    "https://harrypottershop.jp/products/gryffindor-logo-tshirt",
    "https://daigen-miso.co.jp/c/gr123/suzunagi-500",
    "https://tepillow.wixsite.com/t-e-taste/faq",
    "https://takura.info/products/久米桜-オオカミ-1-8l",
    "https://miyagawasaketen.store/products/kumesakura-tanokamisan-crude-go",
    "https://www.shop.post.japanpost.jp/shop/pages/kitte_hagakistore.aspx",
    "https://aniclo.jp/products/4571576740578",
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
    # 新增的社群名單也要驗語意
    ("slack.com", "slack.com", True),
    ("evolutivelabs.slack.com", "slack.com", True),
    ("slackline-shop.jp", "slack.com", False),
    ("x.com", "x.com", True),
    ("x-girl.jp", "x.com", False),
    ("mail.x.com", "x.com", True),
    ("github.com", "github.com", True),
    ("github-shop.example.jp", "github.com", False),
]


def main():
    print("=" * 74)
    print("A. 四類非商品頁連結都要擋")
    print("=" * 74)
    for url, label in SHOULD_BLOCK:
        check(f"擋下 {label}", detect_invalid_link(url) is not None, url[:50])

    print("\n" + "=" * 74)
    print("D. 明顯不是商店的網域要擋，而且理由要是「不是商店」")
    print("=" * 74)
    for url, label in NON_SHOP_BLOCK:
        check(f"擋下 {label}", detect_invalid_link(url) == _MSG_NON_SHOP,
              (detect_invalid_link(url) or "(沒擋)")[:32])
    check("★ 名單有 15 個網域", len(_NON_SHOP_HOSTS) == 15, str(len(_NON_SHOP_HOSTS)))
    check("★ line.me 不在名單裡", "line.me" not in _NON_SHOP_HOSTS)
    check("★ note.com 不在名單裡", "note.com" not in _NON_SHOP_HOSTS)

    print("\n" + "=" * 74)
    print("D2. ★ 刻意不擋的（擋了會誤傷真商店）")
    print("=" * 74)
    for url, label in NON_SHOP_MUST_PASS:
        check(f"放行 {label}", detect_invalid_link(url) != _MSG_NON_SHOP,
              (detect_invalid_link(url) or "None")[:32])

    print("\n" + "=" * 74)
    print(f"E. ★ {len(REAL_GENERIC_URLS)} 個真實 generic 網址的誤擋回測")
    print("=" * 74)
    wrong = [u for u in REAL_GENERIC_URLS
             if detect_invalid_link(u) == _MSG_NON_SHOP]
    check(f"★ 誤擋 0 家（樣本 {len(REAL_GENERIC_URLS)} 個真實商品網址）",
          not wrong, f"誤擋 {len(wrong)}: {wrong[:3]}")
    check("樣本數就是 260（少了代表清單被改動過）",
          len(REAL_GENERIC_URLS) == 260, str(len(REAL_GENERIC_URLS)))

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
