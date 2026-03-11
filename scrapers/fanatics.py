"""
Fanatics Japan (www.fanatics.jp) 爬蟲 Mixin
fanatics.jp 使用 Cloudflare Bot Management 保護。
策略：SeleniumBase UC 模式繞過 Cloudflare JS challenge，
      從 JSON-LD 取商品資訊，從 HTML size button 取 variants。
"""
import re
import json

from scrapers.base import ProductInfo


class FanaticsMixin:

    async def _scrape_fanatics(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, self._fanatics_sync, url
        )
        if result:
            product.title        = result.get("title", "")
            product.price_jpy    = result.get("price")
            product.image_url    = result.get("image", "")
            product.extra_images = result.get("extra_images", [])
            product.brand        = result.get("brand", "Fanatics")
            product.description  = result.get("description", "")
            product.variants     = result.get("variants", [])
        return product

    def _fanatics_sync(self, url: str):
        try:
            from seleniumbase import SB

            with SB(uc=True, headless=True, locale_code="ja") as sb:
                try:
                    sb.uc_open_with_reconnect(url, reconnect_time=6)
                except Exception:
                    sb.open(url)

                try:
                    sb.wait_for_element_present(
                        'h1, script#__NEXT_DATA__, script[type="application/ld+json"]',
                        timeout=20
                    )
                except Exception:
                    pass

                html  = sb.get_page_source()
                title = sb.get_title()

            print(f"[Fanatics] page title={title!r}, html_len={len(html)}")
            if "Access Denied" in title or "access denied" in title.lower():
                print(f"[Fanatics] ❌ Cloudflare 封鎖（title={title!r}）")
                # html 前 500 字看結構
                print(f"[Fanatics] HTML 片段: {html[:500]}")
                return None

            return self._parse_fanatics_html(html, url)

        except Exception as e:
            import traceback
            print(f"[Fanatics] ❌ SeleniumBase 錯誤: {type(e).__name__}: {e}")
            print(traceback.format_exc()[:800])
            return None

    def _parse_fanatics_html(self, html: str, url: str) -> dict | None:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        result = {
            "title": "",
            "price": None,
            "image": "",
            "extra_images": [],
            "brand": "Fanatics",
            "description": "",
            "variants": [],
        }

        # ── 方法 1: __NEXT_DATA__ ────────────────────────────────────
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                next_data = json.loads(next_script.string)
                props = next_data.get("props", {}).get("pageProps", {})
                product_data = (
                    props.get("product") or
                    props.get("productData") or
                    self._deep_find(props, "product")
                )
                if product_data and isinstance(product_data, dict):
                    parsed = self._parse_fanatics_product_json(product_data)
                    if parsed.get("title"):
                        print(f"[Fanatics] ✅ 從 __NEXT_DATA__ 解析成功 / ¥{parsed.get('price')} / {len(parsed.get('variants', []))} variants")
                        return parsed
            except Exception as e:
                print(f"[Fanatics] __NEXT_DATA__ 解析失敗: {e}")

        # ── 方法 2: JSON-LD ──────────────────────────────────────────
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Product", "IndividualProduct"):
                        continue

                    result["title"] = item.get("name", "")
                    result["description"] = item.get("description", "")

                    # 品牌
                    brand = item.get("brand", {})
                    if isinstance(brand, dict):
                        result["brand"] = brand.get("name", "Fanatics")
                    elif isinstance(brand, str):
                        result["brand"] = brand

                    # 圖片
                    imgs = item.get("image", [])
                    if isinstance(imgs, str):
                        imgs = [imgs]
                    if imgs:
                        result["image"] = imgs[0]
                        result["extra_images"] = imgs[1:5]

                    # 價格 ── fanatics.jp 可能是數字或字串，多方式嘗試
                    offers = item.get("offers") or item.get("Offers")
                    price = self._extract_price_from_offers(offers)
                    if price:
                        result["price"] = price
                    

                    if result["title"]:
                        # JSON-LD 沒有 variants，另外從 HTML 抓尺寸
                        result["variants"] = self._extract_fanatics_variants(
                            soup, result["price"] or 0, result["image"]
                        )
                        print(f"[Fanatics] ✅ 從 JSON-LD 解析成功 / ¥{result['price']} / {len(result['variants'])} variants")
                        return result

            except Exception as e:
                print(f"[Fanatics] JSON-LD 解析異常: {e}")
                continue

        # ── 方法 3: HTML fallback ─────────────────────────────────────
        for sel in ['h1[data-testid="product-title"]', 'h1.product-name', 'h1']:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text and "fanatics" not in text.lower():
                    result["title"] = text
                    break

        if not result["price"]:
            # 從頁面文字找 ¥ 數字
            for el in soup.find_all(string=re.compile(r'[¥￥]\s*[\d,]+')):
                m = re.search(r'[¥￥]\s*([\d,]+)', el)
                if m:
                    try:
                        p = int(m.group(1).replace(",", ""))
                        if p > 100:
                            result["price"] = p
                            break
                    except ValueError:
                        pass

        if result["title"]:
            result["variants"] = self._extract_fanatics_variants(
                soup, result["price"] or 0, result["image"]
            )
            print(f"[Fanatics] ✅ 從 HTML fallback 解析 / ¥{result['price']} / {len(result['variants'])} variants")
            return result

        print(f"[Fanatics] ❌ 所有解析方式均失敗")
        return None

    def _extract_price_from_offers(self, offers) -> int | None:
        """從 JSON-LD offers 物件取出價格（JPY）"""
        if not offers:
            return None

        if isinstance(offers, list):
            offers = offers[0]
        if not isinstance(offers, dict):
            return None

        def _parse_val(val):
            if val is None or val == "":
                return None
            try:
                import re as _re
                cleaned = str(val).replace(",", "").replace("\u00a5", "").replace("\uff65", "").strip()
                cleaned = _re.sub(r'\.\d+$', '', cleaned)
                p = int(float(cleaned))
                return p if p > 0 else None
            except (ValueError, TypeError):
                return None

        for field in ("price", "lowPrice", "highPrice", "Price", "LowPrice"):
            p = _parse_val(offers.get(field))
            if p:
                return p

        # fanatics.jp 特殊結構：priceSpecification.price
        price_spec = offers.get("priceSpecification")
        if isinstance(price_spec, dict):
            p = _parse_val(price_spec.get("price"))
            if p:
                return p
        elif isinstance(price_spec, list):
            for spec in price_spec:
                if isinstance(spec, dict):
                    p = _parse_val(spec.get("price"))
                    if p:
                        return p

        return None
    def _extract_fanatics_variants(self, soup, base_price: int, base_image: str) -> list:
        """從 HTML label.size-selector-button 抓尺寸，建立 variants"""
        variants = []

        # fanatics.jp 結構：
        #   label.size-selector-button.available   → 有貨
        #   label.size-selector-button.unavailable → 缺貨
        #   input[value="S"] 內含尺寸值
        labels = soup.select("label.size-selector-button")
        for label in labels:
            inp = label.find("input")
            if not inp:
                continue
            size = (inp.get("value") or "").strip()
            if not size:
                continue
            classes = label.get("class") or []
            in_stock = "unavailable" not in classes
            variants.append({
                "color": "",
                "size": size,
                "sku": f"fanatics-{size}",
                "price": base_price,
                "in_stock": in_stock,
                "image": base_image,
            })

        return variants
    def _parse_fanatics_product_json(self, data: dict) -> dict:
        result = {
            "title": data.get("name") or data.get("title") or "",
            "price": None,
            "image": "",
            "extra_images": [],
            "brand": "Fanatics",
            "description": data.get("description") or data.get("longDescription") or "",
            "variants": [],
        }

        brand = data.get("brand") or data.get("brandName") or {}
        if isinstance(brand, dict):
            result["brand"] = brand.get("name") or brand.get("displayName") or "Fanatics"
        elif isinstance(brand, str):
            result["brand"] = brand

        for field in ("currentPrice", "salePrice", "listPrice", "price", "basePrice"):
            val = data.get(field)
            if val:
                try:
                    result["price"] = int(float(str(val).replace(",", "")))
                    break
                except (ValueError, TypeError):
                    pass

        imgs = data.get("images") or data.get("productImages") or []
        if isinstance(imgs, list):
            urls = [
                (img.get("imageUrl") or img.get("url") or img.get("src") or "")
                if isinstance(img, dict) else str(img)
                for img in imgs
            ]
            urls = [u for u in urls if u]
            if urls:
                result["image"] = urls[0]
                result["extra_images"] = urls[1:5]

        seen = set()
        for v in (data.get("variants") or data.get("skus") or []):
            if not isinstance(v, dict):
                continue
            color = (v.get("color") or v.get("colorName") or "").strip()
            size  = (v.get("size")  or v.get("sizeName")  or "").strip()
            key   = f"{color}|{size}"
            if key in seen:
                continue
            seen.add(key)

            in_stock = bool(v.get("inventoryAvailable") or v.get("available") or v.get("inStock"))
            price = result["price"] or 0
            for pf in ("currentPrice", "salePrice", "price"):
                if v.get(pf):
                    try:
                        price = int(float(str(v[pf]).replace(",", "")))
                        break
                    except (ValueError, TypeError):
                        pass

            result["variants"].append({
                "color": color,
                "size": size,
                "sku": v.get("sku") or v.get("skuId") or f"{color}-{size}",
                "price": price,
                "in_stock": in_stock,
                "image": v.get("imageUrl") or v.get("image") or result["image"],
            })

        return result

    def _deep_find(self, obj, key: str, depth: int = 0):
        if depth > 5:
            return None
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                found = self._deep_find(v, key, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._deep_find(item, key, depth + 1)
                if found is not None:
                    return found
        return None
