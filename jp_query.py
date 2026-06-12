"""
中文 → 日文 查詢處理（加速版）
==============================
- needs_translation(q)：有假名=已是日文不翻；純拉丁/數字=原樣；漢字無假名=翻
- translate_to_jp(q)：回最佳單一日文關鍵字（/api/search 的 fallback）
- suggest(q)：回候補 [{label_zh, keyword_jp}]（/api/suggest，給前台下拉）

加速重點：
1. 先查靜態對照表 jp_query_seed（常用詞秒回，完全不打 LLM）。
2. 共用一個 httpx.AsyncClient（免每次 TLS 握手）。
3. LLM 輸出改「中文|日文」精簡行（token 少約 4 成、生成更快、免 JSON 解析失敗），候補上限 6。
4. in-memory 快取（熱門長尾詞翻一次就存）。

重用 config 的 OPENAI_API_KEY / OPENAI_MODEL（與 seo_title.py 同一把 gpt-4o-mini）。
"""
import re

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL
from jp_query_seed import seed_lookup

_KANA = re.compile(r'[\u3040-\u30ff]')      # 平假名 / 片假名
_CJK = re.compile(r'[\u4e00-\u9fff]')       # 漢字

_translate_cache: dict = {}
_suggest_cache: dict = {}

# 共用 client（lazy init，免每次重建連線）
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    return _client


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


async def _openai_chat(prompt: str, max_tokens: int = 160, temperature: float = 0.2) -> str:
    if not OPENAI_API_KEY:
        return ""
    try:
        resp = await _get_client().post(
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

    # 1) 先查對照表（命中用第一個候補的日文，秒回、不打 LLM）
    seeded = seed_lookup(z)
    if seeded:
        return seeded[0]["keyword_jp"]

    if z in _translate_cache:
        return _translate_cache[z]

    prompt = (
        "你是日本樂天/Yahoo購物的搜尋助手。把下面的中文購物查詢，轉成最能命中商品的"
        "『日文搜尋關鍵字』。品牌/動漫 IP 用日文或片假名（鋼彈→ガンダム、寶可夢→ポケモン）；"
        "屬性用日文（黑色→ブラック）；只輸出日文關鍵字本身，不要說明、標點、引號。\n查詢：" + z
    )
    out = await _openai_chat(prompt, max_tokens=40)
    jp = out.splitlines()[0].strip().strip('「」"\'　') if out else ""
    jp = jp or z
    _translate_cache[z] = jp
    return jp


def _parse_lines(out: str, limit: int) -> list:
    """解析 LLM 的『中文|日文』精簡行。"""
    result = []
    for line in (out or "").replace("｜", "|").splitlines():
        line = line.strip().lstrip("0123456789.-・•、 ").strip()
        if "|" not in line:
            continue
        lz, kj = line.split("|", 1)
        lz, kj = lz.strip(), kj.strip()
        if lz and kj:
            result.append({"label_zh": lz, "keyword_jp": kj})
        if len(result) >= limit:
            break
    return result


async def suggest(zh: str, limit: int = 6) -> list:
    """中文查詢 → 候補清單 [{label_zh, keyword_jp}, ...]，給前台下拉。"""
    z = (zh or "").strip()
    if not z:
        return []

    # 1) 對照表命中 → 秒回，不打 LLM
    seeded = seed_lookup(z)
    if seeded:
        return seeded[:limit]

    # 2) 快取
    key = f"{z}|{limit}"
    if key in _suggest_cache:
        return _suggest_cache[key]

    # 3) LLM（精簡行格式）
    prompt = (
        f"使用者在日本樂天/Yahoo購物用中文搜尋「{z}」，可能對應多種日文商品分類。"
        f"列出最多 {limit} 個候補，每行一個，格式為「中文標籤|日文關鍵字」（用半形直線 | 分隔）。"
        "依最可能的意圖排序；查詢很明確就回 1～3 個即可。品牌/IP 用日文或片假名。"
        "只輸出這些行，不要編號、不要其它文字。"
    )
    out = await _openai_chat(prompt, max_tokens=200)
    result = _parse_lines(out, limit)
    _suggest_cache[key] = result
    return result
