"""
brake_log 的回歸測試（離線，不需要任何憑證、不碰網路）。

怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_brake_log.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_brake_log.py`）

★ 這支要釘死的是**第一階段成不成立的那一件事**：
  line item property 優先於商品 metafield。
  metafield 會在商品被 auto-cleanup 刪掉的那一刻消失 —— 2026-09-07 的
  回溯分析涵蓋率只有 31.6%、2026-06 那批 BEYBLADE 只剩 6.6%，就是因為
  當時只有 metafield 這一條路。優先序寫反了，這件事就白做。

★ 另外釘死「不擋任何東西」：第一階段的輸出裡不可以有任何門檻判斷。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ★ 一定要在 import brake_log 之前設好，_pick_dir 會快取第一次挑到的目錄。
_TMP = tempfile.mkdtemp(prefix="brake_test_")
os.environ["BRAKE_LOG_DIR"] = _TMP

import brake_log
from brake_log import _rows_from_orders, build_summary, note_created, MAX_SCAN_DAYS

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def li(qty=1, title="", props=None, meta=None, product_id="gid://shopify/Product/1"):
    return {
        "quantity": qty,
        "title": title,
        "customAttributes": [{"key": k, "value": v} for k, v in (props or {}).items()],
        "product": ({"id": product_id,
                     "metafield": ({"value": meta} if meta else None)} if product_id else None),
    }


def order(name, created, items, cancelled="", reason="", fin="PAID"):
    return {
        "name": name, "createdAt": created, "cancelledAt": cancelled or None,
        "cancelReason": reason or None, "displayFinancialStatus": fin,
        "lineItems": {"nodes": items},
    }


PROP = "_daigo_source_url"
AMZ = "https://www.amazon.co.jp/-/zh/gp/aw/d/B0GXCM4BCZ/ref=x"
AMZ2 = "https://www.amazon.co.jp/dp/B0GXCM4BCZ"


def main():
    print("=" * 74)
    print("A. 🔴 來源優先序：line item property 蓋過商品 metafield")
    print("=" * 74)
    rows = _rows_from_orders([
        order("GYT1", "2026-09-01T00:00:00Z",
              [li(props={PROP: AMZ}, meta="https://example.jp/wrong")]),
        order("GYT2", "2026-09-02T00:00:00Z", [li(meta=AMZ2)]),
        order("GYT3", "2026-09-03T00:00:00Z", [li()]),
    ])
    check("兩者都有 → 取 property", rows[0]["raw_url"] == AMZ, rows[0]["raw_url"])
    check("  source_from = property", rows[0]["source_from"] == "property")
    check("只有 metafield → 退回 metafield", rows[1]["raw_url"] == AMZ2)
    check("  source_from = metafield", rows[1]["source_from"] == "metafield")
    check("兩者都沒有 → 空", rows[2]["raw_url"] == "" and rows[2]["source_from"] == "")
    check("★ 兩種來源算出同一個 key（不同網址形態的同一件商品）",
          rows[0]["norm_key"] == rows[1]["norm_key"] == "amazon.co.jp#B0GXCM4BCZ",
          f"{rows[0]['norm_key']} / {rows[1]['norm_key']}")
    check("商品已被刪除（product = null）不擲例外",
          _rows_from_orders([order("GYT4", "2026-09-01T00:00:00Z",
                                   [li(product_id=None)])])[0]["raw_url"] == "")
    check("key_rule_version 有跟著寫進每一列",
          all(r["key_rule_version"] for r in rows))

    print("\n" + "=" * 74)
    print("B. 每一列都要留 raw_url（附錄 H：只記 key 的話規則一改就得從頭）")
    print("=" * 74)
    check("raw_url 保留原始字串（沒有被正規化蓋掉）",
          rows[0]["raw_url"] == AMZ and rows[0]["norm_key"] != AMZ)

    print("\n" + "=" * 74)
    print("C. build_summary：只描述分佈，不做任何門檻判斷")
    print("=" * 74)
    orders = [
        # 同一個 ASIN 的兩種網址形態、三張訂單、全部作廢且沒收到錢
        order("GYT10", "2026-09-01T00:00:00Z", [li(title="寶可夢 MEGA", props={PROP: AMZ})],
              cancelled="2026-09-02T00:00:00Z", reason="OTHER", fin="EXPIRED"),
        order("GYT11", "2026-09-03T00:00:00Z", [li(title="寶可夢 MEGA 30週年", props={PROP: AMZ2})],
              cancelled="2026-09-04T00:00:00Z", reason="OTHER", fin="EXPIRED"),
        order("GYT12", "2026-09-06T00:00:00Z", [li(title="寶可夢 MEGA", props={PROP: AMZ})],
              cancelled="2026-09-07T00:00:00Z", reason="OTHER", fin="VOIDED"),
        # 一件正常商品，只出現一次
        order("GYT13", "2026-09-05T00:00:00Z",
              [li(title="安眠指環", props={PROP: "https://www.amazon.co.jp/dp/B0GK35F7FB"})]),
        # 同一張訂單買兩件同樣的東西 —— 不是兩次下單
        order("GYT14", "2026-09-08T00:00:00Z", [
            li(title="UX-20", props={PROP: "https://www.takaratomymall.jp/shop/g/g8202701096139/"}),
            li(title="UX-20", props={PROP: "https://www.takaratomymall.jp/shop/g/g8202701096139"}),
        ]),
    ]
    s = build_summary(_rows_from_orders(orders), days=30)
    top = s["repeat_keys"][0]
    check("line_items 算對", s["line_items"] == 6, str(s["line_items"]))
    check("涵蓋率 100%（全部有來源）", s["coverage_pct"] == 100.0, str(s["coverage_pct"]))
    check("source_from 統計", s["source_from"].get("property") == 6, str(s["source_from"]))
    check("★ ≥2 次的 key 只有兩個", s["keys_repeated"] == 2,
          f"{s['keys_repeated']}：{[k['key'] for k in s['repeat_keys']]}")
    check("★ 兩種 Amazon 形態合成同一個 key、算成 3 次",
          top["key"] == "amazon.co.jp#B0GXCM4BCZ" and top["count"] == 3,
          f"{top['key']} x{top['count']}")
    check("  作廢 3 次", top["cancelled"] == 3, str(top["cancelled"]))
    check("  收到錢 0 次（EXPIRED／VOIDED 都不算成交）", top["paid"] == 0, str(top["paid"]))
    check("  涉及 3 張訂單", top["distinct_orders"] == 3, str(top["distinct_orders"]))
    check("  時間跨度 5.0 天", top["span_days"] == 5.0, str(top["span_days"]))
    check("  同一個 key 底下兩種標題都留著（假合併要靠這個看出來）",
          len(top["titles"]) == 2, str(top["titles"]))
    check("同一張訂單的兩個 line item：count=2 但 distinct_orders=1",
          any(k["count"] == 2 and k["distinct_orders"] == 1 for k in s["repeat_keys"]),
          str([(k["count"], k["distinct_orders"]) for k in s["repeat_keys"]]))
    check("只出現一次的不進 repeat_keys",
          all("B0GK35F7FB" not in k["key"] for k in s["repeat_keys"]))
    check("直方圖", s["histogram"] == {"1": 1, "2": 1, "3": 1}, str(s["histogram"]))
    check("★ 摘要裡沒有任何門檻／判斷欄位（第一階段不擋東西）",
          not any(k in s for k in ("blocked", "should_block", "threshold", "n_value")),
          str(sorted(s.keys())))
    check("摘要有講明白第一階段的定位", "不擋" in s.get("note", ""), s.get("note", "")[:30])

    print("\n" + "=" * 74)
    print("D. 收款口徑：未取消 ≠ 收到錢")
    print("=" * 74)
    U = "https://item.rakuten.co.jp/shop/abc/"
    for fin, expect in [("PAID", 1), ("PARTIALLY_PAID", 1), ("REFUNDED", 1),
                        ("PARTIALLY_REFUNDED", 1), ("EXPIRED", 0), ("VOIDED", 0),
                        ("PENDING", 0), ("AUTHORIZED", 0)]:
        rs = _rows_from_orders([
            order("A", "2026-09-01T00:00:00Z", [li(props={PROP: U})], fin=fin),
            order("B", "2026-09-02T00:00:00Z", [li(props={PROP: U})], fin=fin),
        ])
        got = build_summary(rs, 30)["repeat_keys"][0]["paid"]
        check(f"{fin} → paid {expect}/2", got == expect * 2, str(got))

    print("\n" + "=" * 74)
    print("E. note_created：寫得進去，而且壞掉不可以影響建單")
    print("=" * 74)
    r = note_created(AMZ, product_id=123, created_via="auto", title="寶可夢 MEGA")
    check("回傳算好的 key", r["norm_key"] == "amazon.co.jp#B0GXCM4BCZ", r["norm_key"])
    check("matched_site_rule = amazon", r["matched_site_rule"] == "amazon")
    files = [f for f in os.listdir(_TMP) if f.startswith("created-")]
    check("有寫出 created-YYYY-MM-DD.jsonl", len(files) == 1, str(files))
    if files:
        import json
        line = json.loads(open(os.path.join(_TMP, files[0]), encoding="utf-8").readline())
        check("  raw_url 有留", line["raw_url"] == AMZ)
        check("  created_via 有留", line["created_via"] == "auto")
        check("  product_id 有留", line["product_id"] == 123)
        check("  brake_suitable 有留", line["brake_suitable"] is True)
    check("沒有 source_url → 跳過，不寫也不炸（手動建單常常這樣）",
          note_created("")["norm_key"] == "")
    check("爛 URL → 跳過", note_created("not a url")["norm_key"] == "")

    # 故意把寫入弄壞，證明例外不會往上冒到建單流程
    _orig = brake_log._append
    brake_log._append = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        r = note_created(AMZ, product_id=1, created_via="auto")
        check("★ 寫入失敗時 note_created 不擲例外（建單不可以因此失敗）", True)
    except Exception as e:
        check("★ 寫入失敗時 note_created 不擲例外（建單不可以因此失敗）", False,
              f"{type(e).__name__}: {e}")
    finally:
        brake_log._append = _orig

    print("\n" + "=" * 74)
    print("F. 60 天上限（token 沒有 read_all_orders，再多也只會靜默回 60 天）")
    print("=" * 74)
    check("MAX_SCAN_DAYS = 60", MAX_SCAN_DAYS == 60, str(MAX_SCAN_DAYS))

    class _FakeShopify:
        def __init__(self):
            self.asked = None

        async def fetch_orders_for_brake(self, days):
            self.asked = days
            return []

    import asyncio
    fake = _FakeShopify()
    asyncio.run(brake_log.scan_orders(fake, days=365, write=False))
    check("★ days=365 被夾成 60，不會靜默拿到截斷的資料還以為是一年",
          fake.asked == 60, str(fake.asked))
    asyncio.run(brake_log.scan_orders(fake, days=30, write=False))
    check("days=30 照傳", fake.asked == 30, str(fake.asked))
    asyncio.run(brake_log.scan_orders(fake, days=0, write=False))
    check("days=0 夾成 1（不會查到未來）", fake.asked == 1, str(fake.asked))

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print(f"（暫存目錄 {_TMP}）")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
