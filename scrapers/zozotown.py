"""
ZOZOTOWN 爬蟲 Mixin（v2 — 2026 改走雅虎店 SSR，繞過 Akamai）
====================================================================
背景：
- zozo.jp 受 Akamai Bot Manager 保護，機房 IP 幾乎必擋，舊版靠 SeleniumBase UC
  仍時常失敗，且吃 RAM。
- 突破點：ZOZOTOWN 在 Yahoo!ショッピング 有官方店（seller_id=zozo），而且
  **zozo.jp 的 goods ID == 雅虎店商品 key**（已實測：106919347 兩邊一致）。
  雅虎店商品頁是乾淨 SSR、meta 標籤就含價格/標題/圖/品番/カラー/サイズ，
  且無 Akamai、無需 appid、無需 Selenium。

策略：
1. 從客人貼的 zozo.jp 網址抽 goods ID
2. 組 https://store.shopping.yahoo.co.jp/zozo/<goodsID>.html
3. httpx 抓該頁，解析 OpenGraph / product meta
4. 雅虎店查無此商品（目錄為 zozo.jp 子集）→ 退回舊版 zozo.jp 爬蟲 _scrape_zozotown_legacy

注意（價格來源切換）：
- 抓到的價格是「雅虎店價格」（含可能的セール価格），不一定等於 zozo.jp 價格。
  多數情況相同或更低（雅虎還有 PayPay 點數）。買手若改從雅虎店下單，報價即一致。
- source_url 預設指向雅虎店連結（與價格一致的下單來源）。
  若你希望保留客人原本的 zozo.jp 連結，把 USE_YAHOO_AS_SOURCE 設 False。
"""
import re
import asyncio

import httpx
from bs4 import BeautifulSoup

from config import SCRAPE_TIMEOUT, USER_AGENT, PROXY_URL
from scrapers.base import ProductInfo


_MIN_PRICE = 50
_MAX_PRICE = 2_000_000

# 報價與下單來源是否切到雅虎店（True=source_url 用雅虎連結，與抓到的價格一致）
# 報價/資料一律取自雅虎店頁面（繞過 Akamai）；source_url 則用客人原本的 zozo.jp 連結
#   False = source_url 用 zozo.jp（顯示在 Shopify、前端也送 zozo.jp → 與 scrape 快取同 key，
#           變體不會因重爬而遺失）。價格仍是雅虎店價（有價差由人工自行去雅虎店下單）。
USE_YAHOO_AS_SOURCE = False

_YAHOO_STORE = "https://store.shopping.yahoo.co.jp/zozo/{gid}.html"


class ZozotownMixin:

    async def _scrape_zozotown(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="")

        gid = self._zozo_extract_goods_id(url)
        if not gid:
            print(f"[ZOZO] ❌ 無法從 URL 抽 goods ID: {url}")
            # 仍嘗試舊版（可能是非 goods 頁）
            return await self._zozo_legacy_or_self(url, product)

        print(f"[ZOZO] goods ID: {gid} → 改走雅虎店")
        ok = await self._zozo_via_yahoo(url, gid, product)
        if ok and product.is_valid:
            return product

        # 雅虎店查無此商品（zozo.jp 子集）或抓取失敗 → 退回舊版 zozo.jp 爬蟲
        print(f"[ZOZO] 雅虎店無此商品或抓取失敗，退回舊版 zozo.jp 爬蟲")
        return await self._zozo_legacy_or_self(url, product)

    async def _zozo_legacy_or_self(self, url: str, product: ProductInfo) -> ProductInfo:
        """有舊版 _scrape_zozotown_legacy 就用，否則回傳目前 product（可能 invalid）。"""
        if hasattr(self, "_scrape_zozotown_legacy"):
            try:
                return await self._scrape_zozotown_legacy(url)
            except Exception as e:
                print(f"[ZOZO] legacy 失敗: {type(e).__name__}: {e}")
        return product

    # ─────────────────────────────────────────────────────────────────
    # goods ID 抽取
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _zozo_extract_goods_id(url: str) -> str:
        # zozo.jp/shop/<brand>/goods/<id>/  或  goods-sale/<id>/
        m = re.search(r'/goods(?:-sale)?/(\d{4,})', url)
        if m:
            return m.group(1)
        # 已經是雅虎店連結 store.shopping.yahoo.co.jp/zozo/<id>.html
        m = re.search(r'/zozo/(\d{4,})\.html', url)
        if m:
            return m.group(1)
        return ""

    # ─────────────────────────────────────────────────────────────────
    # 雅虎店抓取（httpx，SSR）
    # ─────────────────────────────────────────────────────────────────
    async def _zozo_via_yahoo(self, zozo_url: str, gid: str, product: ProductInfo) -> bool:
        yahoo_url = _YAHOO_STORE.format(gid=gid)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        }
        proxy_arg = PROXY_URL if PROXY_URL else None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_arg) as client:
                resp = await client.get(yahoo_url, headers=headers)
                print(f"[ZOZO] 雅虎店 {yahoo_url} → {resp.status_code}, {len(resp.text)} bytes")
                if resp.status_code != 200 or not resp.text:
                    return False
                self._zozo_parse_yahoo(resp.text, zozo_url, yahoo_url, gid, product)
        except Exception as e:
            print(f"[ZOZO] httpx 錯誤: {type(e).__name__}: {e}")
            return False

        return bool(product.price_jpy)

    def _zozo_parse_yahoo(self, html: str, zozo_url: str, yahoo_url: str,
                          gid: str, product: ProductInfo) -> None:
        soup = BeautifulSoup(html, "html.parser")

        def meta(prop=None, name=None):
            if prop:
                el = soup.find("meta", attrs={"property": prop})
                if el and el.get("content"):
                    return el["content"].strip()
            if name:
                el = soup.find("meta", attrs={"name": name})
                if el and el.get("content"):
                    return el["content"].strip()
            return ""

        # ── 標題：og:title 去掉「 : ZOZOTOWN Yahoo!店 …」尾巴 ──
        title = meta(prop="og:title")
        title = re.split(r'\s*[:：]\s*ZOZOTOWN\s*Yahoo', title)[0].strip()
        if title:
            product.title = title

        # ── 價格：product:price:amount（税込；可能是セール価格）──
        price = meta(prop="product:price:amount") or meta(name="product:price:amount")
        v = self._zozo_to_int(price)
        if v:
            product.price_jpy = v

        # ── 主圖：og:image ──
        img = meta(prop="og:image")
        if img:
            product.image_url = img

        # ── og:description 解析（品番 / カラー / サイズ / 素材 等）──
        desc = meta(prop="og:description")
        fields = self._zozo_parse_desc(desc)

        # 品牌
        brand = fields.get("ブランド", "")
        if brand:
            product.brand = brand.split("，")[0].split(",")[0].strip()

        # 描述：商品名 + 素材 + 原産国 + 品番
        desc_bits = []
        if fields.get("商品名"):
            desc_bits.append(fields["商品名"])
        if fields.get("素材"):
            desc_bits.append(f"素材：{fields['素材']}")
        if fields.get("原産国"):
            desc_bits.append(f"原産国：{fields['原産国']}")
        if fields.get("ブランド品番"):
            desc_bits.append(f"品番：{fields['ブランド品番']}")
        product.description = "｜".join(desc_bits)

        # ── 變體：カラー × サイズ ──
        colors = self._zozo_split_multi(fields.get("カラー", ""))
        sizes = self._zozo_split_multi(fields.get("サイズ", ""))
        product.variants = self._zozo_build_variants(colors, sizes, gid, product.price_jpy)

        # ── 額外圖片：body 內同商品的 _N_d_500.jpg ──
        product.extra_images = self._zozo_extra_images(soup, product.image_url)

        # ── 下單來源 ──
        product.source_url = yahoo_url if USE_YAHOO_AS_SOURCE else zozo_url

        if product.is_valid:
            print(f"[ZOZO] ✅ {product.title[:50]!r} | ¥{product.price_jpy:,} | "
                  f"brand={product.brand!r} | variants={len(product.variants)} | "
                  f"images={1 + len(product.extra_images)}")

    # ─────────────────────────────────────────────────────────────────
    # 解析 helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _zozo_parse_desc(desc: str) -> dict:
        """og:description 以 <br> 分段，每段「key:value」拆成 dict。"""
        out = {}
        if not desc:
            return out
        for seg in re.split(r'<br\s*/?>', desc):
            seg = seg.strip()
            if not seg:
                continue
            m = re.match(r'([^:：]+)[:：](.*)', seg)
            if m:
                out[m.group(1).strip()] = m.group(2).strip()
        return out

    @staticmethod
    def _zozo_split_multi(s: str) -> list:
        """カラー/サイズ 以全角／半角逗號分隔。"""
        if not s:
            return []
        parts = re.split(r'[，,]', s)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _zozo_build_variants(colors: list, sizes: list, gid: str, price) -> list:
        # 單色單尺寸（或皆無）→ 視為單品，不建變體
        eff_colors = colors or [""]
        eff_sizes = sizes or [""]
        if len(eff_colors) <= 1 and len(eff_sizes) <= 1:
            return []
        variants = []
        for c in eff_colors:
            for s in eff_sizes:
                label = "-".join([p for p in (c, s) if p])
                variants.append({
                    "color": c,
                    "size": s,
                    "sku": f"{gid}-{label}" if label else gid,
                    "price": price or 0,
                    "in_stock": True,   # 雅虎 meta 無逐變體庫存，預設 True
                    "image": "",
                })
        return variants

    @staticmethod
    def _zozo_extra_images(soup: BeautifulSoup, main: str) -> list:
        urls = []
        for tag in soup.find_all("img"):
            src = tag.get("src") or ""
            if "z-shopping.c.yimg.jp" in src and src != main and src not in urls:
                urls.append(src)
            if len(urls) >= 8:
                break
        return urls

    @staticmethod
    def _zozo_to_int(value) -> int | None:
        if value is None:
            return None
        s = re.sub(r'[^0-9]', '', str(value))
        if not s:
            return None
        try:
            v = int(s)
        except ValueError:
            return None
        return v if _MIN_PRICE <= v <= _MAX_PRICE else None
