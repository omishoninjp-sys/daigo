"""
搜尋詞紀錄 → 需求情報
====================
把客人在站內搜尋頁打的字記下來，用來看「有人實際在找、但你沒上架/沒貨」的真實需求。

設計：
- SQLite 單檔，預設放 Zeabur Volume 的 /data/search_log.db（持久）。
  沒掛 Volume 時自動退回 ./search_log.db（可跑，但重部署會清空）→ 不會卡你先測。
- 記錄是 fire-and-forget：包在 try/except，永遠不影響 /api/search 本身。
- 寫入走 asyncio.to_thread，不阻塞 async 回應。

環境變數（選填）：
  SEARCH_LOG_DB   自訂 DB 路徑（預設 /data/search_log.db，退回 ./search_log.db）

main.py 用法：
  from search_log import log_search, stats
  await log_search(raw=q, translated=searched, source=req.source, result_count=len(results))
  data = await stats(days=30)
"""
import os
import sqlite3
import asyncio
import threading
from datetime import datetime, timezone, timedelta

_DEFAULT_PATHS = [
    os.environ.get("SEARCH_LOG_DB", "").strip() or "/data/search_log.db",
    "./search_log.db",
]

_conn: sqlite3.Connection | None = None
_db_path: str = ""
_lock = threading.Lock()


def _pick_path() -> str:
    for p in _DEFAULT_PATHS:
        if not p:
            continue
        d = os.path.dirname(p) or "."
        try:
            os.makedirs(d, exist_ok=True)
            # 測試可寫
            test = os.path.join(d, ".sl_write_test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return p
        except Exception:
            continue
    return "./search_log.db"


def _get_conn() -> sqlite3.Connection | None:
    global _conn, _db_path
    if _conn is not None:
        return _conn
    try:
        _db_path = _pick_path()
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                raw          TEXT NOT NULL,
                translated   TEXT,
                source       TEXT,
                result_count INTEGER
            );
        """)
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_searches_ts ON searches(ts);")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_searches_raw ON searches(raw);")
        _conn.commit()
        if _db_path.startswith("./") or _db_path == "search_log.db":
            print(f"[SearchLog] ⚠️ 用本機檔 {_db_path}（未掛 Volume，重部署會清空）")
        else:
            print(f"[SearchLog] ✓ DB: {_db_path}")
    except Exception as e:
        print(f"[SearchLog] ❌ 初始化失敗: {type(e).__name__}: {e}")
        _conn = None
    return _conn


# ─────────────────────────────────────────────────────────────
# 寫入
# ─────────────────────────────────────────────────────────────
def _log_sync(raw: str, translated: str, source: str, result_count: int) -> None:
    conn = _get_conn()
    if conn is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn.execute(
            "INSERT INTO searches (ts, raw, translated, source, result_count) VALUES (?,?,?,?,?)",
            (ts, (raw or "")[:200], (translated or "")[:200], (source or "")[:40], int(result_count)),
        )
        conn.commit()


async def log_search(raw: str, translated: str = "", source: str = "",
                     result_count: int = 0) -> None:
    """記一筆搜尋。fire-and-forget，永不拋例外。"""
    raw = (raw or "").strip()
    if not raw:
        return
    try:
        await asyncio.to_thread(_log_sync, raw, translated, source, result_count)
    except Exception as e:
        print(f"[SearchLog] ⚠️ 寫入略過: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────
# 查詢（給需求情報面板）
# ─────────────────────────────────────────────────────────────
def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()


def _stats_sync(days: int, limit: int) -> dict:
    conn = _get_conn()
    if conn is None:
        return {"available": False}
    since = _since(days)
    cur = conn.cursor()

    # 總覽
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT raw) FROM searches WHERE ts >= ?", (since,))
    total, distinct_terms = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM searches WHERE ts >= ? AND result_count = 0", (since,))
    zero_total = cur.fetchone()[0]

    # 熱門搜尋詞（含平均結果數、零結果率）
    cur.execute("""
        SELECT raw,
               COUNT(*) AS cnt,
               ROUND(AVG(result_count), 1) AS avg_results,
               SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END) AS zero_cnt
        FROM searches WHERE ts >= ?
        GROUP BY raw ORDER BY cnt DESC, raw ASC LIMIT ?
    """, (since, limit))
    top_terms = [
        {"raw": r[0], "count": r[1], "avg_results": r[2] or 0,
         "zero_rate": round((r[3] or 0) / r[1], 2) if r[1] else 0}
        for r in cur.fetchall()
    ]

    # 零結果搜尋詞（核心：有人找、但你沒上架/沒貨）
    cur.execute("""
        SELECT raw, COUNT(*) AS cnt, MAX(ts) AS last_ts
        FROM searches WHERE ts >= ? AND result_count = 0
        GROUP BY raw ORDER BY cnt DESC, last_ts DESC LIMIT ?
    """, (since, limit))
    zero_terms = [{"raw": r[0], "count": r[1], "last_ts": r[2]} for r in cur.fetchall()]

    # 每日搜尋量
    cur.execute("""
        SELECT substr(ts, 1, 10) AS d, COUNT(*) AS cnt
        FROM searches WHERE ts >= ?
        GROUP BY d ORDER BY d ASC
    """, (since,))
    daily = [{"date": r[0], "count": r[1]} for r in cur.fetchall()]

    # 最近 30 筆
    cur.execute("""
        SELECT ts, raw, translated, source, result_count
        FROM searches WHERE ts >= ? ORDER BY ts DESC LIMIT 30
    """, (since,))
    recent = [
        {"ts": r[0], "raw": r[1], "translated": r[2], "source": r[3], "result_count": r[4]}
        for r in cur.fetchall()
    ]

    return {
        "available": True,
        "db_path": _db_path,
        "days": days,
        "totals": {
            "searches": total or 0,
            "distinct_terms": distinct_terms or 0,
            "zero_result_searches": zero_total or 0,
        },
        "top_terms": top_terms,
        "zero_terms": zero_terms,
        "daily": daily,
        "recent": recent,
    }


async def stats(days: int = 30, limit: int = 50) -> dict:
    try:
        return await asyncio.to_thread(_stats_sync, days, limit)
    except Exception as e:
        print(f"[SearchLog] ❌ 統計失敗: {type(e).__name__}: {e}")
        return {"available": False, "error": str(e)}
