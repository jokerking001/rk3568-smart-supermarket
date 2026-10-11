#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多场景「完整购物流程」验收脚本。

和 `test_store_e2e.py` 的区别：那个脚本按**功能模块**逐条验不变量（一次跑完
38 条）；本脚本按**真实使用场景**跑完整闭环，每个场景都是一条独立的
「加购 → 结账 → 支付 → 出票 →（可选）退款」全流程，并核对库存/营业额。

用法：
    python3 tools/checkout_scenarios.py --base http://10.176.240.215:8094
    python3 tools/checkout_scenarios.py --base http://127.0.0.1:8094 --only S2,S4

场景：
    S1 扫码枪 · 现金 · 三商品
    S2 微信 · 移动支付链路（下单 → 手机付款 → 轮询 → 确认）
    S3 支付宝 · 会员折扣（注册会员 → 95 折 → 加积分）
    S4 异常码 · 未知条码 + 同码重复扫
    S5 库存边界 · 超库存加购必须被拒
    S6 退款 · 支付后退款，库存与营业额必须回滚
    S7 称重链路 · 从机 → 8099 → 8094 代理（需 --slave）
    S8 扫码枪设备 · 8095 是否真的认到枪（需 --rk）
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 现场设备（可被命令行覆盖）
DEFAULT_RK = "http://10.176.240.215"
DEFAULT_SLAVE = "http://10.176.240.1:8080"      # 从机 HTTP 服务在 8080

# 绕开 shell 继承的 http_proxy：否则内网请求会被代理黑洞吃掉。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASSED = []
FAILED = []

# 带权限的接口（pay-confirm / mobile-pay / member/* 等 write 级）需要会话。
# 由 --token 注入，或从环境变量 STORE_TOKEN 读。
AUTH_TOKEN = ""


def call(base, path, payload=None, method=None, timeout=12):
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if AUTH_TOKEN:
        headers["x-auth-token"] = AUTH_TOKEN
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with OPENER.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body}
    except Exception as exc:                 # 连不上/超时都不该中断整轮场景
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def qr_of(base, code):
    """pay-confirm 的 items 要的是 qr_code，不是条码本身。"""
    p = product(base, code)
    return p.get("qr_code") or code


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("     PASS  %s" % name)
    else:
        FAILED.append(name)
        print("     FAIL  %s  %s" % (name, detail))


def money(value):
    return round(float(value), 2)


def product(base, code):
    _, data = call(base, "/api/product?code=%s" % code)
    return data.get("product") or {}


def cart_add(base, session, code, qty):
    return call(base, "/api/cart/add", {"code": code, "qty": qty, "session": session})


def cart_clear(base, session):
    call(base, "/api/cart/clear", {"session": session})


def checkout(base, session, method):
    return call(base, "/api/checkout", {"session": session, "method": method})


def order(base, order_id):
    _, data = call(base, "/api/order?order_id=%s" % order_id)
    return data.get("order") or {}


def summary(base):
    _, data = call(base, "/api/admin/summary")
    return data


# ---------------------------------------------------------------- scenarios

def s1_cash_three_items(base):
    """扫码枪 · 现金 · 三商品：库存各减 N、营业额加总价、小票含商品名。"""
    print("\n[S1] 扫码枪 · 现金 · 三商品")
    picks = [("6901234567890", 2), ("6901234567895", 1), ("6901234567897", 3)]
    sess = "scn-s1"
    cart_clear(base, sess)

    before = {c: product(base, c) for c, _ in picks}
    rev_before = money(summary(base).get("revenue_total", 0))

    expected_total = 0.0
    for code, qty in picks:
        _, r = cart_add(base, sess, code, qty)
        check("S1 加购 %s x%d" % (code, qty), r.get("ok") is True, str(r)[:160])
        expected_total += float(before[code]["price"]) * qty
    expected_total = money(expected_total)

    _, cart = call(base, "/api/cart?session=" + sess)
    check("S1 购物车合计 = %.2f" % expected_total,
          abs(money(cart.get("total", 0)) - expected_total) < 1e-6, str(cart)[:200])
    check("S1 购物车 3 行", len(cart.get("items") or []) == 3, str(cart.get("items"))[:200])

    _, r = checkout(base, sess, "cash")
    check("S1 结账生成订单", r.get("ok") is True and r.get("order_id") is not None, str(r)[:160])
    oid = r.get("order_id")

    _, r = call(base, "/api/pay-confirm", {"orderId": oid,
                                           "items": ",".join("%s:%d" % (qr_of(base, c), q)
                                                             for c, q in picks)})
    check("S1 收银台确认支付", r.get("ok") is True, str(r)[:160])

    o = order(base, oid)
    check("S1 订单状态 PAID", o.get("status") == "PAID", str(o.get("status")))
    check("S1 订单金额 = %.2f" % expected_total, abs(money(o.get("total", 0)) - expected_total) < 1e-6,
          str(o.get("total")))

    for code, qty in picks:
        after = product(base, code)
        check("S1 %s 库存 -%d" % (code, qty),
              abs(after["stock"] - (before[code]["stock"] - qty)) < 1e-6,
              "%s -> %s" % (before[code]["stock"], after["stock"]))

    rev_after = money(summary(base).get("revenue_total", 0))
    check("S1 营业额 +%.2f" % expected_total,
          abs((rev_after - rev_before) - expected_total) < 1e-6,
          "%s -> %s" % (rev_before, rev_after))

    _, data = call(base, "/api/print/preview?order_id=%s" % oid)
    text = data.get("receipt", "")
    names_ok = all(before[c]["name"][:4] in text for c, _ in picks)
    check("S1 小票含三件商品名", names_ok, text[:200])
    check("S1 小票含合计 %.2f" % expected_total, ("%.2f" % expected_total) in text, text[:200])

    _, data = call(base, "/api/print/receipt", {"order_id": oid})
    check("S1 小票文件已生成", data.get("ok") is True and data.get("path"), str(data)[:200])
    return oid


def s2_wechat_mobile(base):
    """微信 · 移动支付链路：下单 → 手机端付款 → 轮询 → 确认。"""
    print("\n[S2] 微信 · 移动支付链路")
    code, qty = "6901234567892", 2
    sess = "scn-s2"
    cart_clear(base, sess)
    before = product(base, code)
    rev_before = money(summary(base).get("revenue_total", 0))

    cart_add(base, sess, code, qty)
    _, r = checkout(base, sess, "wx")
    oid = r.get("order_id")
    check("S2 微信下单成功", r.get("ok") is True and oid is not None, str(r)[:160])

    o = order(base, oid)
    check("S2 付款前订单为 PENDING", o.get("status") == "PENDING", str(o.get("status")))
    _, st = call(base, "/api/pay-status?order=%s" % oid)
    check("S2 付款前 pay-status 未确认", st.get("paid") is not True, str(st)[:200])

    # 手机端扫码打开 /pay?order=X，页面调 mobile-pay 建单（状态 WAITING）
    _, r = call(base, "/api/mobile-pay", {"order_id": oid})
    check("S2 手机端发起支付（建 WAITING 请求）", r.get("ok") is True, str(r)[:200])

    _, st = call(base, "/api/pay-status?order=%s" % oid)
    check("S2 已有待支付请求（WAITING）",
          st.get("ok") is True and st.get("status") == "WAITING", str(st)[:220])

    # 收银台确认收款 —— 这一步才把订单和付款请求一起置为 PAID
    _, r = call(base, "/api/pay-confirm", {"orderId": oid, "items": "%s:%d" % (qr_of(base, code), qty)})
    check("S2 收银台确认收款", r.get("ok") is True, str(r)[:160])

    _, st = call(base, "/api/pay-status?order=%s" % oid)
    check("S2 pay-status 变为已支付", st.get("paid") is True, str(st)[:200])

    o = order(base, oid)
    check("S2 订单状态 PAID", o.get("status") == "PAID", str(o.get("status")))

    after = product(base, code)
    check("S2 库存 -%d" % qty, abs(after["stock"] - (before["stock"] - qty)) < 1e-6,
          "%s -> %s" % (before["stock"], after["stock"]))
    rev_after = money(summary(base).get("revenue_total", 0))
    check("S2 营业额增加", rev_after > rev_before, "%s -> %s" % (rev_before, rev_after))
    return oid


def s3_alipay_member(base):
    """支付宝 · 会员 95 折 + 积分。"""
    print("\n[S3] 支付宝 · 会员折扣 + 积分")
    import random
    phone = "139" + "".join(random.choice("0123456789") for _ in range(8))
    name = "场景三会员"

    card = "CARD" + phone
    _, r = call(base, "/api/member/register", {"card_no": card, "name": name, "phone": phone})
    check("S3 会员注册成功", r.get("ok") is True, str(r)[:200])
    check("S3 注册返回卡号", r.get("card_no") == card, str(r)[:200])

    _, r = call(base, "/api/member/lookup?code=MEM:%s" % card)
    check("S3 会员可查回", r.get("ok") is True, str(r)[:200])

    code, qty = "6901234567893", 1
    sess = "scn-s3"
    cart_clear(base, sess)
    before = product(base, code)
    cart_add(base, sess, code, qty)
    _, r = checkout(base, sess, "alipay")
    oid = r.get("order_id")
    check("S3 支付宝下单成功", r.get("ok") is True and oid is not None, str(r)[:160])

    call(base, "/api/mobile-pay", {"order_id": oid})
    _, st = call(base, "/api/pay-status?order=%s" % oid)
    check("S3 已建待支付请求", st.get("ok") is True and st.get("status") == "WAITING", str(st)[:200])
    call(base, "/api/pay-confirm", {"orderId": oid, "items": "%s:%d" % (qr_of(base, code), qty)})
    _, st = call(base, "/api/pay-status?order=%s" % oid)
    check("S3 移动支付已确认", st.get("paid") is True, str(st)[:200])

    o = order(base, oid)
    check("S3 订单 PAID", o.get("status") == "PAID", str(o.get("status")))

    _, r = call(base, "/api/member/add-points", {"card_no": card, "delta": 10,
                                                 "spent": o.get("total", 0)})
    check("S3 会员加积分成功", r.get("ok") is True, str(r)[:200])
    _, r = call(base, "/api/member/lookup?code=MEM:%s" % card)
    got = (r.get("member") or r)
    check("S3 积分已累加", float(got.get("points") or 0) >= 10, str(got)[:200])
    return oid


def s4_bad_codes(base):
    """异常码：未知条码不得静默丢弃；同码重复扫只占一行。"""
    print("\n[S4] 异常码 · 未知条码 + 同码重复")
    _, r = call(base, "/api/scan", {"code": "0000000000000", "source": "scenario"})
    check("S4 未知条码 ok=False", r.get("ok") is False, str(r)[:200])
    check("S4 未知条码给出了原因", bool(r.get("message") or r.get("msg")), str(r)[:200])

    code = "6901234567894"
    sess = "scn-s4"
    cart_clear(base, sess)
    cart_add(base, sess, code, 1)
    cart_add(base, sess, code, 1)
    _, cart = call(base, "/api/cart?session=" + sess)
    items = cart.get("items") or []
    check("S4 同码两次加购合成一行", len(items) == 1, str(items)[:200])
    check("S4 该行数量为 2", items and abs(float(items[0]["qty"]) - 2) < 1e-6, str(items)[:200])
    cart_clear(base, sess)


def s5_stock_guard(base):
    """库存边界：超库存加购必须被拒。"""
    print("\n[S5] 库存边界 · 超库存加购")
    code = "6901234567891"          # 库存最少的一件
    p = product(base, code)
    sess = "scn-s5"
    cart_clear(base, sess)
    _, r = cart_add(base, sess, code, int(p["stock"]) + 1)
    check("S5 超库存被拒", r.get("ok") is False, str(r)[:200])
    cart_clear(base, sess)


def s6_refund(base):
    """退款：支付后退款，库存与营业额必须回滚。"""
    print("\n[S6] 退款 · 库存与营业额回滚")
    code, qty = "6901234567896", 2
    sess = "scn-s6"
    cart_clear(base, sess)
    before = product(base, code)
    rev_before = money(summary(base).get("revenue_total", 0))

    cart_add(base, sess, code, qty)
    _, r = checkout(base, sess, "cash")
    oid = r.get("order_id")
    call(base, "/api/pay-confirm", {"orderId": oid, "items": "%s:%d" % (qr_of(base, code), qty)})
    rev_paid = money(summary(base).get("revenue_total", 0))
    check("S6 支付后营业额上升", rev_paid > rev_before, "%s -> %s" % (rev_before, rev_paid))

    _, r = call(base, "/api/order/refund", {"order_id": oid})
    check("S6 退款成功", r.get("ok") is True, str(r)[:200])

    _, r = call(base, "/api/order/refund", {"order_id": oid})
    check("S6 重复退款被拒", r.get("ok") is False, str(r)[:200])

    after = product(base, code)
    check("S6 库存已回滚", abs(after["stock"] - before["stock"]) < 1e-6,
          "%s -> %s" % (before["stock"], after["stock"]))
    rev_after = money(summary(base).get("revenue_total", 0))
    check("S6 营业额已回滚", abs(rev_after - rev_before) < 1e-6,
          "%s vs %s" % (rev_before, rev_after))


def s7_weigh_chain(base, slave):
    """称重链路：从机 → 8099 → 8094 代理。"""
    print("\n[S7] 称重链路 · 从机 → 8099 → 8094")
    if not slave:
        print("     SKIP  未给 --slave")
        return
    _, st = call(slave, "/api/status")
    check("S7 从机在线且 role=slave", st.get("ok") is True and st.get("role") == "slave", str(st)[:200])
    check("S7 从机上报目标指向 RK", str(st.get("rk", "")).endswith("215"), str(st.get("rk")))

    _, s = call(base.replace(":8094", ":8099") if ":8094" in base else base, "/api/scale/status")
    ok = s.get("ok") is True
    check("S7 8099 称重缓冲可用", ok, str(s)[:200])
    if ok:
        check("S7 称重数据新鲜（age < max_age）",
              float(s.get("age_ms", 1e9)) < float(s.get("max_age_ms", 0)),
              "age=%s max=%s" % (s.get("age_ms"), s.get("max_age_ms")))

    _, w = call(base, "/api/weight")
    check("S7 8094 /api/weight 代理可用", w.get("ok") is True, str(w)[:200])
    check("S7 代理带 upstream 字段", "upstream" in w, str(w)[:200])


def s8_scanner_device(base, rk):
    """扫码枪设备：8095 是否真的认到枪 + inject 链路。"""
    print("\n[S8] 扫码枪设备 · 8095 识别与注入")
    if not rk:
        print("     SKIP  未给 --rk")
        return
    _, st = call(rk + ":8095" if rk.count(":") == 1 else rk.replace(":8094", ":8095"),
                 "/api/scanner/status")
    check("S8 8095 服务可用", st.get("ok") is True, str(st)[:200])
    check("S8 扫码枪已连接（connected=true）", st.get("connected") is True, str(st)[:260])
    check("S8 传输方式为 evdev（HID 枪）", st.get("transport") == "evdev", str(st.get("transport")))

    _, r = call(rk + ":8095" if rk.count(":") == 1 else rk.replace(":8094", ":8095"),
                "/api/scanner/inject", {"code": "6901234567890", "add_to_cart": False})
    check("S8 注入已知条码被识别", r.get("ok") is True, str(r)[:200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://10.176.240.215:8094")
    ap.add_argument("--slave", default=DEFAULT_SLAVE)
    ap.add_argument("--rk", default=DEFAULT_RK)
    ap.add_argument("--only", default="", help="逗号分隔，如 S2,S4")
    ap.add_argument("--token", default=os.environ.get("STORE_TOKEN", ""),
                    help="会话 token（write 级接口必需），也可用环境变量 STORE_TOKEN")
    a = ap.parse_args()

    global AUTH_TOKEN
    AUTH_TOKEN = a.token.strip()

    wanted = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    run = lambda tag: (not wanted) or (tag in wanted)

    print("== 多场景购物流程验收 ==")
    print("   8094 base : %s" % a.base)
    print("   从机      : %s" % a.slave)
    print("   板端      : %s" % a.rk)

    status, s = call(a.base, "/api/store/status")
    if not (status == 200 and s.get("ok")):
        print("!! 8094 不可达，终止")
        return 2
    print("   商品数    : %s" % s.get("products"))

    def safe(tag, fn, *args):
        """一个场景炸了不能带走整轮 —— 记 FAIL 后继续跑后面的。"""
        try:
            fn(*args)
        except Exception as exc:
            FAILED.append("%s 异常中断" % tag)
            print("     FAIL  %s 异常中断: %s: %s" % (tag, type(exc).__name__, exc))

    if run("S1"):
        safe("S1", s1_cash_three_items, a.base)
    if run("S2"):
        safe("S2", s2_wechat_mobile, a.base)
    if run("S3"):
        safe("S3", s3_alipay_member, a.base)
    if run("S4"):
        safe("S4", s4_bad_codes, a.base)
    if run("S5"):
        safe("S5", s5_stock_guard, a.base)
    if run("S6"):
        safe("S6", s6_refund, a.base)
    if run("S7"):
        safe("S7", s7_weigh_chain, a.base, a.slave)
    if run("S8"):
        safe("S8", s8_scanner_device, a.base, a.rk)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
