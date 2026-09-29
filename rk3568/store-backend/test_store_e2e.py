#!/usr/bin/env python3
"""End-to-end acceptance test for the RK3568 store backend.

Runs against any base URL, so the same script validates the local working copy and
the deployed board service:

    python3 test_store_e2e.py --base http://127.0.0.1:8094

Every check asserts an observable business invariant (stock movement, money totals,
order lifecycle) rather than just "HTTP 200", because the whole point of moving off
SPIFFS is that inventory and revenue must stay consistent.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

PASSED = []
FAILED = []

# The service is always reached over loopback or the local hotspot.  An HTTP proxy
# inherited from the shell (http_proxy/https_proxy) would break that, so bypass it
# explicitly instead of depending on the host's no_proxy rules.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(base, path, payload=None, method=None, timeout=10):
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with OPENER.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body}


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s %s" % (name, detail))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--session", default="e2e-test")
    args = parser.parse_args()
    base, session = args.base, args.session

    print("== store backend acceptance test against %s ==" % base)

    # --- 1. service reachable and catalog seeded -------------------------
    status, data = call(base, "/api/store/status")
    check("status endpoint responds", status == 200 and data.get("ok") is True, str(data)[:160])
    check("catalog is seeded", data.get("products", 0) >= 8,
          "products=%s" % data.get("products"))

    status, products = call(base, "/api/products")
    codes = {p["code"]: p for p in products}
    check("seed barcodes present", "6901234567890" in codes and "6901234567894" in codes,
          "got %d rows" % len(products))

    coke = codes["6901234567890"]
    stock_before = coke["stock"]
    sold_before = coke["today_sold"]

    # --- 2. barcode resolution (handoff 10.6) ---------------------------
    status, data = call(base, "/api/product?code=6901234567890")
    check("barcode resolves to SKU", data.get("ok") and data["product"]["name"].startswith("可口可乐"),
          str(data)[:160])

    status, data = call(base, "/api/scan", {"code": "6901234567890", "source": "e2e"})
    check("known barcode records as matched", data.get("ok") is True, str(data)[:160])

    status, data = call(base, "/api/scan", {"code": "0000000000000", "source": "e2e"})
    check("unknown barcode is reported unmatched", data.get("ok") is False, str(data)[:160])

    status, events = call(base, "/api/scan/events?consume=0&limit=5")
    check("scan events are queryable", isinstance(events, list) and len(events) >= 1,
          "events=%s" % events)

    # --- 3. cart ----------------------------------------------------------
    call(base, "/api/cart/clear", {"session": session})
    status, data = call(base, "/api/cart/add", {"code": "6901234567890", "qty": 2, "session": session})
    check("add to cart succeeds", data.get("ok") is True, str(data)[:160])

    status, cart = call(base, "/api/cart?session=" + session)
    check("cart total is 2 x 3.50 = 7.00", abs(cart["total"] - 7.00) < 1e-6, str(cart)[:200])
    check("cart line quantity is 2", cart["items"] and abs(cart["items"][0]["qty"] - 2) < 1e-6,
          str(cart.get("items"))[:200])

    # over-ordering must be rejected, not silently accepted
    status, data = call(base, "/api/cart/add", {"code": "6901234567890", "qty": 9999, "session": session})
    check("over-stock add is rejected", data.get("ok") is False, str(data)[:160])

    # --- 4. checkout ------------------------------------------------------
    status, data = call(base, "/api/checkout", {"session": session, "method": "cash"})
    check("checkout creates an order", data.get("ok") is True and data.get("order_id"),
          str(data)[:160])
    order_id = data.get("order_id")

    status, cart = call(base, "/api/cart?session=" + session)
    check("cart is emptied after checkout", cart["items"] == [], str(cart)[:160])

    status, data = call(base, "/api/product?code=6901234567890")
    after = data["product"]
    check("stock deducted by 2", abs(after["stock"] - (stock_before - 2)) < 1e-6,
          "before=%s after=%s" % (stock_before, after["stock"]))
    check("today_sold increased by 2", abs(after["today_sold"] - (sold_before + 2)) < 1e-6,
          "before=%s after=%s" % (sold_before, after["today_sold"]))

    status, data = call(base, "/api/order?order_id=%s" % order_id)
    check("order is PENDING before payment", data["order"]["status"] == "PENDING", str(data)[:160])
    check("order total is 7.00", abs(data["order"]["total"] - 7.00) < 1e-6, str(data)[:160])
    check("order line snapshot stored", len(data["order"]["items"]) == 1, str(data)[:200])

    # --- 5. payment -------------------------------------------------------
    status, summary_before = call(base, "/api/admin/summary")
    status, data = call(base, "/api/order/confirm", {"order_id": order_id})
    check("payment succeeds", data.get("ok") is True, str(data)[:160])

    status, data = call(base, "/api/order/confirm", {"order_id": order_id})
    check("double payment is rejected", data.get("ok") is False, str(data)[:160])

    status, summary_after = call(base, "/api/admin/summary")
    check("revenue increased by 7.00",
          abs((summary_after["revenue_total"] - summary_before["revenue_total"]) - 7.00) < 1e-6,
          "%s -> %s" % (summary_before["revenue_total"], summary_after["revenue_total"]))

    status, trend = call(base, "/api/admin/trend")
    check("trend returns 7 days", len(trend) == 7, "len=%d" % len(trend))
    check("today appears in trend with 7.00", abs(trend[-1]["revenue"] - 7.00) < 1e-6,
          str(trend[-1]))

    # --- 6. receipt -------------------------------------------------------
    status, data = call(base, "/api/print/preview?order_id=%s" % order_id)
    text = data.get("receipt", "")
    check("receipt contains the product", "可口可乐" in text, text[:200])
    check("receipt contains the total", "7.00" in text, text[:200])

    status, data = call(base, "/api/print/receipt", {"order_id": order_id})
    check("receipt file generated", data.get("ok") is True and data.get("path"), str(data)[:200])

    # --- 7. refund restores stock and revenue -----------------------------
    status, data = call(base, "/api/order/refund", {"order_id": order_id})
    check("refund succeeds", data.get("ok") is True, str(data)[:160])

    status, data = call(base, "/api/order/refund", {"order_id": order_id})
    check("double refund is rejected", data.get("ok") is False, str(data)[:160])

    status, data = call(base, "/api/product?code=6901234567890")
    restored = data["product"]
    check("stock restored after refund", abs(restored["stock"] - stock_before) < 1e-6,
          "expected=%s got=%s" % (stock_before, restored["stock"]))
    check("today_sold restored after refund", abs(restored["today_sold"] - sold_before) < 1e-6,
          "expected=%s got=%s" % (sold_before, restored["today_sold"]))

    status, summary_final = call(base, "/api/admin/summary")
    check("revenue restored after refund",
          abs(summary_final["revenue_total"] - summary_before["revenue_total"]) < 1e-6,
          "%s vs %s" % (summary_final["revenue_total"], summary_before["revenue_total"]))

    # --- 8. admin catalog operations --------------------------------------
    status, data = call(base, "/api/admin/restock", {"code": "6901234567890", "qty": 10})
    check("restock succeeds", data.get("ok") is True, str(data)[:160])

    status, data = call(base, "/api/admin/add-product",
                        {"code": "TEST-E2E-001", "name": "端到端测试商品", "price": 1.23, "stock": 5})
    check("add product succeeds", data.get("ok") is True, str(data)[:160])

    status, data = call(base, "/api/admin/add-product",
                        {"code": "TEST-E2E-001", "name": "重复", "price": 1.0, "stock": 1})
    check("duplicate barcode rejected", data.get("ok") is False, str(data)[:160])

    status, data = call(base, "/api/admin/delete", {"code": "TEST-E2E-001"})
    check("delete product succeeds", data.get("ok") is True, str(data)[:160])

    # --- 9. vision bridge must not fake SKU recognition -------------------
    status, data = call(base, "/api/vision/observe", {"label": "bottle", "confidence": 0.95})
    check("COCO label does not resolve to a SKU", data.get("ok") is False, str(data)[:200])

    status, data = call(base, "/api/vision/candidates")
    check("vision reports sku model not ready", data.get("sku_model_ready") is False, str(data)[:200])

    # --- 10. final consistency --------------------------------------------
    status, data = call(base, "/api/store/status")
    check("service still healthy after full lifecycle", data.get("ok") is True, str(data)[:160])

    print("\n== %d passed, %d failed ==" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
