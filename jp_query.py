"""
中文 → 日文 查詢處理（加速版 + 品牌組合）
========================================
- needs_translation(q)：有假名=已是日文不翻；純拉丁/數字=原樣；漢字無假名=翻
- translate_to_jp(q)：回最佳單一日文關鍵字（/api/search 的 fallback）
- suggest(q)：回候補 [{label_zh, keyword_jp}]（/api/suggest，給前台下拉）

處理順序（suggest / translate 都一致）：
0. 組合查詢「英文/品牌 + 中文分類」（例：snidel 上衣）
   → 保留品牌原樣，只把中文分類展開成日文，品牌接在每個日文關鍵字前
   （走靜態對照表，秒回；不打 LLM）
1. 靜態對照表 jp_query_seed（常用詞秒回）
2. in-memory 快取
3. LLM（gpt-4o-mini，精簡行輸出；prompt 也叮嚀保留品牌字當後援）
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


# ─────────────────────────────────────────────────────────────────────
# 組合查詢：英文/品牌 + 中文分類（例：「snidel 上衣」「NB 鞋子」）
# ─────────────────────────────────────────────────────────────────────
def _combo_split(zh: str):
    """
    偵測「非中文前綴（品牌/英數）+ 單一中文分類」。
    僅當：恰好一個 token 能在對照表命中，且其餘 token 皆非中文時成立
    （中文修飾詞如「黑色」交給 LLM，避免半翻譯怪怪的）。
    回 (prefix, base_candidates) 或 None。
    """
    toks = (zh or "").split()
    if len(toks) < 2:
        return None
    cat_idx = [i for i, t in enumerate(toks) if seed_lookup(t)]
    if len(cat_idx) != 1:
        return None
    ci = cat_idx[0]
    others = [t for i, t in enumerate(toks) if i != ci]
    if any(has_cjk(t) for t in others):
        return None                      # 還有中文修飾詞 → 交給 LLM
    prefix = " ".join(others).strip()
    base = seed_lookup(toks[ci])
    if not prefix or not base:
        return None
    return prefix, base


def _combo_suggest(prefix: str, base: list, limit: int) -> list:
    out = []
    for c in base:
        out.append({
            "label_zh": f"{prefix} {c['label_zh']}",
            "keyword_jp": f"{prefix} {c['keyword_jp']}",
        })
        if len(out) >= limit:
            break
    return out


# ─────────────────────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────────────────────
async def _openai_chat(prompt: str, max_tokens: int = 200, temperature: float = 0.2) -> str:
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

    # 0) 品牌 + 中文分類 → 品牌 + 對照表第一個日文（秒回）
    combo = _combo_split(z)
    if combo:
        prefix, base = combo
        return f"{prefix} {base[0]['keyword_jp']}"

    # 1) 對照表
    seeded = seed_lookup(z)
    if seeded:
        return seeded[0]["keyword_jp"]

    if z in _translate_cache:
        return _translate_cache[z]

    prompt = (
        "你是日本樂天/Yahoo購物的搜尋助手。把下面的中文購物查詢，轉成最能命中商品的"
        "『日文搜尋關鍵字』。保留查詢中的英文或品牌字（原樣不翻）；中文品牌/IP 用日文或"
        "片假名（鋼彈→ガンダム）；屬性用日文（黑色→ブラック）；只輸出日文關鍵字本身，"
        "不要說明、標點、引號。\n查詢：" + z
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

    # 0) 品牌 + 中文分類（例：snidel 上衣）→ 保留品牌、展開分類，秒回
    combo = _combo_split(z)
    if combo:
        prefix, base = combo
        return _combo_suggest(prefix, base, limit)

    # 1) 對照表命中 → 秒回
    seeded = seed_lookup(z)
    if seeded:
        return seeded[:limit]

    # 2) 快取
    key = f"{z}|{limit}"
    if key in _suggest_cache:
        return _suggest_cache[key]

    # 3) LLM（精簡行格式；叮嚀保留品牌字）
    prompt = (
        f"使用者在日本樂天/Yahoo購物用中文搜尋「{z}」，可能對應多種日文商品分類。"
        f"列出最多 {limit} 個候補，每行一個，格式「中文標籤|日文關鍵字」（用半形直線 | 分隔）。"
        "規則：保留查詢裡的英文或品牌字（原樣不翻），只把中文分類詞展開成不同日文關鍵字，"
        "並把品牌字放在每個日文關鍵字前面（例：『snidel 上衣』→ snidel 上衣|snidel トップス、"
        "snidel T恤|snidel Tシャツ）。中文品牌/IP 用日文或片假名。"
        "依最可能的意圖排序；查詢很明確就回 1～3 個即可。只輸出這些行，不要編號或其它文字。"
    )
    out = await _openai_chat(prompt, max_tokens=220)
    result = _parse_lines(out, limit)
    _suggest_cache[key] = result
    return result
