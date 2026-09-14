"""
A' 請款前重驗價（price_verify）：HMAC 驗簽、綁毛利門檻、fail-closed 編排。

離線、不連 Shopify（get_meta / scrape_fn 用假的）。
特別釘死：**驗證函式拋錯 → 訂單不會變成 OK**（fail-closed 核心）。
"""
import sys
import json
import hmac as _hmac
import hashlib
import base64
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import price_verify as pv

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(_hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# ═══════════════════════════════════════════════════════════════════
def test_hmac():
    print("\n【1】HMAC 驗簽（secret 為空一律 False —— 不可當通過）")
    body = b'{"id":123}'
    good = _sign(body, "s3cret")
    check("正確簽章 → True", pv.verify_hmac(body, good, "s3cret") is True)
    check("錯誤簽章 → False", pv.verify_hmac(body, good, "wrong") is False)
    check("竄改 body → False", pv.verify_hmac(b'{"id":999}', good, "s3cret") is False)
    check("★ secret 為空 → False（沒有 secret 不可以跳過驗簽）", pv.verify_hmac(body, good, "") is False)
    check("header 為空 → False", pv.verify_hmac(body, "", "s3cret") is False)
    check("None 也不炸", pv.verify_hmac(body, None, "s3cret") is False)


def test_classify():
    print("\n【2】門檻：現成本 vs 售價（綁毛利，非百分比）")
    T = pv
    # 售價 100，記錄原價 80
    check("現成本 ≥ 售價 → 需確認（現100=售100）", T.classify(100, 100, 80)[0] == T.TAG_BLOCK)
    check("現成本 > 售價 → 需確認", T.classify(120, 100, 80)[0] == T.TAG_BLOCK)
    check("現成本 = 售價×0.9 → 毛利偏低", T.classify(90, 100, 80)[0] == T.TAG_WARN)
    check("現成本 = 售價×0.95 → 毛利偏低", T.classify(95, 100, 80)[0] == T.TAG_WARN)
    check("現成本 < 售價×0.9 且 > 記錄×0.8 → OK", T.classify(70, 100, 80)[0] == T.TAG_OK)
    check("現成本 = 記錄×0.8 → 降價（②）", T.classify(64, 100, 80)[0] == T.TAG_DROP)
    check("現成本 < 記錄×0.8 → 降價", T.classify(50, 100, 80)[0] == T.TAG_DROP)
    check("現成本 None → 失敗（不是 OK）", T.classify(None, 100, 80)[0] == T.TAG_FAIL)
    check("現成本 0 → 失敗", T.classify(0, 100, 80)[0] == T.TAG_FAIL)
    check("售價 None → 失敗", T.classify(70, None, 80)[0] == T.TAG_FAIL)
    print("  —— 真實樣本 ——")
    # 14300→現15400、售17160：現<售×0.9? 17160*0.9=15444 > 15400 → OK
    check("★ 14300→現15400 售17160 → OK（+8% 不擋，仍有毛利）",
          T.classify(15400, 17160, 14300)[0] == T.TAG_OK, str(T.classify(15400, 17160, 14300)))
    # 3916→現7700、售4895：現>售 → 需確認
    check("★ 3916→現7700 售4895 → 需確認（+97% 虧本）",
          T.classify(7700, 4895, 3916)[0] == T.TAG_BLOCK, str(T.classify(7700, 4895, 3916)))
    # 1320→現2112、售1650：現>售 → 需確認
    check("1320→現2112 售1650 → 需確認", T.classify(2112, 1650, 1320)[0] == T.TAG_BLOCK)
    # 字串價格也吃
    check("字串 '15400' 也解析", T.classify("15400", "17160", "14300")[0] == T.TAG_OK)


def _order(*prices_and_pids):
    return {"id": 1, "line_items": [{"product_id": p, "price": s, "title": f"t{p}"}
                                    for p, s in prices_and_pids]}


async def _meta_ok(pid):
    return f"https://src/{pid}", 80          # source_url + 記錄原價 80


async def _scrape_const(url):
    return 70                                # 現成本 70 → OK


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_verify_order_ok():
    print("\n【3】verify_order：全部 OK 才 OK")
    order = _order((111, 100), (222, 100))    # 售價 100，現成本 70 → 都 OK
    res = run(pv.verify_order(order, _meta_ok, _scrape_const))
    check("整單 ok=True", res["ok"] is True)
    check("apply = {OK}", res["apply"] == {pv.TAG_OK}, str(res["apply"]))


def test_verify_order_mixed():
    print("\n【4】一條需確認 + 一條 OK → 整單非 OK，不含 OK 標籤")
    async def scrape(url):
        return 150 if url.endswith("222") else 70   # 222 現成本150>售100 → 需確認
    order = _order((111, 100), (222, 100))
    res = run(pv.verify_order(order, _meta_ok, scrape))
    check("ok=False", res["ok"] is False)
    check("★ apply 含需確認、不含 OK", pv.TAG_BLOCK in res["apply"] and pv.TAG_OK not in res["apply"],
          str(res["apply"]))


def test_verify_order_scrape_raises():
    print("\n【5】★★ fail-closed 核心：scrape 拋錯 → 該 line 失敗 → 整單絕不 OK")
    async def scrape_boom(url):
        raise RuntimeError("scraper 掛了")
    order = _order((111, 100))
    res = run(pv.verify_order(order, _meta_ok, scrape_boom))
    check("ok=False", res["ok"] is False)
    check("★ 該 line = 失敗", res["lines"][0]["tag"] == pv.TAG_FAIL, str(res["lines"][0]))
    check("★★ apply 不含 OK（驗證函式拋錯不會變成 OK）", pv.TAG_OK not in res["apply"], str(res["apply"]))
    check("apply 含失敗", pv.TAG_FAIL in res["apply"])
    # 一好一壞：好的那條也不能把整單救成 OK
    async def scrape_half(url):
        if url.endswith("222"):
            raise RuntimeError("boom")
        return 70
    res2 = run(pv.verify_order(_order((111, 100), (222, 100)), _meta_ok, scrape_half))
    check("★ 一條爬失敗 → 整單仍不 OK", res2["ok"] is False and pv.TAG_OK not in res2["apply"], str(res2["apply"]))


def test_verify_order_no_source():
    print("\n【6】商品缺 source_url → 失敗（不是 OK）")
    async def meta_none(pid):
        return None, None
    res = run(pv.verify_order(_order((111, 100)), meta_none, _scrape_const))
    check("該 line = 失敗", res["lines"][0]["tag"] == pv.TAG_FAIL)
    check("整單不 OK", res["ok"] is False and pv.TAG_OK not in res["apply"])

    print("  空 line_items → 不 OK、apply 不含 OK（怪單當人工）")
    res2 = run(pv.verify_order({"id": 1, "line_items": []}, _meta_ok, _scrape_const))
    check("空單 ok=False", res2["ok"] is False)
    check("空單 apply 不含 OK", pv.TAG_OK not in res2["apply"], str(res2["apply"]))


def test_meta_get_raises():
    print("\n【7】get_meta 本身拋錯 → 失敗（不是 OK）")
    async def meta_boom(pid):
        raise RuntimeError("graphql 掛了")
    res = run(pv.verify_order(_order((111, 100)), meta_boom, _scrape_const))
    check("★ apply 不含 OK", pv.TAG_OK not in res["apply"] and pv.TAG_FAIL in res["apply"], str(res["apply"]))


def main_():
    print("=" * 74)
    print("A' price_verify：HMAC + 綁毛利門檻 + fail-closed")
    print("=" * 74)
    test_hmac()
    test_classify()
    test_verify_order_ok()
    test_verify_order_mixed()
    test_verify_order_scrape_raises()
    test_verify_order_no_source()
    test_meta_get_raises()
    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main_())
