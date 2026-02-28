"""
商品資訊爬取模組 v3
- Amazon.co.jp: requests + BeautifulSoup（快速、穩定）
- ZOZOTOWN: undetected-chromedriver（繞過 Akamai）
- 其他網站: Playwright 無頭瀏覽器
"""
import re
import json
import asyncio
from urllib.parse import urlparse
from dataclasses import dataclass, asdict, field
from collections import Counter

import httpx
from bs4 import BeautifulSoup
from config import SCRAPE_TIMEOUT, USER_AGENT, ZOZO_SCRAPER_URL, PROXY_URL

# ============ ProductInfo ============

@dataclass
class ProductInfo:
    title: str = ""
    price_jpy: int | None = None
    image_url: str = ""
    description: str = ""
    source_url: str = ""
    brand: str = ""
    currency: str = "JPY"
    extra_images: list = field(default_factory=list)
    variants: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @property
    def is_valid(self):
        return bool(self.title and self.price_jpy and self.price_jpy > 0)


# ============ Platform Detection ============

def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "zozo" in host:
        return "zozotown"
    if "amazon.co.jp" in host or "amazon.jp" in host or "amzn.asia" in host or "amzn.to" in host:
        return "amazon"
    if "rakuten.co.jp" in host:
        return "rakuten"
    return "generic"


# ============ Scraper ============

class Scraper:
    def __init__(self):
        pass

    async def scrape(self, url: str) -> ProductInfo:
        platform = detect_platform(url)

        if platform == "zozotown":
            return await self._scrape_zozotown(url)
        elif platform == "amazon":
            return await self._scrape_amazon(url)
        else:
            return await self._scrape_with_playwright(url)

    # ============================================================
    # Amazon.co.jp - requests（不需要瀏覽器，速度快）
    # ============================================================
    async def _scrape_amazon(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        try:
            # 短連結展開
            if "amzn.asia" in url or "amzn.to" in url:
                async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
                    resp = await c.head(url)
                    url = str(resp.url)
                    product.source_url = url

            # 驗證 ASIN
            am = re.search(r'/(?:dp|gp/product|gp/aw/d|ASIN)/([A-Z0-9]{10})', url)
            if not am:
                return product  # 不是商品頁

            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
                "Referer": "https://www.amazon.co.jp/",
                "Upgrade-Insecure-Requests": "1",
            }

            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return product
                if "captcha" in str(resp.url).lower():
                    return product
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # 登入頁檢查
            if soup.find("form", {"name": "signIn"}) or soup.select_one("#ap_email"):
                return product

            # 標題
            el = soup.select_one("#productTitle")
            if el:
                product.title = el.get_text(strip=True)
            if not product.title:
                t = soup.find("title")
                if t:
                    txt = t.get_text(strip=True)
                    if "サインイン" not in txt and "Sign" not in txt:
                        product.title = txt

            # 品牌
            el = soup.select_one("#bylineInfo") or soup.select_one(".po-brand .po-break-word")
            if el:
                b = el.get_text(strip=True)
                b = re.sub(r'^(ブランド[：:]\s*|Brand[：:]\s*|Visit the |のストアを表示)', '', b)
                product.brand = re.sub(r'\s*(Store|ストア)$', '', b).strip()

            # 價格
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

            # 圖片
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

            # 說明
            bullets = soup.select("#feature-bullets li span.a-list-item")
            if bullets:
                product.description = "\n".join(
                    [b.get_text(strip=True) for b in bullets if len(b.get_text(strip=True)) > 2]
                )[:500]

            print(f"[Amazon] ✅ {product.title[:40]} / ¥{product.price_jpy:,}" if product.price_jpy else f"[Amazon] ⚠️ 價格未找到")

        except Exception as e:
            print(f"[Amazon] ❌ 錯誤: {e}")

        return product

    # ============================================================
    # ZOZOTOWN - undetected-chromedriver（繞過 Akamai）
    # ============================================================
    async def _scrape_zozotown(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)

        # 方法 1: 直接 HTTP 請求 + proxy（最快最簡單）
        result = await self._fetch_zozo_http(url)
        if result and result.title:
            return result

        # 方法 2: 外部 product-fetcher（如果有設定）
        if ZOZO_SCRAPER_URL:
            result = await self._scrape_zozo_via_proxy(url)
            if result and result.title:
                return result

        # 方法 3: undetected-chromedriver（最後手段）
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_zozo_uc, url
            )
            if data and data.get("title"):
                product.title = data.get("title", "")
                product.price_jpy = data.get("price", 0) or None
                product.brand = data.get("brand", "")
                product.description = data.get("description", "")[:500]
                images = data.get("images", [])
                if images:
                    product.image_url = images[0]
                    product.extra_images = images[1:9]
                print(f"[ZOZO] ✅ UC: {product.title[:40]} / ¥{product.price_jpy:,}" if product.price_jpy else "[ZOZO] ⚠️ 無價格")
            else:
                print("[ZOZO] ⚠️ undetected-chromedriver 未取得資料")
        except Exception as e:
            print(f"[ZOZO] ❌ UC 錯誤: {e}")

        return product

    async def _fetch_zozo_http(self, url: str) -> ProductInfo | None:
        """用 curl_cffi 模擬 Chrome TLS 指紋請求 ZOZOTOWN"""
        import json, re
        product = ProductInfo(source_url=url)

        try:
            from curl_cffi.requests import AsyncSession

            headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
                'Cache-Control': 'max-age=0',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
            }

            proxy = PROXY_URL if PROXY_URL else None
            proxies = {'http': proxy, 'https': proxy} if proxy else None

            async with AsyncSession(impersonate="chrome131") as session:
                print(f"[ZOZO-HTTP] curl_cffi 請求: {url}" + (f" (proxy)" if proxy else ""))
                resp = await session.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    timeout=20,
                    allow_redirects=True,
                )
                html = resp.text
                print(f"[ZOZO-HTTP] 回應: {resp.status_code} | {len(html)} bytes")

                if resp.status_code != 200:
                    print(f"[ZOZO-HTTP] ⚠️ 非 200: {resp.status_code}")
                    print(f"[ZOZO-HTTP] 內容: {html[:300]}")
                    return None

                # 解析 ld+json
                ld_matches = re.findall(
                    r'<script\s+type=["\']application/ld\+json["\']\s*>(.*?)</script>',
                    html, re.DOTALL
                )
                for ld_text in ld_matches:
                    try:
                        ld = json.loads(ld_text)
                        if isinstance(ld, list):
                            ld = next((x for x in ld if x.get('@type') == 'Product'), ld[0] if ld else {})
                        if ld.get('@type') == 'Product':
                            product.title = ld.get('name', '')
                            brand = ld.get('brand', '')
                            product.brand = brand.get('name', '') if isinstance(brand, dict) else str(brand)
                            product.description = (ld.get('description', '') or '')[:500]

                            images = ld.get('image', [])
                            if isinstance(images, str):
                                images = [images]
                            images = [i for i in images if isinstance(i, str) and 'c.imgz.jp' in i]
                            if images:
                                product.image_url = images[0]
                                product.extra_images = images[1:9]

                            offers = ld.get('offers', {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            if offers.get('price'):
                                product.price_jpy = int(offers['price'])
                            break
                    except (json.JSONDecodeError, StopIteration):
                        continue

                # fallback: __NEXT_DATA__
                if not product.title:
                    nd_match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
                    if nd_match:
                        try:
                            nd = json.loads(nd_match.group(1))
                            props = nd.get('props', {}).get('pageProps', {})
                            prod = props.get('product') or props.get('goods') or props.get('item') or {}
                            if prod.get('name'):
                                product.title = prod['name']
                            if prod.get('brandName'):
                                product.brand = prod['brandName']
                            if prod.get('price'):
                                product.price_jpy = int(prod['price'])
                            imgs = prod.get('images', [])
                            if imgs:
                                product.image_url = imgs[0] if isinstance(imgs[0], str) else imgs[0].get('url', '')
                                product.extra_images = [i if isinstance(i, str) else i.get('url', '') for i in imgs[1:9]]
                        except:
                            pass

                # fallback: og tags
                if not product.title:
                    og_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
                    if og_match:
                        product.title = re.sub(r'\s*[-|]\s*ZOZOTOWN.*$', '', og_match.group(1))

                if not product.image_url:
                    og_img = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
                    if og_img:
                        product.image_url = og_img.group(1)

                if not product.price_jpy:
                    price_match = re.search(r'[¥￥]([\d,]+)', html)
                    if price_match:
                        product.price_jpy = int(price_match.group(1).replace(',', ''))

                if product.title:
                    print(f"[ZOZO-HTTP] ✅ {product.title[:40]} / ¥{product.price_jpy:,}" if product.price_jpy else f"[ZOZO-HTTP] ✅ {product.title[:40]}")
                    return product
                else:
                    print(f"[ZOZO-HTTP] ⚠️ 無法解析")
                    print(f"[ZOZO-HTTP] HTML前500: {html[:500]}")
                    return None

        except ImportError:
            print("[ZOZO-HTTP] curl_cffi 未安裝，跳過")
            return None
        except Exception as e:
            print(f"[ZOZO-HTTP] ❌ 錯誤: {e}")
            return None

    def _fetch_zozo_uc(self, url: str) -> dict | None:
        """
        用 undetected-chromedriver 跑 ZOZOTOWN
        Proxy 認證方式：pproxy 本地轉發（不需要 extension、不需要 selenium-wire）
        """
        import os, sys, tempfile, shutil, time as _time, subprocess, socket

        try:
            import undetected_chromedriver as uc
        except ImportError:
            print("[ZOZO] undetected-chromedriver 未安裝")
            return None

        tmp_dir = os.path.join(tempfile.gettempdir(), f'daigo_uc_{int(_time.time() * 1000)}')
        os.makedirs(tmp_dir, exist_ok=True)
        driver = None
        proxy_proc = None

        try:
            options = uc.ChromeOptions()
            options.add_argument('--lang=ja-JP')
            options.add_argument('--window-size=1920,1080')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            options.add_argument(f'--user-data-dir={tmp_dir}')
            # 禁用所有自動更新和背景下載（避免塞爆 proxy）
            options.add_argument('--disable-extensions')
            options.add_argument('--disable-component-update')
            options.add_argument('--disable-background-networking')
            options.add_argument('--disable-sync')
            options.add_argument('--no-first-run')
            options.add_argument('--disable-default-apps')
            options.add_argument('--disable-background-timer-throttling')
            options.add_argument('--disable-backgrounding-occluded-windows')
            options.add_argument('--disable-client-side-phishing-detection')
            options.add_argument('--disable-hang-monitor')

            # Proxy: 用 pproxy 起本地轉發，Chrome 連本地（不需認證）
            if PROXY_URL:
                from urllib.parse import urlparse as _urlparse
                _pp = _urlparse(PROXY_URL)

                # 找空閒 port
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('127.0.0.1', 0))
                local_port = sock.getsockname()[1]
                sock.close()

                # 本地 proxy 轉發（純 threading，不依賴 asyncio）
                proxy_proc = subprocess.Popen(
                    [sys.executable, '/app/run_pproxy.py',
                     str(local_port), _pp.hostname, str(_pp.port),
                     _pp.username or '', _pp.password or ''],
                    stdout=None, stderr=None,  # 讓 log 直接輸出到主進程
                )
                _time.sleep(2)  # 等 proxy 啟動
                # 確認沒有立刻掛掉
                if proxy_proc.poll() is not None:
                    print(f"[ZOZO] ⚠️ proxy 轉發啟動失敗 (exit code: {proxy_proc.returncode})")
                    proxy_proc = None
                else:
                    options.add_argument(f'--proxy-server=http://127.0.0.1:{local_port}')
                    # 只讓 zozo.jp 走 proxy，其他直連（避免 Google 背景請求搶頻寬）
                    options.add_argument('--proxy-bypass-list=<-loopback>;*.google.com;*.googleapis.com;*.gstatic.com;*.gvt1.com;*.gvt2.com;accounts.google.com')
                    print(f"[ZOZO] proxy 轉發 :{local_port} → {_pp.hostname}:{_pp.port}（僅 zozo.jp）")

            # 自動偵測 Chrome 版本
            ver = int(os.environ.get('CHROME_VERSION', '0'))
            if ver == 0:
                try:
                    import subprocess as _sp
                    out = _sp.check_output(['google-chrome', '--version'], text=True)
                    ver = int(out.strip().split()[-1].split('.')[0])
                    print(f"[ZOZO] 偵測到 Chrome {ver}")
                except Exception:
                    pass
            kwargs = {}
            if ver > 0:
                kwargs['version_main'] = ver

            use_headless = os.environ.get('UC_HEADLESS', 'true').lower() in ('1', 'true', 'yes')
            driver = uc.Chrome(options=options, headless=use_headless, **kwargs)
            driver.set_page_load_timeout(30)  # 最多等 30 秒載入頁面

            # 快速暖機
            driver.get('about:blank')
            _time.sleep(1)

            print(f"[ZOZO] 載入: {url}")
            try:
                driver.get(url)
            except Exception as e:
                # TimeoutException 表示頁面沒在 30 秒內載完，但 DOM 可能已經有資料
                print(f"[ZOZO] 頁面載入超時（可能正常）: {type(e).__name__}")

            for i in range(10):
                _time.sleep(3 if i < 3 else 2)  # 前 3 次多等一點
                try:
                    html = driver.page_source
                    title = driver.title
                except:
                    continue

                has_data = ('application/ld+json' in html or
                           '__NEXT_DATA__' in html or
                           'og:title' in html)

                # Debug: 前 2 次印頁面資訊
                if i < 2:
                    print(f"[ZOZO] 嘗試 {i+1}: {len(html)} bytes | title={title[:80]} | data={has_data}")
                    # 印 <head> 裡有什麼
                    import re as _re
                    scripts = _re.findall(r'<script[^>]*>(.{0,50})', html[:5000])
                    print(f"[ZOZO] scripts: {scripts[:5]}")
                    metas = _re.findall(r'<meta[^>]*>', html[:5000])
                    print(f"[ZOZO] metas: {metas[:5]}")
                    # HTML 前 1000 字
                    print(f"[ZOZO] HTML前1000: {html[:1000]}")
                    # HTML 中間（跳過前面）
                    mid = len(html) // 2
                    print(f"[ZOZO] HTML中間500: {html[mid:mid+500]}")
                    # 看有沒有 Akamai 標記
                    if 'akamai' in html.lower() or 'challenge' in html.lower() or 'captcha' in html.lower():
                        print(f"[ZOZO] ⚠️ 偵測到 Akamai challenge 頁面")
                    if 'Access Denied' in html or 'access denied' in title.lower():
                        print(f"[ZOZO] ⚠️ Access Denied")
                else:
                    print(f"[ZOZO] 嘗試 {i+1}: {len(html)} bytes | data={has_data}")

                if has_data:
                    result = driver.execute_script(r"""
                        var r = {title:'', brand:'', price:0, price_text:'',
                                 original_price:0, original_price_text:'', discount:'',
                                 images:[], description:'', item_id:'', in_stock:true};

                        var m = location.pathname.match(/\/goods(?:-sale)?\/(\d+)/);
                        if (m) r.item_id = m[1];

                        document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s) {
                            try {
                                var d = JSON.parse(s.textContent);
                                if (Array.isArray(d)) d = d.find(function(i){return i['@type']==='Product'}) || d[0];
                                if (d && d['@type'] === 'Product') {
                                    r.title = d.name || '';
                                    var b = d.brand || '';
                                    r.brand = (typeof b === 'object') ? (b.name || '') : String(b);
                                    r.description = d.description || '';
                                    var img = d.image || [];
                                    if (typeof img === 'string') img = [img];
                                    r.images = img.filter(function(i){return typeof i === 'string' && i.indexOf('c.imgz.jp') !== -1}).slice(0,15);
                                    var offers = d.offers || {};
                                    if (Array.isArray(offers)) offers = offers[0] || {};
                                    if (offers.price) {
                                        r.price = parseInt(offers.price);
                                        r.price_text = '\u00a5' + r.price.toLocaleString();
                                    }
                                    if (offers.availability && offers.availability.indexOf('OutOfStock') !== -1) r.in_stock = false;
                                }
                            } catch(e) {}
                        });

                        if (!r.title) {
                            var nd = document.getElementById('__NEXT_DATA__');
                            if (nd) {
                                try {
                                    var props = JSON.parse(nd.textContent).props.pageProps;
                                    var prod = props.product || props.goods || props.item || {};
                                    if (prod.name) r.title = prod.name;
                                    if (prod.brandName) r.brand = prod.brandName;
                                    if (prod.price) { r.price = parseInt(prod.price); r.price_text = '\u00a5' + r.price.toLocaleString(); }
                                    if (prod.images) r.images = prod.images.map(function(i){return i.url || i}).slice(0,15);
                                } catch(e) {}
                            }
                        }

                        if (!r.title) {
                            var og = document.querySelector('meta[property="og:title"]');
                            if (og) r.title = og.content.replace(/\s*[-|]\s*ZOZOTOWN.*$/, '');
                        }
                        if (r.images.length === 0) {
                            var ogImg = document.querySelector('meta[property="og:image"]');
                            if (ogImg && ogImg.content) r.images.push(ogImg.content);
                        }

                        if (!r.price) {
                            document.querySelectorAll('[class*="price"], [class*="Price"]').forEach(function(el) {
                                if (!r.price) {
                                    var pm = el.textContent.match(/[\u00a5\uffe5]([\d,]+)/);
                                    if (pm) { r.price = parseInt(pm[1].replace(/,/g,'')); r.price_text = '\u00a5' + r.price.toLocaleString(); }
                                }
                            });
                        }

                        var seen = {};
                        r.images.forEach(function(u){ seen[u] = true; });
                        document.querySelectorAll('img[src*="c.imgz.jp"], img[data-src*="c.imgz.jp"]').forEach(function(img) {
                            var src = img.src || img.getAttribute('data-src') || '';
                            if (src && !seen[src] && img.naturalWidth > 50) {
                                r.images.push(src);
                                seen[src] = true;
                            }
                        });
                        document.querySelectorAll('[srcset*="c.imgz.jp"]').forEach(function(el) {
                            var parts = (el.getAttribute('srcset') || '').split(',');
                            parts.forEach(function(p) {
                                var u = p.trim().split(/\s+/)[0];
                                if (u && u.indexOf('c.imgz.jp') !== -1 && !seen[u]) {
                                    r.images.push(u);
                                    seen[u] = true;
                                }
                            });
                        });
                        r.images = r.images.slice(0, 20);

                        return r;
                    """)

                    try: driver.quit()
                    except: pass
                    return result

                if 'access denied' in title.lower() and i >= 2:
                    print("[ZOZO] 被 Akamai 擋住")
                    break

            try: driver.quit()
            except: pass

        except Exception as e:
            print(f"[ZOZO] uc 錯誤: {e}")
            import traceback; traceback.print_exc()
            if driver:
                try: driver.quit()
                except: pass
        finally:
            if proxy_proc:
                try: proxy_proc.terminate()
                except: pass
            try: shutil.rmtree(tmp_dir, ignore_errors=True)
            except: pass

        return None

    async def _scrape_zozo_via_proxy(self, url: str) -> ProductInfo | None:
        """備用：代理到外部 product-fetcher"""
        product = ProductInfo(source_url=url)
        try:
            print(f"[ZOZO] 代理到 {ZOZO_SCRAPER_URL}")
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{ZOZO_SCRAPER_URL.rstrip('/')}/api/fetch",
                    json={"url": url},
                )
                data = resp.json()

            if data.get("error"):
                print(f"[ZOZO] 外部爬蟲錯誤: {data['error']}")
                return None

            product.title = data.get("title", "")
            product.price_jpy = data.get("price", 0) or None
            product.brand = data.get("brand", "")
            product.description = data.get("description", "")[:500]
            images = data.get("images", [])
            if images:
                product.image_url = images[0]
                product.extra_images = images[1:9]
            return product if product.title else None

        except Exception as e:
            print(f"[ZOZO] 外部爬蟲連線失敗: {e}")
            return None

    # ============================================================
    # 通用 - Playwright（其他日本網站）
    # ============================================================
    async def _scrape_with_playwright(self, url: str) -> ProductInfo:
        product = ProductInfo(source_url=url)
        try:
            html = await self._fetch_playwright(url)
            soup = BeautifulSoup(html, "html.parser")

            # JSON-LD
            self._extract_json_ld(soup, product)
            # OG tags
            self._extract_og_tags(soup, product)
            # Generic HTML
            if not product.title or not product.price_jpy:
                self._extract_generic(soup, product)

            # 價格合理性檢查
            if product.price_jpy and (product.price_jpy < 100 or product.price_jpy > 1000000):
                print(f"[Generic] ⚠️ 價格不合理 ¥{product.price_jpy}，重置")
                product.price_jpy = None

            # 相對 URL 修正
            if product.image_url and not product.image_url.startswith("http"):
                base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                product.image_url = base + product.image_url

        except Exception as e:
            print(f"[Generic] ❌ 錯誤: {e}")

        return product

    async def _fetch_playwright(self, url: str) -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="ja-JP",
                extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
            try:
                await page.route("**/*", lambda route: (
                    route.abort() if route.request.resource_type in ("media", "font") else route.continue_()
                ))
                await page.goto(url, wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT * 1000)
                await page.wait_for_timeout(2000)
                return await page.content()
            finally:
                await page.close()
                await context.close()
                await browser.close()

    # ============================================================
    # Extractors（通用解析器）
    # ============================================================
    def _extract_json_ld(self, soup, product):
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") in ("Product", "IndividualProduct")), data[0] if data else {})
                if data.get("@type") not in ("Product", "IndividualProduct"):
                    if "@graph" in data:
                        for item in data["@graph"]:
                            if item.get("@type") == "Product":
                                data = item
                                break
                    else:
                        continue

                if not product.title:
                    product.title = data.get("name", "")
                if not product.image_url and data.get("image"):
                    img = data["image"]
                    product.image_url = img[0] if isinstance(img, list) else (img.get("url", "") if isinstance(img, dict) else str(img))
                if not product.brand and data.get("brand"):
                    b = data["brand"]
                    product.brand = b.get("name", "") if isinstance(b, dict) else str(b)
                if not product.description:
                    product.description = (data.get("description") or "")[:500]
                if not product.price_jpy:
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        p = self._normalize_price(price)
                        if p and 100 <= p <= 1000000:
                            product.price_jpy = p
            except (json.JSONDecodeError, StopIteration):
                continue

    def _extract_og_tags(self, soup, product):
        og = {}
        for meta in soup.find_all("meta", property=True):
            og[meta["property"]] = meta.get("content", "")
        if not product.title:
            product.title = og.get("og:title", "")
        if not product.image_url:
            product.image_url = og.get("og:image", "")
        if not product.description:
            product.description = og.get("og:description", "")[:500]
        if not product.price_jpy:
            p = og.get("product:price:amount", "")
            if p:
                product.price_jpy = self._normalize_price(p)

    def _extract_generic(self, soup, product):
        if not product.title:
            t = soup.find("title")
            if t:
                product.title = t.get_text(strip=True)
        if not product.image_url:
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if not any(s in src.lower() for s in ["logo", "icon", "banner", "sprite", "blank"]):
                    product.image_url = src
                    break
        if not product.price_jpy:
            product.price_jpy = self._find_price_in_html(soup)

    def _find_price_in_html(self, soup):
        text = soup.get_text()
        # 優先：税込價格
        tax_prices = re.findall(r'([0-9,]+)\s*円\s*[（\(]?\s*税込', text)
        if tax_prices:
            p = self._normalize_price(tax_prices[0])
            if p and 100 <= p <= 1000000:
                return p
        # price class
        for sel in ['[class*="price"]', '[class*="Price"]', '[id*="price"]']:
            for el in soup.select(sel):
                m = re.search(r'[¥￥]?\s*([\d,]+)', el.get_text(strip=True))
                if m:
                    p = int(m.group(1).replace(',', ''))
                    if 100 <= p <= 1000000:
                        return p
        # ¥ 或 円
        prices = re.findall(r'[¥￥]\s*([0-9,]+)', text)
        prices += re.findall(r'([0-9,]+)\s*円', text)
        if prices:
            normalized = [self._normalize_price(p) for p in prices]
            normalized = [p for p in normalized if p and 100 <= p <= 1000000]
            if normalized:
                return Counter(normalized).most_common(1)[0][0]
        return None

    @staticmethod
    def _normalize_price(price):
        if isinstance(price, (int, float)):
            return int(price)
        if isinstance(price, str):
            cleaned = re.sub(r'[^0-9.]', '', price)
            return int(float(cleaned)) if cleaned else None
        return None
