"""
數值型環境變數的容錯驗證（離線，不連外）
==========================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_config_env.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_config_env.py`）

★ 這支存在的理由（2026-09-03）：
   config.py 原本 9 個變數都是 int(os.getenv("X", "預設"))。
   **os.getenv 的預設值只在「變數不存在」時生效** —— 變數存在但值是空字串時
   拿到的是 ""，int("") 直接 ValueError，config 載入失敗，
   **整個 API 起不來**：不只摘要不寄，連 create-order 一起掛。
   而「在 Zeabur 建了變數但值留空」是很容易發生的操作。

   其中兩個特別要命：
     MIN_SERVICE_FEE_JPY     直接進售價運算
     DAIGO_AUTO_DELETE_DAYS  決定刪掉幾天前的商品

★ 怎麼測：config.py 是 module-level 的，同一個 process 內改環境變數再 import
   不會重新求值。所以每個情境都開一個**新的 subprocess**，
   帶不同的環境變數載入 config，把結果吐回來對答案。
   四個情境各一次 subprocess（不是每個變數各一次），跑得完。
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


# 變數名 → (預設值, 測試用的正常值)
VARS = {
    "MIN_SERVICE_FEE_JPY": (300, 500),
    "DEFAULT_JPY_TO_TWD_RATE": (0.0, 0.23),
    "SCRAPE_TIMEOUT": (30, 45),
    "CACHE_TTL": (1800, 600),
    "MAX_CONCURRENT_SCRAPES": (3, 5),
    "SCRAPE_QUEUE_TIMEOUT": (90, 120),
    "DAIGO_AUTO_DELETE_DAYS": (30, 14),
    "DIGEST_HOUR_UTC": (1, 3),
    "DIGEST_STREAK_DAYS": (7, 10),
}

# ★ 這兩個退回預設值時警告要更明顯
CRITICAL = ("MIN_SERVICE_FEE_JPY", "DAIGO_AUTO_DELETE_DAYS")

# 故意用一個好認的字串當「非數字」的值 —— 用來證明警告**不會把值印出來**
SENTINEL = "SUPERSECRETVALUE12345"

_DUMP = (
    "import json, config; "
    "print('@@' + json.dumps({k: getattr(config, k) for k in %r}))"
    % (list(VARS),)
)


def load(env_overrides, drop=()):
    """開一個新 process 載入 config，回 (值的 dict, 輸出全文)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["PYTHONIOENCODING"] = "utf-8"
    for k in drop:
        env.pop(k, None)
    env.update(env_overrides)
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", _DUMP],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    out = (r.stdout or "") + (r.stderr or "")
    vals = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@"):
            vals = json.loads(line[2:])
    return vals, out, r.returncode


# ═══════════════════════════════════════════════════════════════════
def test_unset():
    print()
    print("【1】未設定 → 全部用預設值，不可以有警告")
    vals, out, rc = load({}, drop=list(VARS))
    check("config 載入成功", rc == 0 and vals is not None, f"rc={rc}")
    if not vals:
        return
    for name, (default, _) in VARS.items():
        check(f"{name} = {default}", vals[name] == default, str(vals[name]))
    check("★ 沒有解析失敗的警告（未設定是設計好的路徑，不是降級）",
          "解析失敗" not in out, out[-160:])
    check("★ 但有一行彙總，看得出線上實際用哪些預設值",
          "數值變數未設定，使用預設值" in out, "")


def test_empty_string():
    print()
    print("【2】★ 空字串 → 退回預設值並警告，**不可以拋例外**")
    vals, out, rc = load({k: "" for k in VARS})
    check("★ config 仍然載入成功（本來這裡整個 API 起不來）",
          rc == 0 and vals is not None, f"rc={rc} {out[-200:] if rc else ''}")
    if not vals:
        return
    for name, (default, _) in VARS.items():
        check(f"{name} 退回 {default}", vals[name] == default, str(vals[name]))
        check(f"{name} 有印警告", f"{name} 解析失敗" in out, "")
    check("警告說得出拿到什麼（空字串）", out.count("空字串（長度 0）") == len(VARS),
          str(out.count("空字串（長度 0）")))
    check("警告說得出退回哪個預設值", "退回預設值 300" in out and "退回預設值 30" in out)


def test_non_numeric():
    print()
    print("【3】非數字 → 退回預設值並警告；且**不可以把值印出來**")
    vals, out, rc = load({k: SENTINEL for k in VARS})
    check("config 載入成功", rc == 0 and vals is not None, f"rc={rc}")
    if not vals:
        return
    for name, (default, _) in VARS.items():
        check(f"{name} 退回 {default}", vals[name] == default, str(vals[name]))
    leaked = [l for l in out.splitlines() if SENTINEL in l]
    check("★ 警告裡不含值本身（環境變數可能被誤填成金鑰）",
          not leaked, f"洩漏在：{leaked[:1]}")
    check("★ 改成印型別與長度", f"str，長度 {len(SENTINEL)}" in out,
          [l for l in out.splitlines() if "解析失敗" in l][:1])

    # 只有空白字元也要當成不可用
    vals2, out2, rc2 = load({k: "   " for k in VARS})
    check("只有空白字元也退回預設值",
          rc2 == 0 and vals2 and vals2["CACHE_TTL"] == 1800,
          str(vals2 and vals2["CACHE_TTL"]))
    check("警告分得出「只有空白字元」", "只有空白字元" in out2, out2[-120:])


def test_valid_values():
    print()
    print("【4】正常值 → 行為完全不變（沒有因為容錯而改掉語意）")
    vals, out, rc = load({k: str(v[1]) for k, v in VARS.items()})
    check("config 載入成功", rc == 0 and vals is not None, f"rc={rc}")
    if not vals:
        return
    for name, (_, good) in VARS.items():
        check(f"{name} = {good}（吃到設定值）", vals[name] == good, str(vals[name]))
    check("★ 沒有任何警告", "解析失敗" not in out, out[-160:])

    # 前後有空白的正常值要照樣吃得到（Zeabur 貼上很容易多一個空格）
    vals2, _, _ = load({k: f"  {v[1]}  " for k, v in VARS.items()})
    check("★ 值前後有空白仍然解析得出來",
          vals2 and vals2["MAX_CONCURRENT_SCRAPES"] == 5,
          str(vals2 and vals2["MAX_CONCURRENT_SCRAPES"]))


def test_critical_vars():
    print()
    print("【5】★ 進售價運算 / 決定刪除範圍的兩個，警告要更明顯")
    vals, out, rc = load({k: "" for k in CRITICAL})
    check("config 載入成功", rc == 0 and vals is not None, f"rc={rc}")
    lines = [l for l in out.splitlines() if "[Config]" in l]
    for name in CRITICAL:
        own = [l for l in lines if name in l]
        check(f"{name} 有更醒目的標記（🔴🔴 不是 ⚠️）",
              any("🔴🔴" in l for l in own), str(own[:1]))
        check(f"{name} 說得出後果", len(own) >= 2, f"{len(own)} 行")
    check("★ MIN_SERVICE_FEE_JPY 講明它進售價運算",
          "售價運算" in out and "報價" in out)
    check("★ DAIGO_AUTO_DELETE_DAYS 講明它決定刪除範圍",
          "刪掉幾天前的商品" in out and "刪除範圍" in out)
    check("兩個都叫人去 Zeabur 檢查", out.count("請到 Zeabur 檢查") == 2,
          str(out.count("請到 Zeabur 檢查")))

    # 非關鍵變數不可以用同一個等級的標記
    vals2, out2, _ = load({"CACHE_TTL": ""}, drop=list(CRITICAL))
    check("★ 一般變數只用 ⚠️，不搶注意力",
          "CACHE_TTL 解析失敗" in out2 and "🔴🔴" not in out2, out2[-140:])


def test_no_bare_getenv():
    print()
    print("【6】config.py 裡不可以再有裸的 int/float(os.getenv(...)) 賦值")
    import io
    import re
    src = io.open("config.py", encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    bad = re.findall(r"^[A-Z_]+\s*=\s*(?:int|float)\s*\(", code, re.M)
    check("★ 沒有漏網的（漏一個就等於留一顆同樣的地雷）", not bad, str(bad))
    used = len(re.findall(r"^[A-Z_]+\s*=\s*_(?:int|float)_env\(", code, re.M))
    check("★ 9 個賦值全部改用 _int_env / _float_env", used == 9, f"{used} 個")


def main_():
    print("=" * 74)
    print("數值型環境變數的容錯")
    print("=" * 74)
    test_unset()
    test_empty_string()
    test_non_numeric()
    test_valid_values()
    test_critical_vars()
    test_no_bare_getenv()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main_())
