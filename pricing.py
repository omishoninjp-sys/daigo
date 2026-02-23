"""
定價模組 - 依日幣價格區間計算代購售價
"""
import httpx
from config import PRICING_TIERS, MIN_SERVICE_FEE_JPY, DEFAULT_JPY_TO_TWD_RATE


def calculate_selling_price(original_price_jpy: int) -> dict:
    """
    根據日幣原價，計算代購售價

    Returns:
        {
            "original_price_jpy": 12000,
            "markup_rate": 1.30,
            "service_fee_jpy": 3600,
            "selling_price_jpy": 15600,
            "reference_price_twd": 3276  (參考用)
        }
    """
    markup_rate = 1.30  # 預設

    for low, high, rate in PRICING_TIERS:
        if low <= original_price_jpy <= high:
            markup_rate = rate
            break

    # 計算加成金額
    service_fee = int(original_price_jpy * (markup_rate - 1))

    # 確保最低手續費
    if service_fee < MIN_SERVICE_FEE_JPY:
        service_fee = MIN_SERVICE_FEE_JPY

    selling_price_jpy = original_price_jpy + service_fee

    # 取得台幣參考價
    twd_rate = get_jpy_to_twd_rate()
    reference_price_twd = int(selling_price_jpy * twd_rate) if twd_rate else None

    return {
        "original_price_jpy": original_price_jpy,
        "markup_rate": markup_rate,
        "service_fee_jpy": service_fee,
        "selling_price_jpy": selling_price_jpy,
        "reference_price_twd": reference_price_twd,
        "twd_rate": twd_rate,
    }


_cached_rate = {"value": None, "timestamp": 0}


def get_jpy_to_twd_rate() -> float | None:
    """取得 JPY → TWD 匯率（有快取）"""
    import time

    if DEFAULT_JPY_TO_TWD_RATE > 0:
        return DEFAULT_JPY_TO_TWD_RATE

    # 快取 1 小時
    now = time.time()
    if _cached_rate["value"] and (now - _cached_rate["timestamp"]) < 3600:
        return _cached_rate["value"]

    try:
        # 使用免費匯率 API
        resp = httpx.get(
            "https://api.exchangerate-api.com/v4/latest/JPY",
            timeout=5,
        )
        data = resp.json()
        rate = data["rates"].get("TWD")
        if rate:
            _cached_rate["value"] = rate
            _cached_rate["timestamp"] = now
            return rate
    except Exception as e:
        print(f"[匯率] 取得失敗: {e}")

    # fallback 預設匯率
    return 0.21
