"""
每日爬取摘要信驗證（離線，不寄信、不連外）
============================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_daily_digest.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_daily_digest.py`）

規格：spec-scrape-monitoring.md 第四節（每日摘要）＋第五節（寄信方式）。
只做每日摘要，不做即時警報。

★ 這支要釘死的四件事：

  1. **心跳**（規格第 105 行，最容易漏的一條）
     零失敗、零紀錄**都要寄**。沒收到信要能區分「沒問題」和「系統掛了」。
     所以斷言下在「send 被呼叫恰好一次」，不是下在文字內容 ——
     那才擋得住日後有人在 run_once 裡加 early return。

  2. **重複寄防護有兩層**
     容器重啟迴圈就重跑，跨過寄送時刻會再寄一次。2026-09-02 光這一天就
     重新部署 5 次以上，不是理論風險。
       標記檔（Volume，跨重啟） + 記憶體（本容器）
     標記檔在 Volume 掛掉或磁碟滿時**寫入會失敗**，所以記憶體那層必須
     在寫檔之外先設好。

  3. **嚴重度分級**
     0/12（完全不能用）和 2/8（不穩）處置不同，只給百分比看不出差別。

  4. **build_summary 的回傳值不可以動**
     verify_scrape_log_api.py 的 48 項全部斷言在那個 dict 上。

不連外：sender 全部是假的，httpx 完全沒被呼叫。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="digest_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP
# admin 端點用獨立金鑰；本機 .env 通常沒設，給測試一把（不覆蓋已存在的設定）
os.environ.setdefault("ADMIN_SECRET_KEY", "test-admin-key-for-verify")

import scrape_monitor as sm
import scrape_digest as dg

dg._SEND_RETRY_SLEEP = 0          # 測試不要真的睡 60 秒

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


_TODAY = datetime.now(timezone.utc).date()
D0 = _TODAY.isoformat()
D1 = (_TODAY - timedelta(days=1)).isoformat()
D2 = (_TODAY - timedelta(days=2)).isoformat()
D3 = (_TODAY - timedelta(days=3)).isoformat()


def entry(day, domain, ok, kind="", status=None, ms=1200, err=""):
    return {"ts": f"{day}T09:00:00+00:00", "domain": domain,
            "platform_id": "generic", "source": "x", "ok": ok,
            "failure_kind": kind, "http_status": status, "elapsed_ms": ms,
            "error_brief": err, "url_path": "/item/x", "warnings": ""}


def write(day, rows):
    with open(os.path.join(_TMP, f"{day}.jsonl"), "w",
              encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def clear_marker():
    dg._SENT_DAYS.clear()
    try:
        os.remove(dg._marker_path())
    except Exception:
        pass


class Spy:
    """假 sender：記下每次呼叫，依 results 決定成功與否。"""

    def __init__(self, results=(True,), boom=False):
        self.calls = []
        self.results = list(results)
        self.boom = boom

    async def __call__(self, subject, body):
        self.calls.append((subject, body))
        if self.boom:
            raise RuntimeError("寄信端掛了")
        return self.results.pop(0) if self.results else True

    @property
    def n(self):
        return len(self.calls)

    @property
    def body(self):
        return self.calls[-1][1] if self.calls else ""


def run(c):
    return asyncio.get_event_loop().run_until_complete(c)


# ═══════════════════════════════════════════════════════════════════
def test_heartbeat():
    print()
    print("【1】★ 心跳：三種輸入都要寄，而且恰好一次")

    cases = [
        ("有失敗", [entry(D1, "a.jp", True), entry(D1, "b.jp", False, "parse_failed", 200)]),
        ("零失敗", [entry(D1, "a.jp", True), entry(D1, "a.jp", True)]),
        ("零紀錄", []),
    ]
    for label, rows in cases:
        clear_marker()
        write(D1, rows)
        spy = Spy()
        r = run(dg.run_once(D1, sender=spy))
        check(f"{label} → send 恰好呼叫 1 次", spy.n == 1, f"{spy.n} 次")
        check(f"{label} → sent=True", r["sent"] is True, str(r))

    # 內容要分得出「零失敗」與「零紀錄」
    clear_marker(); write(D1, [entry(D1, "a.jp", True)])
    spy = Spy(); run(dg.run_once(D1, sender=spy))
    check("★ 零失敗信寫「今日無失敗」", "今日無失敗" in spy.body, spy.body[:60])

    clear_marker(); write(D1, [])
    spy = Spy(); run(dg.run_once(D1, sender=spy))
    check("★ 零紀錄信寫「沒有任何爬取紀錄」（與零失敗不同）",
          "沒有任何爬取紀錄" in spy.body and "今日無失敗" not in spy.body,
          spy.body[:70])


def test_content():
    print()
    print("【2】信件內容：分組、只給數字、最慢")
    clear_marker()
    rows = ([entry(D1, "ok.jp", True)] * 6
            + [entry(D1, "store.plusmember.jp", False, "parse_failed", 200)] * 4
            + [entry(D1, "dior.com", False, "blocked", 403)] * 3
            + [entry(D1, "gone.jp", False, "not_found", 404)]
            + [entry(D1, "slow.jp", False, "timeout", None, ms=18300)] * 3)
    write(D1, rows)
    spy = Spy(); run(dg.run_once(D1, sender=spy))
    b = spy.body
    print(b)
    check("標題含日期", D1 in spy.calls[0][0], spy.calls[0][0])
    check("有總數與成功率", "成功 6 / 失敗 11" in b, b[:80])
    check("『需要處理』列出解析失敗的網域", "store.plusmember.jp" in b)
    check("『需要處理』列出被擋的網域", "dior.com" in b)
    check("★ 『不用管』只給數字，不列網域名",
          "已下架/404 1 次" in b and "gone.jp" not in b)
    slow_sec = b.split("今日最慢")[-1]
    check("『今日最慢』有 slow.jp 且換算成秒",
          "slow.jp" in slow_sec and "18.3s" in b, b[-120:])
    check("★ 平均 1.2s 的正常網域不該被列成「最慢」（那是雜訊）",
          "ok.jp" not in slow_sec and "1.2s" not in slow_sec, slow_sec[:120])
    check("★ 『成功率偏低』不重複列已在『需要處理』的網域",
          "store.plusmember.jp" not in b.split("成功率偏低")[-1].split("■")[0]
          if "成功率偏低" in b else True)
    check("★ 但補上『需要處理』看不到的（失敗全是 timeout 的 slow.jp）",
          "slow.jp" in b.split("成功率偏低")[-1].split("■")[0]
          if "成功率偏低" in b else False)


def test_severity_tag():
    print()
    print("【3】★ 嚴重度分級：0/12（完全不能用）vs 2/8（不穩）")
    clear_marker()
    rows = ([entry(D1, "dead.jp", False, "parse_failed", 200)] * 12
            + [entry(D1, "flaky.jp", False, "parse_failed", 200)] * 6
            + [entry(D1, "flaky.jp", True)] * 2
            + [entry(D1, "fine.jp", True)] * 20
            + [entry(D1, "fine.jp", False, "parse_failed", 200)])
    write(D1, rows)
    spy = Spy(); run(dg.run_once(D1, sender=spy))
    b = spy.body
    dead = [l for l in b.splitlines() if "dead.jp" in l]
    flaky = [l for l in b.splitlines() if "flaky.jp" in l]
    fine = [l for l in b.splitlines() if "fine.jp" in l]
    print("\n".join(dead + flaky + fine))
    check("★ 0/12 標「完全不能用」", any("完全不能用" in l for l in dead), str(dead[:1]))
    check("★ 2/8 標「不穩」而不是「完全不能用」",
          any("不穩" in l for l in flaky)
          and not any("完全不能用" in l for l in flaky), str(flaky[:1]))
    check("★ 20/21 兩個標記都不帶（高流量偶發不是危機）",
          not any(("不穩" in l or "完全不能用" in l) for l in fine), str(fine[:1]))
    check("成功率是該網域全部爬取的分母，不是只算失敗",
          any("0/12" in l for l in dead) and any("2/8" in l for l in flaky),
          str(dead[:1] + flaky[:1]))
    check("★ 20/21 不進『成功率偏低』那區",
          "fine.jp" not in b.split("成功率偏低")[-1].split("■")[0]
          if "成功率偏低" in b else True)


def test_streaks():
    print()
    print("【4】連續失敗天數")
    for d in (D0, D1, D2, D3):
        try:
            os.remove(os.path.join(_TMP, f"{d}.jsonl"))
        except Exception:
            pass
    # a.jp：三天都有失敗（第 2 天同時也有成功 → 寬鬆定義仍算失敗日）
    # b.jp：只有今天失敗
    # c.jp：今天失敗、昨天沒失敗 → 斷開
    write(D0, [entry(D0, "a.jp", False, "parse_failed", 200),
               entry(D0, "b.jp", False, "parse_failed", 200),
               entry(D0, "c.jp", False, "parse_failed", 200)])
    write(D1, [entry(D1, "a.jp", False, "parse_failed", 200),
               entry(D1, "a.jp", True),
               entry(D1, "c.jp", True)])
    write(D2, [entry(D2, "a.jp", False, "parse_failed", 200)])
    write(D3, [entry(D3, "x.jp", True)])

    s = dg.failure_streaks([D0, D1, D2, D3])
    check("★ a.jp 連續 3 天（中間那天有成功也照算）", s.get("a.jp") == 3, str(s))
    check("b.jp 只有 1 天", s.get("b.jp") == 1, str(s))
    check("★ c.jp 斷開後不累計（只有 1 天）", s.get("c.jp") == 1, str(s))
    check("沒失敗過的網域不在裡面", "x.jp" not in s, str(s))

    # 標記只在 >= 2 天時出現
    clear_marker()
    spy = Spy(); run(dg.run_once(D0, sender=spy))
    b = spy.body
    check("★ a.jp 那行標「連續 3 天失敗」",
          any("a.jp" in l and "連續 3 天失敗" in l for l in b.splitlines()), b[:200])
    check("b.jp 只有 1 天，不標連續",
          not any("b.jp" in l and "連續" in l for l in b.splitlines()), b[:200])


def test_dedupe_two_layers():
    print()
    print("【5】★ 重複寄的兩層防護")
    clear_marker(); write(D1, [entry(D1, "a.jp", True)])

    spy = Spy()
    r1 = run(dg.run_once(D1, sender=spy))
    r2 = run(dg.run_once(D1, sender=spy))
    check("第一次寄出", r1["sent"] is True and spy.n == 1, str(spy.n))
    check("★ 第二次跳過（send 沒有被再呼叫）",
          r2["skipped"] is True and spy.n == 1, f"{r2} n={spy.n}")

    # 只剩標記檔（模擬容器重啟：記憶體清空）
    dg._SENT_DAYS.clear()
    spy2 = Spy()
    r3 = run(dg.run_once(D1, sender=spy2))
    check("★ 記憶體清空後靠標記檔仍然跳過（跨重啟）",
          r3["skipped"] is True and spy2.n == 0, f"{r3} n={spy2.n}")

    # 只剩記憶體（模擬 Volume 掛掉：標記檔寫不進去 / 讀不到）
    clear_marker()
    orig = dg._marker_path
    dg._marker_path = lambda: os.path.join(_TMP, "no-such-dir", "x", ".digest_sent")
    try:
        spy3 = Spy()
        r4 = run(dg.run_once(D1, sender=spy3))
        check("寫檔失敗時仍然寄得出去（不因此漏寄）",
              r4["sent"] is True and spy3.n == 1, f"{r4} n={spy3.n}")
        check("★ 標記檔寫不進去，記憶體那層仍記得（同容器內不會重寄）",
              dg.already_sent(D1) is True and D1 in dg._SENT_DAYS)
        spy4 = Spy()
        r5 = run(dg.run_once(D1, sender=spy4))
        check("★ 因此第二次仍然跳過", r5["skipped"] is True and spy4.n == 0,
              f"{r5} n={spy4.n}")
    finally:
        dg._marker_path = orig

    # 讀標記檔失敗 → 當成沒寄過（寧可重寄，不可漏掉心跳）
    dg._SENT_DAYS.clear()
    dg._marker_path = lambda: os.path.join(_TMP, "no-such-dir", "x", ".digest_sent")
    try:
        check("★ 兩層都讀不到時回 False（寧可重寄也不漏心跳）",
              dg.already_sent(D1) is False)
    finally:
        dg._marker_path = orig


def test_send_failure():
    print()
    print("【6】寄信失敗的 fail-safe")
    clear_marker(); write(D1, [entry(D1, "a.jp", True)])

    spy = Spy(results=[False, False, True])
    r = run(dg.run_once(D1, sender=spy))
    check("前兩次失敗、第三次成功", r["sent"] is True and r["attempts"] == 3, str(r))

    clear_marker()
    spy = Spy(results=[False, False, False])
    r = run(dg.run_once(D1, sender=spy))
    check("三次都失敗 → sent=False，不 raise", r["sent"] is False and spy.n == 3, str(r))
    check("★ 寄失敗仍寫標記（否則容器一重啟就變成寄信轟炸）",
          dg.already_sent(D1) is True)

    clear_marker()
    spy = Spy(boom=True)
    r = run(dg.run_once(D1, sender=spy))
    check("★ sender 直接拋例外也不 raise", r["sent"] is False, str(r))

    # 讀紀錄本身壞掉
    clear_marker()
    orig = sm.read_day
    sm.read_day = lambda day="": (_ for _ in ()).throw(RuntimeError("讀檔炸了"))
    try:
        r = run(dg.run_once(D1, sender=Spy()))
        check("★ 讀紀錄爆掉也不 raise（背景任務不可以死）", r["sent"] is False, str(r))
    finally:
        sm.read_day = orig


def test_schedule():
    print()
    print("【7】排程：固定時刻、報告前一日")
    now = datetime(2026, 9, 2, 0, 30, tzinfo=timezone.utc)
    check("00:30 → 距離 01:00 是 1800 秒", dg._seconds_until(1, now) == 1800,
          str(dg._seconds_until(1, now)))
    now2 = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
    check("★ 剛好整點 → 算明天（不會在同一秒重跑）",
          dg._seconds_until(1, now2) == 86400, str(dg._seconds_until(1, now2)))
    now3 = datetime(2026, 9, 2, 23, 0, tzinfo=timezone.utc)
    check("23:00 → 跨日到隔天 01:00 是 7200 秒",
          dg._seconds_until(1, now3) == 7200, str(dg._seconds_until(1, now3)))
    check("★ 報告的是前一個 UTC 日（當天資料是半截的）",
          dg.target_day(now) == "2026-09-01", dg.target_day(now))


def test_send_email_guard():
    print()
    print("【8】send_email：設定不全時回 False，不 raise、不連外")
    orig = (dg.RESEND_API_KEY, dg.DIGEST_FROM, dg.DIGEST_TO)
    dg.RESEND_API_KEY, dg.DIGEST_FROM, dg.DIGEST_TO = "", "", ""
    try:
        check("沒有金鑰時回 False", run(dg.send_email("s", "b")) is False)
    finally:
        dg.RESEND_API_KEY, dg.DIGEST_FROM, dg.DIGEST_TO = orig
    check("預設 DIGEST_ENABLED 是關的（部署了不會突然寄信）",
          dg.DIGEST_ENABLED is False, str(dg.DIGEST_ENABLED))
    # 關閉時迴圈要立刻 return，不可以卡住
    dg.DIGEST_ENABLED = False
    run(asyncio.wait_for(dg.daily_digest_loop(), timeout=5))
    check("★ DIGEST_ENABLED=false 時迴圈立刻結束", True)


def test_summary_unchanged():
    print()
    print("【9】build_summary 的回傳值沒有被動到")
    write(D1, [entry(D1, "a.jp", True), entry(D1, "b.jp", False, "parse_failed", 200)])
    s = dg.build_summary([D1])
    check("★ 欄位集合不變（48 項測試全部斷言在這上面）",
          set(s) == {"days", "log_dir", "by_day", "total", "ok", "failed",
                     "success_rate_pct", "failure_kinds", "domains_by_failure",
                     "samples"}, str(sorted(s)))
    check("數字對得上", (s["total"], s["ok"], s["failed"]) == (2, 1, 1), str(s["total"]))
    check("新增的統計沒有混進來",
          "streaks" not in s and "slowest" not in s and "domain_rates" not in s)
    import main
    check("★ main.py 的端點確實改用這支", main._pick_samples is dg.pick_samples)


# ═══════════════════════════════════════════════════════════════════
# 手動觸發端點（規格第七節：先寄給自己看一次格式）
# ═══════════════════════════════════════════════════════════════════
def test_manual_endpoint():
    print()
    print("【10】手動觸發端點 /api/admin/scrape-log/digest")
    from fastapi.testclient import TestClient
    from config import ADMIN_SECRET_KEY, API_SECRET_KEY
    import main

    client = TestClient(main.app)      # 不用 with：不跑 lifespan，不啟動背景任務
    HEAD = {"X-Admin-Key": ADMIN_SECRET_KEY}
    PATH = "/api/admin/scrape-log/digest"

    # ── 金鑰：公開金鑰打不開（CLAUDE.md：會吐資料的端點一律走 admin）──
    r = client.post(PATH, headers={"X-API-Key": API_SECRET_KEY})
    check("★ 公開金鑰 → 403（那把印在 storefront 頁面上）", r.status_code == 403,
          str(r.status_code))
    check("沒帶 key → 403", client.post(PATH).status_code == 403)
    check("錯的 key → 403",
          client.post(PATH, headers={"X-Admin-Key": "wrong"}).status_code == 403)

    # 之後都用假 sender，避免本機真的有 RESEND_API_KEY 時寄出真信
    spy = Spy()
    orig_send = dg.send_email
    dg.send_email = spy
    try:
        clear_marker()
        write(D1, [entry(D1, "a.jp", True),
                   entry(D1, "b.jp", False, "parse_failed", 200)])

        # ── 指定日期 ──
        r = client.post(f"{PATH}?day={D1}", headers=HEAD)
        check("帶 admin key → 200", r.status_code == 200, str(r.status_code))
        d = r.json()
        check("回傳的 day 是指定的那天", d["day"] == D1, str(d.get("day")))
        check("★ 回傳帶 body（Resend 沒設好也看得到格式）",
              "每日爬取摘要" in (d.get("body") or ""), (d.get("body") or "")[:50])
        check("sent=True（假 sender）", d["sent"] is True, str(d))

        # ── 不寫標記檔 ──
        check("★ marked=False（回傳裡講明白）", d["marked"] is False, str(d.get("marked")))
        check("★ 沒有寫 .digest_sent —— 排程之後仍然會寄（心跳不會被手動觸發弄斷）",
              dg.already_sent(D1) is False)
        check("★ 也沒有寫進記憶體那層", D1 not in dg._SENT_DAYS, str(dg._SENT_DAYS))

        # 排程照樣寄得出去
        spy2 = Spy(); dg.send_email = spy2
        r2 = run(dg.run_once(D1))
        check("★ 手動觸發過之後，排程仍然照寄", r2["sent"] is True and spy2.n == 1,
              f"{r2} n={spy2.n}")
        check("排程那次有寫標記", dg.already_sent(D1) is True)

        # ── 排程寄過之後，手動仍然觸發得了（不被 already_sent 擋）──
        spy3 = Spy(); dg.send_email = spy3
        r3 = client.post(f"{PATH}?day={D1}", headers=HEAD)
        check("★ 排程今天寄過了，手動仍然能再看一次",
              r3.status_code == 200 and r3.json()["sent"] is True and spy3.n == 1,
              f"{r3.status_code} n={spy3.n}")

        # ── 預設日期 = 前一個 UTC 日 ──
        spy4 = Spy(); dg.send_email = spy4
        r4 = client.post(PATH, headers=HEAD)
        check("★ 省略 day → 前一個 UTC 日（與排程一致）",
              r4.json()["day"] == dg.target_day(), r4.json().get("day"))

        # ── DIGEST_ENABLED=false 也要能觸發 ──
        check("前提：DIGEST_ENABLED 目前是關的", dg.DIGEST_ENABLED is False)
        spy5 = Spy(); dg.send_email = spy5
        r5 = client.post(f"{PATH}?day={D1}", headers=HEAD)
        check("★ DIGEST_ENABLED=false 仍然觸發得了（不然要先開才能看格式，順序反了）",
              r5.status_code == 200 and r5.json()["sent"] is True, str(r5.status_code))

        # ── day 驗證：這是路徑注入的防線 ──
        for bad, why in [("../../etc/passwd", "路徑注入"),
                         ("2026-13-45", "不是有效日期"),
                         ("2026-9-1", "格式不合"),
                         ("abc", "不是日期"),
                         ("2026-09-01;rm", "夾帶指令")]:
            rb = client.post(PATH, params={"day": bad}, headers=HEAD)
            check(f"★ day={bad!r} → 400（{why}）", rb.status_code == 400,
                  str(rb.status_code))

        # ── 寄不出去時要講得出原因，而且不 raise ──
        dg.send_email = Spy(results=[False])
        r6 = client.post(f"{PATH}?day={D1}", headers=HEAD)
        check("寄不出去仍回 200 且 sent=False",
              r6.status_code == 200 and r6.json()["sent"] is False, str(r6.status_code))
        check("★ note 說得出可能的原因（沒設 RESEND_API_KEY）",
              "RESEND_API_KEY" in r6.json()["note"], r6.json().get("note", "")[:60])
        check("★ 寄不出去也照樣回 body（格式仍看得到）",
              "每日爬取摘要" in r6.json()["body"])

        # ── 手動觸發只試 1 次，不可以卡住 HTTP 請求 2 分鐘 ──
        spy7 = Spy(results=[False])
        dg.send_email = spy7
        client.post(f"{PATH}?day={D1}", headers=HEAD)
        check("★ 手動觸發只送 1 次（預設 3 次會讓請求卡 2 分鐘）", spy7.n == 1,
              f"{spy7.n} 次")
    finally:
        dg.send_email = orig_send


def test_run_once_flags():
    print()
    print("【11】run_once 的 mark / retries 旗標")
    clear_marker(); write(D1, [entry(D1, "a.jp", True)])

    spy = Spy()
    r = run(dg.run_once(D1, sender=spy, mark=False))
    check("mark=False → 有寄", r["sent"] is True and spy.n == 1)
    check("mark=False → marked=False", r["marked"] is False)
    check("★ mark=False → 不寫標記（兩層都沒寫）",
          dg.already_sent(D1) is False and D1 not in dg._SENT_DAYS)

    spy = Spy()
    r = run(dg.run_once(D1, sender=spy, mark=True))
    check("mark=True → marked=True 且有寫標記",
          r["marked"] is True and dg.already_sent(D1) is True)

    clear_marker()
    spy = Spy(results=[False, False, False])
    r = run(dg.run_once(D1, sender=spy, retries=1))
    check("★ retries=1 只送 1 次", spy.n == 1 and r["attempts"] == 1, f"n={spy.n}")

    clear_marker()
    spy = Spy(results=[False, False, True])
    r = run(dg.run_once(D1, sender=spy))
    check("retries 省略 → 沿用預設 3 次", r["attempts"] == 3 and r["sent"] is True,
          str(r["attempts"]))

    # ★ 同一支函式兩條路徑：排版/重試/fail-safe 只有一份
    clear_marker()
    spy = Spy(boom=True)
    r = run(dg.run_once(D1, sender=spy, mark=False))
    check("★ mark=False 也一樣不 raise（fail-safe 沒有第二份實作）",
          r["sent"] is False, str(r))


# ═══════════════════════════════════════════════════════════════════
# 編碼：整封信都是中文，編錯就等於沒有價值
# ═══════════════════════════════════════════════════════════════════
class CapturedResp:
    status_code = 200


class CapturingClient:
    """假的 httpx.AsyncClient：記下 send_email 實際送出去的 header 與 bytes。"""
    last = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        CapturingClient.last = {"url": url, "kwargs": kw}
        return CapturedResp()


class FakeHttpxMod:
    def __init__(self, real):
        self._real = real

    def AsyncClient(self, *a, **kw):
        return CapturingClient()

    def __getattr__(self, k):
        return getattr(self._real, k)


CJK_BODY = ("【daigo 每日爬取摘要】2026-09-01（UTC）" + chr(10)
            + "成功 61 / 失敗 17（成功率 78.2%）" + chr(10)
            + "    japan.us.mercari.com  0/12  ← 完全不能用・連續 3 天失敗")
CJK_SUBJ = "【daigo 每日爬取摘要】2026-09-01"


def _send_and_capture(subject, body):
    """真的跑 send_email，攔下它送出去的東西。"""
    import httpx as real_httpx
    orig_httpx = dg.httpx
    orig = (dg.RESEND_API_KEY, dg.DIGEST_FROM, dg.DIGEST_TO)
    dg.httpx = FakeHttpxMod(real_httpx)
    dg.RESEND_API_KEY, dg.DIGEST_FROM, dg.DIGEST_TO = ("re_test", "a@goyoutati.com",
                                                       "b@example.com")
    try:
        ok = run(dg.send_email(subject, body))
    finally:
        dg.httpx = orig_httpx
        dg.RESEND_API_KEY, dg.DIGEST_FROM, dg.DIGEST_TO = orig
    return ok, CapturingClient.last


def test_utf8_wire():
    print()
    print("【12】★ 寄出去的 payload 編碼（中文信變亂碼這封信就沒價值）")
    ok, cap = _send_and_capture(CJK_SUBJ, CJK_BODY)
    kw = cap["kwargs"]
    check("send_email 回 True", ok is True)
    check("打的是 Resend 的端點", cap["url"].endswith("/emails"), cap["url"])

    # ① Content-Type 必須含 charset=utf-8
    ct = {k.lower(): v for k, v in (kw.get("headers") or {}).items()}.get("content-type", "")
    check("★ Content-Type 含 charset=utf-8", "charset=utf-8" in ct.lower(), repr(ct))
    check("Content-Type 仍是 application/json", ct.lower().startswith("application/json"),
          repr(ct))

    # ② body 必須是我們自己編好的 bytes，不是交給 httpx 的 json=
    body_bytes = kw.get("content")
    check("★ 用 content= 送 bytes（不是 json=，那會由函式庫決定編碼）",
          isinstance(body_bytes, bytes), type(body_bytes).__name__)
    check("★ 沒有用 json= 參數", "json" not in kw, str(sorted(kw)))
    check("沒有用 data=（那是表單編碼，會壞掉）", "data" not in kw, str(sorted(kw)))

    # ③ bytes 必須是合法 UTF-8 且無損往返
    decoded = None
    try:
        decoded = body_bytes.decode("utf-8")
        okdec = True
    except Exception:
        okdec = False
    check("★ bytes 是合法 UTF-8", okdec)
    if okdec:
        payload = json.loads(decoded)
        check("★ text 無損往返（一個字都沒變）", payload["text"] == CJK_BODY,
              repr(payload["text"][:30]))
        check("★ subject 無損往返", payload["subject"] == CJK_SUBJ,
              repr(payload["subject"]))

    # ④ 中文是原生 UTF-8，不是 Latin-1 也不是 cp950
    check("★ 「【」編成 UTF-8 的 e3 80 90", bytes([0xE3, 0x80, 0x90]) in body_bytes,
          repr(body_bytes[:40]))
    check("★ 不是 cp950/Big5（「【」在 Big5 是 a1 61）",
          bytes([0xA1, 0x61]) not in body_bytes)
    check("★ 不是 Latin-1 誤編（那樣根本編不出非 ASCII 的中文）",
          any(b > 0x7F for b in body_bytes))

    # ⑤ 反面：拿 Latin-1 去讀會壞掉 —— 證明我們送的確實是 UTF-8 而不是別的
    # 「【」的 UTF-8 是 e3 80 90；用 Latin-1 讀就變成 ã + 兩個控制字元 ——
    # 那正是主控台亂碼的長相。這條證明我們送的是 UTF-8 而不是別的編碼。
    mis = body_bytes.decode("latin-1")
    mojibake = bytes([0xE3, 0x80, 0x90]).decode("latin-1")
    check("★ 用 Latin-1 誤讀會變成典型亂碼（證明送的確實是 UTF-8）",
          mojibake in mis and CJK_BODY not in mis, repr(mojibake))


def _can_cp950(ch):
    try:
        ch.encode("cp950")
        return True
    except Exception:
        return False


def test_utf8_encode_payload():
    print()
    print("【13】encode_payload 本身")
    raw = dg.encode_payload(CJK_SUBJ, CJK_BODY, sender="a@b.c", to="d@e.f")
    check("回傳是 bytes", isinstance(raw, bytes), type(raw).__name__)
    d = json.loads(raw.decode("utf-8"))
    check("欄位齊全", set(d) == {"from", "to", "subject", "text"}, str(sorted(d)))
    check("to 是 list（Resend 要陣列）", d["to"] == ["d@e.f"], str(d["to"]))
    check("★ 中文沒有被跳脫成 \\uXXXX（原生 UTF-8，抓包看得懂）",
          ("\\u" + "3010") not in raw.decode("utf-8"), raw[:30].decode("utf-8", "replace"))

    # ★ 上面的樣本含「・」（U+30FB），cp950 編不出來 —— 萬一有人把編碼改成 cp950，
    #   會觸發退路、被 UTF-8 救回來，測試就抓不到。2026-09-03 的負向驗證正是
    #   這樣被瞞過去的（注入 cp950 全綠）。
    #   所以另外用一個**每個字 Big5 都編得出來**的樣本，讓退路不會啟動。
    big5_ok = "摘要 完全不能用 連續 3 天失敗"
    assert all(_can_cp950(ch) for ch in big5_ok), "樣本必須整串 cp950 編得出來"
    raw3 = dg.encode_payload("摘要", big5_ok, sender="a@b.c", to="d@e.f")
    check("★ 用 Big5 編得出來的樣本反證：送的是 UTF-8 不是 cp950",
          "摘".encode("utf-8") in raw3 and "摘".encode("cp950") not in raw3,
          f'utf8={"摘".encode("utf-8")!r} big5={"摘".encode("cp950")!r}')
    check("退路沒有被觸發（這串沒有 cp950 編不出的字）",
          json.loads(raw3.decode("utf-8"))["text"] == big5_ok)

    # 落單的 surrogate：不可以整封寄不出去（心跳比完美的文字重要）
    bad = "摘要" + chr(0xD800) + "尾巴"
    try:
        raw2 = dg.encode_payload(CJK_SUBJ, bad, sender="a@b.c", to="d@e.f")
        threw = False
    except Exception:
        raw2, threw = None, True
    check("★ 內容含落單 surrogate 也不 raise（信照樣寄得出去）", threw is False)
    if raw2 is not None:
        check("替換後仍是合法 UTF-8", isinstance(raw2.decode("utf-8"), str))
        check("其餘中文沒有被波及",
              "摘要" in json.loads(raw2.decode("utf-8"))["text"])


def main_():
    print("=" * 74)
    print("每日爬取摘要信")
    print(f"紀錄目錄導到暫存目錄：{_TMP}")
    print("=" * 74)
    test_heartbeat()
    test_content()
    test_severity_tag()
    test_streaks()
    test_dedupe_two_layers()
    test_send_failure()
    test_schedule()
    test_send_email_guard()
    test_summary_unchanged()
    test_manual_endpoint()
    test_run_once_flags()
    test_utf8_wire()
    test_utf8_encode_payload()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main_()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
