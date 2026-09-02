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
