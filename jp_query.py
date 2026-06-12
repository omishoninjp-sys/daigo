"""
中文 → 日文 查詢處理
====================
- needs_translation(q)：判斷是否需要翻（有假名=已是日文不翻；純拉丁/數字=原樣；漢字無假名=翻）
- translate_to_jp(q)：回最佳單一日文關鍵字（/api/search 的 fallback 用）
- suggest(q)：回候補清單 [{label_zh, keyword_jp}, ...]（/api/suggest 用，給前台下拉）

重用 config 的 OPENAI_API_KEY / OPENAI_MODEL（與 seo_title.py 同一把 gpt-4o-mini）。
有 in-memory 快取：熱門詞翻一次就存，之後免呼叫、秒回。
"""
import re
import json

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL

_KANA = re.compile(r'[\u3040-\u30ff]')      # 平假名 / 片假名
_CJK = re.compile(r'[\u4e00-\u9fff]')       # 漢字

_translate_cache: dict = {}
_suggest_cache: dict = {}


def has_kana(s: str) -> bool:
    return bool(_KANA.search(s or ""))


def has_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))


def needs_translation(q: str) -> bool:
    """有假名→已是日文（不翻）；純拉丁/數字→原樣搜（不翻）；含漢字且無假名→翻。"""
    q = (q or "").strip()
    if not q:
        return False
    if has_kana(q):
        return False
    if has_cjk(q):
        return True
    return False


async def _openai_chat(prompt: str, max_tokens: int = 200, temperature: float = 0.2) -> str:
    if not OPENAI_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": OPENAI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": temperature, "max_tokens": max_tokens},
            )
        if resp.status_code != 200:
            print(f"[JPQuery] OpenAI {resp.status_code}: {resp.text[:200]}")
            return ""
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print(f"[JPQuery] OpenAI 呼叫失敗: {type(e).__name__}: {e}")
        return ""


async def translate_to_jp(zh: str) -> str:
    """中文購物查詢 → 最佳單一日文搜尋關鍵字。不需翻譯時原樣回。"""
    z = (zh or "").strip()
    if not z or not needs_translation(z):
        return z
    if z in _translate_cache:
        return _translate_cache[z]

    prompt = (
        "你是日本樂天購物網的搜尋助手。把下面的中文購物查詢，轉成最能在日本樂天市場命中商品的"
        "『日文搜尋關鍵字』。規則：用日本賣家會用的詞；品牌 / 動漫 IP 用日文或片假名"
        "（例：鋼彈→ガンダム、寶可夢→ポケモン、樂高→レゴ）；屬性用日文（黑色→ブラック）；"
        "只輸出日文關鍵字本身，不要說明、不要標點、不要引號。\n查詢：" + z
    )
    out = await _openai_chat(prompt, max_tokens=60)
    jp = ""
    if out:
        jp = out.splitlines()[0].strip().strip('「」"\'　')
    jp = jp or z
    _translate_cache[z] = jp
    return jp


async def suggest(zh: str, limit: int = 8) -> list:
    """中文查詢 → 候補清單 [{label_zh, keyword_jp}, ...]，給前台下拉讓使用者自己挑。"""
    z = (zh or "").strip()
    if not z:
        return []
    key = f"{z}|{limit}"
    if key in _suggest_cache:
        return _suggest_cache[key]

    prompt = (
        "使用者在日本樂天購物網用中文搜尋，查詢可能對應到多種不同的日文商品分類。"
        f"請針對查詢「{z}」，回一個 JSON 陣列（最多 {limit} 個），每個元素格式："
        '{"label_zh":"繁體中文標籤","keyword_jp":"日文搜尋關鍵字"}。'
        "依最可能的意圖排序；若查詢很明確（單一商品 / 品牌）就回 1～3 個相近的即可。"
        "品牌 / IP 用日文或片假名。只輸出 JSON，不要 markdown、不要其它文字。"
    )
    out = await _openai_chat(prompt, max_tokens=400)
    if not out:
        return []

    out = re.sub(r'^```(?:json)?\s*', '', out.strip())
    out = re.sub(r'\s*```$', '', out).strip()
    try:
        arr = json.loads(out)
    except Exception as e:
        print(f"[JPQuery] suggest JSON 解析失敗: {e} | {out[:160]}")
        return []

    result = []
    for x in arr if isinstance(arr, list) else []:
        if isinstance(x, dict):
            lz = str(x.get("label_zh") or "").strip()
            kj = str(x.get("keyword_jp") or "").strip()
            if lz and kj:
                result.append({"label_zh": lz, "keyword_jp": kj})
        if len(result) >= limit:
            break

    _suggest_cache[key] = result
    return result
