"""
takaratomy.py – タカラトミー商品ページ爬蟲

支援兩個站點：
1. takaratomymall.jp（公式通販モール）
   - Shift_JIS 編碼
   - 主資料源：JSON-LD <script type="application/ld+json">
   - 圖片路徑：/img/goods/<dir>/<goods_id>_<hash>.jpg
   - ⚠️ 圖片有防盜鏈（hotlink protection）→ 必須在 scraper 內下載成 base64
2. beyblade.takaratomy.co.jp（旧 brand site）→ 舊邏輯保留

httpx がトップページにリダイレクトされるため SeleniumBase UC を使用。
"""
import asyncio
import base64
import json
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scrapers.base import ProductInfo


class TakaratomyMixin:

    async def _scrape_takaratomy(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        clean_url = url.split("#")[0].strip()

        host = (urlparse(clean_url).hostname or "").lower()

        if "takaratomymall.jp" in host:
            return await self._scrape_takaratomymall(clean_url, product)
        else:
            return await self._scrape_takaratomy_legacy(clean_url, product)

    # ─────────────────────────────────────────────────────────────────
    # takaratomymall.jp
    # ─────────────────────────────────────────────────────────────────
    async def _scrape_takaratomymall(self, url: str, product: ProductInfo) -> ProductInfo:
        # ⚠️ queue-it 偵測：如果 URL 已經是 queue-it.net，直接擲錯不快取
        if "queue-it.net" in url:
            print(f"[Takaratomy] ⚠️ 偵測到 queue-it 排隊頁面 URL: {url[:120]}")
            raise ValueError(
                "此商品目前處於タカラトミーモール線上排隊系統（queue-it），"
                "暫時無法自動代購。請稍後（通常 30 分鐘 - 數小時後）再試一次，"
                "或改在非熱門時段下單。"
            )

        html = await asyncio.to_thread(self._takaratomy_get_html_with_queue_handling, url)
        if not html:
            print(f"[Takaratomy] ❌ HTML 取得失敗: {url}")
            return product

        # ⚠️ 抓到的 HTML 可能還是 queue-it 頁面（driver 等不到）
        if self._takaratomy_is_queue_page(html):
            print(f"[Takaratomy] ⚠️ HTML 內容為 queue-it 排隊頁，無法解析商品")
            raise ValueError(
                "タカラトミーモール正在排隊系統中，未能在限時內進入商品頁。"
                "請稍後再試或聯絡客服。"
            )

        soup = BeautifulSoup(html, "html.parser")

        # 商品 ID（從 URL 抽，e.g. /shop/g/g8202609930907/ → 8202609930907）
        goods_id = ""
        m = re.search(r'/shop/g/g(\d+)', url)
        if m:
            goods_id = m.group(1)

        # ── ★ 主路徑：JSON-LD Product schema ★ ──
        product_data = self._takaratomymall_find_product_jsonld(soup)
        if product_data:
            # 標題
            name = product_data.get("name", "").strip()
            if name:
                product.title = name

            # 價格
            offers = product_data.get("offers")
            if isinstance(offers, dict):
                v = self._takaratomy_to_int(offers.get("price"))
                if v:
                    product.price_jpy = v
                # 庫存
                avail = (offers.get("availability") or "").lower()
                if "outofstock" in avail or "soldout" in avail:
                    product.in_stock = False

            # 主圖（JSON-LD image 是線上完整 URL，不需重組）
            ld_image = product_data.get("image")
            if isinstance(ld_image, str) and ld_image:
                product.image_url = ld_image
            elif isinstance(ld_image, list) and ld_image:
                product.image_url = ld_image[0]

        # ── 標題 fallback：og:title / <title> ──
        if not product.title:
            self._takaratomymall_fill_title(soup, product)

        # ── 價格 fallback：HTML 元素文字 ──
        if not product.price_jpy:
            v = self._takaratomymall_extract_price_from_html(soup)
            if v:
                product.price_jpy = v

        # ── 圖片：完整列表（主圖 + 子圖）──
        images = self._takaratomymall_extract_images(soup, html, goods_id, product.image_url)
        if images:
            product.image_url = images[0]
            product.extra_images = images[1:10]

        # ── ★ 防盜鏈處理：下載主圖成 base64（給 Shopify 上架用）★ ──
        if product.image_url:
            b64 = await self._takaratomymall_download_image_base64(product.image_url, url)
            if b64:
                product.image_base64 = b64
                print(f"[Takaratomy] ✅ 主圖已轉 base64 ({len(b64)} chars)")
            else:
                print(f"[Takaratomy] ⚠️ 主圖 base64 下載失敗，Shopify 上架可能會失敗")

        # ── 品牌 ──
        product.brand = "タカラトミー"

        # ── 描述 ──
        if not product.description:
            md = soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                product.description = md["content"][:800]

        # ── 庫存（HTML 文字 fallback）──
        if product.in_stock:
            page_text = soup.get_text(" ", strip=True)
            if any(kw in page_text for kw in ["売り切れ", "販売終了", "在庫切れ", "完売"]):
                product.in_stock = False

        title_short = (product.title or "")[:50]
        if product.price_jpy:
            print(
                f"[Takaratomy] ✅ {title_short!r} | "
                f"¥{product.price_jpy:,} | images={len(images)} | "
                f"goods_id={goods_id} | in_stock={product.in_stock}"
            )
        else:
            print(f"[Takaratomy] ⚠️ 価格未取得 ({title_short!r}) | images={len(images)}")

        return product

    @staticmethod
    def _takaratomymall_find_product_jsonld(soup: BeautifulSoup) -> dict | None:
        """找頁面內 JSON-LD 的 Product schema"""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        return None

    @staticmethod
    def _takaratomymall_fill_title(soup: BeautifulSoup, product: ProductInfo) -> None:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"].strip()
            title = re.split(r'\s*[｜\|]\s*', title)[0].strip()
            if title:
                product.title = title
                return

        t = soup.find("title")
        if t:
            title = t.get_text(strip=True)
            title = re.split(r'\s*[｜\|]\s*', title)[0].strip()
            if title:
                product.title = title

    @staticmethod
    def _takaratomy_to_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = int(value)
        else:
            s = str(value).strip().replace(",", "")
            if not s:
                return None
            try:
                v = int(float(s))
            except (ValueError, TypeError):
                return None
        if 10 <= v <= 5_000_000:
            return v
        return None

    def _takaratomymall_extract_price_from_html(self, soup: BeautifulSoup) -> int | None:
        """從 HTML 元素抽價格（fallback）"""
        # takaratomymall 標準價格 selector
        for sel in [
            ".tt_block17__pricePriceText1",
            ".tt_block17__pricePrice",
            ".price_",
            ".tt_product4-1__price",
            ".goods_price",
            ".price",
        ]:
            for el in soup.select(sel):
                cls = " ".join(el.get("class", []))
                if "cross" in cls or "through" in cls:
                    continue
                text = el.get_text(" ", strip=True)
                # 「1,800円(税込)」格式
                m = re.search(r'([\d,]{2,})\s*円', text)
                if m:
                    v = self._takaratomy_to_int(m.group(1))
                    if v:
                        return v
                m = re.search(r'¥\s*([\d,]{2,})', text)
                if m:
                    v = self._takaratomy_to_int(m.group(1))
                    if v:
                        return v

        # og:price:amount
        for sel in [
            {"property": "og:price:amount"},
            {"property": "product:price:amount"},
            {"itemprop": "price"},
        ]:
            el = soup.find("meta", attrs=sel)
            if el and el.get("content"):
                v = self._takaratomy_to_int(el["content"])
                if v:
                    return v

        # 全頁文字找「<數字>円(税込)」
        text_all = soup.get_text(" ", strip=True)
        m = re.search(r'([\d,]{2,})\s*円\s*[\(（]\s*税込', text_all)
        if m:
            v = self._takaratomy_to_int(m.group(1))
            if v:
                return v

        return None

    @staticmethod
    def _takaratomymall_extract_images(
        soup: BeautifulSoup,
        html: str,
        goods_id: str,
        ld_image_url: str = "",
    ) -> list[str]:
        """
        takaratomymall.jp 圖片抽取

        策略：
        1. og:image 主圖（/img/goods/S/<gid>_<hash>.jpg）
        2. JSON-LD image（含目錄資訊，e.g. /img/goods/5/...）
        3. 從本地存檔的檔名 + 反推目錄重組所有子圖 URL
        4. 直接抓頁面內所有完整線上 URL fallback
        """
        from collections import Counter

        result: list[str] = []
        seen: set[str] = set()
        host_base = "https://takaratomymall.jp"

        # ─── Step 1: og:image 主圖 ───
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            url = og["content"].strip()
            if url and url not in seen:
                seen.add(url)
                result.append(url)

        # ─── Step 2: JSON-LD image（保險）───
        if ld_image_url and ld_image_url not in seen:
            seen.add(ld_image_url)
            result.append(ld_image_url)

        # ─── Step 3: 從頁面所有完整線上 URL（最可靠）───
        # ⚠️ 限制只取「當前商品 ID」的圖片，避免推薦商品的圖混入
        if goods_id:
            full_url_pattern = re.compile(
                rf'https?://takaratomymall\.jp/img/goods/([^/]+)/({re.escape(goods_id)}_[a-f0-9]+\.(?:jpg|jpeg|png))',
                re.IGNORECASE,
            )
        else:
            full_url_pattern = re.compile(
                r'https?://takaratomymall\.jp/img/goods/([^/]+)/(\d+_[a-f0-9]+\.(?:jpg|jpeg|png))',
                re.IGNORECASE,
            )
        # 統計目錄出現次數（用於 step 4 反推）
        dir_counter: Counter = Counter()
        for m in full_url_pattern.finditer(html):
            d = m.group(1)
            url = f"{host_base}/img/goods/{d}/{m.group(2)}"
            if url not in seen:
                seen.add(url)
                result.append(url)
            if d != "S":  # S 目錄是主圖固定路徑，不算
                dir_counter[d] += 1

        # ─── Step 4: 抓本地存檔/lazyload 中的檔名，反推目錄重組 ───
        thumb_dir = None
        if dir_counter:
            thumb_dir = dir_counter.most_common(1)[0][0]
        elif goods_id:
            thumb_dir = goods_id[-1]
        else:
            thumb_dir = "5"

        if goods_id:
            filename_pattern = re.compile(
                rf'({re.escape(goods_id)}_[a-f0-9]+\.(?:jpg|jpeg|png))',
                re.IGNORECASE,
            )
        else:
            filename_pattern = re.compile(
                r'(\d{10,14}_[a-f0-9]+\.(?:jpg|jpeg|png))',
                re.IGNORECASE,
            )

        # 主圖檔名（避免重複）
        main_filename = ""
        if result:
            main_filename = result[0].split("/")[-1]

        seen_fnames: set[str] = set()
        for m in filename_pattern.finditer(html):
            fname = m.group(1)
            if fname in seen_fnames or fname == main_filename:
                continue
            seen_fnames.add(fname)
            url = f"{host_base}/img/goods/{thumb_dir}/{fname}"
            if url not in seen:
                seen.add(url)
                result.append(url)

        return result

    async def _takaratomymall_download_image_base64(self, image_url: str, referer_url: str) -> str:
        """
        下載 takaratomymall 圖片成 base64

        策略（依序嘗試）：
        1. driver fetch（最可靠 - 沿用已建立的 session/cookie）
        2. httpx with Referer header（fallback）

        失敗時回傳空字串。
        """
        # ── 策略 1：用 driver 在頁面內 fetch 圖片 ───────────────────────
        # 因為 driver 剛剛訪問過商品頁，cookie / Cloudflare clearance 都在
        try:
            b64 = await asyncio.to_thread(self._takaratomy_driver_fetch_image, image_url)
            if b64:
                return b64
        except Exception as e:
            print(f"[Takaratomy] driver fetch image 失敗: {type(e).__name__}: {e}")

        # ── 策略 2：httpx + Referer fallback ───────────────────────────
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            "Referer": referer_url or "https://takaratomymall.jp/",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
        }
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(image_url)
                if resp.status_code != 200:
                    print(
                        f"[Takaratomy] httpx 圖片下載失敗 status={resp.status_code}: {image_url[:80]}"
                    )
                    return ""
                ctype = resp.headers.get("content-type", "").lower()
                if not any(t in ctype for t in ["image/", "octet-stream"]):
                    print(f"[Takaratomy] 非圖片類型: {ctype}")
                    return ""
                if len(resp.content) < 1024:
                    print(f"[Takaratomy] 圖片過小: {len(resp.content)} bytes")
                    return ""
                return base64.b64encode(resp.content).decode("ascii")
        except Exception as e:
            print(f"[Takaratomy] httpx 例外: {type(e).__name__}: {e}")
            return ""

    def _takaratomy_driver_fetch_image(self, image_url: str) -> str:
        """
        在 driver 內執行 JS 用 fetch() 取得圖片，轉 base64
        利用 driver 已有的 cookie / Referer 自動繞過防盜鏈
        """
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""

            # 用 fetch + FileReader 把 blob 轉成 base64 dataURL
            # 注意：image_url 帶單引號要 escape
            safe_url = image_url.replace("'", "\\'").replace("\n", "")
            script = f"""
            const url = '{safe_url}';
            const callback = arguments[arguments.length - 1];
            (async () => {{
              try {{
                const r = await fetch(url, {{credentials: 'include'}});
                if (!r.ok) {{
                  callback({{ok: false, status: r.status}});
                  return;
                }}
                const blob = await r.blob();
                if (blob.size < 1024) {{
                  callback({{ok: false, status: 'small', size: blob.size}});
                  return;
                }}
                const reader = new FileReader();
                reader.onloadend = () => {{
                  const dataUrl = reader.result;
                  // dataUrl 格式: data:image/jpeg;base64,XXXX
                  const idx = dataUrl.indexOf(',');
                  const b64 = idx >= 0 ? dataUrl.substring(idx + 1) : '';
                  callback({{ok: true, b64: b64, size: blob.size}});
                }};
                reader.onerror = () => callback({{ok: false, status: 'reader_error'}});
                reader.readAsDataURL(blob);
              }} catch (e) {{
                callback({{ok: false, status: 'exception', msg: String(e)}});
              }}
            }})();
            """
            # set timeout
            driver.set_script_timeout(20)
            result = driver.execute_async_script(script)

            if not isinstance(result, dict):
                print(f"[Takaratomy] driver fetch 回傳非 dict: {result}")
                return ""

            if result.get("ok") and result.get("b64"):
                size = result.get("size", "?")
                print(f"[Takaratomy] driver fetch 成功 ({size} bytes)")
                return result["b64"]
            else:
                print(f"[Takaratomy] driver fetch 失敗: {result}")
                return ""
        except Exception as e:
            print(f"[Takaratomy] _takaratomy_driver_fetch_image 例外: {type(e).__name__}: {e}")
            return ""

    # ─────────────────────────────────────────────────────────────────
    # 舊版 takaratomy.co.jp / beyblade.takaratomy.co.jp
    # ─────────────────────────────────────────────────────────────────
    async def _scrape_takaratomy_legacy(self, url: str, product: ProductInfo) -> ProductInfo:
        html = await asyncio.to_thread(self._takaratomy_get_html, url)
        if not html:
            print(f"[Takaratomy] ❌ HTML 取得失敗: {url}")
            return product

        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            raw = re.sub(r"\s*[｜|].*$", "", raw).strip()
            if raw:
                product.title = raw

        price_el = soup.select_one(".price")
        if price_el:
            price_text = price_el.get_text(strip=True).split("（")[0]
            m = re.search(r"[\d,]+", price_text)
            if m:
                try:
                    product.price_jpy = int(m.group().replace(",", ""))
                except ValueError:
                    pass

        product.brand = "タカラトミー"

        spec_el = soup.select_one(".spec")
        if spec_el:
            product.description = spec_el.get_text(separator="\n", strip=True)[:800]

        seen: set[str] = set()
        imgs: list[str] = []
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if "_list" in src:
                continue
            if not re.match(r"_image/", src):
                continue
            full = urljoin(url, src)
            if full not in seen:
                seen.add(full)
                imgs.append(full)
        if imgs:
            product.image_url = imgs[0]
            product.extra_images = imgs[1:10]

        title_short = (product.title or "")[:50]
        if product.price_jpy:
            print(
                f"[Takaratomy] ✅ {title_short!r} | "
                f"¥{product.price_jpy:,} | images={len(imgs)}"
            )
        else:
            print(f"[Takaratomy] ⚠️ 価格未取得 ({title_short!r})")

        return product

    # ─────────────────────────────────────────────────────────────────
    # HTML 抓取
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _takaratomy_is_queue_page(html: str) -> bool:
        """偵測 HTML 是否為 queue-it 排隊頁"""
        if not html:
            return False
        # queue-it 頁面特徵
        markers = [
            "queue-it.net",
            "queueit",
            "We are protecting your access to",
            "You are now in line",
            "Please wait while we connect you",
            "あなたは現在順番待ちです",
            "タカラトミーモールに接続中",
        ]
        return any(m in html for m in markers)

    def _takaratomy_get_html_with_queue_handling(self, url: str, max_wait: int = 90) -> str:
        """
        取得 takaratomymall HTML，自動處理 queue-it 排隊
        - 進入頁面後若被導去 queue-it.net，最多等 max_wait 秒
        - 偵測到 URL 回到 takaratomymall.jp 才回傳 HTML
        - 超時則回傳 queue 頁的 HTML（呼叫端會 raise）
        """
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()
            driver.get(url)
            time.sleep(3)

            # 檢查當下 URL 是否在 queue
            for elapsed in range(0, max_wait, 5):
                try:
                    cur_url = driver.current_url
                except Exception:
                    cur_url = ""

                if "queue-it.net" in cur_url:
                    print(
                        f"[Takaratomy] ⏳ 在 queue-it 排隊中... ({elapsed}s elapsed) "
                        f"current_url={cur_url[:100]}"
                    )
                    time.sleep(5)
                    continue
                else:
                    # 已脫離 queue
                    if elapsed > 0:
                        print(f"[Takaratomy] ✓ 通過 queue-it（等待 {elapsed}s）")
                    break

            # 滾動觸發 lazy load
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
                time.sleep(1)
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(1)
            except Exception:
                pass

            html = driver.page_source
            self._driver_use_count += 1
            return html
        except Exception as e:
            print(f"[Takaratomy] SeleniumBase 失敗: {type(e).__name__}: {e}")
            return ""

    def _takaratomy_get_html(self, url: str) -> str:
        """SeleniumBase UC で HTML 取得（driver 已自動處理編碼）- 舊版 takaratomy.co.jp 用"""
        try:
            driver = self._ensure_driver()
            if not driver:
                return ""
            self._clean_driver_tabs()
            driver.get(url)
            time.sleep(3)
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
                time.sleep(1)
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(1)
            except Exception:
                pass
            html = driver.page_source
            self._driver_use_count += 1
            return html
        except Exception as e:
            print(f"[Takaratomy] SeleniumBase 失敗: {type(e).__name__}: {e}")
            return ""
