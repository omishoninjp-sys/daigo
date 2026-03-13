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


def _extract_json_at_key(src: str, key: str):
    """
    用括號計數從 HTML 中安全提取指定 key 的 JSON object 或 array。
    比 regex 更可靠，不受巢狀 {} 影響。
    """
    import json as _j
    marker = f'"{key}"'
    pos = src.find(marker)
    if pos == -1:
        return None
    colon = src.find(':', pos + len(marker))
    if colon == -1:
        return None
    i = colon + 1
    while i < len(src) and src[i] in ' \t\r\n':
        i += 1
    if i >= len(src) or src[i] not in '{[':
        return None
    open_b = src[i]
    close_b = '}' if open_b == '{' else ']'
    depth = 0
    in_str = False
    escape = False
    for j in range(i, min(i + 500_000, len(src))):
        c = src[j]
        if escape:
            escape = False
            continue
        if c == '\\' and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == open_b:
            depth += 1
        elif c == close_b:
            depth -= 1
            if depth == 0:
                try:
                    return _j.loads(src[i:j + 1])
                except Exception:
                    return None
    return None


def _dim_looks_like_size(vals: list) -> bool:
    """
    判斷一個維度的值列表是否「看起來像尺寸」。
    - 含數字+cm/mm：Sサイズ 約43cm、30mm → True
    - 含 S/M/L/XL 等尺碼縮寫 → True
    - 含典型顏色詞（シルバー、ブラック、ホワイト、レッド...）→ False
    沒有明確特徵時回傳 False（預設當成 color）。
    """
    if not vals:
        return False

    SIZE_PATTERNS = [
        r'\d+\s*(?:cm|mm|inch|インチ)',   # 43cm, 30mm
        r'[SsMmLlXx]{1,3}サイズ',         # Sサイズ, XLサイズ
        r'^\s*[SsMmLlXx]{1,3}\s*$',       # 純粹 S / M / L / XL
        r'[SsMmLlXx]{1,3}\s*\d+',         # S43, L51
        r'^\s*\d+[./]\d+\s*$',            # 24.5 / 38/40
        r'^\s*\d+\s*$',                   # 純數字（腰圍、容量等）
    ]
    COLOR_WORDS = [
        "シルバー", "silver", "ゴールド", "gold",
        "ブラック", "black", "ホワイト", "white",
        "レッド", "red", "ブルー", "blue",
        "ピンク", "pink", "グレー", "grey", "gray",
        "グリーン", "green", "パープル", "purple",
        "ベージュ", "beige", "ブラウン", "brown",
        "オレンジ", "orange", "イエロー", "yellow",
        "ネイビー", "navy", "カーキ", "khaki",
        "クリア", "clear", "透明",
    ]

    size_score = 0
    color_score = 0
    for v in vals:
        vl = v.lower()
        for pat in SIZE_PATTERNS:
            if re.search(pat, v, re.IGNORECASE):
                size_score += 1
                break
        for cw in COLOR_WORDS:
            if cw.lower() in vl:
                color_score += 1
                break

    return size_score > color_score


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

            # ══════════════════════════════════════════════════════
            # Variants（依序嘗試，方法0成功就不繼續）
            # ══════════════════════════════════════════════════════
            import json as _json
            variants_found = []

            # ────────────────────────────────────────────────────
            # 方法0：dimensionValuesDisplayData（最可靠）
            # 直接列舉每個有效 ASIN 對應的實際選項組合，
            # 不依賴 DOM visibility，所有 variants 都在 JS 裡。
            # 格式：{ "ASIN": ["val_dim0", "val_dim1", ...], ... }
            # ────────────────────────────────────────────────────
            dvdd = _extract_json_at_key(html, "dimensionValuesDisplayData")
            print(f"[Amazon DEBUG] 方法0 dvdd entries={len(dvdd) if dvdd else 0}")

            if dvdd and isinstance(dvdd, dict):
                # 取得維度順序（dim_names[i] 對應 dvdd[asin][i]）
                dim_names = []

                # 優先從 twisterData.dimensions 取
                twister = _extract_json_at_key(html, "twisterData")
                if twister and isinstance(twister, dict):
                    for d in twister.get("dimensions", []):
                        dim_names.append(d.get("name", ""))

                # 備援：dimensionToDisplayNameMap 的 key 順序
                if not dim_names:
                    dtdnm = _extract_json_at_key(html, "dimensionToDisplayNameMap")
                    if dtdnm and isinstance(dtdnm, dict):
                        dim_names = list(dtdnm.keys())

                print(f"[Amazon DEBUG] 方法0 dim_names={dim_names}")

                # 判斷哪個 index 是 color，哪個是 size
                color_idx = -1
                size_idx = -1
                for i, name in enumerate(dim_names):
                    nl = name.lower()
                    if any(k in nl for k in ["color", "colour", "カラー", "色", "style"]):
                        color_idx = i
                    elif any(k in nl for k in ["size", "サイズ", "寸"]):
                        size_idx = i

                # 維度名稱判斷失敗時：先用值內容辨識，再用位置啟發式
                if color_idx == -1 and size_idx == -1:
                    sample = next(iter(dvdd.values()), [])
                    if isinstance(sample, list):
                        if len(sample) == 1:
                            # 單維度：看值像 size 還是 color
                            all_vals_0 = [str(v[0]) for v in dvdd.values() if isinstance(v, list) and len(v) > 0]
                            if _dim_looks_like_size(all_vals_0):
                                size_idx = 0
                            else:
                                color_idx = 0
                        elif len(sample) >= 2:
                            # 多維度：用值內容判斷每個維度
                            all_vals_0 = [str(v[0]) for v in dvdd.values() if isinstance(v, list) and len(v) > 0]
                            all_vals_1 = [str(v[1]) for v in dvdd.values() if isinstance(v, list) and len(v) > 1]
                            dim0_is_size  = _dim_looks_like_size(all_vals_0)
                            dim1_is_size  = _dim_looks_like_size(all_vals_1)
                            if dim0_is_size and not dim1_is_size:
                                size_idx  = 0
                                color_idx = 1
                            elif dim1_is_size and not dim0_is_size:
                                color_idx = 0
                                size_idx  = 1
                            else:
                                # 無法判斷，保留預設 0=color 1=size
                                color_idx = 0
                                size_idx  = 1
                    print(f"[Amazon DEBUG] 方法0 值內容辨識: color_idx={color_idx}, size_idx={size_idx}")

                for asin, vals in dvdd.items():
                    if not isinstance(vals, list):
                        continue
                    color, size = "", ""
                    if color_idx >= 0 and color_idx < len(vals):
                        color = str(vals[color_idx])
                    if size_idx >= 0 and size_idx < len(vals):
                        size = str(vals[size_idx])
                    # 單維度且 color_idx/size_idx 都沒設定時的 fallback
                    if not color and not size and len(vals) >= 1:
                        size = str(vals[0])
                    variants_found.append({
                        "color": color,
                        "size":  size,
                        "sku":   asin,   # ASIN 當 SKU
                        "in_stock": True,
                        "image": "",
                    })

                print(f"[Amazon DEBUG] 方法0: {len(variants_found)} variants "
                      f"(color_idx={color_idx}, size_idx={size_idx})")

            # ────────────────────────────────────────────────────
            # 方法1：twisterData.dimensions（cross-product，方法0失敗時）
            # ────────────────────────────────────────────────────
            if not variants_found:
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

            # ────────────────────────────────────────────────────
            # 方法2：inline-twister-row DOM
            # ────────────────────────────────────────────────────
            if not variants_found or all(not v["color"] and not v["size"] for v in variants_found):
                variants_found = []
                color_els = soup.select("#inline-twister-row-color_name li.inline-twister-swatch")
                size_els  = soup.select("#inline-twister-row-size_name li.inline-twister-swatch")
                colors, sizes = [], []
                EXCLUDE_COLOR = {"利用可能なオプションを表示", "すべてのオプションを表示", "展開", ""}
                for el in color_els:
                    if "swatch-prototype" in el.get("class", []): continue
                    txt = el.select_one(".swatch-title-text")
                    val = txt.get_text(strip=True) if txt else el.get_text(strip=True)
                    if val and val not in EXCLUDE_COLOR: colors.append(val)
                for el in size_els:
                    if "swatch-prototype" in el.get("class", []): continue
                    val = el.get_text(strip=True)
                    if val: sizes.append(val)
                print(f"[Amazon DEBUG] 方法2 inline-twister: colors={colors} sizes={sizes}")
                if colors or sizes:
                    for c in (colors or [""]):
                        for s in (sizes or [""]):
                            variants_found.append({"color": c, "size": s, "in_stock": True, "image": ""})

            # 方法2b：#variation_ DOM
            if not variants_found:
                size_vals = []
                for sel in [
                    "#variation_size_name ul li",
                    "#variation_size_name .a-button-text",
                    "#native_dropdown_selected_size_name option",
                    "[data-action='twister-swatch'][data-dp-url*='size'] span",
                ]:
                    els = soup.select(sel)
                    if els:
                        size_vals = [el.get("data-value", el.get_text(strip=True)).strip() for el in els if el.get_text(strip=True).strip()]
                        if size_vals:
                            print(f"[Amazon DEBUG] 方法2b size selector={sel!r}: {size_vals}")
                            break
                color_vals = []
                for sel in [
                    "#variation_color_name ul li",
                    "#variation_color_name .a-button-text",
                    "#native_dropdown_selected_color_name option",
                    "#variation_style_name ul li",
                ]:
                    els = soup.select(sel)
                    if els:
                        color_vals = [el.get("data-value", el.get("title", el.get_text(strip=True))).strip() for el in els if el.get("data-value") or el.get_text(strip=True).strip()]
                        if color_vals:
                            print(f"[Amazon DEBUG] 方法2b color selector={sel!r}: {color_vals}")
                            break
                colors = color_vals or [""]
                sizes  = size_vals  or [""]
                for c in colors:
                    for s in sizes:
                        variants_found.append({"color": c, "size": s, "in_stock": True, "image": ""})
                print(f"[Amazon DEBUG] 方法2b: colors={color_vals} sizes={size_vals} → {len(variants_found)} variants")

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
                print(f"[Amazon DEBUG] 方法3: {len(variants_found)} variants" if variants_found else "[Amazon DEBUG] 方法3: 找不到")

            # 方法4：variationDisplayLabels / dimensionValues
            if not variants_found or all(not v["color"] and not v["size"] for v in variants_found):
                variants_found = []
                color_vals, size_vals = [], []

                vdl_match = re.search(r'"variationDisplayLabels"\s*:\s*(\{.+?\})\s*,\s*"', html, re.DOTALL)
                if vdl_match:
                    try:
                        vdl = _json.loads(vdl_match.group(1))
                        for k, v in vdl.items():
                            k_lower = k.lower()
                            vals = list(v.values()) if isinstance(v, dict) else []
                            if any(x in k_lower for x in ["color", "colour", "style"]):
                                color_vals = [str(x) for x in vals]
                            elif "size" in k_lower:
                                size_vals = [str(x) for x in vals]
                        print(f"[Amazon DEBUG] 方法4a variationDisplayLabels: colors={color_vals} sizes={size_vals}")
                    except Exception:
                        pass

                if not color_vals and not size_vals:
                    dv_match = re.search(r'"dimensionValues"\s*:\s*(\[.+?\])\s*[,}]', html, re.DOTALL)
                    if dv_match:
                        try:
                            dv = _json.loads(dv_match.group(1))
                            for dim in dv:
                                name = dim.get("name", "").lower()
                                vals = dim.get("values", [])
                                if any(x in name for x in ["color", "colour", "style"]):
                                    color_vals = vals
                                elif "size" in name:
                                    size_vals = vals
                                elif not color_vals:
                                    color_vals = vals
                            print(f"[Amazon DEBUG] 方法4b dimensionValues: colors={color_vals} sizes={size_vals}")
                        except Exception:
                            pass

                if not color_vals and not size_vals:
                    for sel in ["#twister span.a-button-text", ".twisterSwatchText"]:
                        els = soup.select(sel)
                        if els:
                            txts = [e.get_text(strip=True) for e in els if e.get_text(strip=True)]
                            if txts:
                                color_vals = txts
                                print(f"[Amazon DEBUG] 方法4c button text: {txts}")
                                break

                colors = color_vals or [""]
                sizes  = size_vals  or [""]
                for c in colors:
                    for s in sizes:
                        variants_found.append({"color": c, "size": s, "in_stock": True, "image": ""})
                if variants_found and any(v["color"] or v["size"] for v in variants_found):
                    print(f"[Amazon DEBUG] 方法4: {len(variants_found)} variants")
                else:
                    variants_found = []
                    print(f"[Amazon DEBUG] 找不到 variants")

            if variants_found:
                product.variants = variants_found

            print(f"[Amazon] ✅ {product.title[:40]} / ¥{product.price_jpy:,}" if product.price_jpy else f"[Amazon] ⚠️ 價格未找到")

        except Exception as e:
            print(f"[Amazon] ❌ 錯誤: {e}")

        return product
