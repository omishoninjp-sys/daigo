"""
PreToolUse 守門員驗證（離線，不真的跑任何危險命令）
=====================================================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_hook_guard.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_hook_guard.py`）

擋的是 CLAUDE.md 第 44-52 行的三類不可逆動作：git push／刪除／批次改線上資料。

★ 這支怎麼測：把 hook 的輸入 JSON 從 stdin 餵給 .claude/hooks/guard.py
  （真的開一個 subprocess，不是 import 邏輯來測），檢查
    · exit code 是不是 2（**只有 2 會擋下命令**，1 是 non-blocking error，
      命令照樣執行 —— 這是最容易搞錯的地方）
    · stdout 的 JSON 是不是 permissionDecision=deny
  危險命令一個都不會真的被執行，因為 guard.py 只是讀 JSON 然後回答。

🔴 「不擋的」那組跟「要擋的」一樣重要。擋太多會變成每次都要繞過，
   那比沒有更糟。所以放行組裡放的是這個 session 真的跑過的命令。
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []
# GUARD_PATH：改守門員時先把副本放 scratchpad 跑過這支，再換掉正式檔案 ——
# guard 自己改壞是 fail-closed，會把所有命令都擋掉。
GUARD = os.environ.get("GUARD_PATH") or os.path.join(".claude", "hooks", "guard.py")
GUARD_IMPL = os.path.join(os.path.dirname(GUARD), "guard_impl.py")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


def run_guard(command, tool="Bash"):
    """回 (exit_code, decision, reason)。decision 取自 stdout 的 JSON。"""
    payload = json.dumps({
        "session_id": "test", "hook_event_name": "PreToolUse",
        "cwd": os.getcwd(), "tool_name": tool,
        "tool_input": {"command": command},
    }, ensure_ascii=False)
    r = subprocess.run([sys.executable, GUARD], input=payload,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    decision, reason = "", ""
    try:
        out = json.loads((r.stdout or "").strip())
        hso = out.get("hookSpecificOutput") or {}
        decision = hso.get("permissionDecision", "")
        reason = hso.get("permissionDecisionReason", "")
    except Exception:
        pass
    return r.returncode, decision, reason


def expect_block(command, label, tool="Bash"):
    code, decision, reason = run_guard(command, tool)
    ok = code == 2 and decision == "deny"
    check(f"擋下 {label}", ok, f"exit={code} decision={decision!r} {reason[:44]}")
    return ok


def expect_allow(command, label, tool="Bash"):
    code, decision, reason = run_guard(command, tool)
    ok = code == 0 and decision == ""
    check(f"放行 {label}", ok, f"exit={code} {reason[:60]}")
    return ok


# ═══════════════════════════════════════════════════════════════════
def test_git_push():
    print()
    print("【1】第一類：git push")
    for cmd, label in [
        ("git push", "git push"),
        ("git push origin main", "git push origin main"),
        ("git push -f origin main", "git push -f"),
        ("git push --force-with-lease", "--force-with-lease"),
        ("git -C /some/dir push", "git -C <dir> push"),
        ("git -c user.name=x push origin HEAD", "git -c k=v push"),
        ("git --git-dir=.git push", "--git-dir= 之後的 push"),
        ("git add . && git push", "藏在 && 後面"),
        ("git status; git push origin main", "藏在 ; 後面"),
    ]:
        expect_block(cmd, label)


def test_delete():
    print()
    print("【2】第二類：刪除")
    for cmd, label in [
        ("rm x.txt", "rm"),
        ("rm -rf build/", "rm -rf"),
        ("rmdir olddir", "rmdir"),
        ("git clean -fd", "git clean"),
        ("git rm --cached x", "git rm"),
        ("git reset --hard origin/main", "git reset --hard"),
        ("git checkout -- scrapers/generic.py", "git checkout -- <path>"),
        ("git restore scrapers/generic.py", "git restore <path>"),
        ("python -c \"import os; os.remove('x')\"", "os.remove 內嵌"),
        ("python -c \"import shutil; shutil.rmtree('x')\"", "shutil.rmtree 內嵌"),
        ("cat a.txt | rm b.txt", "管線第二段的 rm"),
    ]:
        expect_block(cmd, label)
    # PowerShell 工具走的是另一條路（不經過 Bash）
    for cmd, label in [
        ("Remove-Item -Recurse -Force build", "Remove-Item"),
        ("del x.txt", "del"),
        ("ri x.txt", "ri（Remove-Item 別名）"),
        ("REMOVE-ITEM x.txt", "大小寫不敏感"),
    ]:
        expect_block(cmd, label, tool="PowerShell")


def test_bulk():
    print()
    print("【3】第三類：批次改線上資料")
    for cmd, label in [
        ("python -c \"print('productDelete')\"", "內嵌 productDelete"),
        ("python -c \"x = productUpdate(1)\"", "內嵌 productUpdate"),
        ("python -c \"bulkOperationRunMutation()\"", "bulkOperationRunMutation"),
        ("python -c \"await shopify.cleanup_old_daigo_products()\"",
         "cleanup_old_daigo_products"),
        ("curl -X POST https://x.zeabur.app/api/admin/cleanup", "/api/admin/cleanup"),
        ("powershell -EncodedCommand aGVsbG8=", "-EncodedCommand（看不到內容）"),
    ]:
        expect_block(cmd, label)
    expect_allow("curl https://x.zeabur.app/api/admin/cleanup/preview?days=30",
                 "/api/admin/cleanup/preview（只看不刪）")


def test_allow():
    print()
    print("【4】★ 不可以誤擋 —— 這些都是這個 session 真的跑過的命令")
    for cmd, label in [
        ("git status --short", "git status"),
        ("git add scrape_monitor.py tests/verify_scrape_monitor.py", "git add"),
        ("git diff --stat", "git diff"),
        ("git log --oneline -3", "git log"),
        ("git checkout -b feature/x", "git checkout -b（開分支不是丟修改）"),
        ("git restore --staged x.py", "git restore --staged（只取消暫存）"),
        ("git stash list", "git stash list"),
        ("PYTHONPATH=. python -X utf8 tests/verify_pricing.py", "跑測試"),
        (".venv/Scripts/python.exe -X utf8 tests/verify_cleanup_retry.py",
         "★ 跑含 productDelete 字樣的測試（tests/ 豁免）"),
        ("PYTHONPATH=. .venv/Scripts/python.exe -X utf8 tests/verify_graphql_retry.py",
         "★ 跑含 bulk 字樣的測試"),
        ("grep -rn 'shutil.rmtree' scrapers/", "grep 找 rmtree（讀取不是執行）"),
        ("cat CLAUDE.md | head -20", "cat + head"),
        ("sed -n '1,30p' config.py", "sed -n 讀取"),
        ("python -c \"print(1+1)\"", "無害的 python -c"),
        ("curl -s https://x.zeabur.app/api/admin/scrape-log?days=2", "讀取爬取紀錄"),
    ]:
        expect_allow(cmd, label)


def test_commit_message_not_misread():
    print()
    print("【5】★ heredoc 與引號：commit message 提到危險字不可以擋掉自己的 commit")
    msg = ("git commit -q -F - <<'EOF'\n"
           "修正 X\n\n"
           "CLAUDE.md 說 git push、刪除檔案或資料、批次改線上資料，"
           "三類做之前一定要問。\n"
           "順帶把 rm -rf 那段註解改掉，並說明 productDelete 的重試風險。\n"
           "EOF")
    expect_allow(msg, "commit message 內文提到 git push / rm -rf / productDelete")
    expect_allow('git commit -m "不要 push，先問過"', "-m 訊息裡有 push")
    expect_allow('echo "rm -rf /"', "echo 一個字串")
    # 但 heredoc 結束之後的真命令仍然要擋
    after = ("git commit -F - <<'EOF'\n訊息\nEOF\ngit push origin main")
    expect_block(after, "heredoc 結束後真的 git push")


def test_failsafe():
    print()
    print("【6】★ fail-closed：守門員自己壞掉時要擋，不是放行")
    impl = GUARD_IMPL
    backup = open(impl, encoding="utf-8").read()
    try:
        with open(impl, "w", encoding="utf-8", newline="\n") as f:
            f.write(backup + "\n\nthis is not valid python !!!\n")
        code, decision, reason = run_guard("git status")
        check("★ guard_impl 有語法錯誤時，連無害的命令也被擋（fail-closed）",
              code == 2 and decision == "deny", f"exit={code} {reason[:60]}")
        check("★ 理由說得出是守門員自己壞了", "守門員自己壞了" in reason, reason[:70])
    finally:
        with open(impl, "w", encoding="utf-8", newline="\n") as f:
            f.write(backup)
    code, decision, _ = run_guard("git status")
    check("還原後恢復正常", code == 0 and decision == "", f"exit={code}")

    # 壞掉的輸入不可以害它放行
    r = subprocess.run([sys.executable, GUARD], input="not json at all",
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    check("★ 輸入不是 JSON 時也 fail-closed", r.returncode == 2, f"exit={r.returncode}")


def test_other_tools_untouched():
    print()
    print("【7】其他工具不受影響（matcher 只掛 Bash|PowerShell）")
    code, decision, _ = run_guard("git push origin main", tool="Read")
    check("tool_name 不是 Bash/PowerShell 時不介入", code == 0 and decision == "",
          f"exit={code}")


def test_stdin_utf8():
    print()
    print("【8】★ stdin 是 UTF-8，不是 locale（cp950）—— 2026-09-15")
    # 中文尾位元組落在 0xA1–0xBF 的字（缺 ba／單 ae／新 b0）會把緊接的 ASCII
    # 吃掉當 cp950 第二位元組。fail-closed 那邊只是吵，fail-open 那邊是防線有洞。
    for cmd, label in [
        ('echo "建單"', "中文緊接 \"（以前 JSONDecodeError 誤擋）"),
        ('grep -n "source_url\\|缺\\|SAFE_TAG" price_verify.py', "中文緊接 \\|（以前 Invalid \\escape）"),
        ('print("--- 驗證時間 vs 建單時間（x）")', "全形括號緊接 \""),
    ]:
        expect_allow(cmd, label)
    for cmd, label in [
        ("echo 缺|rm -rf y", "★ 中文緊接 |rm —— 管線符號以前會被吃掉、rm 黏進 echo → 放行"),
        ("echo 新|git push origin main", "★ 中文緊接 |git push"),
        ("echo 缺;rm -rf y", "中文緊接 ;rm（; 不在 cp950 trail 範圍，本來就擋得到）"),
    ]:
        expect_block(cmd, label)


def test_c_flag_project_modules():
    print()
    print("【9】★ python -c 提到專案模組 = 等同執行該模組；stdin 餵程式碼一律擋")
    for cmd, label in [
        ('python -c "import main"', "-c import main（main.py 含 tagsAdd）"),
        ('python -c "from shopify_client import ShopifyClient"', "-c from shopify_client（含 productDelete）"),
        ('python -c "import importlib; importlib.import_module(\'main\')"', "-c importlib（不解析語法也抓得到）"),
        ('python -c "__import__(\'shopify_client\')"', "-c __import__"),
        ("python -m main", "-m main"),
        ("python - <<'EOF'\nimport main\nEOF", "python - <<EOF（stdin）"),
        ("python <<'EOF'\nprint(1)\nEOF", "python <<EOF（stdin，沒有 -）"),
        ('echo "import main" | python', "echo … | python"),
        ("cat x.py | python -", "cat … | python -"),
    ]:
        expect_block(cmd, label)
    for cmd, label in [
        ('python -c "from pricing import calculate_selling_price; print(calculate_selling_price(1000))"',
         "-c 提到 pricing（沒有 marker）"),
        ('python -c "import config; print(config.MIN_SERVICE_FEE_JPY)"', "-c 提到 config"),
        ('python -c "import json, sys; print(json.dumps(sys.argv))"', "-c 只用標準庫"),
        ("python -V", "python -V"),
        ("python --version", "python --version"),
        ("python -m pip list", "-m pip（標準庫／第三方，不是專案模組）"),
        ("python -m json.tool x.json", "-m json.tool"),
        ("python -m http.server 8000", "-m http.server"),
        ("python -X utf8 tests/verify_pricing.py > out.txt 2>&1", "腳本 + 重導向"),
    ]:
        expect_allow(cmd, label)


def test_m_read_only_whitelist():
    print()
    print("【10】★ -m 白名單：只認 -m 緊接的那個 token")
    for mod in ("py_compile", "compileall", "pyflakes", "black", "isort", "ruff"):
        expect_allow(f"python -m {mod} main.py", f"-m {mod} main.py（main.py 含 tagsAdd 也放行）")
    expect_allow("python -m py_compile shopify_client.py main.py scrapers/generic.py", "-m py_compile 多檔")
    expect_allow("python -m ruff check .", "-m ruff check .")
    for cmd, label in [
        ('python -c "import py_compile; import main"', "★ -c 裡出現 py_compile 不算白名單"),
        ("python -m py_compile main.py; python main.py", "白名單段之後的 python main.py 照擋"),
        ("python -m py_compile main.py && python -c \"import shopify_client\"", "&& 後的 -c"),
    ]:
        expect_block(cmd, label)


def _mutate(path, old, new, run):
    """把守門員某一行改壞、跑 run()、一定還原。用來證明測試真的抓得到。"""
    backup = open(path, encoding="utf-8").read()
    assert backup.count(old) == 1, f"mutation 目標不唯一/不存在: {old!r}"
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(backup.replace(old, new))
        return run()
    finally:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(backup)


def test_negative():
    print()
    print("【11】★ 負向驗證：把修正拿掉，對應的測試要紅（證明測試抓得到）")
    # (a) stdin 改回 locale 解碼 → 中文管線那條要變成放行
    def a():
        code, dec, _ = run_guard("echo 缺|rm -rf y")
        code2, dec2, reason2 = run_guard('echo "建單"')
        return code == 0 and dec == "" and code2 == 2 and "守門員自己壞了" in reason2
    check("(a) 拿掉 UTF-8 解碼 → `echo 缺|rm -rf y` 放行、`echo \"建單\"` JSONDecodeError",
          _mutate(GUARD, 'sys.stdin.buffer.read().decode("utf-8")', "sys.stdin.read()", a))
    # (b) 白名單改成子字串比對 → -c 裡有 py_compile 就放行
    def b():
        code, dec, _ = run_guard('python -c "import py_compile; import main"')
        return code == 0 and dec == ""
    check("(b) 白名單改子字串比對 → `-c \"import py_compile; import main\"` 放行",
          _mutate(GUARD_IMPL, "blob, visible = [], False",
                  "if any(m in seg for m in _M_READ_ONLY):\n            return None\n"
                  "        blob, visible = [], False", b))
    # (c) 拿掉 -c 展開 → import main 放行
    def c():
        code, dec, _ = run_guard('python -c "import main"')
        return code == 0 and dec == ""
    check("(c) 拿掉 -c 的模組展開 → `-c \"import main\"` 放行",
          _mutate(GUARD_IMPL, "blob.extend(_modules_named_in(code, cwd))", "pass", c))
    # (d) 拿掉 stdin 規則 → heredoc 餵 python 放行
    def d():
        code, dec, _ = run_guard("python <<'EOF'\nimport main\nEOF")
        return code == 0 and dec == ""
    check("(d) 拿掉 stdin 規則 → `python <<EOF` 放行",
          _mutate(GUARD_IMPL, "if not visible:", "if False:", d))
    code, dec, _ = run_guard("echo 缺|rm -rf y")
    check("還原後恢復正常（中文管線 rm 仍被擋）", code == 2 and dec == "deny", f"exit={code}")


def main_():
    print("=" * 74)
    print(f"PreToolUse 守門員  ({GUARD})")
    print("=" * 74)
    if not os.path.isfile(GUARD):
        print(f"❌ 找不到 {GUARD}")
        return 1
    test_git_push()
    test_delete()
    test_bulk()
    test_allow()
    test_commit_message_not_misread()
    test_failsafe()
    test_other_tools_untouched()
    test_stdin_utf8()
    test_c_flag_project_modules()
    test_m_read_only_whitelist()
    test_negative()
    print()
    print("=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main_())
