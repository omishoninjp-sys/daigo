"""
PreToolUse 守門員 —— 判斷邏輯
==============================
擋 CLAUDE.md 第 44-52 行定義的三類不可逆動作：
  一、git push（任何形式）
  二、刪除檔案或資料（含 git reset --hard / git checkout -- 這種「丟掉未提交的工作」）
  三、批次改線上資料（只擋明顯的）

🔴 不擋的東西同樣重要：`git commit` / `add` / `status` / `diff` / `log`、
   跑測試、`cat` / `grep` / `sed -n` 這類讀取。
   **擋太多會變成每次都要繞過，那比沒有更糟。**

★ 為什麼不用 regex 掃整行
  `git commit -m "…不要 push…"` 會被 `\bgit\b.*\bpush\b` 誤擋，
  而這個專案的 commit message 常常提到 push。
  改成「切段 → tokenize → 找出真正的子命令」。

★ heredoc 一定要先剝掉
  `git commit -F - <<'EOF' … EOF` 的內文會被當成一行行的命令，
  commit message 裡出現 `git push` 或 `rm -rf` 就會誤擋自己的 commit。
"""
import json
import os
import re
import shlex

# ─────────────────────────────────────────────────────────────────────
# 一、git 子命令
# ─────────────────────────────────────────────────────────────────────
# 吃掉下一個 token 的 git 全域旗標
_GIT_FLAGS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                       "--exec-path", "--super-prefix"}

_GIT_BLOCKED_SUBCMD = {
    "push": "git push",
    "clean": "git clean（會刪掉未追蹤的檔案）",
    "rm": "git rm（會刪檔）",
}

# ─────────────────────────────────────────────────────────────────────
# 二、刪除
# ─────────────────────────────────────────────────────────────────────
_DELETE_CMDS = {
    # bash
    "rm": "rm", "rmdir": "rmdir", "unlink": "unlink", "shred": "shred",
    # PowerShell（大小寫不敏感，這裡一律小寫比對）
    "remove-item": "Remove-Item", "ri": "Remove-Item（ri）",
    "del": "del", "erase": "erase", "rd": "rd",
    "clear-content": "Clear-Content", "remove-itemproperty": "Remove-ItemProperty",
}

# 內嵌程式碼裡的刪除。★ 只在命令**真的會執行程式碼**時才算 ——
#   否則 `grep "shutil.rmtree" x.py` 這種讀取也會被擋。
_DELETE_CODE = (
    "os.remove(", "os.unlink(", "os.rmdir(", "os.removedirs(",
    "shutil.rmtree(", ".unlink(", "send2trash",
)

# ─────────────────────────────────────────────────────────────────────
# 三、批次改線上資料
# ─────────────────────────────────────────────────────────────────────
_BULK_CODE = (
    "productDelete", "productUpdate", "productSet",
    "bulkOperationRunMutation", "productVariantsBulkUpdate",
    "tagsAdd", "tagsRemove", "inventorySetQuantities",
    "cleanup_old_daigo_products",
)

_RUNNERS = {"python", "python3", "pythonw", "py", "powershell", "pwsh"}
# 這些只是包一層，要看它後面真正的命令
_WRAPPERS = {"sudo", "env", "time", "nohup", "nice", "xargs", "command"}

_SCRIPT_EXTS = (".py", ".ps1")


# ─────────────────────────────────────────────────────────────────────
def _strip_heredocs(command):
    """
    把 heredoc 的內文整段拿掉。

    ★ 不拿掉的話，`git commit -F - <<'EOF' … EOF` 的 commit message
      會被一行行當成命令看待 —— 訊息裡寫到「git push」或「rm -rf」
      就會把自己的 commit 擋掉。這個專案的 commit message 常常提到這些。
    """
    lines = command.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        i += 1
        if not m:
            continue
        marker = m.group(2)
        while i < len(lines) and lines[i].strip() != marker:
            i += 1          # 內文整段跳過
        i += 1              # 連結束標記一起跳過
    return "\n".join(out)


def _segments(command):
    """
    切成一個個「命令」。管線每一段也各自是一個命令。

    ★ 一定要認引號。用 re.split 會把
      `python -c "import os; os.remove('x')"` 從引號裡的 `;` 切成兩段，
      `-c` 的內容就散掉了 —— 那條刪除因此完全擋不到。
      2026-09-04 第一次跑測試就是這樣紅的：切段發生在 tokenize 之前，
      所以切段自己必須知道引號在哪。
    """
    out, buf, i, quote = [], [], 0, None
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and i + 1 < len(command):
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command[i:i + 2] in ("&&", "||"):
            out.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|&\n":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def _tokenize(segment):
    try:
        toks = shlex.split(segment, comments=False)
    except ValueError:
        toks = segment.split()
    return toks


def _cmd_word(toks):
    """跳過 VAR=value 與 sudo/env 這類包裝，回 (命令字, 其餘 token)。"""
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):     # VAR=value
            i += 1
            continue
        base = os.path.basename(t.replace("\\", "/")).lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base in _WRAPPERS:
            i += 1
            continue
        return base, toks[i + 1:]
    return "", []


def _git_subcommand(rest):
    """跳過 git 的全域旗標，回真正的子命令。"""
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in _GIT_FLAGS_WITH_ARG:
            i += 2
            continue
        if t.startswith("--") and "=" in t:
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t, rest[i + 1:]
    return "", []


def _is_test_path(path):
    """tests/ 底下的檔案豁免內容掃描。

    ★ 沒有這條的話，`python tests/verify_cleanup_retry.py` 會被擋 ——
      那支測試檔裡就有 productDelete / bulk 字樣，但它是純離線測試。
      擋掉整套測試正是「擋太多比沒有更糟」的樣子。
    """
    p = path.replace("\\", "/").lower()
    return "/tests/" in p or p.startswith("tests/")


def _read_script(path):
    try:
        if os.path.isfile(path) and os.path.getsize(path) < 2_000_000:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _check_segment(seg):
    toks = _tokenize(seg)
    if not toks:
        return None
    cmd, rest = _cmd_word(toks)
    if not cmd:
        return None

    # ── 一、git ────────────────────────────────────────────────────
    if cmd == "git":
        sub, tail = _git_subcommand(rest)
        if sub in _GIT_BLOCKED_SUBCMD:
            return "[guard] 擋下 %s。CLAUDE.md：git push／刪除／批次改線上資料，" \
                   "三類做之前一定要問。" % _GIT_BLOCKED_SUBCMD[sub]
        if sub == "reset" and any(t == "--hard" for t in tail):
            return "[guard] 擋下 git reset --hard —— 會丟掉未提交的工作。" \
                   "今天一整天都在靠備份檔逐位元組還原，那些備份 reset 掉就沒了。"
        if sub == "checkout" and "--" in tail:
            return "[guard] 擋下 git checkout -- <path> —— 會丟掉未提交的修改。"
        if sub == "restore" and not any(t == "--staged" for t in tail):
            return "[guard] 擋下 git restore <path> —— 會丟掉未提交的修改" \
                   "（--staged 只取消暫存，那個不擋）。"
        return None

    # ── 二、刪除命令 ───────────────────────────────────────────────
    if cmd in _DELETE_CMDS:
        return "[guard] 擋下 %s —— 刪除檔案或資料要先問（CLAUDE.md 第 44-52 行）。" \
               % _DELETE_CMDS[cmd]

    # ── 執行程式碼才需要看內容 ──────────────────────────────────────
    if cmd in _RUNNERS:
        blob = []
        i = 0
        while i < len(rest):
            t = rest[i]
            low = t.lower()
            if low in ("-c", "-command", "--command"):
                blob.append(" ".join(rest[i + 1:]))
                break
            if low in ("-encodedcommand", "-e", "-enc"):
                return "[guard] 擋下 -EncodedCommand —— 內容經過編碼，守門員看不到, " \
                       "無法判斷安全性。請改用未編碼的形式。"
            if t.lower().endswith(_SCRIPT_EXTS):
                if not _is_test_path(t):
                    blob.append(_read_script(t))
            i += 1
        text = "\n".join(blob)

        for marker in _DELETE_CODE:
            if marker in text:
                return "[guard] 擋下含 `%s` 的程式碼 —— 那會刪除檔案或資料。" % marker
        for marker in _BULK_CODE:
            if marker in text:
                return "[guard] 擋下含 `%s` 的程式碼 —— 那會批次改線上資料。" % marker
    return None


def inspect(raw):
    """回 deny 的理由字串；沒問題回 None。"""
    data = json.loads(raw) if raw and raw.strip() else {}
    tool = data.get("tool_name") or ""
    if tool not in ("Bash", "PowerShell"):
        return None
    command = (data.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return None

    # ── 三、線上 admin 端點（整條命令看，不分段）──────────────────
    # preview 不刪東西，先剔掉再判斷 —— 它的字首就是 cleanup。
    probe = command.replace("/api/admin/cleanup/preview", "")
    if "/api/admin/cleanup" in probe:
        return "[guard] 擋下 /api/admin/cleanup —— 那會永久刪除線上商品。" \
               "只看不刪請用 /api/admin/cleanup/preview。"

    for seg in _segments(_strip_heredocs(command)):
        reason = _check_segment(seg)
        if reason:
            return reason
    return None
