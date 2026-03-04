"""
SEO 標題生成模組
用 ChatGPT 將日文商品名翻譯 + 結構化，產出適合台灣搜尋的標題和 tags

標題格式：日本代購｜{品牌/IP中文} {商品類型中文} - {原始商品名}｜{來源平台}
"""
import json
import re
import httpx
from urllib.parse import urlparse

from config import OPENAI_API_KEY, OPENAI_MODEL


# 平台名稱對照表（給標題用）
PLATFORM_DISPLAY_NAMES = {
    "amazon": "Amazon JP",
    "zozotown": "ZOZOTOWN",
    "uniqlo": "UNIQLO",
    "muji": "MUJI",
    "beams": "BEAMS",
    "rakuten": "樂天",
    "mercari": "Mercari",
    "shopify_jp": "",       # 用 brand 代替
    "generic": "",          # 用 domain 代替
}


def _get_platform_name(source_url: str, platform: str, brand: str) -> str:
    """從 URL + 平台代碼產出顯示用的平台名"""
    name = PLATFORM_DISPLAY_NAMES.get(platform, "")
    if name:
        return name

    # shopify_jp / generic → 用 brand 或 domain
    if brand:
        return brand

    host = (urlparse(source_url).hostname or "").lower()
    # 去掉 www. 和 .co.jp/.com 等
    short = host.replace("www.", "").replace("store.", "")
    short = re.sub(r'\.(co\.jp|com|jp|net)$', '', short)
    return short.capitalize() if short else ""


def _detect_platform_from_url(url: str) -> str:
    """簡化版平台偵測（避免 import scraper 造成循環依賴）"""
    host = (urlparse(url).hostname or "").lower()
    if "zozo" in host: return "zozotown"
    if "amazon.co.jp" in host or "amazon.jp" in host or "amzn" in host: return "amazon"
    if "uniqlo.com" in host: return "uniqlo"
    if "muji.com" in host: return "muji"
    if "beams.co.jp" in host: return "beams"
    if "rakuten.co.jp" in host: return "rakuten"
    if "mercari.com" in host: return "mercari"
    if "animate" in host: return "Animate"
    if "suruga-ya" in host: return "駿河屋"
    if "mandarake" in host: return "まんだらけ"
    if "amiami" in host: return "AmiAmi"
    if "toranoana" in host: return "虎之穴"
    if "melonbooks" in host: return "Melonbooks"
    return "generic"


async def generate_seo_title(
    original_title: str,
    brand: str = "",
    source_url: str = "",
    platform: str = "",
) -> dict:
    """
    用 ChatGPT 生成 SEO 標題和 tags

    回傳:
    {
        "title": "日本代購｜怪獸8號 鳴海弦 徽章 - ちびとこ 3WAY缶バッジ｜Animate",
        "tags": ["日本代購", "怪獸8號", "鳴海弦", "徽章", "Animate", "動漫周邊"],
    }
    """
    if not platform:
        platform = _detect_platform_from_url(source_url)
    platform_name = _get_platform_name(source_url, platform, brand)

    # === 呼叫 ChatGPT ===
    if OPENAI_API_KEY:
        try:
            result = await _call_chatgpt(original_title, brand, platform_name)
            if result:
                return _build_title_from_gpt(result, original_title, platform_name)
        except Exception as e:
            print(f"[SEO] ChatGPT 失敗: {type(e).__name__}: {e}")

    # === Fallback: 不用 ChatGPT，至少改善格式 ===
    return _build_fallback_title(original_title, brand, platform_name)


async def _call_chatgpt(original_title: str, brand: str, platform_name: str) -> dict | None:
    """呼叫 OpenAI API 分析商品標題"""

    prompt = f"""你是台灣電商 SEO 專家。分析以下日本商品標題，回傳 JSON（不要 markdown）。

商品標題：{original_title}
品牌：{brand or '未知'}
來源平台：{platform_name or '未知'}

回傳格式（純 JSON，無 markdown）：
{{
  "brand_zh": "品牌或 IP 名稱的繁體中文（例：怪獸8號、海賊王、UNIQLO）",
  "character_zh": "角色名繁體中文（沒有則空字串）",
  "product_type_zh": "商品類型繁體中文（例：徽章、T恤、公仔、模型、帽子、包包、鞋子、外套）",
  "clean_title_zh": "簡潔的繁體中文商品描述（10-25字，保留重要日文原名）",
  "extra_tags": ["額外的搜尋關鍵字，2-4個，繁體中文"]
}}

規則：
- brand_zh：如果是動漫/遊戲 IP，用台灣常用的繁體中文譯名
- character_zh：角色名用台灣常用譯名，沒有角色就留空
- product_type_zh：用台灣消費者常搜的用詞（徽章不是別針，公仔不是人形）
- clean_title_zh：保留日文商品名中的關鍵特徵（如 3WAY、限定等），但主體用中文
- extra_tags：想想台灣消費者會搜什麼詞來找到這個商品"""

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 300,
            },
        )

        if resp.status_code != 200:
            print(f"[SEO] OpenAI API {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()

        # 清理 markdown 包裝
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        result = json.loads(text)
        print(f"[SEO] ChatGPT 結果: {json.dumps(result, ensure_ascii=False)}")
        return result

    return None


def _build_title_from_gpt(gpt: dict, original_title: str, platform_name: str) -> dict:
    """用 ChatGPT 結果組裝最終標題和 tags"""

    brand_zh = gpt.get("brand_zh", "").strip()
    character_zh = gpt.get("character_zh", "").strip()
    product_type_zh = gpt.get("product_type_zh", "").strip()
    clean_title_zh = gpt.get("clean_title_zh", "").strip()
    extra_tags = gpt.get("extra_tags", [])

    # === 組裝標題 ===
    # 格式：日本代購｜{品牌/IP} {角色} {類型} - {中文描述}｜{平台}
    title_parts = []

    # 前段：品牌 + 角色 + 類型
    front = ""
    if brand_zh:
        front += brand_zh
    if character_zh:
        front += f" {character_zh}"
    if product_type_zh:
        front += f" {product_type_zh}"
    front = front.strip()

    # 中段：中文描述（如果和前段重複太多就用原標題）
    mid = clean_title_zh or original_title

    # 避免前段和中段重複
    if front and mid:
        # 如果 mid 開頭就是 front 的內容，去掉重複
        for part in [brand_zh, character_zh, product_type_zh]:
            if part and mid.startswith(part):
                mid = mid[len(part):].strip()
                # 去掉開頭的標點
                mid = re.sub(r'^[・\s]+', '', mid)

    # 組裝
    if front and mid:
        title_main = f"{front} - {mid}"
    elif front:
        title_main = front
    else:
        title_main = mid or original_title

    # 加平台
    if platform_name:
        title = f"日本代購｜{title_main}｜{platform_name}"
    else:
        title = f"日本代購｜{title_main}"

    # Shopify 標題限制 255 字元，超過就截斷
    if len(title) > 250:
        title = title[:247] + "..."

    # === Tags ===
    tags = ["日本代購", "代購", "daigo"]
    if brand_zh:
        tags.append(brand_zh)
    if character_zh:
        tags.append(character_zh)
    if product_type_zh:
        tags.append(product_type_zh)
    if platform_name:
        tags.append(platform_name)
    for t in extra_tags:
        if isinstance(t, str) and t.strip() and t.strip() not in tags:
            tags.append(t.strip())

    # 去重保序
    seen = set()
    unique_tags = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique_tags.append(t)

    print(f"[SEO] 最終標題: {title}")
    print(f"[SEO] Tags: {unique_tags}")

    return {
        "title": title,
        "tags": unique_tags,
    }


def _build_fallback_title(original_title: str, brand: str, platform_name: str) -> dict:
    """ChatGPT 不可用時的 fallback 標題"""

    # 簡單清理：去掉常見的平台後綴
    clean = original_title
    for suffix in ["| アニメイト", "- ZOZOTOWN", "| BEAMS", "| 楽天市場", "通販", "| Amazon"]:
        clean = clean.replace(suffix, "").strip()

    if brand and platform_name:
        title = f"日本代購｜{brand} {clean}｜{platform_name}"
    elif brand:
        title = f"日本代購｜{brand} {clean}"
    elif platform_name:
        title = f"日本代購｜{clean}｜{platform_name}"
    else:
        title = f"日本代購｜{clean}"

    if len(title) > 250:
        title = title[:247] + "..."

    tags = ["日本代購", "代購", "daigo"]
    if brand:
        tags.append(brand)
    if platform_name and platform_name not in tags:
        tags.append(platform_name)

    return {
        "title": title,
        "tags": tags,
    }
