"""
PreToolUse 守門員 —— 啟動器（CLAUDE.md 第 44-52 行的三類不可逆動作）
====================================================================
🔴 這支要**極簡，而且盡量永遠不要改**。真正的判斷邏輯在 guard_impl.py。

理由：hook 腳本自己有語法錯誤時，Python 直接 exit 1 —— 那是
**non-blocking error，命令照樣執行**。`try/except` 攔不到自己所在檔案的
SyntaxError（那在編譯期就爆了）。把邏輯拆到另一支用 import 載入，
它壞掉會在 import 時拋例外，這裡接得到，才有辦法 fail-closed。

fail-closed 是刻意的取捨：守門員壞掉時**所有 Bash/PowerShell 命令都會被擋**，
很吵但看得見。fail-open 是安靜地讓危險命令通過 —— 那正是要防的東西。
（與 cleanup 的訂單查詢 fail-closed 同一個理由。）

★ exit code：只有 **2** 會擋下命令（exit 1 是 non-blocking error，命令照跑）。
  這裡同時輸出 permissionDecision=deny 的 JSON **並且** exit 2 ——
  兩個機制任一個失效，另一個還在。
★ JSON 用 ensure_ascii=True：Windows 主控台可能是 cp950，
  直接寫中文會 UnicodeEncodeError → 連 deny 都送不出去 → 變成 fail-open。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
怎麼接上（設定檔本身不進版控，所以設定內容記在這裡）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`.claude/settings.local.json`（機器專屬、已加進 .gitignore）：

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash|PowerShell",
            "hooks": [
              {
                "type": "command",
                "command": "C:/Users/Shan/AppData/Local/Programs/Python/Python311/python.exe C:/Users/Shan/documents/daigo/.claude/hooks/guard.py",
                "timeout": 30
              }
            ]
          }
        ]
      }
    }

★ 用**絕對路徑**、不用 ${CLAUDE_PROJECT_DIR} —— 它在某些情況下是空的，
  結果是 hook 路徑找不到 → non-zero exit → 只報 hook error，命令照樣跑。
★ 路徑刻意選沒有空白的：python 用系統的那支（不是 .venv，venv 重建就壞），
  正斜線在 Windows 也吃得到，這樣 shell form 不用處理引號跳脫。
★ matcher 一定要含 **PowerShell** —— 這個環境有獨立的 PowerShell 工具，
  `Remove-Item` 可以完全不經過 Bash。只掛 Bash 等於留一扇門。

🔴 **改完設定要重開 Claude Code。** 設定是 session 啟動時讀的，
  session 中途新增的設定檔不會生效 —— 2026-09-04 實測：檔案寫好、
  guard 手動呼叫回 exit 2，但 `git push origin main` 照樣執行成功。
  接上之後一定要用真實命令重驗一次，看的是「命令有沒有真的沒跑到」
  （檔案還在不在），不是「hook 有沒有被呼叫」。
"""
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _deny(reason):
    try:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }, ensure_ascii=True))
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.write(reason)
        sys.stderr.flush()
    except Exception:
        pass
    sys.exit(2)


try:
    _raw = sys.stdin.read()
except Exception:
    _raw = ""

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import guard_impl
    _reason = guard_impl.inspect(_raw)
except BaseException as _e:                      # noqa: BLE001 —— 故意包到底
    _deny("[guard] 守門員自己壞了（%s: %s）—— fail-closed，"
          "所有 Bash/PowerShell 命令都會被擋，直到 .claude/hooks/ 修好。"
          % (type(_e).__name__, _e))

if _reason:
    _deny(_reason)

sys.exit(0)
