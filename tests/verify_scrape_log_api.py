"""
爬取紀錄匯出端點的驗證（/api/admin/scrape-log 與 .../summary）
==============================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_scrape_log_api.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_scrape_log_api.py`）

不連外、不碰 Shopify：紀錄目錄導到暫存目錄，塞已知的假紀錄，再用 TestClient
打端點，對答案。要驗的是「summary 算出來的數字能不能拿來判斷分類準不準」，
所以樣本抽法、網域排序、壞行處理都要驗，不是只驗端點有回 200。
"""
import os
import sys
import json
import shutil
import tempfile
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 紀錄目錄導到暫存目錄（必須在 import scrape_monitor / main 之前設）
_TMP = tempfile.mkdtemp(prefix="scrapelogapi_test_")
os.environ["SCRAPE_LOG_DIR"] = _TMP

from fastapi.testclient import TestClient

import scrape_monitor as sm
import main
from config import API_SECRET_KEY

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# ─────────────────────────────────────────────────────────────────────
# 假紀錄（欄位與 scrape_monitor.record() 寫出來的一致）
# ─────────────────────────────────────────────────────────────────────
_TODAY = datetime.now(timezone.utc).date()
DAY0 = _TODAY.isoformat()
DAY1 = (_TODAY - timedelta(days=1)).isoformat()
DAY2 = (_TODAY - timedelta(days=2)).isoformat()


def entry(day, domain, ok, kind="", status=None, err="", ms=1200,
          platform_id="generic", source="GenericHttpxSource", path="/item/x"):
    return {
        "ts": f"{day}T09:00:00+00:00",
        "domain": domain,
        "platform_id": platform_id,
        "source": source,
        "ok": ok,
        "failure_kind": kind,
        "http_status": status,
        "elapsed_ms": ms,
        "error_brief": err,
        "url_path": path,
    }


FIXTURES = {
    DAY0: [
        entry(DAY0, "item.rakuten.co.jp", True, status=200, platform_id="rakuten"),
        entry(DAY0, "item.rakuten.co.jp", True, status=200, platform_id="rakuten"),
        entry(DAY0, "zozo.jp", True, status=200, platform_id="zozotown"),
        entry(DAY0, "jp.mercari.com", True, status=200, platform_id="mercari"),
        entry(DAY0, "store.plusmember.jp", False, "parse_failed", 200),
        entry(DAY0, "store.plusmember.jp", False, "parse_failed", 200),
        entry(DAY0, "fujitaka-japan.com", False, "parse_failed", 200),
        entry(DAY0, "zozo.jp", False, "blocked", 403, platform_id="zozotown"),
        entry(DAY0, "item.rakuten.co.jp", False, "not_found", 404, platform_id="rakuten"),
    ],
    DAY1: [
        entry(DAY1, "item.rakuten.co.jp", True, status=200, platform_id="rakuten"),
        entry(DAY1, "amazon.co.jp", True, status=200, platform_id="amazon"),
        entry(DAY1, "store.plusmember.jp", False, "parse_failed", 200),
        entry(DAY1, "store.plusmember.jp", False, "parse_failed", 200),
        entry(DAY1, "suruga-ya.jp", False, "timeout", None,
              err="TimeoutError: read timed out", ms=30000),
    ],
    # days=2 不該撈到這天
    DAY2: [
        entry(DAY2, "should-not-appear.example", True, status=200),
        entry(DAY2, "should-not-appear.example", False, "blocked", 403),
    ],
}

# 壞行：匯出要原樣保留，summary 要跳過（read_day 的既有行為）
BAD_LINE = '{"ts": "壞掉的一行'


def write_fixtures():
    for day, rows in FIXTURES.items():
        lines = [json.dumps(r, ensure_ascii=False) for r in rows]
        if day == DAY1:
            lines.append(BAD_LINE)
        with open(os.path.join(_TMP, f"{day}.jsonl"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")


client = TestClient(main.app)          # 不用 with：不跑 lifespan，不啟動清理任務
HEAD = {"X-API-Key": API_SECRET_KEY}


# ─────────────────────────────────────────────────────────────────────
def test_auth():
    print("\n【1】verify_api_key 保護")
    for path in ("/api/admin/scrape-log", "/api/admin/scrape-log/summary"):
        r = client.get(path, headers={"X-API-Key": "definitely-wrong-key"})
        check(f"錯的 key 打 {path} → 403", r.status_code == 403, str(r.status_code))
        r = client.get(path)
        check(f"沒帶 key 打 {path} → 403", r.status_code == 403, str(r.status_code))
    if not API_SECRET_KEY:
        print("  ⚠️ API_SECRET_KEY 是空的，後面的測試等於沒驗到金鑰")


def test_export():
    print("\n【2】匯出原始 JSONL")
    r = client.get("/api/admin/scrape-log?days=2", headers=HEAD)
    check("回 200", r.status_code == 200, str(r.status_code))

    lines = [l for l in r.text.splitlines() if l.strip()]
    expected_n = len(FIXTURES[DAY0]) + len(FIXTURES[DAY1]) + 1   # +1 壞行
    check("筆數 = 今天 + 昨天（含壞行）", len(lines) == expected_n,
          f"{len(lines)} 行，預期 {expected_n}")
    check("header X-Log-Lines 與內容一致",
          r.headers.get("X-Log-Lines") == str(len(lines)), r.headers.get("X-Log-Lines"))
    check("header X-Log-Days = 今天,昨天",
          r.headers.get("X-Log-Days") == f"{DAY0},{DAY1}", r.headers.get("X-Log-Days"))
    check("header X-Log-Dir 指到實際目錄",
          r.headers.get("X-Log-Dir") == _TMP, r.headers.get("X-Log-Dir"))

    check("前 9 行是今天的（新到舊）",
          [json.loads(l)["ts"][:10] for l in lines[:len(FIXTURES[DAY0])]]
          == [DAY0] * len(FIXTURES[DAY0]))
    check("days=2 不含前天的紀錄", "should-not-appear.example" not in r.text)
    check("壞行原樣保留（匯出不解析）", BAD_LINE in lines,
          "匯出要看得到檔案實際長什麼樣")

    first = json.loads(lines[0])
    check("每行是完整的一筆紀錄",
          set(first) == {"ts", "domain", "platform_id", "source", "ok",
                         "failure_kind", "http_status", "elapsed_ms",
                         "error_brief", "url_path"}, str(sorted(first)))

    r1 = client.get("/api/admin/scrape-log?days=1", headers=HEAD)
    n1 = len([l for l in r1.text.splitlines() if l.strip()])
    check("days=1 只給今天", n1 == len(FIXTURES[DAY0]), f"{n1} 行")

    r0 = client.get("/api/admin/scrape-log?days=0", headers=HEAD)
    check("days=0 → 400", r0.status_code == 400, str(r0.status_code))

    r99 = client.get("/api/admin/scrape-log?days=999", headers=HEAD)
    days99 = r99.headers.get("X-Log-Days", "").split(",")
    check("days 上限夾到 30 天", r99.status_code == 200 and len(days99) == 30,
          f"{len(days99)} 天")


def test_summary_numbers():
    print("\n【3】summary 的數字")
    r = client.get("/api/admin/scrape-log/summary?days=2", headers=HEAD)
    check("回 200", r.status_code == 200, str(r.status_code))
    s = r.json()

    ok_n = sum(1 for d in (DAY0, DAY1) for e in FIXTURES[d] if e["ok"])
    fail_n = sum(1 for d in (DAY0, DAY1) for e in FIXTURES[d] if not e["ok"])
    total = ok_n + fail_n

    check("total 不含壞行（read_day 跳過）", s["total"] == total,
          f'{s["total"]}，預期 {total}')
    check("ok / failed 正確", (s["ok"], s["failed"]) == (ok_n, fail_n),
          f'{s["ok"]}/{s["failed"]}，預期 {ok_n}/{fail_n}')
    check("success_rate_pct 正確",
          s["success_rate_pct"] == round(ok_n / total * 100, 1),
          str(s["success_rate_pct"]))
    check("by_day 逐日拆開",
          s["by_day"][DAY0]["total"] == len(FIXTURES[DAY0])
          and s["by_day"][DAY1]["total"] == len(FIXTURES[DAY1]),
          str(s["by_day"]))
    check("days=2 不含前天", DAY2 not in s["by_day"])

    check("failure_kinds 各類筆數正確",
          s["failure_kinds"] == {"parse_failed": 5, "blocked": 1,
                                 "not_found": 1, "timeout": 1},
          json.dumps(s["failure_kinds"], ensure_ascii=False))
    kinds_order = list(s["failure_kinds"])
    check("failure_kinds 由多到少排序", kinds_order[0] == "parse_failed",
          str(kinds_order))


def test_summary_domains():
    print("\n【4】網域失敗排序")
    s = client.get("/api/admin/scrape-log/summary?days=2", headers=HEAD).json()
    doms = s["domains_by_failure"]
    check("只列有失敗的網域", all(d["failed"] > 0 for d in doms),
          str([d["domain"] for d in doms]))
    check("成功的網域不列入（amazon 全成功）",
          "amazon.co.jp" not in [d["domain"] for d in doms])
    check("失敗最多的排第一（plusmember 4 次）",
          doms[0]["domain"] == "store.plusmember.jp" and doms[0]["failed"] == 4,
          f'{doms[0]["domain"]} {doms[0]["failed"]} 次')
    check("失敗次數由多到少",
          [d["failed"] for d in doms] == sorted([d["failed"] for d in doms], reverse=True),
          str([(d["domain"], d["failed"]) for d in doms]))

    rakuten = next(d for d in doms if d["domain"] == "item.rakuten.co.jp")
    check("網域同時給 total（才算得出該網域的失敗率）",
          rakuten["total"] == 4 and rakuten["failed"] == 1, str(rakuten))
    check("網域帶各 failure_kind 明細",
          rakuten["kinds"] == {"not_found": 1}, str(rakuten["kinds"]))


def test_summary_samples():
    print("\n【5】每種 failure_kind 抽 3 筆樣本")
    s = client.get("/api/admin/scrape-log/summary?days=2", headers=HEAD).json()
    samples = s["samples"]

    check("每種 failure_kind 都有樣本",
          set(samples) == {"parse_failed", "blocked", "not_found", "timeout"},
          str(sorted(samples)))
    check("每種最多 3 筆", all(len(v) <= 3 for v in samples.values()),
          str({k: len(v) for k, v in samples.items()}))
    check("parse_failed 有 5 筆 → 抽滿 3 筆", len(samples["parse_failed"]) == 3)

    pf_domains = [x["domain"] for x in samples["parse_failed"]]
    check("優先抽不同網域（5 筆分屬 2 個網域 → 樣本涵蓋這 2 個）",
          set(pf_domains) == {"store.plusmember.jp", "fujitaka-japan.com"},
          str(pf_domains))
    check("網域不夠才用同網域補滿", len(pf_domains) == 3, str(pf_domains))

    one = samples["blocked"][0]
    check("樣本含 domain / http_status / error_brief",
          {"domain", "http_status", "error_brief"} <= set(one), str(sorted(one)))
    check("樣本欄位就是規格那組（另附 ts/platform_id/source/elapsed_ms/url_path）",
          set(one) == {"ts", "domain", "platform_id", "source",
                       "http_status", "elapsed_ms", "error_brief", "url_path"},
          str(sorted(one)))
    check("blocked 樣本值正確",
          one["domain"] == "zozo.jp" and one["http_status"] == 403, str(one))
    check("timeout 樣本帶 error_brief",
          samples["timeout"][0]["error_brief"].startswith("TimeoutError"),
          samples["timeout"][0]["error_brief"])


def test_empty_and_missing():
    print("\n【6】沒紀錄的時候不能炸")
    for name in os.listdir(_TMP):
        os.remove(os.path.join(_TMP, name))

    r = client.get("/api/admin/scrape-log?days=2", headers=HEAD)
    check("檔案不存在 → 200 空內容", r.status_code == 200 and r.text == "",
          f"{r.status_code} / {r.text[:40]!r}")
    check("空內容仍有 header 可分辨「沒紀錄」vs「路徑撈錯」",
          r.headers.get("X-Log-Lines") == "0" and r.headers.get("X-Log-Dir") == _TMP,
          f'lines={r.headers.get("X-Log-Lines")} dir={r.headers.get("X-Log-Dir")}')

    s = client.get("/api/admin/scrape-log/summary?days=2", headers=HEAD).json()
    check("summary 零筆時 total=0", s["total"] == 0, str(s["total"]))
    check("零筆時 success_rate_pct 是 None，不是 0%（分母不存在≠成功率 0）",
          s["success_rate_pct"] is None, str(s["success_rate_pct"]))
    check("零筆時 domains/samples 是空的",
          s["domains_by_failure"] == [] and s["samples"] == {})


def test_does_not_break_scraping():
    print("\n【7】匯出端點不影響爬取紀錄本身")
    sm.start("https://example.com/item/1?sessionid=SECRET")
    sm.note_http(200)

    class _P:
        is_valid = True
        title = "x"
        platform_id = "generic"

    sm.record("https://example.com/item/1?sessionid=SECRET", product=_P(), elapsed_ms=5)
    r = client.get("/api/admin/scrape-log?days=1", headers=HEAD)
    check("剛寫入的紀錄馬上撈得到", "example.com" in r.text, r.text[:80])
    check("匯出不含 query string（隱私：紀錄本來就沒存）",
          "SECRET" not in r.text and "sessionid" not in r.text)


# ─────────────────────────────────────────────────────────────────────
def main_():
    print("=" * 74)
    print("爬取紀錄匯出端點驗證")
    print(f"紀錄目錄導到暫存目錄：{_TMP}")
    print("=" * 74)
    write_fixtures()
    test_auth()
    test_export()
    test_summary_numbers()
    test_summary_domains()
    test_summary_samples()
    test_empty_and_missing()
    test_does_not_break_scraping()

    print("\n" + "=" * 74)
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
