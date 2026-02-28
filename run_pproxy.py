"""pproxy 啟動器 - 強制繞過 uvloop"""
import sys
import asyncio

# 強制重置 event loop policy 為標準 asyncio（不用 uvloop）
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from pproxy.server import main
main()
