"""
Chrome driver 管理 Mixin
負責 SeleniumBase UC driver 的建立、維護、重建
"""
import threading
from config import PROXY_URL


# 通用有效尺寸集合（供 MUJI、BEAMS 等使用）
VALID_SIZES = {
    "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL",
    "F", "フリー", "FREE",
    # 日本鞋碼 (cm)
    *[str(n) for n in range(19, 32)],
    *[str(n) for n in range(55, 120, 5)],
    # US 鞋碼（整號 + 半號，5~14）
    *[str(n) for n in range(5, 15)],
    *[f"{n}.5" for n in range(5, 14)],
}


class DriverMixin:
    def __init__(self):
        self._driver = None
        self._driver_lock = threading.Lock()
        self._driver_use_count = 0
        self._driver_max_uses = 50
        self._proxy_verified = False

    def get_driver_status(self) -> dict:
        return {
            "alive": self._driver is not None,
            "use_count": self._driver_use_count,
            "max_uses": self._driver_max_uses,
        }

    def _create_driver(self):
        """建立或重建 Chrome driver"""
        try:
            from seleniumbase import Driver
        except ImportError:
            print("[Driver] seleniumbase 未安裝")
            return None

        if self._driver:
            try:
                self._driver.quit()
            except:
                pass
            self._driver = None

        proxy_arg = None
        if PROXY_URL:
            from urllib.parse import urlparse as _urlparse
            _pp = _urlparse(PROXY_URL)
            proxy_arg = f"{_pp.hostname}:{_pp.port}"
            print(f"[Driver] 建立 Chrome UC + proxy: {proxy_arg}")
        else:
            print(f"[Driver] 建立 Chrome UC（無 proxy）")

        self._driver = Driver(
            uc=True,
            headless=False,
            proxy=proxy_arg,
            locale_code='ja',
            chromium_arg='--lang=ja-JP,--disable-component-update,--disable-background-networking,--disable-sync,--no-first-run,--no-sandbox,--disable-dev-shm-usage',
        )
        self._driver_use_count = 0
        self._proxy_verified = False
        print(f"[Driver] ✅ Chrome 已啟動")
        return self._driver

    def _ensure_driver(self):
        """確保 driver 存活，必要時重建"""
        need_recreate = False

        if self._driver is None:
            need_recreate = True
        elif self._driver_use_count >= self._driver_max_uses:
            print(f"[Driver] 已使用 {self._driver_use_count} 次，重建中...")
            need_recreate = True
        else:
            try:
                _ = self._driver.title
            except:
                print(f"[Driver] Chrome 已斷線，重建中...")
                need_recreate = True

        if need_recreate:
            self._create_driver()

        return self._driver

    def _clean_driver_tabs(self):
        """關閉多餘的 tab，清除 cookies"""
        try:
            handles = self._driver.window_handles
            if len(handles) > 1:
                for h in handles[1:]:
                    self._driver.switch_to.window(h)
                    self._driver.close()
                self._driver.switch_to.window(handles[0])
            self._driver.delete_all_cookies()
        except:
            pass
