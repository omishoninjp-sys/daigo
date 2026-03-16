"""
在本地跑這個腳本，確認 ec-store.net 的實際 HTML 結構
用法：python debug_ecstore.py
"""
import time
from bs4 import BeautifulSoup
from seleniumbase import SB

URL = "https://www.ec-store.net/sws/g/g13264616/"

with SB(headless=True, uc=True) as sb:
    sb.open(URL)
    time.sleep(3)

    # 捲動觸發 lazy load
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)

    html = sb.get_page_source()
    soup = BeautifulSoup(html, "html.parser")

    print("=" * 60)
    print("1. H1 tag")
    print("=" * 60)
    for h1 in soup.find_all("h1"):
        print(f"  class={h1.get('class')} id={h1.get('id')}")
        print(f"  text: {h1.get_text(strip=True)[:80]}")

    print("\n" + "=" * 60)
    print("2. BREADCRUMB（麵包屑）")
    print("=" * 60)
    for sel in ["ol.breadcrumb li", "ul.breadcrumb li", ".breadcrumb a",
                "#breadcrumb li", "#breadcrumb a", ".bread li", ".bread a",
                "nav a", "[class*='bread']"]:
        els = soup.select(sel)
        if els:
            print(f"  ✅ {sel}: {[e.get_text(strip=True) for e in els[:6]]}")

    print("\n" + "=" * 60)
    print("3. PRICE elements")
    print("=" * 60)
    for sel in ["[itemprop='price']", ".item-price", ".goods-price",
                ".price", "#price", "[class*='price']", "[id*='price']"]:
        el = soup.select_one(sel)
        if el:
            print(f"  ✅ {sel}: content={el.get('content')} text={el.get_text(strip=True)[:50]}")

    print("\n" + "=" * 60)
    print("4. IMAGES（取前 10 張）")
    print("=" * 60)
    count = 0
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src", "")
        if src and "lazyloading" not in src and src.startswith("http"):
            print(f"  class={img.get('class')} src={src[:90]}")
            count += 1
            if count >= 10:
                break
    if count == 0:
        print("  ⚠️ 沒有非 lazy 的圖片，印出前 5 個 img tag：")
        for img in soup.find_all("img")[:5]:
            print(f"    {img}")

    print("\n" + "=" * 60)
    print("5. SELECT elements（顏色/尺寸下拉）")
    print("=" * 60)
    for sel in soup.find_all("select"):
        print(f"  <select name='{sel.get('name')}' id='{sel.get('id')}' class='{sel.get('class')}'>")
        for opt in sel.find_all("option")[:8]:
            print(f"    value='{opt.get('value')}' → {opt.get_text(strip=True)}")

    print("\n" + "=" * 60)
    print("6. RADIO buttons（顏色/尺寸）")
    print("=" * 60)
    for radio in soup.find_all("input", {"type": "radio"})[:10]:
        print(f"  name={radio.get('name')} value={radio.get('value')} class={radio.get('class')}")

    print("\n" + "=" * 60)
    print("7. DESCRIPTION candidates")
    print("=" * 60)
    for sel in ["[itemprop='description']", ".item-detail", ".goods-detail",
                ".item-description", "#item-description", ".description",
                ".detail-text", "#detail", "[class*='detail']", "[id*='detail']"]:
        el = soup.select_one(sel)
        if el:
            print(f"  ✅ {sel}: {el.get_text(strip=True)[:120]}")

    print("\n" + "=" * 60)
    print("8. JSON-LD")
    print("=" * 60)
    import json
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string or "")
            print(json.dumps(d, ensure_ascii=False, indent=2)[:1000])
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("9. 所有含 'color'/'colour'/'size'/'variant' 的 class 或 id")
    print("=" * 60)
    for el in soup.find_all(True):
        classes = " ".join(el.get("class") or [])
        eid = el.get("id", "")
        combined = (classes + " " + eid).lower()
        if any(k in combined for k in ["color", "colour", "size", "variant", "sku"]):
            print(f"  <{el.name} class='{classes}' id='{eid}'> {el.get_text(strip=True)[:50]}")
