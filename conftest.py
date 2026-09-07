"""讓 tests/ 底下的測試 import 得到專案根目錄的模組。

pytest 會自動載入 rootdir 的 conftest.py，所以
    pytest tests/verify_restricted_category.py
不需要再設 PYTHONPATH。

⚠️ 但本專案的測試多半是**直接執行的腳本**（python tests/xxx.py），
   那條路徑不會載入 conftest.py。直接執行時仍需要：
       PYTHONPATH=. python tests/verify_restricted_category.py
   （23 個既有測試全部都是這個情況，不是這支測試獨有的問題。）
"""
import pathlib
import sys

_ROOT = str(pathlib.Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
