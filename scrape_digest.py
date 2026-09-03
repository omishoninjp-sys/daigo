"""
每日爬取摘要信（spec-scrape-monitoring.md 第四節「每日摘要」＋第五節「寄信方式」）
================================================================================
這支做三件事：
  1. 聚合    build_summary() —— 從 main.py 的 summary 端點搬過來，**輸出一字不改**
  2. 排版    render_digest() —— 規格第四節那個格式
  3. 寄送    daily_digest_loop() —— 固定時刻、一天一封、fail-safe

**只做每日摘要，不做即時警報（規格第四節後半的三個條件）。**

🔴 這封信要回答的問題（規格最後一節）：
   「這個月哪個網域最該修？修了能救回多少？」
   回答不了就是做錯了。所以每一行都帶「失敗次數 + 該網域成功率」，
   而不是只有全站一個數字 —— 2026-09-02 實測近 6 天：

       前三大來源 mercari 96/98、zozo 43/45、rakuten 34/36 → 173/179 = 96.6%
       同期       japan.us.mercari.com  0/12   一次都沒成功過，六天沒人知道
                  gu-global.com         5/25   20%
                  suruga-ya.jp          3/11   27%

   **全站成功率會把單一來源的崩壞整個蓋掉。**

🔴 心跳（規格第 105 行，最容易漏的一條）
   當天零失敗**也要寄**一行「今日無失敗」。沒收到信要能區分
   「沒問題」和「系統掛了」。所以這支**沒有任何 early return** ——
   零失敗、零紀錄都只影響內文長什麼樣，不影響寄不寄。
   測試用「任何輸入下 send 都被呼叫恰好一次」釘死這件事，
   那比檢查文字內容更能擋住日後有人加 early return。
"""
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

import httpx

import scrape_monitor
from config import (DIGEST_ENABLED, DIGEST_FROM, DIGEST_HOUR_UTC,
                    DIGEST_STREAK_DAYS, DIGEST_TO, RESEND_API_KEY)

# ─────────────────────────────────────────────────────────────────────
# 聚合（原本在 main.py 的 /api/admin/scrape-log/summary 裡）
# ─────────────────────────────────────────────────────────────────────
# 樣本只給這幾個欄位。url_path 已經去掉 query string，整筆直接給沒有外洩問題。
_SAMPLE_FIELDS = ("ts", "domain", "platform_id", "source",
                  "http_status", "elapsed_ms", "error_brief", "url_path")


def pick_samples(rows: list, limit: int = 3) -> list:
    """
    每種 failure_kind 抽幾筆看細節。

    優先抽不同網域：同一個壞掉的網域連刷 3 筆，看起來像 3 個證據，其實只有 1 個，
    判斷不出分類準不準。網域不夠才拿同網域的補滿。
    """
    picked, seen_domain, seen_id = [], set(), set()
    for r in rows:
        d = r.get("domain")
        if d in seen_domain:
            continue
        seen_domain.add(d)
        seen_id.add(id(r))
        picked.append(r)
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        for r in rows:
            if id(r) in seen_id:
                continue
            picked.append(r)
            if len(picked) >= limit:
                break
    return [{k: r.get(k) for k in _SAMPLE_FIELDS} for r in picked]


def build_summary(day_list: list) -> dict:
    """
    ★ 這支的回傳值是 /api/admin/scrape-log/summary 的完整回應，**不可以動**。
      verify_scrape_log_api.py 的 48 項全部斷言在這個 dict 上，
      新增的東西（連續天數、最慢、逐網域成功率）一律另開函式，不要塞進來。

    day_list 必須是**已驗證過**的日期清單（main.py 的 _scrape_log_days 負責，
    它會 raise HTTPException —— 那是 HTTP 層的事，不搬進這支非 HTTP 模組）。
    """
    by_day: dict = {}
    entries: list = []
    for day in day_list:
        rows = scrape_monitor.read_day(day)
        ok_n = sum(1 for r in rows if r.get("ok"))
        by_day[day] = {"total": len(rows), "ok": ok_n, "failed": len(rows) - ok_n}
        entries.extend(rows)

    total = len(entries)
    ok_count = sum(1 for r in entries if r.get("ok"))
    failed = total - ok_count

    kinds: dict = {}
    by_domain: dict = {}
    failures_by_kind: dict = {}
    for r in entries:
        dom = r.get("domain") or "(unknown)"
        slot = by_domain.setdefault(dom, {"domain": dom, "total": 0, "failed": 0, "kinds": {}})
        slot["total"] += 1
        if r.get("ok"):
            continue
        kind = r.get("failure_kind") or "other"
        kinds[kind] = kinds.get(kind, 0) + 1
        slot["failed"] += 1
        slot["kinds"][kind] = slot["kinds"].get(kind, 0) + 1
        failures_by_kind.setdefault(kind, []).append(r)

    # 只列有失敗的網域（沒失敗的網域不需要決定任何事），失敗次數多的排前面
    domains = sorted(
        (d for d in by_domain.values() if d["failed"] > 0),
        key=lambda d: (-d["failed"], -d["total"], d["domain"]),
    )

    return {
        "days": day_list,
        "log_dir": scrape_monitor.log_dir(),
        "by_day": by_day,
        "total": total,
        "ok": ok_count,
        "failed": failed,
        "success_rate_pct": round(ok_count / total * 100, 1) if total else None,
        "failure_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "domains_by_failure": domains,
        "samples": {k: pick_samples(v) for k, v in
                    sorted(failures_by_kind.items(), key=lambda kv: -len(kv[1]))},
    }


# ─────────────────────────────────────────────────────────────────────
# 摘要信專用的統計（不進 build_summary 的回傳值）
# ─────────────────────────────────────────────────────────────────────
def failure_streaks(days: list) -> dict:
    """
    每個網域「連續幾天有失敗」（從 days[0] 往回數，days 必須是新到舊）。

    ★ days[0] **必須是報告的那一天**，不是今天。呼叫端用
      scrape_monitor.days_back_from(報告日, N) 產生，不要用 recent_days()。

    ★ 定義用寬鬆版：**當天該網域有失敗就算失敗日**，即使同一天也有成功。
      嚴格版（當天完全沒成功才算）會把最該抓的案例漏掉 ——
      gu-global.com 2026-09-02 有 3 次失敗、1 次成功，嚴格版連續天數會斷在這裡，
      而它正是「五次有四次不能用」的來源。
      寬鬆版的代價是高流量網域偶發 1 次也會被標記，所以**每一行都附成功率**：
      `item.rakuten.co.jp 2 次 (34/36)` 一眼看得出不是危機。
    """
    streak: dict = {}
    still_counting = set()
    first = True
    for day in days:
        rows = scrape_monitor.read_day(day)
        # ★ 變數名一定要是「那一天」不是「今天」—— 2026-09-03 錨錯的那個 bug，
        #   在程式碼裡看起來就是這個名字讓人以為它一定從今天開始。
        failed_that_day = {r.get("domain") for r in rows
                           if not r.get("ok") and r.get("domain")}
        if first:
            still_counting = set(failed_that_day)
            first = False
        else:
            still_counting &= failed_that_day
        for dom in still_counting:
            streak[dom] = streak.get(dom, 0) + 1
        if not still_counting:
            break
    return streak


def domain_rates(rows: list) -> dict:
    """{domain: (ok, total)}。分母是該網域當天全部的爬取，不是只有失敗的。"""
    out: dict = {}
    for r in rows:
        dom = r.get("domain") or "(unknown)"
        ok, total = out.get(dom, (0, 0))
        out[dom] = (ok + (1 if r.get("ok") else 0), total + 1)
    return out


_SLOW_MIN_MS = 5000       # 低於這個不算「慢」，列出來只是雜訊


def slowest_domains(rows: list, top: int = 3, min_n: int = 2,
                    min_avg_ms: int = _SLOW_MIN_MS) -> list:
    """
    平均耗時最長的網域 [(domain, avg_ms, n)]。

    ★ min_n=2：單獨一筆 42 秒的離群值不該佔據「今日最慢」那一欄 ——
      要看的是「這個網域一直很慢」，不是「有一次很慢」。
    ★ min_avg_ms：取前三名而沒有下限的話，全站都正常的日子會列出
      「平均 1.2s」當今日最慢 —— 那不是資訊，是雜訊。
      沒有網域夠慢時整段不出現。
    """
    acc: dict = {}
    for r in rows:
        ms = r.get("elapsed_ms")
        if not isinstance(ms, (int, float)):
            continue
        dom = r.get("domain") or "(unknown)"
        s, n = acc.get(dom, (0, 0))
        acc[dom] = (s + ms, n + 1)
    out = [(d, s / n, n) for d, (s, n) in acc.items()
           if n >= min_n and s / n >= min_avg_ms]
    out.sort(key=lambda x: -x[1])
    return out[:top]


# ─────────────────────────────────────────────────────────────────────
# 排版
# ─────────────────────────────────────────────────────────────────────
_NEED_ACTION = (("parse_failed", "解析失敗"), ("blocked", "被擋（機房 IP）"))
_IGNORE = (("not_found", "已下架/404"), ("timeout", "超時"), ("other", "其他"))

_LOW_RATE_PCT = 70.0      # 低於這個就列進「成功率偏低」
_LOW_RATE_MIN_N = 3       # 樣本太少沒有意義（0/1 只是雜訊）


def _rate_tag(ok: int, total: int) -> str:
    """
    成功率後面的嚴重度標記。

    ★ 0/12 和 2/8 的嚴重度完全不同：前者是**完全不能用**（這個來源等於沒接），
      後者是**不穩**（多試幾次會過）。處置也不同 —— 前者要停下來查，
      後者可以排進待辦。只給百分比看不出這個差別。
    """
    if total <= 0:
        return ""
    if ok == 0:
        return "・完全不能用"
    if ok / total * 100 < _LOW_RATE_PCT:
        return "・不穩"
    return ""


def _line(dom: str, n: int, rates: dict, streaks: dict) -> str:
    ok, total = rates.get(dom, (0, 0))
    rate = f"({ok}/{total})" if total else ""
    marks = []
    st = streaks.get(dom, 0)
    if st >= 2:
        marks.append(f"連續 {st} 天失敗")
    tag = _rate_tag(ok, total).lstrip("・")
    if tag:
        marks.append(tag)
    suffix = ("  ← " + "・".join(marks)) if marks else ""
    return f"    {dom:<28}{n:>3} 次  {rate}{suffix}"


def render_digest(day: str, rows: list, streaks: dict) -> tuple:
    """回 (subject, body)。**任何輸入都要回得出東西**，不可以回 None。"""
    subject = f"【daigo 每日爬取摘要】{day}"
    L = [f"{subject}（UTC）", ""]

    # ── 心跳：零紀錄與零失敗要分開講，兩者在「系統有沒有活著」上意義不同 ──
    if not rows:
        L.append("⚠️ 當日沒有任何爬取紀錄 —— 可能沒有流量，"
                 "也可能是服務或紀錄機制停擺")
        return subject, "\n".join(L)

    total = len(rows)
    ok = sum(1 for r in rows if r.get("ok"))
    failed = total - ok
    rates = domain_rates(rows)

    if failed == 0:
        L.append(f"今日無失敗（成功 {ok} / 失敗 0）")
        return subject, "\n".join(L)

    L.append(f"成功 {ok} / 失敗 {failed}（成功率 {ok / total * 100:.1f}%）")
    L.append("")

    kinds: dict = {}
    for r in rows:
        if r.get("ok"):
            continue
        k = r.get("failure_kind") or "other"
        kinds.setdefault(k, {})
        d = r.get("domain") or "(unknown)"
        kinds[k][d] = kinds[k].get(d, 0) + 1

    # ── 需要處理 ──
    L.append("■ 需要處理")
    listed_above = set()          # 已經列過的網域，下一區不重複列
    any_action = False
    for kind, label in _NEED_ACTION:
        doms = kinds.get(kind) or {}
        if not doms:
            continue
        any_action = True
        L.append(f"  {label}  {sum(doms.values())} 次")
        for dom, n in sorted(doms.items(), key=lambda kv: (-kv[1], kv[0])):
            listed_above.add(dom)
            L.append(_line(dom, n, rates, streaks))
    if not any_action:
        L.append("  （無）")
    L.append("")

    # ── 成功率偏低的來源（規格沒有，2026-09-02 加）──
    # ★ 排除「需要處理」已經列過的網域 —— 那區每一行本來就帶成功率了，
    #   重複一次只是稀釋注意力。這一區真正的價值是補上**那區看不到的**：
    #   失敗全部落在 not_found／timeout（「不用管」組）的網域不會出現在上面，
    #   但 0/12 這種數字仍然該被看見。
    low = []
    for dom, (o, t) in rates.items():
        if dom in listed_above:
            continue
        if t >= _LOW_RATE_MIN_N and o / t * 100 < _LOW_RATE_PCT:
            low.append((dom, o, t))
    if low:
        low.sort(key=lambda x: (x[1] / x[2], -(x[2] - x[1])))
        L.append(f"■ 成功率偏低的來源（≧{_LOW_RATE_MIN_N} 次且低於 {_LOW_RATE_PCT:.0f}%，"
                 f"上面沒列到的）")
        for dom, o, t in low:
            st = streaks.get(dom, 0)
            marks = ([f"連續 {st} 天失敗"] if st >= 2 else []) + \
                    ([_rate_tag(o, t).lstrip("・")] if _rate_tag(o, t) else [])
            suffix = ("  ← " + "・".join(marks)) if marks else ""
            L.append(f"    {dom:<28}{o:>3}/{t:<4}{o / t * 100:5.1f}%{suffix}")
        L.append("")

    # ── 不用管：只給數字，不列細節（規格第 103 行）──
    ignore_bits = [f"{label} {sum((kinds.get(k) or {}).values())} 次"
                   for k, label in _IGNORE if kinds.get(k)]
    if ignore_bits:
        L.append("■ 不用管")
        L.append("  " + "     ".join(ignore_bits))
        L.append("")

    # ── 今日最慢 ──
    slow = slowest_domains(rows)
    if slow:
        L.append("■ 今日最慢")
        for dom, avg, n in slow:
            L.append(f"    {dom:<28}平均 {avg / 1000:.1f}s（{n} 次）")

    return subject, "\n".join(L).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────
# 寄送（Resend；規格第五節明確禁止 Gmail SMTP）
# ─────────────────────────────────────────────────────────────────────
_RESEND_URL = "https://api.resend.com/emails"

# 🔴 這封信整封是中文，編碼錯了就等於沒有價值（2026-09-03）
#   ★ 為什麼不用 httpx 的 json= 參數：
#     httpx 0.28.1 的 encode_json 是 `json_dumps(..., ensure_ascii=False).encode("utf-8")`，
#     送出去的 bytes 本來就對 —— **但那是它的內部實作**，而且它設的
#     Content-Type 是 `application/json`（沒有 charset）。
#     舊版 httpx 用 ensure_ascii=True（純 ASCII 跳脫，也安全），
#     哪天再改一次、或有人手滑把 json= 換成 data=（表單編碼），
#     就會變成客人看到一整封亂碼而我們毫無防備。
#   ★ 所以自己 dumps、自己 encode("utf-8")、自己宣告 charset，
#     三件事都不交給函式庫決定。測試釘住的也是這三件。
_CONTENT_TYPE = "application/json; charset=utf-8"


def encode_payload(subject: str, body: str, sender: str = None,
                   to: str = None) -> bytes:
    """
    把 Resend 的 payload 編成 UTF-8 bytes。**明確編碼，不交給 httpx 決定。**

    ensure_ascii=False：讓中文以原生 UTF-8 進 bytes（e3 80 90 …）而不是
    跳脫成 \\uXXXX。兩者 Resend 都讀得懂，但原生的在抓包和 log 裡看得懂，
    出事的時候差很多。
    """
    data = {"from": sender if sender is not None else DIGEST_FROM,
            "to": [to if to is not None else DIGEST_TO],
            "subject": subject, "text": body}
    text = json.dumps(data, ensure_ascii=False)
    # 🔴 **整支只有這一個編碼點**。原本退路裡還有第二份
    #   `json.dumps(...).encode("utf-8")`，2026-09-03 的負向驗證抓到：
    #   把主要路徑改成 cp950 時，退路照樣用 UTF-8 把它救回來，測試因此全綠 ——
    #   那就是「修了一個路徑沒修到另一個」的形狀，兩份遲早會分岔。
    #   現在退路只換 errors 參數，編碼目標寫死一次。
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as e:
        # ★ 幾乎不會發生（要有落單的 surrogate），但**心跳比完美的文字重要**：
        #   寧可寄出一封有幾個 ? 的信，也不要因為一個怪字元整封寄不出去。
        print(f"[Digest] ⚠️ 內容含無法編碼的字元，已替換後寄出: {e}")
        return text.encode("utf-8", "replace")


async def send_email(subject: str, body: str) -> bool:
    """
    寄一封純文字信。成功回 True，其餘一律 False —— **永遠不 raise**。

    ★ 設定不全時回 False 而不是拋例外：寄信是附加功能，
      少一個環境變數不可以把背景任務打掛。
    """
    if not RESEND_API_KEY or not DIGEST_FROM or not DIGEST_TO:
        print("[Digest] ⏭️ 未設定 RESEND_API_KEY / DIGEST_FROM / DIGEST_TO，不寄信")
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                         "Content-Type": _CONTENT_TYPE},
                content=encode_payload(subject, body),   # ★ 不用 json=，見上面
            )
        if r.status_code in (200, 201, 202):
            print(f"[Digest] ✅ 已寄出：{subject}")
            return True
        # ★ 不印回應主體全文 —— 那可能夾帶金鑰或收件人資料
        #   （2026-09-02 seo_title.py 的教訓，同一種錯不要再犯一次）
        print(f"[Digest] ❌ 寄信失敗 HTTP {r.status_code}")
        return False
    except Exception as e:
        print(f"[Digest] ❌ 寄信例外: {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────
# 「今天寄過了」的兩層防護
# ─────────────────────────────────────────────────────────────────────
# 🔴 為什麼要兩層（2026-09-02）：
#   容器重啟迴圈就重跑，跨過寄送時刻就會再寄一次。光是這一天我們就重新部署了
#   5 次以上，所以這不是理論風險。
#     第一層 標記檔  /data/scrape_log/.digest_sent（Volume，跨重啟有效）
#     第二層 記憶體  _SENT_DAYS（本容器生命週期內有效）
#   標記檔在 Volume 掛掉或磁碟滿的時候會**寫入失敗**，那時只剩記憶體那層 ——
#   於是同一個容器內不會重寄，只有「寫檔失敗 + 容器重啟」同時發生才可能重寄。
#   兩層都寫、兩層都查，任一層說寄過就跳過。
_MARKER_NAME = ".digest_sent"
_SENT_DAYS: set = set()


def _marker_path() -> str:
    return os.path.join(scrape_monitor.log_dir(), _MARKER_NAME)


def already_sent(day: str) -> bool:
    """任一層說寄過就算寄過。查詢失敗一律回 False（寧可重寄，不可漏寄心跳）。"""
    if day in _SENT_DAYS:
        return True
    try:
        path = _marker_path()
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            return f.read().strip() == day
    except Exception as e:
        print(f"[Digest] ⚠️ 讀標記檔失敗（當成沒寄過）: {type(e).__name__}: {e}")
        return False


def mark_sent(day: str) -> None:
    """
    ★ 記憶體那層**先寫**，而且在 try 之外 —— 寫檔失敗時它仍然生效。
      順序反過來的話，寫檔一拋例外就兩層都沒設到，等於沒有防護。
    """
    _SENT_DAYS.add(day)
    try:
        with open(_marker_path(), "w", encoding="utf-8") as f:
            f.write(day)
    except Exception as e:
        print(f"[Digest] ⚠️ 寫標記檔失敗（本容器記憶體仍記得，重啟後可能重寄）: "
              f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────
# 一次摘要
# ─────────────────────────────────────────────────────────────────────
_SEND_RETRY = 3
_SEND_RETRY_SLEEP = 60


async def run_once(day: str, sender=None, mark: bool = True,
                   retries: int = None) -> dict:
    """
    寄出某一天的摘要。回 {sent, skipped, subject, body, attempts, marked}。
    **永遠不 raise。**

    ★ 沒有任何 early return 是刻意的（規格第 105 行的心跳）：
      零失敗、零紀錄都只影響內文，不影響寄不寄。
      唯一會跳過的是「今天已經寄過」——那不是內容判斷，是重複防護。

    mark=False 是**手動觸發**用的（/api/admin/scrape-log/digest）：
      · 不查 already_sent —— 排程今天寄過了，手動仍然要能再看一次
      · 不寫 .digest_sent —— 手動觸發不可以影響排程的判斷，
        否則「我手動看一次格式」會讓當天的自動摘要不寄，心跳就斷了
    ★ 用同一支函式加旗標，不另開 run_manual()：排版、重試、fail-safe
      只有一份，日後改了不會只改到其中一條路徑。

    retries 讓手動觸發只試 1 次 —— 預設的 3 次會讓 HTTP 請求卡上 2 分鐘。
    """
    out = {"sent": False, "skipped": False, "subject": "", "body": "",
           "attempts": 0, "marked": False}
    try:
        if mark and already_sent(day):
            out["skipped"] = True
            return out

        rows = scrape_monitor.read_day(day)
        # 🔴 錨點必須是**報告的那一天**，不是「今天」（2026-09-03 修）。
        #   本來是 recent_days(...)，錨在現在 —— 排程 01:00 UTC 跑的時候
        #   「今天」幾乎是空的，failure_streaks 第一個集合就是空集合、
        #   立刻 break，於是「連續 N 天失敗」在排程那條路徑上從來沒生效過。
        #   手動觸發看得到，只是因為當天打、錨點剛好對。
        streaks = failure_streaks(
            scrape_monitor.days_back_from(day, DIGEST_STREAK_DAYS))
        subject, body = render_digest(day, rows, streaks)
        out["subject"] = subject
        out["body"] = body

        n_try = _SEND_RETRY if retries is None else max(1, int(retries))
        send = sender or send_email
        for attempt in range(1, n_try + 1):
            out["attempts"] = attempt
            if await send(subject, body):
                out["sent"] = True
                break
            if attempt < n_try:
                await asyncio.sleep(_SEND_RETRY_SLEEP)

        # ★ 寄失敗也要標記。不標的話容器一重啟就整套重來，
        #   一次 Resend 故障會變成寄信轟炸。真正的訊號是「信沒來」。
        if mark:
            mark_sent(day)
            out["marked"] = True
        if not out["sent"]:
            print(f"[Digest] ❌ {day} 的摘要 {n_try} 次都寄不出去"
                  + ("，今天不再重試" if mark else "（手動觸發）"))
    except Exception as e:
        print(f"[Digest] ❌ 產生摘要失敗（略過，不影響其他任務）: {type(e).__name__}: {e}")
    return out


# ─────────────────────────────────────────────────────────────────────
# 排程
# ─────────────────────────────────────────────────────────────────────
def _seconds_until(hour_utc: int, now=None) -> float:
    """距離下一個 hour_utc 整點還有幾秒。已經過了就算明天的。"""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def target_day(now=None) -> str:
    """
    要報告哪一天：**前一個 UTC 日**。

    ★ 不報當天：在 01:00 UTC 醒來時當天才過了一小時，數字是半截的。
      報前一天拿到的是完整、之後不會再變的資料，逐日之間才比較得起來。
      紀錄檔本來就以 UTC 日期命名，這裡跟著用 UTC，信裡也標明。
    """
    now = now or datetime.now(timezone.utc)
    return (now.date() - timedelta(days=1)).isoformat()


async def daily_digest_loop():
    """
    每天固定時刻寄一封摘要。

    ★ 與 _auto_cleanup_loop 的差別，兩點不可以照抄：
      ① cleanup 是「每 24 小時」會漂移；摘要要對齊固定時刻，所以睡到下一個整點
      ② cleanup 沒有重複執行的問題（冪等）；寄信有 —— 見上面的兩層防護
    ★ 這支**不碰** _auto_cleanup_loop 的任何東西（含今天新加的退避）。
    """
    if not DIGEST_ENABLED:
        print("[Digest] ⏸️ DIGEST_ENABLED=false，每日摘要未啟用")
        return
    print(f"[Digest] ✅ 每日摘要已啟用（每天 {DIGEST_HOUR_UTC:02d}:00 UTC 寄出前一日）")
    while True:
        try:
            await asyncio.sleep(_seconds_until(DIGEST_HOUR_UTC))
            await run_once(target_day())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 迴圈本身永遠不能死掉，否則之後每一天的心跳都沒了
            print(f"[Digest] ❌ 迴圈例外（繼續）: {type(e).__name__}: {e}")
            await asyncio.sleep(300)
