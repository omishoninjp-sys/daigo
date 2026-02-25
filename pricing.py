"""定價模組"""
import time
import httpx
from config import PRICING_TIERS, MIN_SERVICE_FEE_JPY, DEFAULT_JPY_TO_TWD_RATE


def calculate_selling_price(original_price_jpy: int) -> dict:
    markup_rate = 1.30
    for low, high, rate in PRICING_TIERS:
        if low <= original_price_jpy <= high:
            markup_rate = rate
            break

    service_fee = int(original_price_jpy * (markup_rate - 1))
    if service_fee < MIN_SERVICE_FEE_JPY:
        service_fee = MIN_SERVICE_FEE_JPY

    selling_price_jpy = original_price_jpy + service_fee
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
    if DEFAULT_JPY_TO_TWD_RATE > 0:
        return DEFAULT_JPY_TO_TWD_RATE

    now = time.time()
    if _cached_rate["value"] and (now - _cached_rate["timestamp"]) < 3600:
        return _cached_rate["value"]

    try:
        resp = httpx.get("https://api.exchangerate-api.com/v4/latest/JPY", timeout=5)
        data = resp.json()
        rate = data["rates"].get("TWD")
        if rate:
            _cached_rate["value"] = rate
            _cached_rate["timestamp"] = now
            return rate
    except Exception as e:
        print(f"[匯率] 取得失敗: {e}")

    return 0.21
