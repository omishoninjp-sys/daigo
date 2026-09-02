"""
Amazon.co.jp 爬蟲 Mixin
使用 requests + BeautifulSoup（快速、不需瀏覽器）

代購原則：貼一個網址＝要一個商品。
只認 URL 的那一個 ASIN，建「單一規格」商品；不做多規格展開。
（移除舊版的 variants 抓取——它會「列出所有規格卻共用同一個價」，
  例如把『5本組合／全種セット』標成單條的 ¥1,260，造成請款金額錯位。）
"""
import re

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


# ── 爬取監控埋點（fail-safe：監控壞掉絕不影響爬取）──────────────────
# ★ 2026-09-02 補。Amazon 佔營收 10.6%，但七條失敗路徑一條都沒有交給監控，
#   其中 A2（URL 不含 ASIN）與 A5（被導向登入頁）**連 print 都沒有**，
#   連 Zeabur log 都查不到。record() 只會留下 error_brief='' 的空紀錄。
def _note_error(error, where=""):
    try:
        import scrape_monitor
        scrape_monitor.note_error(error, where)
    except Exception:
        pass


def _note_http(status, body="", final_url=""):
    """★ 少了這個，403/429 會被 classify_failure 分成 other 而不是 blocked ——
    blocked 這個分類存在的目的就是回答「要不要買住宅代理」。"""
    try:
        import scrape_monitor
        scrape_monitor.note_http(status, body, final_url)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# brand 抽取
# ══════════════════════════════════════════════════════════════════════
# ★ 2026-09-02 重寫。舊版是
#     el = soup.select_one("#bylineInfo") or soup.select_one(".po-brand .po-break-word")
#     b  = re.sub(r'^(ブランド[：:]|Brand[：:]|Visit the |のストアを表示)', '', b)
#   兩個問題：
#   1. **選擇器優先序反了。** `#bylineInfo` 是標題底下那一行，內容可能是品牌、
#      賣家店鋪、或書籍作者＋格式；`.po-brand .po-break-word` 是商品規格表的
#      品牌欄，本來就是乾淨的品牌名。應該優先拿乾淨的那個，不是去洗髒的那個。
#   2. `のストアを表示` 是**後綴**，卻被放進前綴錨定 `^(...)` 的 alternation，
#      永遠剝不掉。前後綴一定要分成兩個 regex。
#
#   實測 118 件 Amazon 商品，79 件（67%）的 brand 被污染，四種形態：
#     京セラ(Kyocera)のストアを表示          38 件
#     访问 コミネ(KOMINE) 品牌旗舰店          21 件   ← Amazon 會依 URL 語系回簡中
#     品牌：Eye coffret                    13 件
#     白浜鴎(著)形式:単行本                    7 件   ← 這種根本不是品牌
#
#   brand 錯了會同時進標題、tags 與 Shopify 的 vendor 欄位。

# 這不是品牌，是作者／演出者／商品格式 —— 命中就讓 brand 留空。
# ★ 剝掉後綴之後剩下的「白浜鴎」仍然不該當品牌；音樂那種多人串接
#   （…(アーティスト),…(演奏),…(作曲)）根本不存在可取出的品牌。
#   留空的話 vendor 會 fallback 成「代購商品」，標題也不會有奇怪前綴 ——
#   比填一個錯的好：錯的 brand 會同時污染標題、tags、vendor 三個地方。
_BRAND_NOT_A_BRAND = re.compile(
    r"\(著\)|\(編\)|\(訳\)|\(作者\)|"
    r"\((?:Artist|Composer|Conductor|Performer|アーティスト|演奏|作曲|指揮|"
    r"艺术家|表演者|作曲家|指揮者)[^)]*\)|"
    r"形式[:：]|Format[:：]|格式[:：]")

# 前綴（錨定 ^）：日／英／簡中三種語系
_BRAND_PREFIX = re.compile(
    r"^(?:ブランド|品牌名|品牌|Brand)\s*[：:]\s*|"
    r"^Visit\s+the\s+|^访问\s+|^訪問\s+")

# 後綴（錨定 $）：★ 舊版把這些誤放進前綴 alternation，這就是那個 bug
_BRAND_SUFFIX = re.compile(
    r"\s*(?:のストアを表示|のストア|ブランドストア|"
    r"品牌旗舰店|品牌旗艦店|品牌店|Store|ストア)\s*$")

_BRAND_MAX_LEN = 40


def _clean_brand(raw: str) -> str:
    """把 byline / 規格表的原始文字洗成品牌名；洗不出來就回空字串。"""
    b = (raw or "").strip()
    if not b:
        return ""
    if _BRAND_NOT_A_BRAND.search(b):
        return ""
    b = _BRAND_PREFIX.sub("", b)
    b = _BRAND_SUFFIX.sub("", b).strip()
    # 最後一道保險：過長或含逗號代表多值串接，不是單一品牌
    if not b or len(b) > _BRAND_MAX_LEN or "," in b or "，" in b:
        return ""
    return b


def _extract_brand(soup) -> str:
    """
    ★ 先拿規格表的品牌欄（乾淨），沒有才退回 bylineInfo（要洗）。
      順序不可以反過來 —— 那等於明明有乾淨來源卻去洗髒的。
    """
    el = soup.select_one(".po-brand .po-break-word")
    if el:
        b = _clean_brand(el.get_text(strip=True))
        if b:
            return b
    el = soup.select_one("#bylineInfo")
    if el:
        return _clean_brand(el.get_text(strip=True))
    return ""


class AmazonMixin:

    async def _scrape_amazon(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        try:
            # 短連結展開
            if "amzn.asia" in url or "amzn.to" in url:
                _asin_pattern = r'/(?:dp|gp/product|gp/aw/d|ASIN)/([A-Z0-9]{10})'
                _desktop_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
                    resp = await c.get(url, headers={"User-Agent": _desktop_ua})
                    all_urls = [str(r.url) for r in resp.history] + [str(resp.url)]
                    print(f"[Amazon] redirect chain: {all_urls}")
                    found_asin = None
                    for _u in all_urls:
                        _m = re.search(_asin_pattern, _u)
                        if _m:
                            found_asin = _m.group(1)
                            break
                if found_asin:
                    url = f"https://www.amazon.co.jp/dp/{found_asin}"
                    print(f"[Amazon] 短連結展開 → {url}")
                else:
                    url = str(resp.url)
                    print(f"[Amazon] 短連結展開 (無法提取 ASIN): {url}")
                    # ★ 這裡不是失敗，是**降級繼續**：拿轉址後的 url 硬跑下去。
                    #   後面所有結果都建立在這個猜測上，所以即使最後成功，
                    #   紀錄裡也要留下「這筆的 url 是猜的」。
                    _note_error(f"短連結展開後抽不到 ASIN，改用轉址後網址繼續: {url[:80]}",
                                "Amazon")
                product.source_url = url

            am = re.search(r'/(?:dp|gp/product|gp/aw/d|ASIN)/([A-Z0-9]{10})', url)
            if not am:
                # ★ 原本完全靜默（連 print 都沒有）——
                #   客人貼搜尋頁或分類頁時就走這裡，而 log 上一片空白。
                print(f"[Amazon] ❌ URL 不含 ASIN: {url[:90]}")
                _note_error("URL 不含 ASIN（/dp/、/gp/product/、/gp/aw/d/、/ASIN/ 都沒有），"
                            "可能是搜尋頁或分類頁", "Amazon")
                return product

            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
                "Referer": "https://www.amazon.co.jp/",
                "Upgrade-Insecure-Requests": "1",
            }

            cookies_base = {
                "i18n-prefs": "JPY",
                "lc-acbjp": "ja_JP",
                "sp-cdn": '"L5Z9:JP"',
                "mature-content-preference": "1",
                "ubid-acbjp": "355-0769823-1641625",
            }
            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=False, cookies=cookies_base) as client:
                resp = await client.get(url, headers=headers)
                for _ in range(5):
                    if resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = resp.headers.get("location", "")
                    if not location:
                        break
                    if not location.startswith("http"):
                        location = "https://www.amazon.co.jp" + location
                    if "black-curtain" in location:
                        ru = re.search(r'returnUrl=([^&]+)', location)
                        if ru:
                            import urllib.parse
                            return_path = urllib.parse.unquote(ru.group(1))
                            asin_m = re.search(r'/dp/([A-Z0-9]{10})', return_path)
                            if asin_m:
                                asin_val = asin_m.group(1)
                                direct_url = f"https://www.amazon.co.jp/dp/{asin_val}"

                                bc_resp = await client.get(location, headers=headers, follow_redirects=True)
                                bc_soup = BeautifulSoup(bc_resp.text, "html.parser")

                                hai_link = None
                                for a in bc_soup.find_all('a'):
                                    if 'はい' in a.get_text():
                                        hai_link = a.get('href', '')
                                        break
                                if not hai_link:
                                    form = bc_soup.find('form')
                                    if form:
                                        hai_link = form.get('action', '')

                                if hai_link:
                                    if not hai_link.startswith('http'):
                                        hai_link = 'https://www.amazon.co.jp' + hai_link
                                    await client.get(hai_link, headers=headers, follow_redirects=True)
                                    print(f"[Amazon] はい クリック → {hai_link[:80]}")

                                print(f"[Amazon] black-curtain 繞過 → {direct_url}")
                                resp = await client.get(direct_url, headers=headers, follow_redirects=False)
                                if resp.status_code in (301, 302) and "black-curtain" not in resp.headers.get("location", ""):
                                    resp = await client.get(resp.headers["location"], headers=headers, follow_redirects=True)
                                elif resp.status_code == 200:
                                    pass
                                break
                    resp = await client.get(location, headers=headers)
                _note_http(resp.status_code, resp.text[:500], str(resp.url))
                if resp.status_code != 200:
                    print(f"[Amazon] HTTP {resp.status_code}")
                    _note_error(f"HTTP {resp.status_code}", "Amazon")
                    return product
                if "captcha" in str(resp.url).lower():
                    # ★ 這裡看的是**最終網址**含不含 captcha，不是頁面內容 ——
                    #   CLAUDE.md 記載 "captcha" in html 會命中每一家 Shopify 商店。
                    print(f"[Amazon] CAPTCHA 偵測到")
                    _note_error(f"被導向 CAPTCHA 頁: {str(resp.url)[:80]}", "Amazon")
                    return product
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            if soup.find("form", {"name": "signIn"}) or soup.select_one("#ap_email"):
                # ★ 原本完全靜默。成人商品或地區限制會被導到登入頁。
                print(f"[Amazon] ❌ 被導向登入頁")
                _note_error("被導向登入頁（signIn 表單或 #ap_email）—— "
                            "成人商品或地區限制", "Amazon")
                return product

            el = soup.select_one("#productTitle")
            if el:
                product.title = el.get_text(strip=True)
            if not product.title:
                t = soup.find("title")
                if t:
                    txt = t.get_text(strip=True)
                    if "サインイン" not in txt and "Sign" not in txt:
                        product.title = txt

            product.brand = _extract_brand(soup)

            # ── 價格：只取「URL 這個 ASIN」的 Buy Box 價（單一規格，不跨規格展開）──
            for sel in [
                "#corePrice_feature_div .a-offscreen",
                "#corePrice_desktop .a-offscreen",
                "#buybox .a-price .a-offscreen",
                "#buyBoxAccordion .a-price .a-offscreen",
                "#newBuyBoxPrice",
                "#price",
                "span.a-price span.a-offscreen",
                ".a-price .a-offscreen",
                ".kindle-price",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                ".a-color-price",
                "#tmm-grid-swatch-PAPERBACK .a-button-selected .a-button-inner span",
            ]:
                el = soup.select_one(sel)
                if el:
                    raw_price = el.get_text(strip=True).replace('￥', '').replace('¥', '').replace(',', '').strip()
                    pm = re.search(r'\d+', raw_price)
                    if pm:
                        candidate = int(pm.group())
                        if candidate > 0:
                            product.price_jpy = candidate
                            break

            # 書籍/雜誌 fallback：從 HTML 直接搜價格 JSON
            if not product.price_jpy:
                m = re.search(r'"priceAmount"\s*:\s*([\d.]+)', html)
                if not m:
                    m = re.search(r'"buyingPrice"\s*:\s*([\d.]+)', html)
                if not m:
                    m = re.search(r'"price"\s*:\s*"[¥￥]([\d,]+)"', html)
                if m:
                    product.price_jpy = int(float(m.group(1).replace(',', '')))

            hi = re.findall(r'"hiRes"\s*:\s*"(https?://[^"]+)"', html)
            if hi:
                all_imgs = list(dict.fromkeys(hi))[:10]
                if all_imgs:
                    product.image_url = all_imgs[0]
                    product.extra_images = all_imgs[1:]
            else:
                el = soup.select_one("#landingImage")
                if el:
                    src = el.get("data-old-hires") or el.get("src", "")
                    if src:
                        product.image_url = src
                for img in soup.select("#altImages img"):
                    src = img.get("src", "")
                    if src and "sprite" not in src and "grey-pixel" not in src:
                        lg = re.sub(r'\._[^.]*_\.', '.', src)
                        if lg != product.image_url and lg not in product.extra_images:
                            product.extra_images.append(lg)

            bullets = soup.select("#feature-bullets li span.a-list-item")
            if bullets:
                product.description = "\n".join(
                    [b.get_text(strip=True) for b in bullets if len(b.get_text(strip=True)) > 2]
                )[:500]

            # 代購只要「這一個 ASIN」：不展開多規格。
            # （舊版在這裡用 dimensionValuesDisplayData / twisterData / inline-twister 等
            #   方法列出所有規格，但只有單一價可用，導致非預設規格被標成預設規格的價。
            #   代購本來就是貼一個網址＝要那一個 ASIN，故移除整段多規格抓取。）

            if product.price_jpy:
                print(f"[Amazon] ✅ {product.title[:40]} / ¥{product.price_jpy:,}")
            else:
                print(f"[Amazon] ⚠️ 價格未找到")
                # ★ 數字本身就是診斷資訊：全部落空代表 Amazon 改版，
                #   而不是某一個選擇器失效。
                _note_error("價格未找到（13 個 CSS 選擇器 + 3 個 regex 全落空，"
                            "疑似頁面改版）", "Amazon")

        except Exception as e:
            print(f"[Amazon] ❌ 錯誤: {e}")
            _note_error(e, "Amazon")

        return product
