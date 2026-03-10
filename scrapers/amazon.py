"""
Amazon.co.jp 爬蟲 Mixin
使用 requests + BeautifulSoup（快速、不需瀏覽器）
"""
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT
from scrapers.base import ProductInfo


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
                product.source_url = url

            am = re.search(r'/(?:dp|gp/product|gp/aw/d|ASIN)/([A-Z0-9]{10})', url)
            if not am:
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
                if resp.status_code != 200:
                    print(f"[Amazon] HTTP {resp.status_code}")
                    return product
                if "captcha" in str(resp.url).lower():
                    print(f"[Amazon] CAPTCHA 偵測到")
                    return product
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            if soup.find("form", {"name": "signIn"}) or soup.select_one("#ap_email"):
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

            el = soup.select_one("#bylineInfo") or soup.select_one(".po-brand .po-break-word")
            if el:
                b = el.get_text(strip=True)
                b = re.sub(r'^(ブランド[：:]\s*|Brand[：:]\s*|Visit the |のストアを表示)', '', b)
                product.brand = re.sub(r'\s*(Store|ストア)$', '', b).strip()

            for sel in [
                "#corePrice_feature_div .a-offscreen",
                "span.a-price span.a-offscreen",
                ".a-price .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
            ]:
                el = soup.select_one(sel)
                if el:
                    pm = re.search(r'[\d,]+', el.get_text(strip=True).replace('￥', '').replace('¥', ''))
                    if pm:
                        product.price_jpy = int(pm.group().replace(',', ''))
                        break

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

            # ── Variants（三種方法依序嘗試）
            import json as _json
            variants_found = []

            # 方法1：twisterData.dimensions（含全部選項和維度名稱）
            # 格式：{"dimensions": [{"name": "color_name", "values": [...], ...}]}
            twister_match = re.search(r'var twisterData\s*=\s*(\{.+?\})\s*;', html, re.DOTALL)
            if not twister_match:
                twister_match = re.search(r'"twisterData"\s*:\s*(\{.+?\})', html, re.DOTALL)
            if twister_match:
                try:
                    twister = _json.loads(twister_match.group(1))
                    dimensions = twister.get("dimensions", [])
                    print(f"[Amazon DEBUG] 方法1 dimensions: {[d.get('name') for d in dimensions]}")
                    color_vals, size_vals = [], []
                    for dim in dimensions:
                        name = dim.get("name", "").lower()
                        vals = [v.get("value", "") for v in dim.get("values", []) if v.get("value")]
                        if any(x in name for x in ["color", "colour", "カラー", "色", "style"]):
                            color_vals = vals
                        elif any(x in name for x in ["size", "サイズ", "寸"]):
                            size_vals = vals
                        elif not color_vals:
                            color_vals = vals
                    colors = color_vals or [""]
                    sizes  = size_vals  or [""]
                    for c in colors:
                        for s in sizes:
                            variants_found.append({"color": c, "size": s, "in_stock": True, "image": ""})
                    print(f"[Amazon DEBUG] 方法1: colors={color_vals} sizes={size_vals} → {len(variants_found)} variants")
                except Exception as e:
                    print(f"[Amazon DEBUG] 方法1 失敗: {e}")

            # 方法2：#variation_ DOM 元素
            if not variants_found:
                color_els = soup.select("#variation_color_name li, #variation_style_name li")
                size_els  = soup.select("#variation_size_name li, #variation_size_type_name li")
                colors = [el.get("data-value", el.get_text(strip=True)) for el in color_els] or [""]
                sizes  = [el.get("data-value", el.get_text(strip=True)) for el in size_els]  or [""]
                for c in colors:
                    for s in sizes:
                        variants_found.append({"color": c, "size": s, "in_stock": True, "image": ""})
                print(f"[Amazon DEBUG] 方法2: colors={colors} sizes={sizes}")

            # 方法3：li[id^=color_name_/size_name_]
            if not variants_found or all(not v["color"] and not v["size"] for v in variants_found):
                variants_found = []
                for el in soup.select("li[id^='color_name_'], li[id^='size_name_']"):
                    txt = el.get_text(strip=True)
                    vinfo = {"color": "", "size": "", "in_stock": True, "image": ""}
                    if re.match(r'^[\d\s./xcmXCM×]+$', txt):
                        vinfo["size"] = txt
                    else:
                        vinfo["color"] = txt
                    variants_found.append(vinfo)
                print(f"[Amazon DEBUG] 方法3: {len(variants_found)} variants" if variants_found else "[Amazon DEBUG] 找不到 variants")

            if variants_found:
                product.variants = variants_found

            print(f"[Amazon] ✅ {product.title[:40]} / ¥{product.price_jpy:,}" if product.price_jpy else f"[Amazon] ⚠️ 價格未找到")

        except Exception as e:
            print(f"[Amazon] ❌ 錯誤: {e}")

        return product
