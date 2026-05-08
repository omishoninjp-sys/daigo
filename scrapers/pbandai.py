"""
PREMIUM BANDAI (p-bandai.jp) 商品爬取 Mixin

平台特性：
- 萬代官方限定商品 EC 站（Gundam、One Piece、特攝、IP 周邊）
- Shift_JIS / CP932 編碼
- 主資料源：JSON-LD <script type="application/ld+json"> Product schema
- 圖片 CDN: bandai-a.akamaihd.net (有 b/ 標準版與 xl/ 大圖版)
- 海外 IP 會被導向 /global_newpc.html 地區選擇頁

⚠️ 業務注意：
- 大量商品為「抽選販売」(限量抽選)，非先到先得
- 抓到 availability="PreOrder" 時將 in_stock 設為 True 但於描述加註抽選資訊
- 受付期間 / 当選発表日期會自動帶出

URL 範例：
  https://p-bandai.jp/item/item-1000249930/
  https://p-bandai.jp/item/item-1000249930/?slide=modal
  https://p-bandai.jp/item/item-1000249930/?cid=xxx
"""
import asyncio
import json
import re
import time

from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


_MIN_PRICE = 100
_MAX_PRICE = 10_000_000


class PBandaiMixin:

    async def _scrape_pbandai(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url, brand="プレミアムバンダイ")
        clean_url = url.split("#")[0].strip()

        # ── URL 標準化：去掉 ?slide=modal / ?cid= 等 query，讓 cache key 一致 ──
        m = re.search(r'p-bandai\.jp/item/(item-\d+)', clean_url)
        if m:
            standardized = f"https://p-bandai.jp/item/{m.group(1)}/"
            if standardized != clean_url:
                print(f"[PBandai] URL 標準化: {clean_url} → {standardized}")
                clean_url = standardized
                product.source_url = clean_url

        html = await asyncio.to_thread(self._pbandai_get_html, clean_url)
        if not html:
            print(f"[PBandai] ❌ HTML 取得失敗: {clean_url}")
            return product

        # ⚠️ 偵測海外 IP 導向 global 頁
        if self._pbandai_is_global_redirect(html):
            print(f"[PBandai] ⚠️ 偵測到 global 重定向頁（海外 IP 限制）")
            raise ValueError(
                "Premium Bandai 偵測到本服務伺服器位置限制，暫時無法取得商品資料。"
                "請於 LINE @544kaytb 提供商品連結，我們將為您手動報價並建立訂單。"
            )

        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── 主資料源：JSON-LD Product schema ──
            ld = self._pbandai_find_product_jsonld(soup)
            if ld:
                self._pbandai_apply_jsonld(ld, product)
            else:
                print(f"[PBandai] ⚠️ 找不到 JSON-LD Product schema")

            # ── 標題 fallback ──
            if not product.title:
                og = soup.find("meta", attrs={"property": "og:title"})
                if og and og.get("content"):
                    title = og["content"].strip()
                    title = re.split(r'\s*[｜\|]\s*(?:プレミアムバンダイ|PREMIUM BANDAI)', title, flags=re.I)[0].strip()
                    if title:
                        product.title = title

            # ── 主圖 fallback：og:image + 改用 xl 大圖版 ──
            if not product.image_url:
                og_img = soup.find("meta", attrs={"property": "og:image"})
                if og_img and og_img.get("content"):
                    product.image_url = og_img["content"].strip()

            # 把主圖改成 xl 大圖版（CDN 支援，畫質好很多）
            if product.image_url and 'bandai-a.akamaihd.net' in product.image_url:
                product.image_url = re.sub(
                    r'/bc/img/model/b/(\d+_\d+\.(?:jpg|jpeg|png))',
                    r'/bc/img/model/xl/\1',
                    product.image_url,
                )

            # ── 額外圖片：抓商品 ID 對應的所有 _N.jpg（最多 9 張）──
            item_id_match = re.search(r'item-(\d+)', clean_url)
            if item_id_match:
                item_id = item_id_match.group(1)
                product.extra_images = self._pbandai_extract_images(html, item_id, product.image_url)

            # ── 抽選販売資訊：加進描述 ──
            lottery_info = self._pbandai_extract_lottery_info(soup)
            if lottery_info:
                # 把抽選資訊放在描述開頭
                existing_desc = product.description or ""
                product.description = lottery_info + "\n\n" + existing_desc

            # ── 商品 ID 加進 description（給客服查詢用）──
            if item_id_match:
                if product.description and item_id_match.group(1) not in product.description:
                    product.description = product.description.rstrip() + f"\n\n商品番号: {item_id_match.group(1)}"

            title_short = (product.title or "")[:60]
            availability_label = "抽選販売" if lottery_info else "通常販売"
            if product.is_valid:
                print(
                    f"[PBandai] ✅ {title_short!r} | ¥{product.price_jpy:,} | "
                    f"images={1 + len(product.extra_images)} | type={availability_label}"
                )
            else:
                print(
                    f"[PBandai] ⚠️ 部分資料缺失 ({title_short!r}) | "
                    f"price={product.price_jpy} | type={availability_label}"
                )

        except ValueError:
            raise
        except Exception as e:
            print(f"[PBandai] ❌ 解析錯誤: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        return product

    # ─────────────────────────────────────────────────────────────────
    # JSON-LD
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _pbandai_find_product_jsonld(soup: BeautifulSoup) -> dict | None:
        """找頁面內 JSON-LD 的 Product schema"""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            # 可能是單物件、陣列、或 @graph
            candidates = []
            if isinstance(data, dict):
                if data.get("@type") == "Product":
                    candidates.append(data)
                elif "@graph" in data and isinstance(data["@graph"], list):
                    for item in data["@graph"]:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            candidates.append(item)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        candidates.append(item)

            if candidates:
                return candidates[0]
        return None

    def _pbandai_apply_jsonld(self, ld: dict, product: ProductInfo) -> None:
        # 標題
        name = (ld.get("name") or "").strip()
        if name:
            product.title = name

        # 品牌
        brand = ld.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            product.brand = str(brand["name"]).strip()
        elif isinstance(brand, str) and brand.strip():
            product.brand = brand.strip()

        # 描述
        desc = (ld.get("description") or "").strip()
        if desc:
            # 去掉「| バンダイナムコグループ公式通販サイト | プレミアムバンダイ。」尾巴
            desc = re.sub(
                r'\s*[\|｜]\s*バンダイナムコグループ公式通販サイト\s*[\|｜]\s*プレミアムバンダイ。?\s*$',
                '',
                desc,
            ).strip()
            product.description = desc[:1500]

        # 主圖
        img = ld.get("image")
        if isinstance(img, str) and img.strip():
            product.image_url = img.strip()
        elif isinstance(img, list) and img:
            product.image_url = str(img[0]).strip()

        # 價格
        offers = ld.get("offers")
        if isinstance(offers, dict):
            v = self._pbandai_to_int(offers.get("price"))
            if v:
                product.price_jpy = v

            # 庫存：PreOrder = 抽選/預購（仍視為可下單）；OutOfStock = 售完
            avail = (offers.get("availability") or "").lower()
            if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
                product.in_stock = False
            else:
                # InStock / PreOrder / LimitedAvailability 都視為可下單
                product.in_stock = True

    @staticmethod
    def _pbandai_to_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip().replace(",", "").replace("，", "")
            if not s:
                return None
            try:
                v = int(float(s))
            except (ValueError, TypeError):
                return None
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
        return None

    # ─────────────────────────────────────────────────────────────────
    # 額外圖片
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _pbandai_extract_images(html: str, item_id: str, main_url: str) -> list[str]:
        """
        從 HTML 抓所有 <item_id>_<N>.jpg 圖片
        並轉成 xl 大圖版（CDN 支援）
        最多回傳 9 張，跳過主圖避免重複
        """
        # 規則：/bc/img/model/(?:b|xl)/<item_id>_<N>.(jpg|png)
        pattern = re.compile(
            rf'https://bandai-a\.akamaihd\.net/bc/img/model/(?:b|xl)/'
            rf'({re.escape(item_id)}_(\d+)\.(?:jpg|jpeg|png))',
            re.IGNORECASE,
        )

        # 取得 main filename 避免重複
        main_filename = ""
        if main_url:
            mm = re.search(r'/(\w+_\d+\.(?:jpg|jpeg|png))(?:\?|$)', main_url)
            if mm:
                main_filename = mm.group(1)

        result: list[str] = []
        seen: set[str] = set()
        seen_filenames: set[str] = set()
        if main_filename:
            seen_filenames.add(main_filename)

        # 收集所有，按編號排序
        found = []
        for m in pattern.finditer(html):
            filename = m.group(1)
            num = int(m.group(2))
            if filename in seen_filenames:
                continue
            seen_filenames.add(filename)
            found.append((num, filename))

        # 按編號排序（_1, _2, _3 ...）
        found.sort(key=lambda x: x[0])

        for num, filename in found:
            # 統一用 xl 版
            url = f"https://bandai-a.akamaihd.net/bc/img/model/xl/{filename}"
            if url not in seen:
                seen.add(url)
                result.append(url)
            if len(result) >= 9:
                break

        return result

    # ─────────────────────────────────────────────────────────────────
    # 抽選販売資訊
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _pbandai_extract_lottery_info(soup: BeautifulSoup) -> str:
        """
        偵測抽選販売 + 抽出受付期間 / 当選発表日期
        若是抽選商品則回傳格式化的提示文字（HTML 格式）
        """
        text = soup.get_text(" ", strip=True)

        # 必要條件：標題或內文含「抽選販売」
        if "抽選販売" not in text and "抽選販賣" not in text:
            return ""

        # 抓受付期間
        lines = []
        m_apply = re.search(r'受付期間\s*([\d\u4e00-\u9fff年月日（）()::／/～\-\s\u300012時～0-9]+)', text)
        if m_apply:
            apply_period = m_apply.group(1).strip()
            # 截到第一個日期區段結尾
            apply_period = re.split(r'\s*※|当選発表|商品|※', apply_period)[0].strip()
            apply_period = apply_period[:60]
            if apply_period:
                lines.append(f"受付期間: {apply_period}")

        m_result = re.search(r'当選発表\s*([\d\u4e00-\u9fff年月日上中下旬（）()::\-\s]+)', text)
        if m_result:
            result_date = m_result.group(1).strip()
            result_date = re.split(r'\s*※|商品|発送|※', result_date)[0].strip()
            result_date = result_date[:40]
            if result_date:
                lines.append(f"当選発表: {result_date}")

        # 組合成中文友善的提示
        notice = (
            "⚠️ 抽選販售商品（限量抽選）\n"
            "本商品為日本萬代抽選販售（非先到先得），需透過抽選方式購買，可能無法保證取得。\n"
        )
        if lines:
            notice += "\n" + "\n".join(lines) + "\n"
        notice += (
            "代購流程：\n"
            "1. 客戶下單後我們會代為應募抽選\n"
            "2. 当選後依代購流程出貨\n"
            "3. 若未当選將全額退款\n"
        )
        return notice

    # ─────────────────────────────────────────────────────────────────
    # 海外 IP 重定向偵測
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _pbandai_is_global_redirect(html: str) -> bool:
        """偵測是否被導向 global 頁面（海外 IP 限制）"""
        if not html:
            return False
        markers = [
            "global_newpc.html",
            "Premium Bandai is International",
            "INTERNATIONAL SHIPPING AVAILABLE",
            "SELECT YOUR REGION",
        ]
        return any(m in html for m in markers)

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    def _pbandai_get_html(self, url: str) -> str:
        """SeleniumBase UC 取得 HTML（p-bandai 使用 Shift_JIS）"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()

            try:
                driver.uc_open_with_reconnect(url, reconnect_time=5)
            except Exception:
                driver.get(url)
            time.sleep(3)

            # 評分式等待
            best_html = ""
            best_score = 0

            for i in range(6):
                time.sleep(2)
                try:
                    html = driver.page_source
                    cur_url = driver.current_url
                except Exception:
                    continue

                # 偵測重定向
                if "global_newpc" in cur_url:
                    print(f"[PBandai][fetch] iter={i} ⚠️ 被導向 global 頁: {cur_url[:100]}")
                    # 仍存 best_html 讓上層處理（會 raise 友善錯誤訊息）
                    best_html = html
                    break

                score = 0
                if 'application/ld+json' in html: score += 5
                if '"@type":"Product"' in html or '"@type": "Product"' in html: score += 5
                if 'bandai-a.akamaihd.net' in html: score += 3
                if 'og:image' in html: score += 2

                if score > best_score:
                    best_score = score
                    best_html = html

                if i >= 1 and score >= 8 and len(html) > 5000:
                    print(f"[PBandai][fetch] iter={i}, score={score}, size={len(html)//1024}KB ✓")
                    self._driver_use_count += 1
                    return html

            self._driver_use_count += 1

            if best_html and len(best_html) > 2000:
                print(f"[PBandai][fetch] 用最佳版本 score={best_score} size={len(best_html)//1024}KB")
                return best_html

            print(f"[PBandai][fetch] ❌ 取得失敗")
            return ""

        except Exception as e:
            print(f"[PBandai] driver 失敗: {type(e).__name__}: {e}")
            return ""
