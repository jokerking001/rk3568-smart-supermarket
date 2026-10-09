# -*- coding: utf-8 -*-
"""store_ext / store_ext_routes 的自测 —— 不需要板子。

分两层测：

  * **模型层**（`store_ext.StoreExt`）—— 业务规则本身
  * **HTTP 层**（`store_ext_routes.ExtRouter`）—— 权限、参数、返回形状、页面

重点验证的都是「看着像能用、实际会出事」的地方：

  1. `privileged()` 的四件事：确认 / 幂等 / 验证结果 / 审计
  2. 提案状态机：自审被拒、重复审核被拒、乐观锁拦下覆盖改价
  3. **原工程的认证漏洞必须堵上**：手工设 `admin_auth=1` 不能登录
  4. 页面调用的每个接口都在路由表里（漏一个页面就白屏）
  5. `GET /api/checkout` 的金额校验：客户端报的总额对不上就撤单
  6. `/api/printer/submit` 收的是裸二进制，不是 JSON
  7. 页面模板占位符必须全填上，缺一个就报错而不是发坏 HTML

用法：
    python rk3568/store-backend/test_store_ext.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store_ext as sx        # noqa: E402
import store_ext_routes as sr  # noqa: E402
import store_service as ss     # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []

# 真实种子商品
COKE = "6901234567890"       # 可口可乐 330ml, 3.50, 库存 48
DOVE = "6901234567891"       # 德芙巧克力 80g, 15.90, 库存 3


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        return True
    FAIL += 1
    FAILURES.append("%s\n        实际 %r\n        期望 %r" % (label, got, want))
    return False


def check_true(label, value, hint=""):
    global PASS, FAIL
    if value:
        PASS += 1
        return True
    FAIL += 1
    FAILURES.append("%s%s" % (label, ("  —— " + hint) if hint else ""))
    return False


def check_in(label, needle, haystack):
    global PASS, FAIL
    if needle in haystack:
        PASS += 1
        return True
    FAIL += 1
    FAILURES.append("%s\n        %r 不在 %r 里" % (label, needle, haystack))
    return False


class Bench(object):
    """一套干净的 Store + StoreExt + Router。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="store-ext-test-")
        os.environ["STORE_ADMIN_PASSWORD"] = "admin123"
        os.environ.pop("STORE_DEVICE_TOKEN", None)
        self.store = ss.Store(os.path.join(self.dir, "s.db"),
                              os.path.join(self.dir, "receipts"))
        self.ext = sx.StoreExt(self.store)
        self.router = sr.ExtRouter(self.ext)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # -------------------------------------------------- HTTP 便捷封装
    def get(self, path, params=None, cookies=None, headers=None):
        return self.router.handle("GET", path, params or {}, {}, b"",
                                  headers or {}, cookies or {})

    def post(self, path, payload=None, params=None, cookies=None, raw=b"",
             headers=None):
        return self.router.handle("POST", path, params or {}, payload or {}, raw,
                                  headers or {}, cookies or {})

    def login(self, username="admin", password="admin123"):
        reply = self.post("/admin/login", {"user": username, "pwd": password})
        body = json.loads(reply.body)
        token = None
        for key, value in reply.headers:
            if key == "Set-Cookie" and value.startswith(sr.SESSION_COOKIE + "="):
                token = value.split("=", 1)[1].split(";", 1)[0]
        return reply, body, token

    def as_admin(self):
        _reply, _body, token = self.login()
        return {sr.SESSION_COOKIE: token}

    def as_cashier(self):
        self.ext.create_user({"username": "cashier1", "password": "cash123",
                              "role": "cashier"})
        _reply, _body, token = self.login("cashier1", "cash123")
        return {sr.SESSION_COOKIE: token}


def body_of(reply):
    if isinstance(reply.body, bytes):
        return reply.body.decode("utf-8")
    return reply.body


def json_of(reply):
    return json.loads(body_of(reply))


# ================================================================ 模型层
def test_privileged_four_guarantees(bench):
    print("[1] privileged() 四件事")
    ext = bench.ext
    ran = []

    ok, message, _ = _priv(ext, "t.a", "admin", "k1", False,
                           lambda c: (ran.append(1), (True, "不该跑", None))[1])
    check("未确认 -> 拒绝", ok, False)
    check_in("未确认 -> 提示带 confirm", "confirm=true", message)
    check("未确认 -> work 没执行", len(ran), 0)

    with ext.conn:
        first = ext.privileged(ext.conn, "t.b", "admin", "k2", True,
                               lambda c: (True, "第一次", {"n": 1}))
    with ext.conn:
        second = ext.privileged(ext.conn, "t.b", "admin", "k2", True,
                                lambda c: (True, "第二次不该跑", {"n": 999}))
    check("幂等 -> 首次结果", first[2]["n"], 1)
    check("幂等 -> 重放返回首次结果", second[2]["n"], 1)
    check_in("幂等 -> 消息有标记", "幂等重放", second[1])

    before = ext.conn.execute("SELECT COUNT(*) AS n FROM op_logs").fetchone()["n"]
    rolled_back = False
    try:
        with ext.conn:
            ext.privileged(ext.conn, "t.c", "admin", "k3", True,
                           lambda c: (True, "ok", {"x": 1}),
                           verify=lambda c, d: (False, "故意失败"))
    except sx.ConfirmRequired as exc:
        rolled_back = "故意失败" in str(exc)
    after = ext.conn.execute("SELECT COUNT(*) AS n FROM op_logs").fetchone()["n"]
    check("验证失败 -> 抛 ConfirmRequired", rolled_back, True)
    check("验证失败 -> 审计行没留下", after, before)

    rows = ext.conn.execute(
        "SELECT action FROM op_logs WHERE action LIKE 't.%'").fetchall()
    actions = sorted(row["action"] for row in rows)
    check_in("成功操作留下审计", "t.b", actions)
    check("失败操作留 .FAILED 审计", _has_failed_log(ext), True)


def _priv(ext, action, user, key, confirm, work):
    with ext.conn:
        return ext.privileged(ext.conn, action, user, key, confirm, work)


def _has_failed_log(ext):
    with ext.conn:
        ext.privileged(ext.conn, "t.d", "admin", "k4", True,
                       lambda c: (False, "故意失败", None))
    row = ext.conn.execute(
        "SELECT COUNT(*) AS n FROM op_logs WHERE action='t.d.FAILED'").fetchone()
    return row["n"] == 1


def test_password(bench):
    print("[2] 密码哈希")
    digest, salt = sx.hash_password("hunter2")
    check_true("哈希不是明文", "hunter2" not in digest)
    check("校验正确密码", sx.verify_password("hunter2", digest, salt), True)
    check("拒绝错误密码", sx.verify_password("hunter3", digest, salt), False)
    digest2, salt2 = sx.hash_password("hunter2")
    check_true("同一密码两次盐不同", salt != salt2)
    check_true("同一密码两次哈希不同", digest != digest2)
    check("盐损坏不炸", sx.verify_password("hunter2", digest, "zz"), False)
    check("哈希为 None 不炸", sx.verify_password("hunter2", None, salt), False)


def test_proposal_state_machine(bench):
    print("[3] 提案状态机")
    ext = bench.ext

    ok, message, _ = ext.pricing_proposal({"code": DOVE, "new_price": 9.9,
                                          "_operator": "cashier1"})
    check("改价提案", ok, True)
    ok, message, _ = ext.pricing_proposal({"code": DOVE, "new_price": 15.90,
                                          "_operator": "cashier1"})
    check("同价提案被拒", ok, False)

    ok, message, _ = ext.pricing_review({"id": 1, "decision": "APPROVE",
                                         "confirm": True, "idem_key": "r0",
                                         "_operator": "cashier1"})
    check("自审被拒", ok, False)
    check_in("自审提示带 allow_self_review", "allow_self_review", message)

    ok, _, _ = ext.pricing_review({"id": 1, "decision": "MAYBE", "confirm": True,
                                   "_operator": "admin"})
    check("非法 decision 被拒", ok, False)

    ok, _, _ = ext.pricing_review({"id": 1, "decision": "APPROVE", "confirm": True,
                                   "idem_key": "r1", "_operator": "admin"})
    check("审批通过", ok, True)
    ok, message, _ = ext.pricing_review({"id": 1, "decision": "APPROVE",
                                         "confirm": True, "idem_key": "r1b",
                                         "_operator": "admin"})
    check("重复审批被拒", ok, False)

    ok, _, _ = ext.pricing_apply({"id": 1, "confirm": True, "idem_key": "a1",
                                  "_operator": "admin"})
    check("应用成功", ok, True)
    check("价格已改", bench.store.find_by_code(DOVE)["price"], 9.9)

    # 乐观锁
    ext.pricing_proposal({"code": DOVE, "new_price": 8.0, "_operator": "cashier1"})
    ext.pricing_review({"id": 2, "decision": "APPROVE", "confirm": True,
                        "idem_key": "r2", "_operator": "admin"})
    ext.update_price({"code": DOVE, "price": 7.0, "confirm": True,
                      "idem_key": "u1", "_operator": "admin"})
    ok, message, _ = ext.pricing_apply({"id": 2, "confirm": True, "idem_key": "a2",
                                        "_operator": "admin"})
    check("乐观锁拦下应用", ok, False)
    check_in("乐观锁提示重新提案", "重新提案", message)
    check("价格没被覆盖", bench.store.find_by_code(DOVE)["price"], 7.0)

    # 补货
    before = bench.store.find_by_code(DOVE)["stock"]
    ext.restock_proposal({"code": DOVE, "qty": 20, "_operator": "cashier1"})
    ext.restock_review({"id": 1, "decision": "APPROVE", "confirm": True,
                        "idem_key": "rr1", "_operator": "admin"})
    ok, _, _ = ext.restock_apply({"id": 1, "confirm": True, "idem_key": "ra1",
                                  "_operator": "admin"})
    check("补货应用", ok, True)
    check("库存增加", bench.store.find_by_code(DOVE)["stock"], before + 20)


def test_rfid_and_member(bench):
    print("[4] 会员 / RFID")
    ext = bench.ext
    ext.register_member({"name": "张三", "phone": "13800000001", "card_no": "M0001"})
    ok, _, data = ext.add_points({"card_no": "M0001", "points": 600, "confirm": True,
                                  "idem_key": "p1", "_operator": "admin"})
    check("积分加成功", ok, True)
    check("等级升级", data["level"], "银卡")
    ok, _, data = ext.add_points({"card_no": "M0001", "points": 600, "confirm": True,
                                  "idem_key": "p1", "_operator": "admin"})
    check("幂等重放不加分", data["points"], 600.0)

    ext.bind_rfid({"rfid_uid": "AA11", "card_no": "M0001", "confirm": True,
                   "idem_key": "b1", "_operator": "admin"})
    ok, _, member = ext.lookup_member(rfid_uid="AA11")
    check("按 RFID 查到会员", member["card_no"], "M0001")

    ext.conn.execute("INSERT INTO rfid_pending(uid, scene, created_at) "
                     "VALUES('AA11','member',?)", (sx.now_iso(),))
    ext.conn.commit()
    polled = ext.poll_rfid(consume=True)
    check("轮询到 1 张卡", polled["count"], 1)
    check("轮询带上会员", polled["events"][0]["member"]["card_no"], "M0001")
    check("轮询后已消费", ext.poll_rfid(consume=True)["count"], 0)


def test_pay_bumps_daily(bench):
    print("[5] 移动支付要进当日统计")
    store, ext = bench.store, bench.ext
    store.add_to_cart(code=COKE, qty=2)
    ok, _, order_id = store.checkout(method="cash")
    check("建订单", ok, True)
    ok, message, _ = ext.mobile_pay({"order_id": order_id, "method": "wechat",
                                     "_operator": "cashier1"})
    check("发起支付要先确认", ok, False)
    check_in("提示带 confirm", "confirm=true", message)
    ok, _, pay = ext.mobile_pay({"order_id": order_id, "method": "wechat",
                                 "confirm": True, "idem_key": "mp1",
                                 "_operator": "cashier1"})
    check("发起支付", ok, True)
    ok, _, _ = ext.pay_confirm({"request_id": pay["id"], "confirm": True,
                                "idem_key": "pc1", "_operator": "cashier1"})
    check("确认收款", ok, True)
    day = time.strftime("%Y-%m-%d")
    row = ext.conn.execute("SELECT revenue, items_sold FROM daily_stats WHERE day=?",
                           (day,)).fetchone()
    check_true("当日统计有这一笔", row is not None)
    if row:
        check("当日营业额", row["revenue"], 7.0)
        check("当日销量", row["items_sold"], 2.0)
    ok, _, _ = ext.pay_confirm({"request_id": pay["id"], "confirm": True,
                                "idem_key": "pc2", "_operator": "cashier1"})
    check("重复确认被拒", ok, False)


def test_printer_result_serialisation(bench):
    print("[6] 打印队列")
    ext = bench.ext
    ok, _, job = ext.printer_submit({"raster": "AA" * 48, "confirm": True,
                                     "idem_key": "j1", "_operator": "cashier1"})
    check("入队", ok, True)
    job_id = job["id"]
    ok, _, data = ext.printer_job(job_id)
    check("取任务返回新状态", data["status"], "PRINTING")
    check("取任务计次数", data["attempts"], 1)
    # result 是 dict —— 必须能落库（这里曾经因为直接绑 dict 而 500）
    ok, message, _ = ext.printer_ack(job_id, False, {"err": "卡纸"})
    check("ack 接受 dict 结果", ok, True)
    raw = ext.conn.execute("SELECT result FROM print_jobs WHERE id=?",
                           (job_id,)).fetchone()["result"]
    check("dict 被序列化", json.loads(raw)["err"], "卡纸")

    states = []
    ok, _, fresh = ext.printer_submit({"raster": "CC" * 48, "confirm": True,
                                       "idem_key": "j-retry", "_operator": "cashier1"})
    check("新建一个任务来试重试", ok, True)
    fresh_id = fresh["id"]
    for _ in range(3):
        ext.printer_job(fresh_id)
        ext.printer_ack(fresh_id, False, {"err": "卡纸"})
        states.append(ext.conn.execute("SELECT status FROM print_jobs WHERE id=?",
                                       (fresh_id,)).fetchone()["status"])
    check("失败重试 3 次后判死", states, ["QUEUED", "QUEUED", "FAILED"])
    ok, message, _ = ext.printer_ack(fresh_id, False, {"err": "再试"})
    check("判死后不再重排", ext.conn.execute(
        "SELECT status FROM print_jobs WHERE id=?", (fresh_id,)).fetchone()["status"],
        "FAILED")


def test_storecfg_whitelist(bench):
    print("[7] 配置白名单")
    ext = bench.ext
    ok, _, _ = ext.set_storecfg({"store_name": "测试超市", "confirm": True,
                                 "idem_key": "c1", "_operator": "admin"})
    check("合法 key 可写", ok, True)
    ok, message, _ = ext.set_storecfg({"config": {"evil_key": "x"}, "confirm": True,
                                       "idem_key": "c2", "_operator": "admin"})
    check("白名单外被拒", ok, False)
    check_in("提示不可编辑", "不可编辑", message)
    check_in("点名了那个 key", "evil_key", message)
    ok, message, _ = ext.set_storecfg({"evil_key": "x", "confirm": True,
                                       "idem_key": "c3", "_operator": "admin"})
    check("平铺传白名单外的 key 也被拒", ok, False)
    check_in("提示缺 config", "config", message)
    cfg = ext.get_storecfg()
    check("读回配置", cfg["config"]["store_name"], "测试超市")
    check_true("暴露可编辑 key 列表", len(cfg["editable_keys"]) > 0)


# ================================================================ HTTP 层
def test_auth_hole_closed(bench):
    print("[8] 认证：原工程的 admin_auth=1 漏洞必须堵上")
    reply = bench.get("/api/admin/list-users", cookies={"admin_auth": "1"})
    check("admin_auth=1 拿不到数据", reply.status, 401)
    reply = bench.get("/api/admin/list-users", cookies={"admin_auth": "whatever"})
    check("随便一个 admin_auth 值也不行", reply.status, 401)
    reply = bench.get("/api/admin/list-users")
    check("没有 cookie 401", reply.status, 401)
    reply = bench.get("/api/admin/list-users", cookies={"mimi_session": "fake"})
    check("假会话 401", reply.status, 401)

    reply, body, token = bench.login()
    check("登录成功", body["ok"], True)
    check("页面认识的角色名", body["role"], "admin")
    check_true("拿到会话 token", bool(token))
    cookie_header = [v for k, v in reply.headers if k == "Set-Cookie"][0]
    check_in("cookie 带 HttpOnly", "HttpOnly", cookie_header)
    check_in("cookie 带 Path=/", "Path=/", cookie_header)

    reply = bench.get("/api/admin/list-users", cookies={sr.SESSION_COOKIE: token})
    check("真会话 200", reply.status, 200)

    reply = bench.get("/api/admin/list-users",
                      headers={"X-Auth-Token": token})
    check("也认 X-Auth-Token 头", reply.status, 200)

    reply = bench.get("/logout", cookies={sr.SESSION_COOKIE: token})
    check("登出 302", reply.status, 302)
    # 一条响应里会有多个 Set-Cookie（会话 cookie + 老 cookie），
    # dict(headers) 会把它们压成一条，所以必须全取出来再拼。
    cookies_cleared = "; ".join(v for k, v in reply.headers if k == "Set-Cookie")
    check_in("登出清会话 cookie", sr.SESSION_COOKIE, cookies_cleared)
    check_in("登出也清老 cookie", sr.LEGACY_COOKIE, cookies_cleared)
    reply = bench.get("/api/admin/list-users", cookies={sr.SESSION_COOKIE: token})
    check("登出后 401", reply.status, 401)


def test_permission_gate(bench):
    print("[9] 权限分级")
    admin = bench.as_admin()
    cashier = bench.as_cashier()

    check("管理员读", bench.get("/api/admin/list-users", cookies=admin).status, 200)
    check("收银员读用户被拒", bench.get("/api/admin/list-users",
                                        cookies=cashier).status, 403)
    check("收银员可读会员", bench.get("/api/member/list", cookies=cashier).status, 200)
    check("收银员可写会员", bench.post("/api/member/register",
                                       {"name": "李四", "card_no": "M0002"},
                                       cookies=cashier).status, 200)
    check("收银员不能删会员", bench.post("/api/member/delete", {"card_no": "M0002"},
                                         cookies=cashier).status, 403)
    check("管理员可删会员", bench.post("/api/member/delete",
                                       {"card_no": "M0002"}, cookies=admin).status, 200)
    check("未登录读会员 401", bench.get("/api/member/list").status, 401)


def test_device_gate(bench):
    print("[10] 设备令牌")
    check("未配令牌时放行", bench.get("/api/printer/job-meta").status, 200)
    bench.router.device_token = "s3cret"
    check("配了令牌后无令牌被拒",
          bench.get("/api/printer/job-meta").status, 401)
    check("带对令牌放行",
          bench.get("/api/printer/job-meta",
                    headers={"X-Device-Token": "s3cret"}).status, 200)
    check("带错令牌被拒",
          bench.get("/api/printer/job-meta",
                    headers={"X-Device-Token": "nope"}).status, 401)
    bench.router.device_token = ""


def test_pages(bench):
    print("[11] 页面")
    # 提取器是字节级照抄原工程的，原工程里 `<!DOCTYPE` 有大小写两种写法，
    # 所以这几个标记按大小写不敏感比对。
    for path, marker, fold in (("/", "<!doctype html>", True),
                               ("/admin", "登录", False),
                               ("/admin/qr-confirm", "<!doctype html>", True),
                               ("/bigscreen", "<!doctype html>", True),
                               ("/customer/qr", "<!doctype html>", True)):
        reply = bench.get(path)
        check("%s 200" % path, reply.status, 200)
        check_true("%s 是 HTML" % path, "html" in reply.content_type)
        text = body_of(reply)
        if fold:
            text = text.lower()
        check_in("%s 有内容" % path, marker, text)

    # 未登录 /admin 给登录页，登录后给管理页
    login_page = body_of(bench.get("/admin"))
    admin_cookie = bench.as_admin()
    admin_page = body_of(bench.get("/admin", cookies=admin_cookie))
    check_true("未登录是登录页", "登录" in login_page)
    check_true("登录后是管理页", "登录" not in admin_page[:4000])
    check_true("两个页面不一样", login_page != admin_page)

    # /pay 的模板占位符
    bench.store.add_to_cart(code=COKE, qty=1)
    _ok, _msg, order_id = bench.store.checkout()
    reply = bench.get("/pay", {"order": order_id})
    check("/pay 200", reply.status, 200)
    text = body_of(reply)
    check_true("占位符全填了", "{{" not in text)
    check_in("金额来自订单", "3.50", text)
    check_in("订单号已填", str(order_id), text)
    check("/pay 缺 order 400", bench.get("/pay").status, 400)
    check("/pay 订单不存在 400", bench.get("/pay", {"order": 9999}).status, 400)

    # /customer 重定向
    reply = bench.get("/customer")
    check("/customer 302", reply.status, 302)
    check("重定向到 /", dict(reply.headers).get("Location"), "/")

    # 静态资源
    reply = bench.get("/manifest.webmanifest")
    check("manifest 类型", reply.content_type, "application/manifest+json")
    reply = bench.get("/icon.svg")
    check("icon 类型", reply.content_type, "image/svg+xml")
    check_true("icon 是 svg", body_of(reply).startswith("<svg"))

    # /qr-svg
    reply = bench.get("/qr-svg", {"text": "http://x/y"})
    check("qr-svg 200", reply.status, 200)
    check("qr-svg 类型", reply.content_type, "image/svg+xml; charset=utf-8")
    check_true("qr-svg 是 svg", body_of(reply).startswith("<svg"))
    check("qr-svg 缺 text 400", bench.get("/qr-svg").status, 400)


def test_page_endpoint_coverage(bench):
    """页面里 fetch 的每个同源接口都必须在路由表里 —— 漏一个页面就白屏。"""
    print("[12] 页面调用的接口覆盖率")
    web_dir = sr.WEB_DIR
    pattern = re.compile(r"""(?:fetch|EventSource)\(\s*['"`](/[^'"`?]*)""")
    called = set()
    for name in os.listdir(web_dir):
        if not name.endswith(".html"):
            continue
        with open(os.path.join(web_dir, name), "r", encoding="utf-8") as handle:
            for match in pattern.finditer(handle.read()):
                called.add(match.group(1))
    # 由 8094 之外的端口提供的、或页面里写死的，不算我们的责任
    ignore = {"/api/weight", "/api/voice-subtitles", "/api/cam-status",
              "/api/admin-cam-start", "/api/admin-cam-stop", "/api/env"}
    missing = []
    for path in sorted(called):
        if path in ignore:
            continue
        if path in sr.ROUTES_GET or path in sr.ROUTES_POST:
            continue
        if any(path.startswith(prefix) for prefix, _h, _p, _m in sr.PREFIX_ROUTES):
            continue
        # 已有服务（store_service.py）提供的
        if path in _SERVICE_PATHS:
            continue
        missing.append(path)
    check_true("页面调用的接口都有实现", not missing,
               "缺：" + ", ".join(missing))
    check_true("扫到的接口数量合理", len(called) >= 30,
               "只扫到 %d 个" % len(called))


# store_service.py 自己已经提供的路由（逐条从它的 do_GET / do_POST 抄的）。
# 扩展层在它**之前**被调用，所以这些路径扩展层不该抢 —— 但**方法不同**不算抢，
# 比如 /api/checkout 的 POST 归 store_service，GET 归扩展层。
_SERVICE_ROUTES = {
    ("GET", "/"), ("GET", "/index.html"), ("GET", "/api/store/status"),
    ("GET", "/api/products"), ("GET", "/api/product"), ("GET", "/api/cart"),
    ("GET", "/api/orders"), ("GET", "/api/order"), ("GET", "/api/scan/events"),
    ("GET", "/api/admin/trend"), ("GET", "/api/admin/summary"),
    ("GET", "/api/admin/oplogs"), ("GET", "/api/print/preview"),
    ("GET", "/api/print/device"), ("GET", "/api/vision/candidates"),
    ("POST", "/api/cart/add"), ("POST", "/api/cart/set"),
    ("POST", "/api/cart/remove"), ("POST", "/api/cart/clear"),
    ("POST", "/api/checkout"), ("POST", "/api/order/confirm"),
    ("POST", "/api/order/refund"), ("POST", "/api/scan"),
    ("POST", "/api/print/receipt"), ("POST", "/api/print/device"),
    ("POST", "/api/vision/observe"), ("POST", "/api/admin/sku-map"),
    ("POST", "/api/admin/add-product"), ("POST", "/api/admin/restock"),
    ("POST", "/api/admin/delete"),
}
_SERVICE_PATHS = set(path for _method, path in _SERVICE_ROUTES)
# `/` 是特例：扩展层用原工程的完整页面覆盖它，找不到页面文件才退回内置看板
_SERVICE_PATHS.discard("/")

import re  # noqa: E402  (放在这里是为了让上面的常量先定义好)


def test_checkout_get(bench):
    print("[13] GET /api/checkout 的名称解析与金额校验")
    cashier = bench.as_cashier()
    reply = bench.get("/api/checkout", {"items": "可口可乐 330mlx2", "total": "7.00"},
                      cookies=cashier)
    body = json_of(reply)
    check("下单成功", body["ok"], True)
    order_id = body["orderId"]
    order = bench.store.get_order(order_id)
    check("订单金额", order["total"], 7.0)
    check("订单条目数", len(order["items"]), 1)

    # 会员 95 折
    reply = bench.get("/api/checkout", {"items": "可口可乐 330mlx2", "total": "6.65"},
                      cookies=cashier)
    body = json_of(reply)
    check("95 折被接受", body["ok"], True)
    check("标记了会员折扣", body["member_discount"], True)
    check("订单金额是折后价", bench.store.get_order(body["orderId"])["total"], 6.65)

    # 金额对不上 -> 撤单
    before = len(bench.store.list_orders(100))
    reply = bench.get("/api/checkout", {"items": "可口可乐 330mlx2", "total": "0.01"},
                      cookies=cashier)
    body = json_of(reply)
    check("乱报总额被拒", body["ok"], False)
    check_in("提示金额对不上", "对不上", body["message"])
    check("没有多出订单", len(bench.store.list_orders(100)), before)

    # 认不出的商品名 -> 明确报错，不能悄悄少算
    reply = bench.get("/api/checkout", {"items": "不存在的东西x1"},
                      cookies=cashier)
    body = json_of(reply)
    check("认不出的商品被拒", body["ok"], False)
    check_in("报错里点名了商品", "不存在的东西", body["message"])

    check("缺 items 400", bench.get("/api/checkout", cookies=cashier).status, 400)


def test_item_name_with_x(bench):
    """商品名里带 x 时不能切错 —— 原工程是 split('x')。"""
    print("[14] 名称里带 x 的切分")
    bench.store.add_product({"code": "T0001", "name": "X牌 混合果x盒", "price": 5.0,
                             "stock": 10, "category": "测试"})
    cashier = bench.as_cashier()
    reply = bench.get("/api/checkout",
                      {"items": "X牌 混合果x盒x2", "total": "10.00"},
                      cookies=cashier)
    body = json_of(reply)
    check("带 x 的名称能下单", body["ok"], True)
    if body["ok"]:
        order = bench.store.get_order(body["orderId"])
        check("数量解析正确", order["items"][0]["qty"], 2.0)
        check("商品正确", order["items"][0]["name"], "X牌 混合果x盒")


def test_printer_binary_upload(bench):
    print("[15] 打印机收裸二进制光栅")
    cashier = bench.as_cashier()
    blob = bytes(range(48))
    reply = bench.post("/api/printer/submit", params={"width": "384", "height": "48",
                                                     "kind": "raster"},
                       raw=blob, cookies=cashier)
    body = json_of(reply)
    check("提交成功", body["ok"], True)
    # 前面几节已经排过别的任务了，取最新那条
    row = bench.ext.conn.execute(
        "SELECT payload, meta FROM print_jobs ORDER BY id DESC LIMIT 1").fetchone()
    import base64
    check("光栅按 base64 存", base64.b64decode(row["payload"]), blob)
    check("meta 记了尺寸", json.loads(row["meta"])["width"], "384")
    check("meta 记了编码", json.loads(row["meta"])["encoding"], "base64")
    check("空光栅被拒", bench.post("/api/printer/submit", {}, raw=b"",
                                   cookies=cashier).status, 400)


def test_json_shapes(bench):
    print("[16] 页面依赖的字段名")
    admin = bench.as_admin()

    body = json_of(bench.get("/api/admin/trend", cookies=admin)) \
        if "/api/admin/trend" in sr.ROUTES_GET else None
    # 这些是扩展层提供的
    body = json_of(bench.get("/api/admin/customer-analytics", cookies=admin))
    check("客户分析有 ok", body["ok"], True)
    check_true("客户分析有漏斗", "funnel" in body)

    body = json_of(bench.get("/api/admin/analysis", cookies=admin))
    check("经营分析有 ok", body["ok"], True)
    check_true("经营分析有 findings", isinstance(body.get("findings"), list))
    check("没配 key 时是规则模式", body["mode"], "rule-based")

    body = json_of(bench.get("/api/admin/storecfg", cookies=admin))
    check("配置有 config", isinstance(body.get("config"), dict), True)

    body = json_of(bench.get("/api/customer/chat", {"q": "可口可乐多少钱"}))
    check("顾客问答 ok", body["ok"], True)
    check_true("回答里带真实价格", "3.50" in body["reply"], body.get("reply"))
    check("没接 MimiClaw 时是规则回答", body["mode"], "rule-based")

    body = json_of(bench.get("/api/bigscreen", {}))
    check("大屏数据 ok", body["ok"], True)
    check_true("大屏有 trend", "trend" in body)

    body = json_of(bench.get("/api/env"))
    check("温湿度明确说不可用", body["available"], False)

    # /api/products 走的是**内建路由**（不经扩展层，所以 bench.get 拿不到它，
    # 见 [17]），但 web/*.html 直接依赖它的字段名：页面按 `qr_code` 取商品码、
    # 按 `icon` 取图标。2026-10-09 实机发现这两者对不上过 —— products 表的
    # 列名是 `code` 且没有 icon 列，导致 `if(!p.name||!p.qr_code)return;`
    # 永远为假，**商品列表 / 促销大屏 / 管理端商品表全是空的，而且一声不响**。
    # 之前的 [16] 只测扩展层端点，所以没兜住。这里直接测内建路由用的那个函数，
    # 把契约钉死。
    products = bench.store.list_products()          # 等价 GET /api/products
    check_true("商品列表非空", isinstance(products, list) and len(products) > 0)
    p0 = products[0] if products else {}
    check_true("商品带 qr_code（页面按它取码）", bool(p0.get("qr_code")),
               sorted(p0.keys()))
    check("qr_code 与 code 一致", p0.get("qr_code"), p0.get("code"))
    check_true("商品带 icon（页面按它取图标）", bool(p0.get("icon")),
               sorted(p0.keys()))
    # 带查询的那条分支共用同一个出口，也要有
    hit = bench.store.list_products("可乐")
    check_true("带查询的商品列表也有 qr_code",
               all(r.get("qr_code") for r in hit), hit)

    body = json_of(bench.get("/api/ext/status"))
    check("扩展状态 ok", body["ok"], True)
    check("设备令牌默认开放", body["device_token"], "unset(开放)")

    # 订单历史的形状与退款见 [21]，这里只确认它别 500
    check("订单历史 200", bench.get("/api/order-history", cookies=admin).status, 200)
    body = json_of(bench.get("/api/scan-gun-result", cookies=admin))
    check_true("扫码结果可消费", body["ok"], True)


def test_unknown_path(bench):
    print("[17] 不认识的路由交回给 store_service")
    check("未知路径返回 None", bench.get("/api/完全不认识"), None)
    check("store_service 的路由不抢", bench.get("/api/products"), None)
    check("未知 POST 返回 None", bench.post("/api/也不认识"), None)
    check("非 GET/POST 返回 None",
          bench.router.handle("PUT", "/api/member/list", {}, {}, b"", {}, {}), None)


def test_route_table(bench):
    print("[18] 路由表自洽")
    check_true("路由数 >= 60", sr.route_count() >= 60,
               "只有 %d 条" % sr.route_count())
    # 每条路由都能在 ExtRouter 上找到处理方法
    missing = []
    for _method, path, handler, _permission in sr.ROUTES:
        if not hasattr(sr.ExtRouter, "h_" + handler):
            missing.append("%s -> h_%s" % (path, handler))
    check_true("处理方法都存在", not missing, ", ".join(missing))
    # 权限名合法
    allowed = {"open", "device", "read", "write", "admin"}
    bad = [p for _m, p, _h, perm in sr.ROUTES if perm not in allowed]
    check_true("权限名合法", not bad, ", ".join(bad))
    # 表里不该混进 store_service 已有的路由（同方法同路径才算抢）。
    # `/` 和 `/index.html` 是**故意**覆盖的：web/ 部署了就用照抄原工程的那套页面，
    # 没部署时 h_page_index 返回 None，store_service 内建面板照常接管。
    intentional = {("GET", "/"), ("GET", "/index.html")}
    overlap = ["%s %s" % (m, p) for m, p, _h, _p2 in sr.ROUTES
               if (m, p) in _SERVICE_ROUTES and (m, p) not in intentional]
    check_true("不和已有服务抢路由", not overlap, ", ".join(overlap))
    check("GET /api/checkout 归扩展层", "/api/checkout" in sr.ROUTES_GET, True)
    check_true("POST /api/checkout 归 store_service",
               "/api/checkout" not in sr.ROUTES_POST)
    # 页面靠下标退款，这条路由漏了页面上的「退款」按钮就会 404
    check("退款路由在表里",
          ("POST", "/api/admin/refund") in
          [(m, p) for m, p, _h, _perm in sr.ROUTES], True)


def test_qr_endpoint(bench):
    print("[19] /qr-svg 端到端")
    reply = bench.get("/qr-svg", {"text": "http://192.168.1.100:8094/customer"})
    check("200", reply.status, 200)
    svg = body_of(reply)
    check_true("是 SVG", svg.startswith("<svg"))
    check_true("含路径数据", 'path d="M' in svg)
    check("空 text 400", bench.get("/qr-svg", {"text": ""}).status, 400)
    # 超长内容要报 400 而不是 500
    check("超长内容 400", bench.get("/qr-svg", {"text": "z" * 300}).status, 400)


def test_implied_confirm(bench):
    """原工程页面用 JS confirm() 弹窗，请求里不带 confirm 参数。

    所以路由层对已过权限校验的请求视为已确认；`STORE_REQUIRE_CONFIRM=1`
    要能恢复严格模式。两条都得测 —— 否则要么页面用不了，要么确认形同虚设。
    """
    print("[20] 确认环节：页面兼容 vs 严格模式")
    cashier = bench.as_cashier()
    reply = bench.post("/api/member/add-points",
                       {"card_no": "M0009", "points": 10}, cookies=cashier)
    body = json_of(reply)
    # 会员不存在 -> 走业务报错，但**不是**「需要确认」拦下来的。
    # 这两条必须分开断言：只查「不是 confirm 拦截」的话，业务错误会被漏掉。
    check_true("业务错误照常报出来", "不存在" in body["message"], body["message"])
    check_true("默认模式不是 confirm 拦截",
               "该操作需要确认" not in body["message"], body["message"])

    bench.ext.register_member({"name": "王五", "card_no": "M0009"})
    reply = bench.post("/api/member/add-points",
                       {"card_no": "M0009", "points": 10}, cookies=cashier)
    body = json_of(reply)
    check("默认模式能直接加分", body["ok"], True)
    check("分数到账", bench.ext.lookup_member(card_no="M0009")[2]["points"], 10.0)

    # 严格模式
    bench.router.require_confirm = True
    reply = bench.post("/api/member/add-points",
                       {"card_no": "M0009", "points": 5}, cookies=cashier)
    body = json_of(reply)
    check("严格模式被拦下", body["ok"], False)
    check_in("严格模式提示 confirm", "confirm=true", body["message"])
    reply = bench.post("/api/member/add-points",
                       {"card_no": "M0009", "points": 5, "confirm": True,
                        "idem_key": "strict-1"}, cookies=cashier)
    check("严格模式显式带 confirm 能过", json_of(reply)["ok"], True)
    bench.router.require_confirm = False

    # 未登录时不会被「隐含确认」放行
    reply = bench.post("/api/member/add-points", {"card_no": "M0009", "points": 1})
    check("未登录仍 401", reply.status, 401)


def test_order_history_and_refund(bench):
    """订单历史 + 退款：页面靠**数组下标**退款，顺序和形状都不能错。

    这里的三件事一起坏掉时页面**不会报错**，只会静静地显示错的东西：

      * 包成 `{"ok":true,"orders":[...]}` —— 页面 `orders=await or.json()`
        之后直接 `orders.forEach`，拿到对象就抛异常，被 `catch(e){}` 吞掉。
        结果：订单列表空白、营收/客单价全显示 ¥0.00，而且没有任何报错。
      * 顺序反了 —— 最新单跑到最下面；更要命的是页面 `refundOrder(i)` 传的
        是数组下标，顺序一反就会**退到别的单上**。
      * `items` 给成 list —— 页面上是一串 `[object Object]`。
    """
    print("[21] 订单历史与退款（页面契约）")
    store, ext = bench.store, bench.ext
    admin = bench.as_admin()

    def stock_of(code):
        return ext.conn.execute("SELECT stock FROM products WHERE code=?",
                                (code,)).fetchone()["stock"]

    def status_of(order_id):
        return ext.conn.execute("SELECT status FROM orders WHERE id=?",
                                (order_id,)).fetchone()["status"]

    # 造三张已付订单，商品各不相同，好判断退到了哪一张
    codes = (COKE, DOVE, "6901234567892")
    made = []
    for code in codes:
        store.add_to_cart(code=code, qty=1)
        ok, message, order_id = store.checkout(method="cash")
        check_true("建单 %s" % code, ok, message)
        ok, message, pay = ext.mobile_pay(
            {"order_id": order_id, "method": "wechat", "confirm": True,
             "idem_key": "oh-pay-%s" % order_id, "_operator": "admin"})
        check_true("发起支付 %s" % code, ok, message)
        ok, message, _ = ext.pay_confirm(
            {"request_id": pay["id"], "confirm": True,
             "idem_key": "oh-conf-%s" % order_id, "_operator": "admin"})
        check_true("确认收款 %s" % code, ok, message)
        made.append(order_id)

    reply = bench.get("/api/order-history", cookies=admin)
    check("订单历史 200", reply.status, 200)
    raw = json_of(reply)
    check_true("返回的是**裸数组**", isinstance(raw, list),
               "给了 %s —— 页面 orders.forEach 会抛异常，被 catch 吞掉，"
               "订单列表直接空白" % type(raw).__name__)
    if not isinstance(raw, list):
        return

    ids = [o.get("id") for o in raw]
    check_true("按时间升序（旧的在前）", ids == sorted(ids),
               "页面从尾部倒着渲染，顺序反了最新单会显示在最下面：%s" % ids)
    check("最新单在数组末尾", ids[-1], made[-1])
    check_true("刚才三张单的相对顺序没乱",
               [i for i in ids if i in made] == made, str(ids))

    sample = raw[-1]
    check_true("items 是字符串", isinstance(sample.get("items"), str),
               "给了 %s —— 页面上会显示 [object Object]"
               % type(sample.get("items")).__name__)
    check_in("items 是 名称x数量 格式", "x", str(sample.get("items")))
    check_true("total 是数字", isinstance(sample.get("total"), (int, float)),
               "页面要 .toFixed(2)")
    check_true("time 是 HH:MM:SS",
               isinstance(sample.get("time"), str)
               and len(sample["time"]) == 8 and sample["time"][2] == ":",
               repr(sample.get("time")))
    check_true("paid 是布尔", isinstance(sample.get("paid"), bool),
               repr(sample.get("paid")))
    check_true("method 是字符串", isinstance(sample.get("method"), str),
               repr(sample.get("method")))

    # ---- 按下标退款：必须退到那个下标对应的单，不能退错
    target = ids.index(made[1])          # 中间那张（德芙）
    doves_before = stock_of(DOVE)
    reply = bench.post("/api/admin/refund", {"orderId": str(target)},
                       cookies=admin)
    body = json_of(reply)
    check("按下标退款成功", body.get("ok"), True)
    check("退的是下标对应的那张单", body.get("order_id"), made[1])
    check("库存按下单行恢复", stock_of(DOVE), doves_before + 1.0)
    check("状态变 REFUNDED", status_of(made[1]), "REFUNDED")
    left = [status_of(o) for o in made if o != made[1]]
    check_true("另外两张单没被误退", all(s == "PAID" for s in left), str(left))

    # ---- 按真实主键退款（扩展层自己的客户端走这条）
    reply = bench.post("/api/admin/refund", {"order_id": made[2]}, cookies=admin)
    body = json_of(reply)
    check("按主键退款成功", body.get("ok"), True)
    check("退的是指定的那张", body.get("order_id"), made[2])

    # ---- 重复退款要拦住，不能把钱退两遍
    reply = bench.post("/api/admin/refund", {"order_id": made[2]}, cookies=admin)
    body = json_of(reply)
    check("重复退款被拒", body.get("ok"), False)
    check_in("重复退款说清楚原因", "已退款", body.get("message") or "")

    # ---- 未付订单不能退（原工程返回的就是这句话）
    store.add_to_cart(code=COKE, qty=1)
    _ok, _message, unpaid = store.checkout(method="cash")
    reply = bench.post("/api/admin/refund", {"order_id": unpaid}, cookies=admin)
    body = json_of(reply)
    check("未付订单退款被拒", body.get("ok"), False)
    check("未付订单提示与原工程一致", body.get("msg"), "订单不存在或未支付")
    check("未付订单状态没变", status_of(unpaid), "PENDING")

    # ---- 下标越界：原工程也是这句话 + 400
    reply = bench.post("/api/admin/refund", {"orderId": "9999"}, cookies=admin)
    check("越界下标 400", reply.status, 400)
    check("越界提示与原工程一致", json_of(reply).get("msg"), "订单不存在或未支付")
    reply = bench.post("/api/admin/refund", {"orderId": "-1"}, cookies=admin)
    check("负下标 400", reply.status, 400)
    reply = bench.post("/api/admin/refund", {}, cookies=admin)
    check("完全不给订单号 400", reply.status, 400)

    # ---- 退款是特权操作：收银员不能退
    cashier = bench.as_cashier()
    reply = bench.post("/api/admin/refund", {"order_id": made[0]}, cookies=cashier)
    check("收银员不能退款", reply.status, 403)
    check("收银员退款没生效", status_of(made[0]), "PAID")

    # ---- 退款要落审计，且按下标退的那条能看出当时点的是第几行
    rows = ext.conn.execute(
        "SELECT user, detail FROM op_logs WHERE action='order.refund'"
        " ORDER BY id DESC").fetchall()
    by_index = [r for r in rows if r["detail"].startswith("退款订单 #%s（" % made[1])]
    by_id = [r for r in rows if r["detail"] == "退款订单 #%s" % made[2]]
    check_true("按下标退款有审计记录", bool(by_index),
               "只有 %s" % [r["detail"] for r in rows])
    if by_index:
        check("审计记的是操作人", by_index[0]["user"], "admin")
        check_in("审计带页面下标", "页面下标 %d" % target, by_index[0]["detail"])
    check_true("按主键退款也有审计记录", bool(by_id),
               "只有 %s" % [r["detail"] for r in rows])
    check_true("按主键退款不画蛇添足带下标",
               bool(by_id) and "页面下标" not in by_id[0]["detail"],
               by_id[0]["detail"] if by_id else "没记录")


def main():
    bench = Bench()
    try:
        test_privileged_four_guarantees(bench)
        test_password(bench)
        test_proposal_state_machine(bench)
        test_rfid_and_member(bench)
        test_pay_bumps_daily(bench)
        test_printer_result_serialisation(bench)
        test_storecfg_whitelist(bench)
        test_auth_hole_closed(bench)
        test_permission_gate(bench)
        test_device_gate(bench)
        test_pages(bench)
        test_page_endpoint_coverage(bench)
        test_checkout_get(bench)
        test_item_name_with_x(bench)
        test_printer_binary_upload(bench)
        test_json_shapes(bench)
        test_unknown_path(bench)
        test_route_table(bench)
        test_qr_endpoint(bench)
        test_implied_confirm(bench)
        test_order_history_and_refund(bench)
    finally:
        bench.close()

    print("\n" + "=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        for item in FAILURES[:25]:
            print("    - %s" % item)
        return 1
    print("✅ %d 项全部通过 —— 扩展层与路由可信" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
