"""
中文 → 日文 查詢處理（完整版：品牌 + 屬性 + 分類拆解）
=====================================================
參考 Buyee「Auto-Translate & Search」與電商自動完成通則（屬性感知、不丟條件）。

核心：把查詢拆成三種角色，組裝出「完整」的日文候補，不丟掉任何使用者打的條件——
  · 品牌 / 英文 / 數字  → 原樣保留（snidel、NB、CONVERSE…）
  · 屬性（顏色/長短袖/材質/版型/性別/尺碼/花色…）→ 翻成日文（黃色→イエロー）
  · 分類（上衣、鞋子、包包…）→ 展開成多個日文（トップス / Tシャツ / シャツ…）
分類以外的全部「掛在每個候補上」，只把分類那一格做歧義展開。

處理順序（suggest / translate 一致）：
0. _analyze 能把每個 token 都歸類（恰好一個分類、其餘是品牌或已知屬性）→ 秒回（不打 LLM）
1. in-memory 快取
2. LLM（gpt-4o-mini）後援：prompt 明確要求「保留品牌＋翻譯並保留所有屬性，只展開分類」
   → 接住長尾（不認得的中文修飾詞、罕見屬性、多分類等）

對外介面：
  needs_translation(q) / translate_to_jp(q) / suggest(q)
"""
import re

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL
from jp_query_seed import seed_lookup        # 分類對照（含別名），回 [{label_zh, keyword_jp}] 或 None

_KANA = re.compile(r'[\u3040-\u30ff]')
_CJK = re.compile(r'[\u4e00-\u9fff]')

_translate_cache: dict = {}
_suggest_cache: dict = {}

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
    return has_cjk(q)


# ─────────────────────────────────────────────────────────────────────
# 常用屬性對照（中文 → 日文）。秒回用；長尾交給 LLM。
# ─────────────────────────────────────────────────────────────────────
_ATTR = {
    # 顏色
    "黑色": "ブラック", "黑": "ブラック", "白色": "ホワイト", "白": "ホワイト",
    "紅色": "レッド", "紅": "レッド", "藍色": "ブルー", "藍": "ブルー",
    "綠色": "グリーン", "綠": "グリーン", "黃色": "イエロー", "黃": "イエロー",
    "粉紅": "ピンク", "粉色": "ピンク", "粉": "ピンク", "紫色": "パープル", "紫": "パープル",
    "灰色": "グレー", "灰": "グレー", "棕色": "ブラウン", "咖啡色": "ブラウン",
    "米色": "ベージュ", "米白": "ベージュ", "卡其": "カーキ", "卡其色": "カーキ",
    "橘色": "オレンジ", "橙色": "オレンジ", "金色": "ゴールド", "銀色": "シルバー",
    "海軍藍": "ネイビー", "藏青": "ネイビー", "深藍": "ネイビー",
    # 袖長 / 版長
    "長袖": "長袖", "短袖": "半袖", "無袖": "ノースリーブ", "七分袖": "七分袖",
    "長版": "ロング", "長款": "ロング", "短版": "ショート", "短款": "ショート",
    # 版型
    "寬鬆": "オーバーサイズ", "寬版": "オーバーサイズ", "修身": "スリム",
    "合身": "スリム", "緊身": "タイト",
    # 材質
    "純棉": "コットン", "棉": "コットン", "針織": "ニット", "蕾絲": "レース",
    "牛仔": "デニム", "皮革": "レザー", "皮": "レザー", "羊毛": "ウール",
    "雪紡": "シフォン", "真絲": "シルク", "絲": "シルク", "毛呢": "ウール",
    # 花色
    "條紋": "ストライプ", "格紋": "チェック", "格子": "チェック",
    "素色": "無地", "印花": "プリント", "碎花": "花柄",
    # 對象 / 尺碼
    "女裝": "レディース", "女款": "レディース", "男裝": "メンズ", "男款": "メンズ",
    "童裝": "キッズ", "兒童": "キッズ", "親子": "親子",
    "大尺碼": "大きいサイズ", "加大": "大きいサイズ",
    # 季節
    "春季": "春", "夏季": "夏", "秋季": "秋", "冬季": "冬",
}


def _attr_lookup(tok: str) -> str | None:
    return _ATTR.get((tok or "").strip())


# ─────────────────────────────────────────────────────────────────────
# 拆解：每個 token 歸類成 cat / attr / kw；恰好一個 cat 且無不明中文才成立
# ─────────────────────────────────────────────────────────────────────
def _analyze(zh: str):
    """
    回 (roles, base) 或 None。
      roles: [(kind, zh_token, jp_value), ...]  kind ∈ {cat, attr, kw}
      base:  分類的日文候補 [{label_zh, keyword_jp}, ...]
    None 表示交給 LLM（含不明中文、0 或 >1 個分類）。
    """
    toks = (zh or "").split()
    if not toks:
        return None

    roles = []
    base = None
    cat_count = 0
    for t in toks:
        cands = seed_lookup(t)
        if cands:
            cat_count += 1
            base = cands
            roles.append(("cat", t, None))
            continue
        a = _attr_lookup(t)
        if a:
            roles.append(("attr", t, a))
            continue
        if has_cjk(t):
            return None                      # 不認得的中文 → 交給 LLM
        roles.append(("kw", t, t))           # 品牌 / 英文 / 數字，原樣

    if cat_count != 1:
        return None                          # 0 或多個分類 → 交給 LLM
    return roles, base


def _build_suggest(roles, base, limit) -> list:
    out = []
    for v in base:
        zh_parts, jp_parts = [], []
        for kind, ztok, val in roles:
            if kind == "cat":
                zh_parts.append(v["label_zh"]); jp_parts.append(v["keyword_jp"])
            else:                            # attr / kw
                zh_parts.append(ztok); jp_parts.append(val)
        out.append({"label_zh": " ".join(zh_parts), "keyword_jp": " ".join(jp_parts)})
        if len(out) >= limit:
            break
    return out


def _build_translate(roles, base) -> str:
    v = base[0]
    parts = [v["keyword_jp"] if kind == "cat" else val for kind, _z, val in roles]
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────────────────────
async def _openai_chat(prompt: str, max_tokens: int = 220, temperature: float = 0.2) -> str:
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


_RULES = (
    "規則：保留查詢裡的英文／品牌／型號（原樣不翻）；其餘屬性詞（顏色、袖長、材質、"
    "版型、性別、尺碼、花色…）一律翻成日文並『保留』在結果裡，不可遺漏任何條件；"
    "中文品牌或 IP 用日文或片假名。"
)


async def translate_to_jp(zh: str) -> str:
    """中文購物查詢 → 最佳單一日文搜尋關鍵字（保留全部條件）。不需翻譯時原樣回。"""
    z = (zh or "").strip()
    if not z or not needs_translation(z):
        return z

    an = _analyze(z)
    if an:
        return _build_translate(*an)

    if z in _translate_cache:
        return _translate_cache[z]

    prompt = (
        f"你是日本樂天／Yahoo購物的搜尋助手。把中文購物查詢「{z}」轉成最能命中商品的"
        f"單一日文搜尋關鍵字（各詞以半形空格分隔）。{_RULES}"
        "只輸出日文關鍵字本身，不要說明、引號或標點。"
    )
    out = await _openai_chat(prompt, max_tokens=60)
    jp = (out.splitlines()[0].strip().strip('「」"\'　') if out else "") or z
    _translate_cache[z] = jp
    return jp


def _parse_lines(out: str, limit: int) -> list:
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


async def suggest(zh: str, limit: int = 8) -> list:
    """中文查詢 → 候補清單 [{label_zh, keyword_jp}]，給前台下拉（只展開分類，保留品牌＋屬性）。"""
    z = (zh or "").strip()
    if not z:
        return []

    # 0) 可完整拆解 → 秒回（品牌＋屬性掛在每個分類候補上）
    an = _analyze(z)
    if an:
        return _build_suggest(*an, limit)

    # 1) 快取
    key = f"{z}|{limit}"
    if key in _suggest_cache:
        return _suggest_cache[key]

    # 2) LLM 後援（保留品牌＋所有屬性，只展開分類）
    prompt = (
        f"使用者在日本樂天／Yahoo購物用中文搜尋「{z}」，分類可能對應多種日文說法。"
        f"列出最多 {limit} 個候補，每行一個，格式「中文標籤|日文關鍵字」（半形直線 | 分隔）。"
        f"{_RULES}"
        "只把『分類詞』展開成不同日文，品牌與屬性原封不動掛在每個候補上"
        "（例：『snidel 上衣 黃色』→ snidel 上衣 黃色|snidel トップス イエロー、"
        "snidel T恤 黃色|snidel Tシャツ イエロー）。"
        "依最可能意圖排序；查詢已很明確就回 1～3 個。只輸出這些行，不要編號或其它文字。"
    )
    out = await _openai_chat(prompt, max_tokens=260)
    result = _parse_lines(out, limit)
    _suggest_cache[key] = result
    return result
