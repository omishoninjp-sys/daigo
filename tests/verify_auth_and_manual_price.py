"""
金鑰分離與手動建單售價的驗證
============================
怎麼跑（在專案根目錄）——PowerShell 5.1：
    $env:PYTHONPATH = "."; python -X utf8 tests\verify_auth_and_manual_price.py

（bash／CI：`PYTHONPATH=. python -X utf8 tests/verify_auth_and_manual_price.py`）

兩件事，起因都是同一個發現：**公開金鑰印在 storefront 頁面的
`window.DAIKO_CONFIG` 裡**，任何人檢視原始碼就拿得到。

  1. admin 端點（cleanup／cleanup preview／scrape-log）改用獨立的 X-Admin-Key，
     **不接受公開金鑰** —— cleanup 會永久刪商品、scrape-log 會吐爬取紀錄
  2. `/api/create-manual` 的售價改由伺服器套用 pricing，客人填的是日本原價。
     以前客人填多少就是最終售價，繞過前端直接打 API 填 ¥1 就能用 ¥1 買走。
     前端 daigo.js 確實有自己算加價，但**前端算的東西一律不可信**

不連外：Shopify 與 SEO 都換成假的。
"""
import os
import sys
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 測試用的 admin 金鑰要在 import config 之前設（本機 .env 通常沒有這個變數）
os.environ.setdefault("ADMIN_SECRET_KEY", "test-admin-key-for-verify")

from fastapi.testclient import TestClient

import main as m
from config import API_SECRET_KEY, ADMIN_SECRET_KEY
from pricing import calculate_selling_price

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return cond


client = TestClient(m.app)          # 不用 with：不跑 lifespan，不啟動清理任務

ADMIN_PATHS = ["/api/admin/scrape-log", "/api/admin/scrape-log/summary",
               "/api/admin/cleanup/preview"]

CAPTURED = {}


async def fake_create(**kw):
    CAPTURED.clear()
    CAPTURED.update(kw)
    return {"product_id": 1, "storefront_url": "https://x/y", "admin_url": "https://a/b"}


async def fake_seo(original_title="", source_url="", **kw):
    return {"title": original_title or "標題", "tags": []}


async def call_manual(**fields):
    req = m.ManualOrderRequest(**fields)
    orig = (m.shopify.create_daigo_product, m.generate_seo_title)
    m.shopify.create_daigo_product = fake_create
    m.generate_seo_title = fake_seo
    try:
        return await m.create_manual_order(req)
    finally:
        m.shopify.create_daigo_product, m.generate_seo_title = orig


# ─────────────────────────────────────────────────────────────────────
def test_admin_key_separation():
    print("\n【1】★ admin 端點不接受公開金鑰")
    for path in ADMIN_PATHS:
        r = client.get(path, headers={"X-API-Key": API_SECRET_KEY})
        check(f"公開金鑰打 {path} → 403", r.status_code == 403, str(r.status_code))
        r = client.get(path, headers={"X-Admin-Key": "wrong-admin-key"})
        check(f"錯的 admin 金鑰 → 403", r.status_code == 403, str(r.status_code))
        r = client.get(path)
        check(f"完全沒帶金鑰 → 403", r.status_code == 403, str(r.status_code))

    # 對的 admin 金鑰要能過驗證（preview 會真的打 Shopify，所以只驗 scrape-log 那兩支）
    for path in ADMIN_PATHS[:2]:
        r = client.get(path + "?days=1", headers={"X-Admin-Key": ADMIN_SECRET_KEY})
        check(f"正確的 admin 金鑰打 {path} → 200", r.status_code == 200, str(r.status_code))


def test_public_endpoints_still_work():
    print("\n【2】公開端點仍用公開金鑰（不可以被順手改壞）")
    r = client.get("/api/search-stats", headers={"X-API-Key": API_SECRET_KEY})
    check("公開金鑰打 /api/search-stats → 200", r.status_code == 200, str(r.status_code))
    r = client.get("/api/search-stats", headers={"X-Admin-Key": ADMIN_SECRET_KEY})
    check("只帶 admin 金鑰打公開端點 → 403（兩把互不通用）",
          r.status_code == 403, str(r.status_code))
    r = client.get("/api/health")
    check("/api/health 不需要金鑰", r.status_code == 200, str(r.status_code))


def test_missing_key_is_fail_closed():
    print("\n【3】★ 金鑰沒設定時要一律拒絕，不可以變成完全開放")
    # 以前 API_SECRET_KEY 預設空字串、Header 預設也是空字串 →
    # 變數沒設時「連 header 都不用帶」就通過，等於整個 API 對外開放。
    orig_pub, orig_adm = m.API_SECRET_KEY, m.ADMIN_SECRET_KEY
    m.API_SECRET_KEY = ""
    m.ADMIN_SECRET_KEY = ""
    try:
        r = client.get("/api/search-stats")
        check("公開端點：沒設金鑰又沒帶 header → 不可放行",
              r.status_code != 200, str(r.status_code))
        r = client.get("/api/admin/scrape-log?days=1")
        check("admin 端點：沒設金鑰又沒帶 header → 不可放行",
              r.status_code != 200, str(r.status_code))
    finally:
        m.API_SECRET_KEY, m.ADMIN_SECRET_KEY = orig_pub, orig_adm


def test_transitional_old_key():
    print(chr(10) + "【3.5】⏳ 過渡雙金鑰：輪替期間接受舊公開金鑰，但只限公開端點")
    OLD = "old-public-key-being-rotated-out"
    orig = m.API_SECRET_KEY_OLD
    m.API_SECRET_KEY_OLD = OLD
    try:
        r = client.get("/api/search-stats", headers={"X-API-Key": OLD})
        check("輪替期間舊公開金鑰仍可用（避免頁面改好前的空窗）",
              r.status_code == 200, str(r.status_code))
        r = client.get("/api/admin/scrape-log?days=1", headers={"X-API-Key": OLD})
        check("★ 舊金鑰打 admin 端點仍是 403（admin 永遠不吃舊值）",
              r.status_code == 403, str(r.status_code))
        r = client.get("/api/admin/scrape-log?days=1", headers={"X-Admin-Key": OLD})
        check("★ 舊金鑰也不能當 admin 金鑰用", r.status_code == 403, str(r.status_code))
        check("用到舊金鑰時 log 會計數（判斷何時可以移除的依據）",
              m._old_key_uses["n"] >= 1, str(m._old_key_uses["n"]))
    finally:
        m.API_SECRET_KEY_OLD = orig

    # 沒設定過渡變數時，舊金鑰就該被擋掉 —— 這是移除之後應有的行為
    r = client.get("/api/search-stats", headers={"X-API-Key": OLD})
    check("★ 移除 API_SECRET_KEY_OLD 之後，舊金鑰立刻失效",
          r.status_code == 403, str(r.status_code))


async def test_manual_price_is_server_side():
    print("\n【4】★ 手動建單的售價由伺服器算，客人填的是日本原價")
    resp = await call_manual(title="測試", price_jpy=1)
    expect = calculate_selling_price(1)["selling_price_jpy"]
    check("端點成功", resp.success is True, str(resp.error)[:50])
    check(f"¥1 → 售價 ¥{expect}（最低服務費生效，不是 ¥1）",
          CAPTURED.get("price_jpy") == expect,
          f'實際 {CAPTURED.get("price_jpy")}')
    check("原價欄位記的是客人填的 ¥1",
          CAPTURED.get("original_price_jpy") == 1,
          str(CAPTURED.get("original_price_jpy")))
    check("仍標記為手動來源", CAPTURED.get("created_via") == "manual",
          repr(CAPTURED.get("created_via")))

    resp = await call_manual(title="測試", price_jpy=6000)
    expect = calculate_selling_price(6000)["selling_price_jpy"]
    check(f"¥6,000 → 售價 ¥{expect}（跟爬取那條同一套 pricing）",
          CAPTURED.get("price_jpy") == expect, f'實際 {CAPTURED.get("price_jpy")}')

    # 前端傳來的「售價」不可以被採用 —— 這是繞過前端攻擊的核心
    resp = await call_manual(title="測試", price_jpy=1, original_price_jpy=50000)
    expect = calculate_selling_price(50000)["selling_price_jpy"]
    check("同時給 price_jpy=1 與 original_price_jpy=50000 → 以原價 50000 計算",
          CAPTURED.get("price_jpy") == expect, f'實際 {CAPTURED.get("price_jpy")}')
    check("★ 客人送來的數字不會直接變成售價",
          CAPTURED.get("price_jpy") != 1, str(CAPTURED.get("price_jpy")))

    resp = await call_manual(title="測試", price_jpy=0)
    check("價格 0 仍然拒收", resp.success is False, str(resp.error)[:40])
    resp = await call_manual(title="測試", price_jpy=-5)
    check("負數仍然拒收", resp.success is False, str(resp.error)[:40])


# ─────────────────────────────────────────────────────────────────────
async def main_():
    print("=" * 74)
    print("金鑰分離 + 手動建單售價驗證（不連外）")
    print("=" * 74)
    test_admin_key_separation()
    test_public_endpoints_still_work()
    test_missing_key_is_fail_closed()
    test_transitional_old_key()
    await test_manual_price_is_server_side()

    print("\n" + "=" * 74)
    print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_()))
