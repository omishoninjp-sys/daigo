"""
MUJI 無印良品（muji.com）Platform
=================================
商品頁伺服器 HTML 內嵌 schema.org JSON-LD（Product/offers），價格為税込、
含該變體的 availability，沿用共用 scrapers/jsonld.py 預設解析即可。

單變體策略：「貼哪個 GTIN 就抓哪個」——正確價格 + 該變體庫存，不組尺寸/顏色選擇器。
客人貼他要的那件即可；需要其他尺寸就貼該尺寸的網址。

🔴 理由（2026-09-02 重新查證，原本寫的理由是錯的）
  舊註解寫「頁面上沒有選擇器」——**錯**。實測 4550723454025
  （ベビー 脇に縫い目のないリブ編みカバーオール ¥1,990）頁面上**有**顏色與尺寸
  選擇器：3 個顏色色票 + 3 個尺寸（60 / 70 / 80）。
  但它們是 `<a href>` **連到別的 GTIN 商品頁**，點下去整頁換掉、URL 跟著變 ——
  不是同頁切換的變體選單。所以結論不變，理由要這樣講。

  RSC payload 裡的兩個陣列也是同一件事，它們是**連結**不是同頁變體：
    colorVariations  __typename colorCode colorImage colorName
                     inventoryStatus inventoryText janCode
    sizeVariations   __typename sizeCode sizeName janCode
                     inventoryStatus noStock selected itemUrl
  **兩個都沒有 price 欄位。**

  而且每頁只露出自己的「行」與「列」，不是完整矩陣：
    在 4550723454025（ライトベージュ / 70）
      colors → 生成…3998 / ライトベージュ…4025 / スモーキーブルー…4056（都是尺寸 70）
      sizes  → 60…4032 / 70…4025 / 80…4049（都是 ライトベージュ）
    在 4550723453998（生成 / 70）
      sizes  → 60…4001 / 70…3998 / 80…4018（都是 生成，JAN 全不同）
  3 色×3 尺寸 = 9 個 JAN，單頁只看得到 5 個（3+3−1）。
  要拿完整矩陣，最少得走 min(色數, 尺寸數) 個頁面。

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

  🔴 但**同一支檔案裡的 _muji_apply_variants 不可以直接拿來用**，它有兩個
     真實缺陷（2026-09-02 對照實際 RSC payload 查出來的）：
     (a) 它讀 `v.get("price")`，但 colorVariations / sizeVariations
         **根本沒有 price 欄位** → `or product.price_jpy` 會讓每個變體
         一律繼承當前頁的價格。尺寸不同價的商品會**靜默錯價**，
         而低估售價不會有客人來反映。
     (b) 它把 colorVariations 與 sizeVariations **接成一個平坦清單**
         （3+3=6 筆，顏色那幾筆 size=""、尺寸那幾筆 color=""），
         不是 3×3 矩陣。單頁本來就只露出 3+3−1 個 JAN，
         完整矩陣要走 min(色數, 尺寸數) 個頁面才組得出來。
     _muji_apply_images 那組（多圖）不受這兩點影響，可以撈。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔴 要做多變體，先解掉兩個結構問題 —— 這才是不做的真正理由
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
成本不是「31 秒 × N」。實際拆解（2026-09-02 生產環境 4 筆，31.5～36.1 秒，
平均 33.2 秒，http_status 全是 None —— httpx 連回應都沒拿到）：

    httpx 白等          20 秒   jsonld._fetch 的 timeout=20，Akamai 一定逾時
    Selenium 抓頁    8～18 秒   generic.py:137 uc_open_with_reconnect(6 秒固定)
                               ＋ sleep(2)×最多 6 次
    圖片 base64         1 秒
    ────────────────────────
    合計             約 33 秒

  ① **60 秒硬逾時**（main.py:312 `asyncio.wait_for(scraper.scrape(url), timeout=60)`）
     只剩 27 秒額度，每個額外頁面 8～18 秒 → 3 色×3 尺寸需要 3 個 page load，
     33 + 2×(8～18) = 49～69 秒，**會經常爆**。5 色×6 尺寸必定逾時。

  ② **_driver_lock 是全域單一鎖**（generic.py:140），這才是真正貴的地方。
     MAX_CONCURRENT_SCRAPES=3 只是三個 asyncio 名額；一筆 MUJI 多變體會把
     driver 鎖住 N×8～18 秒，**同時段其他要用 Selenium 的客人全部卡住**，
     SCRAPE_QUEUE_TIMEOUT=90 秒後回 503。同期 log 裡 rakuten / yodobashi /
     revolve 都是 30～40 秒等級的 Selenium 爬取，會被一筆 MUJI 訂單擋掉。

  要做多變體得先把變體抓取**移出請求路徑**（建完商品後非同步補齊之類），
  不是在現在這條路徑上多打幾次。
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
