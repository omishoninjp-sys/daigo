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


def _meta(found=True, daigo=True, src="https://src/x", rec=80):
    return {"found": found, "daigo": daigo, "source_url": src, "recorded": rec}


async def _meta_ok(pid):
    return _meta(src=f"https://src/{pid}")   # 代購品：source_url + 記錄原價 80


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
    print("\n【6】代購品缺 source_url／product 查不到 → 失敗（不是 OK、也不是不適用）")
    async def meta_daigo_no_src(pid):
        return _meta(daigo=True, src=None)
    res = run(pv.verify_order(_order((111, 100)), meta_daigo_no_src, _scrape_const))
    check("有 daigo.* 但沒 source_url → 該 line = 失敗", res["lines"][0]["tag"] == pv.TAG_FAIL)
    check("整單不 OK", res["ok"] is False and pv.TAG_OK not in res["apply"])
    async def meta_not_found(pid):
        return _meta(found=False, daigo=False, src=None, rec=None)
    res = run(pv.verify_order(_order((111, 100)), meta_not_found, _scrape_const))
    check("★ product 查不到（建單後被刪）→ 失敗，不是不適用（fail-closed：分不出是不是代購品）",
          res["lines"][0]["tag"] == pv.TAG_FAIL and res["apply"] == {pv.TAG_FAIL}, str(res["apply"]))

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


# ── 不適用：常態商品／安心GO／自訂項目 ──────────────────────────────────
# 真實形狀（2026-09-15 REST 實測）：
#   GYT20262738  4 line 全常態（小倉山莊 ×3 + 安心GO），product 存在、沒有 daigo.*
#   GYT20262716  安心GO(常態) + 代購 + 「差額」(product_id=None)
#   GYT20262720  兩件已刪的代購品：product_id=None，標題「日本代購｜…」
NORMAL, DAIGO, GONE = 8635038564586, 8732457107690, None


async def _meta_by_pid(pid):
    """常態商品：found 但沒有 daigo.*；代購品：有 source_url。"""
    if pid == NORMAL:
        return _meta(daigo=False, src=None, rec=None)
    return _meta(src=f"https://src/{pid}")


def _line(pid, price, title):
    return {"product_id": pid, "price": price, "title": title}


def test_na_all_normal():
    print("\n【8】整單全常態 → {不適用}（沒有東西可驗，不是驗過沒問題）")
    order = {"id": 1, "line_items": [_line(NORMAL, "3578.0", "だんらん香具山 化妝箱(大)"),
                                     _line(NORMAL, "1500.0", "GOYOUTATI - 安心GO｜最高理賠上限25萬円")]}
    calls = []
    async def scrape_never(url):
        calls.append(url); return 70
    res = run(pv.verify_order(order, _meta_by_pid, scrape_never))
    check("apply = {不適用}", res["apply"] == {pv.TAG_NA}, str(res["apply"]))
    check("★ ok=False（不適用不是「驗過」）、na=True", res["ok"] is False and res.get("na") is True)
    check("每條 line 都是不適用", all(l["tag"] == pv.TAG_NA for l in res["lines"]))
    check("★ 一次都沒有重抓（沒有 source_url 可抓）", calls == [], str(calls))
    check("不適用 ∈ SAFE_TAGS、OK ∈ SAFE_TAGS", pv.TAG_NA in pv.SAFE_TAGS and pv.TAG_OK in pv.SAFE_TAGS)
    check("失敗／需確認 ∉ SAFE_TAGS", not ({pv.TAG_FAIL, pv.TAG_BLOCK} & pv.SAFE_TAGS))


def test_na_mixed_only_daigo_lines_count():
    print("\n【9】混單：整單只看代購 line，不適用不參與判定、也不會和別的標籤共存")
    mixed = {"id": 1, "line_items": [_line(NORMAL, "1500.0", "GOYOUTATI - 安心GO"),
                                     _line(DAIGO, "100", "日本代購｜AI Camera"),
                                     _line(GONE, "13855", "差額")]}
    res = run(pv.verify_order(mixed, _meta_by_pid, _scrape_const))      # 現成本 70 < 售 100 → OK
    check("安心GO + 代購(OK) + 差額 → apply = {OK}", res["apply"] == {pv.TAG_OK}, str(res["apply"]))
    check("ok=True", res["ok"] is True)
    tags = [l["tag"] for l in res["lines"]]
    check("逐 line 明細：不適用／OK／不適用", tags == [pv.TAG_NA, pv.TAG_OK, pv.TAG_NA], str(tags))
    async def scrape_high(url):
        return 150                                                       # 現成本 > 售價 → 需確認
    res2 = run(pv.verify_order(mixed, _meta_by_pid, scrape_high))
    check("★ 代購那條需確認 → 整單 {需確認}，不含 OK、不含不適用",
          res2["apply"] == {pv.TAG_BLOCK}, str(res2["apply"]))
    async def scrape_boom(url):
        raise RuntimeError("boom")
    res3 = run(pv.verify_order(mixed, _meta_by_pid, scrape_boom))
    check("★ 代購那條爬失敗 → 整單 {失敗}（不適用救不了它）", res3["apply"] == {pv.TAG_FAIL}, str(res3["apply"]))


def test_na_null_product_id():
    print("\n【10】product_id=null：自訂項目 → 不適用；已刪代購品（標題 日本代購｜）→ 失敗")
    calls = []
    async def meta_spy(pid):
        calls.append(pid); return _meta()
    custom = {"id": 1, "line_items": [_line(None, "13855", "差額"), _line(None, "500", "G0074 代付 9169")]}
    res = run(pv.verify_order(custom, meta_spy, _scrape_const))
    check("差額／代付 → {不適用}", res["apply"] == {pv.TAG_NA}, str(res["apply"]))
    check("product_id=null 不會去查 metafield", calls == [], str(calls))
    gone = {"id": 1, "line_items": [_line(None, "3375", "日本代購｜塔卡拉托米 玩具 - BEYBLADE X UX-20"),
                                    _line(None, "5625", "日本代購｜BEYBLADE 玩具 - X UX-21 玩具｜樂天")]}
    res = run(pv.verify_order(gone, meta_spy, _scrape_const))
    check("★ 已刪代購品 → {失敗}，不是不適用", res["apply"] == {pv.TAG_FAIL}, str(res["apply"]))
    check("理由寫的是已刪除", "已刪除" in res["lines"][0]["detail"]["reason"], str(res["lines"][0]["detail"]))
    both = {"id": 1, "line_items": [_line(None, "13855", "差額"),
                                    _line(None, "3375", "日本代購｜BEYBLADE X UX-20")]}
    res = run(pv.verify_order(both, meta_spy, _scrape_const))
    check("差額 + 已刪代購品 → {失敗}（不適用那條不參與）", res["apply"] == {pv.TAG_FAIL}, str(res["apply"]))


def _mutate(path, old, new, fn):
    """把 price_verify 某段改壞、重新 import、跑 fn()、一定還原。"""
    import importlib
    backup = open(path, encoding="utf-8").read()
    assert backup.count(old) == 1, f"mutation 目標不唯一/不存在: {old!r}"
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(backup.replace(old, new))
        importlib.reload(pv)
        return fn()
    finally:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(backup)
        importlib.reload(pv)


def test_negative():
    print("\n【11】★ 負向驗證：把修正拿掉，對應的測試要紅")
    path = pv.__file__
    # (a) 拿掉標題前綴判斷 → 已刪代購品變不適用 —— 那是漏掉真問題
    def a():
        gone = {"id": 1, "line_items": [_line(None, "3375", "日本代購｜BEYBLADE X UX-20")]}
        res = run(pv.verify_order(gone, _meta_by_pid, _scrape_const))
        return res["apply"] == {pv.TAG_NA}
    check("(a) 拿掉「日本代購｜」前綴判斷 → 已刪代購品變 {不適用}（漏掉真問題）",
          _mutate(path, "if _is_daigo_title(title):", "if False:", a))
    # (b) 拿掉整單過濾 → 混單因不適用而非 OK —— 那是誤放行的反面：不適用混進判定
    def b():
        mixed = {"id": 1, "line_items": [_line(NORMAL, "1500.0", "安心GO"), _line(DAIGO, "100", "日本代購｜x")]}
        res = run(pv.verify_order(mixed, _meta_by_pid, _scrape_const))
        return res["apply"] != {pv.TAG_OK} and pv.TAG_NA in res["apply"]
    check("(b) 拿掉整單過濾 → 混單 apply 混進不適用、不再是 {OK}",
          _mutate(path, 'verified = {r["tag"] for r in lines if r["tag"] != TAG_NA}',
                  'verified = {r["tag"] for r in lines}', b))
    # 還原後
    mixed = {"id": 1, "line_items": [_line(NORMAL, "1500.0", "安心GO"), _line(DAIGO, "100", "日本代購｜x")]}
    res = run(pv.verify_order(mixed, _meta_by_pid, _scrape_const))
    check("還原後混單 = {OK}", res["apply"] == {pv.TAG_OK}, str(res["apply"]))


# ── backstop 涵蓋範圍：只列「還能改變決定」的訂單 ──────────────────────
# 2026-09-15 線上 backstop days=60 的真實分佈（394 筆，全部沒有安全標籤）：
REAL_394 = {"PAID": 247, "VOIDED": 130, "PARTIALLY_REFUNDED": 7, "PARTIALLY_PAID": 5,
            "EXPIRED": 2, "AUTHORIZED": 2, "REFUNDED": 1}
assert sum(REAL_394.values()) == 394


def _count(all_statuses=False):
    return sum(n for st, n in REAL_394.items() if pv.needs_review([], st, all_statuses=all_statuses))


def test_backstop_scope():
    print("\n【12】backstop 只列商品費用還沒 capture 的訂單（AUTHORIZED / PENDING）")
    for st in ("AUTHORIZED", "PENDING"):
        check(f"{st} 且無安全標籤 → 列", pv.needs_review([], st) is True)
        check(f"{st} 小寫也認", pv.needs_review([], st.lower()) is True)
    for st in ("PAID", "PARTIALLY_PAID", "EXPIRED", "VOIDED", "REFUNDED", "PARTIALLY_REFUNDED"):
        check(f"★ {st} → 不列（錢已收／收不到，看價來不及）", pv.needs_review([], st) is False)
    check("None／空狀態 → 列（fail-closed：狀態看不到就當成還能請款，交給人）",
          pv.needs_review([], None) is True and pv.needs_review([], "") is True)
    check("AUTHORIZED 但已有 OK → 不列", pv.needs_review([pv.TAG_OK], "AUTHORIZED") is False)
    check("AUTHORIZED 但已有 不適用 → 不列", pv.needs_review([pv.TAG_NA], "AUTHORIZED") is False)
    check("AUTHORIZED 帶 失敗 → 列", pv.needs_review([pv.TAG_FAIL], "AUTHORIZED") is True)
    check("AUTHORIZED 帶 待驗 → 列", pv.needs_review([pv.TAG_PENDING], "AUTHORIZED") is True)
    check("標籤有空白也認得 OK", pv.needs_review([" 價格驗證:OK "], "AUTHORIZED") is False)
    print("  —— 真實分佈（2026-09-15 線上 394 筆）——")
    check("★ 預設 → 2 筆（只剩 AUTHORIZED）", _count() == 2, str(_count()))
    check("★ all_statuses=True → 394 筆（主動要求才看到）", _count(all_statuses=True) == 394, str(_count(True)))
    check("all_statuses=True 仍然尊重安全標籤", pv.needs_review([pv.TAG_OK], "PAID", all_statuses=True) is False)


def test_backstop_scope_negative():
    print("\n【13】★ 負向：拿掉狀態過濾 → 394 筆那個狀況要重現")
    def a():
        return _count() == 394
    check("(c) 拿掉 financial_status 過濾 → 預設也變 394 筆",
          _mutate(pv.__file__, "return st in ACTIONABLE_STATUSES", "return True", a))
    check("還原後預設仍是 2 筆", _count() == 2, str(_count()))


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
    test_na_all_normal()
    test_na_mixed_only_daigo_lines_count()
    test_na_null_product_id()
    test_negative()
    test_backstop_scope()
    test_backstop_scope_negative()
    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main_())
