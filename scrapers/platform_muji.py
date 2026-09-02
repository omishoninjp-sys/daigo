"""
MUJI 無印良品（muji.com）Platform
=================================
商品頁伺服器 HTML 內嵌 schema.org JSON-LD（Product/offers），價格為税込、
含該變體的 availability，沿用共用 scrapers/jsonld.py 預設解析即可。

單變體策略（見對話決議 A）：
  MUJI 把每個「顏色×尺寸」都做成獨立 GTIN 商品頁（cmdty/detail/{jan}），
  變體矩陣與逐一尺寸庫存在 Next.js RSC（self.__next_f）串流裡、不易穩定取得。
  因此本平台「貼哪個 GTIN 就抓哪個」：正確價格 + 該變體庫存，不組尺寸/顏色選擇器。
  客人貼他要的那件即可；需要其他尺寸就貼該尺寸的網址。

註冊（scrapers/__init__.py）：
  from scrapers.platform_muji import MujiPlatform
  register(MujiPlatform())          # LegacyPlatform 之前

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔴 MujiMixin（scrapers/muji.py）已於 2026-09-02 刪除
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本 Platform 於 2026-07-21（aa9afdc）註冊時，MujiMixin 就成為死碼 ——
**但當時檔案沒刪、也沒有委派、更沒有任何註記**，於是它看起來還在用。
代價：MujiMixin 裡的圖片 base64 機制隨之靜默消失，7/21 之後建立的
MUJI 商品**全部無圖**（線上 22 件裡 15 件無圖，7/21 之前的 7 件都有圖）。
2026-09-02 才查出來，中間過了六週。

被取代的機制，逐條交代：
  · JSON-LD 價格解析        → JsonLdPlatform 本來就有，重複，刪
  · 自訂 headers / PROXY_URL → jsonld.py 有自己的，刪
  · RSC flight payload 解析  → 只有多圖與變體需要，見下方「多圖」
  · _muji_apply_variants     → 單變體是刻意決定（見上），刪
  · _muji_download_image_b64 → httpx 版，**被 Akamai 擋死，搬過來也沒用**，刪
  · 瀏覽器版 base64 抓圖      → **唯一不可替代的**，已搬進
                               jsonld._browser_image_b64（見下）

🔴 為什麼圖片只能走瀏覽器
  www.muji.com 走 Akamai（DNS → www.muji.com.edgekey.net），**依 TLS 指紋
  擋掉非瀏覽器的請求**。2026-09-02 實測：httpx 與 curl 對整個網域（含首頁）
  都是 TCP+TLS 握手成功、首位元組永遠不來；同一台機器的 Chrome UC 正常。
  所以：
    · 圖片 URL 原樣交給 Shopify 讓它伺服器端抓 → 一定失敗
    · 用 httpx 自己抓圖再轉 base64          → 一樣失敗
    · 只有在**已經通過 Akamai 的瀏覽器 session 裡**用 fetch() 抓 blob
      再轉 base64 才可行（帶著同一組 cookie 與指紋）
  這就是 JsonLdSeleniumSource(image_b64=True) 的用途。

多圖（extra_images）目前是 0
  JSON-LD 只給一張圖。MUJI 的多圖在 RSC payload 的 productImages 裡，
  解析程式碼在 **aa9afdc 之前的 scrapers/muji.py**（_muji_apply_images
  與 _muji_extract_after 一整組），要做多圖時去 git 歷史撈：
      git show aa9afdc~1:scrapers/muji.py
  注意每張 extra image 都要各自跑一次瀏覽器 base64，成本乘以張數。
"""
from scrapers.jsonld import JsonLdHttpxSource, JsonLdSeleniumSource, JsonLdPlatform
from scrapers.platform import _note_error


class MujiPlatform(JsonLdPlatform):
    id = "muji"
    hosts = ("muji.com",)
    # ★ 不可以沿用 JsonLdPlatform 的預設 sources —— 那是**共用的實例**，
    #   在上面開 image_b64 會連 SnidelPlatform 等其他平台一起打開。
    #   這裡自己建一組，只有 Selenium 那支開鉤子。
    sources = [
        JsonLdHttpxSource(tag="MUJI"),
        JsonLdSeleniumSource(tag="MUJI", image_b64=True),
    ]

    async def fetch(self, url: str, engine):
        product = await super().fetch(url, engine)
        # 🔴 httpx 那條成功時不會走到 Selenium，也就沒有 base64 圖 ——
        #    Shopify 伺服器端抓圖會被 Akamai 擋，結果就是無圖商品。
        #    **這裡刻意不強制走 Selenium**（每次開瀏覽器太慢），
        #    但一定要留下訊號 —— 今天這件事就是「能力靜默消失」造成的，
        #    不能再留一個同樣的洞。
        try:
            if (product is not None and getattr(product, "is_valid", False)
                    and getattr(product, "image_url", "")
                    and not getattr(product, "image_base64", "")):
                print("[MUJI] ⚠️ httpx 路徑成功但無 base64 圖片 —— "
                      "Shopify 伺服器端抓圖會被 Akamai 擋，此商品可能無圖")
                _note_error("httpx 路徑成功但無 base64 圖片（Akamai 擋伺服器端抓圖，"
                            "此商品可能無圖）", "MUJI")
        except Exception:
            pass          # 訊號壞掉不可以影響抓取結果
        return product
