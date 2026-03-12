"""
scrapers/adidas.py — Adidas Japan (adidas.jp) Mixin

架構與 humanmade.py 完全相同：
  SeleniumBase UC driver → driver.page_source → BeautifulSoup 解析

策略：
  1. 先打隱藏 REST API /api/products/{model} + /availability
  2. API 失敗 → UC driver 取 HTML → BeautifulSoup 解析
     adidas.jp 是 React SPA，需等尺寸 AAA placeholder 消失
"""

import re
import time as _time
import requests
from typing import Optional

from scrapers.base import ProductInfo, normalize_price


# ── API ──────────────────────────────────────────────────────

_API_PRODUCT = "https://www.adidas.jp/api/products/{model}"
_API_AVAIL   = "https://www.adidas.jp/api/products/{model}/availability"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": "https://www.adidas.jp/",
}


def _extract_model(url: str) -> Optional[str]:
    m = re.search(r"/([A-Z]{2}[0-9]{4})\.html", url)
    return m.group(1) if m else None


def _hires(src: str) -> str:
    """換成 h_2000 高解析版。"""
    return re.sub(r"[hw]_\d+", "h_2000", src)


def _fetch_json(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[AdidasJP] API fetch failed {url}: {e}")
        return None


def _parse_via_api(model: str, source_url: str) -> Optional[ProductInfo]:
    """API 路徑，失敗回傳 None。"""
    data = _fetch_json(_API_PRODUCT.format(model=model))
    if not data or not data.get("name"):
        return None

    title      = data.get("name", "")
    color_name = data.get("attribute_list", {}).get("color", "")
    desc       = data.get("product_description", {}).get("description", "") or ""
    price_jpy  = normalize_price(
        data.get("pricing_information", {}).get("standard_price", 0)
    ) or 0

    # 圖片
    image_url, extra_images = "", []
    for view in data.get("view_list", []):
        src = _hires(view.get("image_url", ""))
        if not src:
            continue
        if not image_url:
            image_url = src
        else:
            extra_images.append(src)

    if not image_url:
        image_url = (
            f"https://assets.adidas.com/images/h_2000,f_auto,q_auto,"
            f"fl_lossy,c_fill,g_auto/{model}_21_model.jpg"
        )

    # 庫存
    in_stock_sizes: set[str] = set()
    avail = _fetch_json(_API_AVAIL.format(model=model))
    for v in (avail or data).get("variation_list", []):
        if int(v.get("availability", 0)) > 0:
            in_stock_sizes.add(v.get("size", ""))

    # variants
    seen: list[str] = []
    variants: list[dict] = []
    for v in data.get("variation_list", []):
        sz = v.get("size", "")
        if not sz or sz in seen:
            continue
        seen.append(sz)
        variants.append({
            "color":    color_name or "Default",
            "size":     sz,
            "price":    price_jpy,
            "in_stock": sz in in_stock_sizes,
        })

    if not variants:
        return None

    return ProductInfo(
        title        = f"{title} — {color_name}" if color_name else title,
        price_jpy    = price_jpy,
        image_url    = image_url,
        extra_images = extra_images,
        description  = desc,
        source_url   = source_url,
        brand        = "adidas",
        currency     = "JPY",
        variants     = variants,
        in_stock     = any(v["in_stock"] for v in variants),
    )


def _parse_html(html: str, source_url: str) -> ProductInfo:
    """
    BeautifulSoup 解析 adidas.jp React SPA 頁面。
    HTML 必須已等待 JS 渲染完成（AAA 消失後）。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    product = ProductInfo(source_url=source_url, brand="adidas", currency="JPY")

    # ── 標題 ──
    el = soup.find("h1", {"data-auto-id": "product-title"})
    if el:
        product.title = el.get_text(strip=True)

    # ── 顏色 ──
    # adidas.jp 實際 DOM：<div data-auto-id="color-label">Sandy Pink</div>
    color_name = ""
    el = soup.find(attrs={"data-auto-id": "color-label"})
    if el:
        color_name = el.get_text(strip=True)
    if product.title and color_name:
        product.title = f"{product.title} — {color_name}"

    # ── 價格 ──
    for sel in [
        {"data-auto-id": "product-price"},
        {"class": re.compile(r"product-price|price__value", re.I)},
    ]:
        el = soup.find(attrs=sel)
        if el:
            val = normalize_price(el.get_text(strip=True))
            if val and val >= 100:
                product.price_jpy = val
                break

    # ── 圖片 ──
    # assets.adidas.com/images/ = 商品圖
    # brand.assets.adidas.com   = 導覽列 banner，排除
    # 同一張圖會出現帶/不帶 fl_lossy 兩個 URL，用 hash 去重
    imgs = []
    seen_hash: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "assets.adidas.com/images/" not in src:
            continue
        # 用 URL 裡的圖片 hash（32位英數字）當 key 去重
        m_hash = re.search(r'/([a-f0-9]{32}_\d+)/', src)
        img_key = m_hash.group(1) if m_hash else src
        if img_key in seen_hash:
            continue
        seen_hash.add(img_key)
        imgs.append(_hires(src))
        if len(imgs) >= 8:
            break

    if imgs:
        product.image_url    = imgs[0]
        product.extra_images = imgs[1:]

    # ── 尺寸 ──
    # adidas.jp 實際 DOM：
    #   <div data-auto-id="size-selector" role="radiogroup">
    #     <button role="radio" aria-label="Size: J/S"><span>J/S</span></button>
    #     <button role="radio" class="...unavailable..." aria-label="サイズ：J/XL は現在...">
    #       <span>J/XL</span></button>
    sizes: list[str] = []
    in_stock: set[str] = set()
    seen_sz: set[str] = set()

    container = soup.find(attrs={"data-auto-id": "size-selector"})
    btns = container.find_all("button", {"role": "radio"}) if container else []

    for btn in btns:
        span = btn.find("span")
        sz = span.get_text(strip=True) if span else btn.get_text(strip=True)
        if not sz or sz in seen_sz:
            continue
        seen_sz.add(sz)
        sizes.append(sz)
        # 缺貨判斷：class 含 "unavailable"
        btn_class = " ".join(btn.get("class", []))
        if "unavailable" not in btn_class:
            in_stock.add(sz)

    if not sizes:
        sizes    = ["One Size"]
        in_stock = {"One Size"}

    product.variants = [
        {
            "color":    color_name or "Default",
            "size":     sz,
            "price":    product.price_jpy or 0,
            "in_stock": sz in in_stock,
        }
        for sz in sizes
    ]
    product.in_stock = bool(in_stock)

    # ── 描述 ──
    el = soup.find(attrs={"data-auto-id": "product-description-content"})
    if el:
        product.description = el.get_text(separator="\n", strip=True)

    return product


# ── Mixin ─────────────────────────────────────────────────────

class AdidasMixin:

    async def _scrape_adidas(self, url: str) -> ProductInfo:
        model = _extract_model(url)

        # Step 1：API
        if model:
            print(f"[AdidasJP] API scrape: model={model}")
            result = _parse_via_api(model, url)
            if result and result.is_valid:
                print(
                    f"[AdidasJP] ✅ API OK → {result.title} "
                    f"¥{result.price_jpy} / {len(result.variants)} variants / "
                    f"images={1 + len(result.extra_images)}"
                )
                return result
            print(f"[AdidasJP] API incomplete, fallback to UC driver")

        # Step 2：UC driver fallback
        html = await self._adidas_fetch_html(url)
        if not html:
            print(f"[AdidasJP] ❌ 無法取得 HTML: {url}")
            return ProductInfo(source_url=url, brand="adidas")

        try:
            product = _parse_html(html, url)
            print(
                f"[AdidasJP] ✅ {product.title} / ¥{product.price_jpy} / "
                f"{len(product.variants)} variants / images={1 + len(product.extra_images)}"
            )
            return product
        except Exception as e:
            print(f"[AdidasJP] ❌ 解析失敗 {url}: {type(e).__name__}: {e}")
            return ProductInfo(source_url=url, brand="adidas")

    async def _adidas_fetch_html(self, url: str) -> str | None:
        """UC driver 取得 JS 渲染後的 HTML，等待 AAA 消失。"""
        with self._driver_lock:
            for attempt in range(2):
                try:
                    driver = self._ensure_driver()
                    if not driver:
                        return None

                    self._driver_use_count += 1
                    self._clean_driver_tabs()

                    try:
                        driver.uc_open_with_reconnect(url, reconnect_time=6)
                    except Exception as e:
                        if "InvalidSession" in type(e).__name__ or "invalid session" in str(e).lower():
                            self._driver = None
                            self._create_driver()
                            continue

                    html = ""
                    session_dead = False

                    for i in range(12):  # 最多等 24 秒
                        _time.sleep(2)

                        # 第一次等待後，用 JS scroll 觸發 React lazy render
                        if i == 1:
                            try:
                                driver.execute_script(
                                    "window.scrollTo(0, document.body.scrollHeight / 3);"
                                )
                            except Exception:
                                pass

                        try:
                            html = driver.page_source
                        except Exception as e:
                            if "InvalidSession" in type(e).__name__:
                                session_dead = True
                                break
                            continue

                        if not html or len(html) < 5000:
                            continue

                        # adidas.jp React SPA：
                        # 尺寸按鈕一開始是 "AAA" placeholder，
                        # 等 JS 執行完才會變成真實尺寸
                        has_title = 'data-auto-id="product-title"' in html
                        has_price = '¥' in html
                        # AAA 在 HTML 裡是連續多個 "AAA"，真實尺寸不會是 AAA
                        aaa_gone  = 'AAA</button>' not in html or i >= 8

                        if has_title and has_price and aaa_gone:
                            return html

                    if session_dead:
                        self._driver = None
                        self._create_driver()
                        continue

                    # 超時但有拿到 HTML，還是回傳試試
                    if html and len(html) > 5000:
                        print(f"[AdidasJP] ⚠️ 超時但仍回傳 HTML（AAA 可能未消失）")
                        return html

                    return None

                except Exception as e:
                    if "InvalidSession" in type(e).__name__ and attempt == 0:
                        self._driver = None
                        self._create_driver()
                        continue
                    print(f"[AdidasJP] fetch 失敗 attempt={attempt}: {e}")
                    return None

        return None
