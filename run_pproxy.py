"""pproxy 啟動器 - 避免 uvloop 衝突"""
import sys
import asyncio

# 強制用標準 event loop（不用 uvloop）
asyncio.set_event_loop(asyncio.new_event_loop())

from pproxy.server import main
main()
