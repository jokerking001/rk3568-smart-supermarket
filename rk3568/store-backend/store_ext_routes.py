# -*- coding: utf-8 -*-
"""store_ext 的 HTTP 层 —— 把原工程 `WebServer.cpp` 的缺口端点接上 8094。

## 为什么单独一个文件

`store_service.py` 里已经有 `StoreHandler`，1156 行 + 77 个回归测试在守着。
迁移文档写得很明确：「能不动就不动」。所以这里做成**并列模块**：
`StoreHandler` 只在最后 404 之前加一句「问一下扩展路由」，别的一概不动。

## 认证：和原工程不一样，这是故意的

原工程把登录写成这样（`WebServer.cpp`）：

    if (cookie.indexOf("admin_auth=1") >= 0) authed = true;

也就是说**任何人在浏览器里手动设一个 `admin_auth=1` 就是管理员**。这是原工程
的洞，迁移时不能照抄。这里改成真会话：

  * 登录成功后服务端下发 `Set-Cookie: mimi_session=<随机 token>; HttpOnly`
  * 之后每个请求都拿这个 token 去 `auth_sessions` 表查角色
  * **不再认 `admin_auth=1`**

页面里那句 `document.cookie='admin_auth=1; path=/'` **一行都不用改**：服务端的
cookie 带 `HttpOnly`，浏览器会忽略 JS 对同名 cookie 的写入（同名不同 HttpOnly
会并存，而服务端只读 `mimi_session`）。这样既保住了「页面逐字搬运」，
又把洞补上了。

## 权限

  * `open`   —— 任何人（顾客端、大屏）
  * `device` —— 机器对机器（打印机、RFID 读卡器）。见 `STORE_DEVICE_TOKEN`
  * `read`   —— 收银员及以上
  * `write`  —— 收银员及以上（写业务数据）
  * `admin`  —— 仅管理员

所有写操作最终都落到 `store_ext.privileged()`，在那里一次性保证
「确认 + 幂等 + 验证结果 + 审计」。

## 兼容性

板端是 **Python 3.7.3**：不用海象运算符、不用字典合并、不用 `list[int]`。
改完跑 `python tools/check_py37.py --all`。
"""
import base64
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

import store_ext as sx

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

SESSION_COOKIE = "mimi_session"
# 原工程那个不安全的标记 cookie。只用来清理，永不作为凭据。
LEGACY_COOKIE = "admin_auth"

# 会话角色 -> 页面认识的角色名。页面里写的是 `d.role || 'staff'`。
PAGE_ROLES = {"admin": "admin", "cashier": "staff", "viewer": "staff"}

# 上游服务。页面从 8094 同源取，所以这里做一层转发，省得页面跨端口。
UPSTREAM_DEFAULTS = {
    "weight": "http://127.0.0.1:8099/api/scale/status",
    "cam_status": "http://127.0.0.1:8090/api/fusion/status",
    "voice": "http://127.0.0.1:8097/api/voice-subtitles",
}

# `名称x数量` —— 非贪婪 + 结尾锚定，取**最后一个**合法切分点，
# 这样商品名里带 'x' 也不会切错（原工程是 `split('x')`，会切错）。
ITEM_RE = re.compile(r"^(?P<name>.+?)\s*[xX×*]\s*(?P<qty>\d+(?:\.\d+)?)$")
CODE_QTY_RE = re.compile(r"^(?P<code>[^:]+):(?P<qty>\d+(?:\.\d+)?)$")

# 会员折扣。页面本地乘了 0.95，服务端要能复现，否则总价对不上。
MEMBER_DISCOUNT = 0.95
TOTAL_TOLERANCE = 0.011

# `/api/order-history` 的窗口大小。原工程 `MAX_ORDERS` 就是 20，满了丢最旧的。
# 这个值同时决定了页面 `refundOrder(i)` 里下标 i 的含义 —— 两处必须一致。
ORDER_HISTORY_LIMIT = 20


class BadRequest(Exception):
    """请求本身有问题，回 400 而不是 500。"""


# ---------------------------------------------------------------- 请求上下文
class Req(object):
    __slots__ = ("method", "path", "params", "payload", "raw", "headers",
                 "cookies", "session", "operator", "device_ok", "body_kind",
                 "implied_confirm", "token")

    def __init__(self, method, path, params, payload, raw, headers, cookies):
        self.method = method
        self.path = path
        self.params = params or {}
        self.payload = payload or {}
        self.raw = raw or b""
        # 头部一律转小写再存 —— HTTP 头名不区分大小写，调用方传
        # `X-Auth-Token` 还是 `x-auth-token` 都得认。
        self.headers = dict((str(k).lower(), v) for k, v in (headers or {}).items())
        self.cookies = cookies or {}
        self.session = None
        self.operator = "anonymous"
        self.device_ok = False
        self.body_kind = "json"
        self.implied_confirm = False
        self.token = None

    def arg(self, *names):
        """依次在 payload、query 里找第一个非空值。"""
        for name in names:
            value = self.payload.get(name)
            if value not in (None, ""):
                return value
            value = self.params.get(name)
            if value not in (None, ""):
                return value
        return None

    def truthy(self, name, default=False):
        value = self.arg(name)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")

    def merged(self, extra=None):
        """给 store_ext 的 payload：请求体 + query + 注入的操作人。

        已通过权限校验的请求会被视为「已确认」—— 原工程的页面用 JS `confirm()`
        弹窗做人机确认，请求里**不带** `confirm` 参数。要恢复严格模式就设
        `STORE_REQUIRE_CONFIRM=1`（见 ExtRouter.require_confirm）。
        """
        data = {}
        for key, value in self.params.items():
            data[key] = value
        data.update(self.payload)
        if self.implied_confirm and "confirm" not in data:
            data["confirm"] = True
        if extra:
            data.update(extra)
        data["_operator"] = self.operator
        return data


class Reply(object):
    """(状态码, 内容, Content-Type, 额外响应头)。"""

    __slots__ = ("status", "body", "content_type", "headers")

    def __init__(self, status, body, content_type="application/json; charset=utf-8",
                 headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []

    def bytes(self):
        if isinstance(self.body, bytes):
            return self.body
        return self.body.encode("utf-8")


def json_reply(payload, status=200, headers=None):
    return Reply(status, json.dumps(payload, ensure_ascii=False), headers=headers)


def triple_to_reply(triple, ok_status=200, bad_status=400, extra=None):
    """把 store_ext 的 (ok, message, data) 变成 JSON。

    data 会被**摊平**到顶层 —— 页面里读的是 `d.role`、`d.orderId` 这种字段，
    不是 `d.data.role`。原工程就是直接摊平的，保持一致。
    """
    ok, message, data = triple
    payload = {"ok": bool(ok)}
    if message:
        payload["message"] = message
        payload["msg"] = message          # 页面用的是 d.msg
    if isinstance(data, dict):
        for key, value in data.items():
            payload.setdefault(key, value)
    if extra:
        payload.update(extra)
    return json_reply(payload, ok_status if ok else bad_status)


# ---------------------------------------------------------------- 上游转发
def fetch_upstream(url, timeout=2.0):
    """同源转发。返回 (ok, payload_or_message, status)。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
        try:
            return True, json.loads(raw.decode("utf-8", "replace")), response.status
        except ValueError:
            return True, {"raw": raw.decode("utf-8", "replace")}, response.status
    except urllib.error.HTTPError as exc:
        return False, "上游返回 HTTP %s" % exc.code, exc.code
    except urllib.error.URLError as exc:
        return False, "上游不可达（%s）" % exc.reason, 502
    except (socket.timeout, OSError) as exc:
        return False, "上游超时/IO 错误（%s）" % exc, 502


# ================================================================ 路由本体
class ExtRouter(object):
    def __init__(self, ext, web_dir=WEB_DIR, env=None):
        self.ext = ext
        self.store = ext.store
        self.web_dir = web_dir
        env = env if env is not None else os.environ
        self.device_token = (env.get("STORE_DEVICE_TOKEN") or "").strip()
        self.allow_self_register = (env.get("STORE_ALLOW_SELF_REGISTER") or "").strip() \
            in ("1", "true", "yes", "on")
        self.allow_rfid_login = (env.get("STORE_RFID_LOGIN") or "1").strip() \
            not in ("0", "false", "no", "off")
        # 默认按页面兼容处理：已通过权限校验的请求视为已确认（页面用 JS confirm 弹窗）。
        # 设 STORE_REQUIRE_CONFIRM=1 恢复「必须显式带 confirm」的严格模式。
        self.require_confirm = (env.get("STORE_REQUIRE_CONFIRM") or "").strip() \
            in ("1", "true", "yes", "on")
        self.upstreams = {}
        for key, default in UPSTREAM_DEFAULTS.items():
            self.upstreams[key] = (env.get("STORE_%s_URL" % key.upper()) or default).strip()
        self._templates = {}
        self._manifest = None

    # ------------------------------------------------------------ 静态页面
    def manifest(self):
        if self._manifest is None:
            path = os.path.join(self.web_dir, "manifest.json")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    self._manifest = json.load(handle)
            except (IOError, OSError, ValueError):
                self._manifest = {"pages": []}
        return self._manifest

    def page_file(self, name):
        for entry in self.manifest().get("pages", []):
            if entry.get("name") == name:
                return entry.get("file")
        return None

    def template(self, name):
        """读页面。带占位符的原样返回，由 render() 填。

        文件不在（web/ 没部署）就返回 None，让调用方退回 store_service 的内置页。
        """
        if name not in self._templates:
            filename = self.page_file(name) or (name + ".html")
            path = os.path.join(self.web_dir, filename)
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as handle:
                self._templates[name] = handle.read()
        return self._templates[name]

    def content_type(self, name):
        for entry in self.manifest().get("pages", []):
            if entry.get("name") == name:
                return entry.get("content_type") or "text/html; charset=utf-8"
        return "text/html; charset=utf-8"

    def render(self, name, values=None):
        text = self.template(name)
        if text is None:
            return None
        if values:
            for key, value in values.items():
                text = text.replace("{{%s}}" % key, str(value))
        # 没登记占位符的表达式必须显眼地报出来，不能悄悄发一段坏 HTML
        leftover = re.findall(r"\{\{[^}]*\}\}", text)
        if leftover:
            raise BadRequest("页面 %s 还有未填的占位符：%s" % (name, ", ".join(leftover)))
        return Reply(200, text, self.content_type(name))

    # ------------------------------------------------------------ 认证
    def resolve_session(self, req):
        """找出会话令牌。找到就顺手记在 req.token 上，登出要用。"""
        token = req.cookies.get(SESSION_COOKIE) or ""
        if not token:
            token = req.headers.get("x-auth-token") or ""
        if not token:
            token = req.params.get("token") or ""
        # 兼容：如果客户端把真 token 放在老 cookie 名里，也认；但 `admin_auth=1` 不认
        if not token:
            legacy = req.cookies.get(LEGACY_COOKIE) or ""
            if legacy and legacy != "1":
                token = legacy
        if not token:
            return None
        req.token = token
        return self.ext.session_of(token)

    def device_ok(self, req):
        """机器对机器端点。没配 STORE_DEVICE_TOKEN 就放行（和原工程一致），
        配了就要求带对。"""
        if not self.device_token:
            return True
        supplied = req.headers.get("x-device-token") or req.arg("device_token") or ""
        return supplied == self.device_token

    def check(self, req, permission):
        """返回 None 表示通过，否则返回一个 Reply。

        通过时顺带把 `implied_confirm` 置上：能走到这里的请求要么是顾客端公开
        接口，要么已经带着有效会话过了角色校验，原工程页面在这之前已经弹过
        JS `confirm()` 了。
        """
        if permission == "open":
            return None
        if permission == "device":
            if self.device_ok(req):
                req.device_ok = True
                req.implied_confirm = not self.require_confirm
                return None
            return json_reply({"ok": False, "message": "设备令牌不对（STORE_DEVICE_TOKEN）"}, 401)
        if not req.session:
            return json_reply({"ok": False, "message": "未登录或会话已过期"}, 401)
        if not self.ext.has_permission(req.session, permission):
            return json_reply({
                "ok": False,
                "message": "当前角色（%s）没有 %s 权限" % (req.session.get("role"), permission),
            }, 403)
        req.implied_confirm = not self.require_confirm
        return None

    # ------------------------------------------------------------ 入口
    def handle(self, method, path, params, payload, raw, headers, cookies):
        """返回 Reply；路径不归我管则返回 None。

        **本层在 store_service 自己的路由之前被调用**（见 store_service 的挂载
        钩子）。这样 `/` 才能换成原工程那套完整页面，而不是 store_service 里那个
        内置的极简看板。处理器主动返回 None 表示「放弃这条」，交回给
        store_service —— 例如 web/ 目录没部署时，`/` 就退回内置看板。
        """
        if method not in ("GET", "POST"):
            return None
        table = ROUTES_GET if method == "GET" else ROUTES_POST
        handler_name, permission = table.get(path) or (None, None)
        if handler_name is None:
            # 前缀匹配的路由（/qr-svg、/printjob.bin 之类）在这里兜
            handler_name, permission = self.prefix_route(method, path)
            if handler_name is None:
                return None
        req = Req(method, path, params, payload, raw, headers, cookies)
        req.session = self.resolve_session(req)
        if req.session:
            req.operator = req.session.get("username") or "unknown"
        elif permission == "device":
            req.operator = "device"
        guard = self.check(req, permission)
        if guard is not None:
            return guard
        handler = getattr(self, "h_" + handler_name)
        try:
            return handler(req)
        except BadRequest as exc:
            return json_reply({"ok": False, "message": str(exc)}, 400)

    def prefix_route(self, method, path):
        """少数带前缀的路由。返回 (handler_name, permission) 或 (None, None)。"""
        for prefix, handler_name, permission, methods in PREFIX_ROUTES:
            if method in methods and path.startswith(prefix):
                return handler_name, permission
        return None, None

    # ============================================================ 页面
    def must_render(self, name, values=None):
        reply = self.render(name, values)
        if reply is None:
            raise BadRequest("页面 %s 不在 web/ 目录里 —— 部署时漏了这个目录？"
                             % name)
        return reply

    def h_page_index(self, req):
        # 找不到就返回 None，让 store_service 的内置看板顶上
        return self.render("index")

    def h_page_admin(self, req):
        if req.session:
            return self.must_render("admin")
        return self.must_render("admin-login")

    def h_page_admin_qr_confirm(self, req):
        return self.must_render("admin-qr-confirm")

    def h_page_login(self, req):
        return Reply(302, "", "text/plain; charset=utf-8",
                     [("Location", "/admin")])

    def h_page_logout(self, req):
        # 光清 cookie 不够 —— 令牌不失效的话，谁抄走了 cookie 还能继续用。
        # 必须服务端也把它删掉。
        if req.token:
            self.ext.logout(req.token)
        return Reply(302, "", "text/plain; charset=utf-8", [
            ("Location", "/admin"),
            ("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
             % SESSION_COOKIE),
            ("Set-Cookie", "%s=; Path=/; Max-Age=0; SameSite=Lax" % LEGACY_COOKIE),
        ])

    def h_page_pay(self, req):
        order_id = req.arg("order", "orderId")
        total = req.arg("t", "total")
        if order_id is None:
            raise BadRequest("缺少 order（订单号）")
        try:
            numeric = int(str(order_id))
        except ValueError:
            raise BadRequest("order 必须是整数")
        # 以库里的订单为准，不信任 query 里的金额
        order = self.store.get_order(numeric)
        if order:
            total = "%.2f" % order["total"]
        elif total is None:
            raise BadRequest("订单 %s 不存在" % order_id)
        return self.must_render("pay", {"order_id": numeric,
                                   "order_no": numeric + 1,
                                   "total": total})

    def h_page_bigscreen(self, req):
        return self.must_render("bigscreen")

    def h_page_customer(self, req):
        # 原工程 /customer 就是重定向到 /
        return Reply(302, "", "text/plain; charset=utf-8", [("Location", "/")])

    def h_page_customer_qr(self, req):
        host = req.headers.get("host") or "127.0.0.1:8094"
        return self.must_render("customer-qr", {"url": "http://%s/customer" % host})

    def h_asset_manifest(self, req):
        return self.must_render("manifest.webmanifest")

    def h_asset_icon(self, req):
        return self.must_render("icon.svg")

    def h_asset_sw(self, req):
        return self.must_render("sw.js")

    # ============================================================ 登录 / 用户
    def h_admin_login(self, req):
        """页面 POST 的是 form-urlencoded：user / pwd / mode。"""
        username = (req.arg("user", "username") or "").strip()
        password = req.arg("pwd", "password") or ""
        mode = (req.arg("mode") or "login").strip().lower()
        if mode == "reg":
            if not self.allow_self_register:
                return json_reply({
                    "ok": False,
                    "msg": "自助注册已关闭。请用管理员账号登录后在后台建号"
                           "（要放开就设 STORE_ALLOW_SELF_REGISTER=1）。",
                }, 403)
            ok, message, data = self.ext.create_user({
                "username": username, "password": password, "role": "cashier",
                "_operator": "self-register"})
            if not ok:
                return json_reply({"ok": False, "msg": message}, 400)
            return json_reply({"ok": True, "msg": message, "role": "staff"})
        if not username or not password:
            return json_reply({"ok": False, "msg": "请填写用户名和密码"}, 400)

        ok, message, data = self.ext.login(
            username, password, req.headers.get("user-agent") or "")
        if not ok:
            return json_reply({"ok": False, "msg": message}, 401)
        headers = [("Set-Cookie",
                    "%s=%s; Path=/; HttpOnly; SameSite=Lax" % (SESSION_COOKIE, data["token"]))]
        return json_reply({
            "ok": True, "msg": message,
            "user": data["username"], "role": PAGE_ROLES.get(data["role"], "staff"),
            "raw_role": data["role"], "token": data["token"],
        }, headers=headers)

    def h_admin_qr_init(self, req):
        ok, message, data = self.ext.qr_init()
        if not ok:
            return json_reply({"ok": False, "msg": message}, 400)
        host = req.headers.get("host") or "127.0.0.1:8094"
        url = "http://%s/admin/qr-confirm?sid=%s" % (host, data["token"])
        # 页面读 d.sid / d.url
        return json_reply({"ok": True, "sid": data["token"], "url": url,
                           "expires_in": data["expires_in"]})

    def h_admin_qr_status(self, req):
        token = req.arg("sid", "token")
        if not token:
            raise BadRequest("缺少 sid")
        ok, message, data = self.ext.qr_status(token)
        if not ok:
            return json_reply({"ok": False, "confirmed": False, "msg": message}, 404)
        payload = {
            "ok": True,
            "status": data.get("status"),
            "confirmed": data.get("status") == "confirmed",
            "user": data.get("username"),
            "role": PAGE_ROLES.get(data.get("role") or "", "staff"),
        }
        headers = []
        if data.get("session_token"):
            # 二维码登录的会话在这里下发。页面那句 admin_auth=1 会被 HttpOnly 挡掉。
            headers.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Lax"
                            % (SESSION_COOKIE, data["session_token"])))
            payload["token"] = data["session_token"]
        return json_reply(payload, headers=headers)

    def h_admin_qr_confirm_page(self, req):
        return self.h_page_admin_qr_confirm(req)

    def h_admin_qr_confirm(self, req):
        """手机端提交凭据。页面 POST 的是 user / pwd / sid。"""
        token = req.arg("sid", "token")
        username = (req.arg("user", "username") or "").strip()
        password = req.arg("pwd", "password") or ""
        if not token:
            raise BadRequest("缺少 sid")
        if not username or not password:
            return json_reply({"ok": False, "msg": "请填写用户名和密码"}, 400)
        ok, message, _ = self.ext.qr_confirm(token, username, password)
        return json_reply({"ok": ok, "msg": message}, 200 if ok else 401)

    def h_admin_qr_do_confirm(self, req):
        return self.h_admin_qr_status(req)

    def h_list_users(self, req):
        return json_reply({"ok": True, "users": self.ext.list_users()})

    def h_create_user(self, req):
        ok, message, data = self.ext.create_user(req.merged())
        return json_reply({"ok": ok, "msg": message, "message": message,
                           "user": (data or {}).get("username")},
                          200 if ok else 400)

    def h_delete_user(self, req):
        ok, message, data = self.ext.delete_user(req.merged())
        return json_reply({"ok": ok, "msg": message, "message": message},
                          200 if ok else 400)

    # ============================================================ 会员
    def h_member_list(self, req):
        return json_reply({"ok": True, "members": self.ext.list_members(req.arg("q"))})

    def h_member_lookup(self, req):
        raw = (req.arg("code", "card_no", "phone", "rfid_uid") or "").strip()
        if not raw:
            raise BadRequest("缺少 code")
        card_no, phone, rfid_uid = None, None, None
        if raw.startswith("MEM:"):
            card_no = raw[4:]
        elif raw.isdigit() and len(raw) == 11:
            phone = raw
        elif raw.isdigit() and len(raw) >= 16:
            rfid_uid = raw
        else:
            card_no = raw
        ok, message, data = self.ext.lookup_member(card_no=card_no, phone=phone,
                                                  rfid_uid=rfid_uid)
        if not ok:
            return json_reply({"ok": False, "msg": message, "message": message}, 404)
        return json_reply({"ok": True, "msg": message, "member": data,
                           "card_no": data.get("card_no"), "name": data.get("name"),
                           "points": data.get("points"), "level": data.get("level")})

    def h_member_register(self, req):
        ok, message, data = self.ext.register_member(req.merged())
        return json_reply({"ok": ok, "msg": message, "message": message,
                           "card_no": (data or {}).get("card_no")},
                          200 if ok else 400)

    def h_member_add_points(self, req):
        return triple_to_reply(self.ext.add_points(req.merged()))

    def h_member_delete(self, req):
        return triple_to_reply(self.ext.delete_member(req.merged()))

    def h_member_bind_rfid(self, req):
        return triple_to_reply(self.ext.bind_rfid(req.merged(), scene="member"))

    def h_member_unbind_rfid(self, req):
        return triple_to_reply(self.ext.unbind_rfid(req.merged()))

    def h_admin_bind_rfid(self, req):
        return triple_to_reply(self.ext.bind_rfid(req.merged()))

    def h_admin_unbind_rfid(self, req):
        return triple_to_reply(self.ext.unbind_rfid(req.merged()))

    def h_rfid_report(self, req):
        """**从机（ESP32-S3）刷卡上报**入口。

        ESP32-S3 降级为外设从机后，读卡的是它，落库的是这里。
        原工程的 `bind_rfid` 只在「管理员点绑定→等刷卡」时写 `rfid_pending`，
        没有「从机主动上报」这条路 —— 从机发出去会 404，且是静默的
        （从机只记失败计数，不报错）。所以补这个端点。

        上报的卡号进 `rfid_pending`，语义与页面轮询 `/api/rfid-poll` 完全一致：
        页面（管理员绑定 / 会员 / 收银台）轮询时就能看到，无需改页面。

        权限 `open`：从机没有会话，且卡号本身不是机密（页面也明文轮询）。
        """
        uid = (req.arg("uid") or req.arg("rfid_uid") or "").strip()
        if not uid:
            return json_reply({"ok": False, "message": "缺少 uid"}, 400)
        scene = (req.arg("scene") or "slave").strip() or "slave"
        return triple_to_reply(self.ext.report_rfid(uid, scene=scene))

    def h_rfid_poll(self, req):
        """刷卡轮询。页面靠这个做「刷卡即登录」。

        ⚠️ 原工程就是「刷一张已绑定的卡 = 登录后台」。这里保留这个行为
        （页面没改），但可以用 `STORE_RFID_LOGIN=0` 关掉。
        """
        result = self.ext.poll_rfid(scene=req.arg("scene"),
                                    consume=req.truthy("consume", True))
        events = result.get("events") or []
        if not events:
            return json_reply({"ok": True, "count": 0, "events": [],
                               "card": None, "user": None})
        first = events[0]
        member = first.get("member") or {}
        payload = {"ok": True, "count": result["count"], "events": events,
                   "card": first.get("uid"), "scene": first.get("scene")}
        headers = []
        if member:
            payload["user"] = member.get("card_no") or member.get("name")
            payload["member"] = member
            if self.allow_rfid_login:
                # 给这个会员开一个真会话（页面拿到后不会再自己设 admin_auth）
                session = self._member_session(member)
                if session:
                    payload["role"] = PAGE_ROLES.get(session["role"], "staff")
                    headers.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Lax"
                                    % (SESSION_COOKIE, session["token"])))
        return json_reply(payload, headers=headers)

    def _member_session(self, member):
        """给刷卡登录的会员开一个会话。没有对应账号就先建一个只读账号。

        这类账号的密码是随机生成的、谁也不知道，所以**不走密码登录**，
        直接开一个 viewer 会话。
        """
        card_no = member.get("card_no") or ""
        if not card_no:
            return None
        username = "member:" + card_no
        row = self.ext.conn.execute("SELECT * FROM users WHERE username=?",
                                    (username,)).fetchone()
        if not row:
            ok, message, _ = self.ext.create_user({
                "username": username, "password": sx.new_token(12),
                "role": sx.ROLE_VIEWER,
                "display_name": member.get("name") or card_no,
                "_operator": "rfid"})
            if not ok:
                return None
        return self._force_session(username)

    def _force_session(self, username):
        row = self.ext.conn.execute("SELECT * FROM users WHERE username=?",
                                    (username,)).fetchone()
        if not row or not row["active"]:
            return None
        token = sx.new_token()
        with self.ext.lock:
            with self.ext.conn:
                self.ext.conn.execute(
                    "INSERT INTO auth_sessions(token, username, role, created_at, "
                    "expires_at, last_seen, user_agent) VALUES(?,?,?,?,?,?,?)",
                    (token, username, row["role"], sx.now_iso(),
                     sx.future_iso(sx.SESSION_TTL), sx.now_iso(), "rfid"))
                self.ext._log(self.ext.conn, "login.rfid",
                              "刷卡登录：%s" % username, username)
        return {"token": token, "username": username, "role": row["role"]}

    def h_customer_member_session(self, req):
        """顾客端绑定/查询会员会话。"""
        session_id = req.arg("session", "session_id") or "guest"
        code = (req.arg("code", "card_no") or "").strip()
        member = None
        if code:
            ok, message, data = self.ext.lookup_member(
                card_no=code[4:] if code.startswith("MEM:") else code)
            if not ok:
                return json_reply({"ok": False, "msg": message, "message": message}, 404)
            member = data
        ok, message, data = self.ext.touch_customer_session(
            session_id, {"note": req.arg("note"),
                         "member_id": member["id"] if member else None})
        return json_reply({"ok": ok, "msg": message, "member": member,
                           "session": data})

    # ============================================================ 改价 / 补货
    def h_pricing_proposal(self, req):
        return triple_to_reply(self.ext.pricing_proposal(req.merged()))

    def h_pricing_review(self, req):
        return triple_to_reply(self.ext.pricing_review(req.merged()))

    def h_pricing_apply(self, req):
        return triple_to_reply(self.ext.pricing_apply(req.merged()))

    def h_restock_proposal(self, req):
        return triple_to_reply(self.ext.restock_proposal(req.merged()))

    def h_restock_review(self, req):
        return triple_to_reply(self.ext.restock_review(req.merged()))

    def h_restock_apply(self, req):
        return triple_to_reply(self.ext.restock_apply(req.merged()))

    def h_update_price(self, req):
        return triple_to_reply(self.ext.update_price(req.merged()))

    def h_list_proposals(self, req):
        kind = req.arg("kind") or "price"
        table = "restock_proposals" if kind in ("restock", "补货") else "price_proposals"
        return json_reply({"ok": True, "kind": kind,
                           "proposals": self.ext.list_proposals(table, req.arg("status"))})

    # ============================================================ 打印
    def h_printer_submit(self, req):
        """页面 POST 的是**裸二进制**光栅（Content-Type: application/octet-stream）。

        原工程直接吃这块内存；这里转成 base64 存进队列，打印机取任务时再还原。
        """
        raster = None
        if req.raw:
            raster = base64.b64encode(req.raw).decode("ascii")
            encoding = "base64"
        else:
            raster = req.arg("raster", "payload")
            encoding = "text"
        if not raster:
            return json_reply({"ok": False, "message": "缺少光栅数据"}, 400)
        width = req.arg("width")
        height = req.arg("height")
        payload = req.merged({
            "raster": raster,
            "kind": req.arg("kind") or "raster",
            "meta": {"width": width, "height": height, "encoding": encoding,
                     "bytes": len(req.raw)},
        })
        return triple_to_reply(self.ext.printer_submit(payload))

    def h_printer_job_meta(self, req):
        return json_reply(self.ext.printer_job_meta())

    def h_printer_job(self, req):
        job_id = req.arg("id", "job_id", "job")
        if not job_id:
            raise BadRequest("缺少 id")
        ok, message, data = self.ext.printer_job(int(job_id))
        if not ok:
            return json_reply({"ok": False, "message": message}, 404)
        return json_reply({"ok": True, "message": message, "job": data,
                           "id": data.get("id"), "payload": data.get("payload"),
                           "kind": data.get("kind")})

    def h_printer_job_bin(self, req):
        """打印机取原始光栅字节。"""
        job_id = req.arg("id", "job_id")
        if not job_id:
            raise BadRequest("缺少 id")
        ok, message, data = self.ext.printer_job(int(job_id))
        if not ok:
            return Reply(404, message, "text/plain; charset=utf-8")
        try:
            blob = base64.b64decode(data.get("payload") or "")
        except (TypeError, ValueError):
            blob = (data.get("payload") or "").encode("utf-8")
        return Reply(200, blob, "application/octet-stream")

    def h_printer_ack(self, req):
        job_id = req.arg("id", "job_id")
        if not job_id:
            raise BadRequest("缺少 id")
        ok_flag = req.truthy("ok", True)
        result = req.arg("result")
        if result is not None and not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return triple_to_reply(self.ext.printer_ack(int(job_id), ok_flag, result))

    # ============================================================ 支付
    def h_mobile_pay(self, req):
        order_id = req.arg("order_id", "orderId", "order")
        if not order_id:
            raise BadRequest("缺少 order_id")
        return triple_to_reply(self.ext.mobile_pay(req.merged({"order_id": order_id})))

    def h_pay_status(self, req):
        request_id = req.arg("request_id", "id")
        order_id = req.arg("order", "order_id", "orderId")
        if not request_id and not order_id:
            raise BadRequest("请给 request_id 或 order")
        ok, message, data = self.ext.pay_status(
            request_id=int(request_id) if request_id else None,
            order_id=int(order_id) if order_id else None)
        if not ok:
            return json_reply({"ok": False, "message": message, "paid": False}, 404)
        return json_reply({"ok": True, "message": message, "status": data["status"],
                           "paid": data["status"] == "PAID", "request": data})

    def h_pay_confirm(self, req):
        request_id = req.arg("request_id", "id")
        order_id = req.arg("orderId", "order_id", "order")
        if request_id:
            return triple_to_reply(self.ext.pay_confirm(
                req.merged({"request_id": int(request_id)})))
        if not order_id:
            raise BadRequest("请给 request_id 或 orderId")

        order_id = int(order_id)
        items = (req.arg("items") or "").strip()
        warning = None
        if items:
            warning = self._check_paid_items(order_id, items)
        ok, message = self.store.confirm_order(order_id, req.arg("method"))
        if not ok:
            return json_reply({"ok": False, "message": message, "msg": message}, 400)
        payload = {"ok": True, "message": message, "msg": message,
                   "order_id": order_id, "orderId": order_id}
        if warning:
            payload["warning"] = warning
        return json_reply(payload)

    def _check_paid_items(self, order_id, items):
        """把页面传来的 `code:qty` 和订单实际条目对一遍。

        对不上**不拦单**（真店里拦住一笔成交比放过更糟），但写进审计。
        """
        order = self.store.get_order(order_id)
        if not order:
            return None
        sent = {}
        for part in items.split(","):
            part = part.strip()
            if not part:
                continue
            match = CODE_QTY_RE.match(part)
            if not match:
                return "收银端传来的 items 有解析不了的条目：%r" % part
            sent[match.group("code")] = float(match.group("qty"))
        actual = {}
        for item in order.get("items") or []:
            actual[item["code"]] = actual.get(item["code"], 0.0) + float(item["qty"])
        if sent != actual:
            note = "收银端 items 与订单 #%s 实际条目不一致（收银端 %s / 实际 %s）" % (
                order_id, sent, actual)
            with self.store.lock:
                with self.store._conn as conn:
                    self.store._log(conn, "pay.items_mismatch", note, "system")
            return note
        return None

    def h_checkout_get(self, req):
        """页面用 GET + `items=<名称>x<数量>, ...` 下单。

        ⚠️ 原工程按显示名重新解析商品，这里沿用（页面改不了），但解析方式更稳：
        用「最后一个 `x数字`」切，商品名里带 x 也不会切错。

        `total` **不盲信**，而且**在落单之前就校验**：只接受两种值 —— 等于明细
        合计，或等于明细合计 × 会员折扣。对不上就直接拒，**不会先建单再撤单**
        （先建后撤会在订单列表里留一堆 REFUNDED 垃圾，还会白白动一次库存）。
        """
        items_text = (req.arg("items") or "").strip()
        if not items_text:
            raise BadRequest("缺少 items")
        method = req.arg("method") or "cash"
        session = req.arg("session") or "default"

        parsed = self._parse_items(items_text)

        # 先算明细合计（口径和 store_service.checkout 保持一致）
        items_total = 0.0
        for code, qty in parsed:
            product = self.store.find_by_code(code)
            if not product:
                raise BadRequest("商品已下架：%s" % code)
            items_total += round(float(product["price"]) * qty + 0.0, 2)
        items_total = round(items_total + 0.0, 2)

        claimed = req.arg("total")
        discount_applied = False
        if claimed is not None:
            try:
                claimed_value = round(float(claimed) + 0.0, 2)
            except (TypeError, ValueError):
                raise BadRequest("total 不是数字：%r" % claimed)
            plain = items_total
            discounted = round(items_total * MEMBER_DISCOUNT + 0.0, 2)
            if abs(claimed_value - plain) <= TOTAL_TOLERANCE:
                pass
            elif abs(claimed_value - discounted) <= TOTAL_TOLERANCE:
                discount_applied = True
            else:
                return json_reply({
                    "ok": False,
                    "message": "收银端报的总额 %.2f 与明细 %.2f 对不上，"
                               "没有下单，请刷新后重试" % (claimed_value, plain),
                    "msg": "金额校验失败",
                }, 400)

        self.store.clear_cart(session)
        for code, qty in parsed:
            ok, message, _ = self.store.add_to_cart(code=code, qty=qty, session=session,
                                                   source="cashier")
            if not ok:
                self.store.clear_cart(session)
                return json_reply({"ok": False, "message": message, "msg": message}, 400)

        ok, message, order_id = self.store.checkout(session, method)
        if not ok:
            return json_reply({"ok": False, "message": message, "msg": message}, 400)

        order = self.store.get_order(order_id)
        if discount_applied and order:
            final = round(order["total"] * MEMBER_DISCOUNT + 0.0, 2)
            with self.store.lock:
                with self.store._conn as conn:
                    conn.execute("UPDATE orders SET total=? WHERE id=?", (final, order_id))
                    self.store._log(conn, "checkout.member_discount",
                                    "订单 #%s 应用会员 95 折：%.2f -> %.2f"
                                    % (order_id, order["total"], final), req.operator)
            order = self.store.get_order(order_id)
        elif order and abs(order["total"] - items_total) > TOTAL_TOLERANCE:
            # 理论上到不了这里；到了就说明我的预估口径和 checkout 不一致，得留痕
            with self.store.lock:
                with self.store._conn as conn:
                    self.store._log(conn, "checkout.total_mismatch",
                                    "订单 #%s 实际 %.2f，预估 %.2f"
                                    % (order_id, order["total"], items_total), req.operator)

        # 页面读的是 d.orderId
        return json_reply({"ok": True, "message": message, "msg": message,
                           "orderId": order_id, "order_id": order_id,
                           "total": order["total"] if order else items_total,
                           "member_discount": discount_applied})

    def _parse_items(self, items_text):
        """`可口可乐 330mlx2, 乐事薯片 75gx1` -> [(code, qty), ...]"""
        catalog = {}
        for product in self.store.list_products():
            catalog[product["name"]] = product["code"]
        parsed = []
        unknown = []
        for part in items_text.split(","):
            part = part.strip()
            if not part:
                continue
            match = ITEM_RE.match(part)
            if not match:
                unknown.append(part)
                continue
            name = match.group("name").strip()
            qty = float(match.group("qty"))
            code = catalog.get(name)
            if code is None:
                # 页面偶尔会多带空格或全角，做一次宽松匹配
                for known, known_code in catalog.items():
                    if known.replace(" ", "") == name.replace(" ", ""):
                        code = known_code
                        break
            if code is None:
                unknown.append(name)
                continue
            parsed.append((code, qty))
        if unknown:
            raise BadRequest("认不出这些商品（名称要和商品表完全一致）：%s"
                             % "、".join(unknown))
        if not parsed:
            raise BadRequest("items 里没有可识别的商品")
        return parsed

    # ============================================================ 报表 / 大屏
    def h_export_report(self, req):
        ok, message, data = self.ext.export_report(
            days=int(req.arg("days") or 7), fmt=(req.arg("format", "fmt") or "csv"))
        if not ok:
            return json_reply({"ok": False, "message": message}, 400)
        body = data.get("content") or ""
        filename = data.get("filename") or "report.csv"
        fmt = (data.get("format") or "csv").lower()
        mime = ("text/csv; charset=utf-8" if fmt == "csv"
                else "application/json; charset=utf-8")
        return Reply(200, body, mime,
                     [("Content-Disposition", 'attachment; filename="%s"' % filename)])

    def h_admin_analysis(self, req):
        # store_ext.admin_analysis() 直接返回 dict（含 ok / mode / findings / note）
        payload = dict(self.ext.admin_analysis())
        payload["type"] = req.arg("type")
        return json_reply(payload)

    def h_customer_analytics(self, req):
        # store_ext.customer_analytics() 直接返回 dict（含 ok）
        return json_reply(self.ext.customer_analytics(int(req.arg("days") or 7)))

    def h_bigscreen_data(self, req):
        return json_reply(self.ext.bigscreen_data())

    def h_reset_trend(self, req):
        return triple_to_reply(self.ext.reset_trend(req.merged()))

    # ============================================================ 工单
    def h_service_request_create(self, req):
        ok, message, data = self.ext.create_ticket(req.merged())
        return json_reply({"ok": ok, "msg": message, "message": message,
                           "id": (data or {}).get("id")}, 200 if ok else 400)

    def h_service_request_update(self, req):
        return triple_to_reply(self.ext.update_ticket(req.merged()))

    def h_service_request_list(self, req):
        return json_reply({"ok": True,
                           "tickets": self.ext.list_tickets(req.arg("status"))})

    # ============================================================ 顾客端
    def h_customer_chat(self, req):
        ok, message, data = self.ext.customer_chat(
            {"message": req.arg("q", "message", "question")})
        if not ok:
            return json_reply({"ok": False, "msg": message, "message": message}, 400)
        return json_reply({"ok": True, "msg": message, "reply": data["reply"],
                           "mode": data["mode"], "note": data["note"],
                           "matched_products": data["matched_products"]})

    def h_customer_session(self, req):
        ok, message, data = self.ext.touch_customer_session(
            req.arg("session", "session_id") or "guest",
            {"note": req.arg("note")})
        return json_reply({"ok": ok, "message": message, "session": data})

    def h_customer_analytics_event(self, req):
        return triple_to_reply(self.ext.analytics_event(req.merged()))

    def h_customer_scan_event(self, req):
        """顾客端「扫到了什么」上报。走 8094 已有的 record_scan。"""
        code = req.arg("code")
        if not code:
            raise BadRequest("缺少 code")
        ok, message, product = self.store.record_scan(code, req.arg("source") or "customer")
        return json_reply({"ok": ok, "message": message, "msg": message,
                           "product": product}, 200 if ok else 404)

    # ============================================================ 配置
    def h_storecfg_get(self, req):
        # store_ext.get_storecfg() 直接返回 {"ok", "config", "editable_keys"}
        return json_reply(self.ext.get_storecfg())

    def h_storecfg_set(self, req):
        return triple_to_reply(self.ext.set_storecfg(req.merged()))

    # ============================================================ 摄像头 / 秤
    def h_cam_start(self, req):
        return self._proxy("cam_status", req, note="摄像头忙状态由 8090 融合服务提供")

    def h_cam_stop(self, req):
        return self._proxy("cam_status", req, note="摄像头忙状态由 8090 融合服务提供")

    def h_cam_status(self, req):
        reply = self._proxy("cam_status", req)
        # 页面靠 d.busy 决定要不要跳过扫描。上游没起来时**放行扫描**更安全：
        # 卡住不扫比多扫一次糟得多。
        if reply.status != 200:
            return json_reply({"ok": True, "busy": False, "available": False,
                               "message": "上游未就绪，已放行扫描"})
        return reply

    def h_weight(self, req):
        return self._proxy("weight", req)

    def h_voice_subtitles(self, req):
        return self._proxy("voice", req)

    def h_env(self, req):
        """大屏用的温湿度。

        DHT22 在 ESP32-S3 从机上，RK3568 这边没有传感器 —— 数据靠从机
        `POST /api/env/report` 报上来（见 `h_env_report`）。
        没收到 / 收到但过期时 `available=False`，大屏会显示「温湿度传感器未接」，
        不会拿旧读数冒充实时值。
        """
        return json_reply(self.ext.latest_env())

    def h_env_report(self, req):
        """**从机（ESP32-S3）温湿度上报**入口。

        和 `h_rfid_report` 是同一类补丁：原工程里 DHT22 由主控自己读、
        读完只拼 AI 提示词，既不入库也不对外；降级成从机后传感器还在
        ESP32-S3 上，而要看数的是 RK3568 的大屏 —— 中间缺一条路。

        权限 `open`：从机没有会话；温湿度既不是机密也无副作用，
        且大屏本身就是公开读的（`GET /api/env` 也是 open）。
        """
        # 从机用 form 编码（`temperature=24.3&humidity=55.1`），
        # 页面/调试用 JSON 也行 —— req.arg() 两者都认。
        t = req.arg("temperature")
        h = req.arg("humidity")
        if t is None or h is None:
            return json_reply({"ok": False,
                               "message": "缺少 temperature / humidity"}, 400)
        source = (req.arg("source") or "esp32s3-dht22").strip() or "esp32s3-dht22"
        return triple_to_reply(self.ext.report_env(t, h, source=source))

    def _proxy(self, key, req, note=None):
        url = self.upstreams.get(key)
        if not url:
            return json_reply({"ok": False, "available": False,
                               "message": "没有配置 %s 的上游地址" % key}, 502)
        ok, payload, status = fetch_upstream(url)
        if not ok:
            return json_reply({"ok": False, "available": False, "upstream": url,
                               "message": payload, "note": note}, 502)
        if isinstance(payload, dict):
            payload.setdefault("ok", True)
            payload["upstream"] = url
            return json_reply(payload)
        return json_reply({"ok": True, "upstream": url, "data": payload})

    # ============================================================ 扫码枪
    def h_scan_gun_result(self, req):
        return json_reply({"ok": True,
                           "events": self.store.take_scan_events(
                               consume=req.truthy("consume", True),
                               limit=int(req.arg("limit") or 20))})

    def h_scan_gun_clear(self, req):
        return json_reply({"ok": True,
                           "events": self.store.take_scan_events(consume=True, limit=200)})

    def h_order_history(self, req):
        """**裸 JSON 数组**，不是 {"ok":true,"orders":[...]}。

        页面里是 `orders = await or.json()` 然后直接 `orders.forEach(...)`。
        原工程 `Product_OrderHistoryToJson()` 返回的就是数组，包一层的话
        页面 `orders.length` 是 undefined、`forEach` 抛异常被 catch 吞掉 ——
        订单列表会是空的，营收/客单价统计全显示 0，而且**不报错**。
        """
        orders = self.order_window(req.arg("limit"))
        body = json.dumps([self._order_view(o) for o in orders],
                          ensure_ascii=False)
        return Reply(200, body, "application/json; charset=utf-8")

    # ------------------------------------------------ 订单历史的「页面视角」
    def order_window(self, limit=None):
        """页面看到的那个数组：最新 N 单、**旧的在前**。

        原工程 `orders[]` 是按下单顺序追加的，序列化也从 0 到 orderCount-1
        顺序输出；页面 `for(i=orders.length-1;i>=0;i--)` 从尾部倒着渲染，
        于是最新的显示在最上面。

        `Store.list_orders()` 是 `ORDER BY id DESC`（最新在前）。直接喂给页面
        会把顺序整个颠倒，而且页面 `refundOrder(i)` 传的**数组下标**会指到
        另一张单上 —— 退款退错单，这是要出事的。所以这里翻回来。

        `MAX_ORDERS` 在原工程里是 20，满了就丢最旧的，正好等于「最新 20 单」。
        """
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = ORDER_HISTORY_LIMIT
        if count <= 0:
            count = ORDER_HISTORY_LIMIT
        orders = self.store.list_orders(count)   # id DESC，最新在前
        orders.reverse()                          # -> 旧的在前，与原工程一致
        return orders

    @staticmethod
    def _order_view(order):
        """还原成原工程 /api/order-history 的字段形状。

        页面对这些字段的要求是硬的：
          o.items  是**字符串** `"名称x数量, 名称x数量"`，直接拼进 HTML
                   （给 list 的话页面上会出现一堆 [object Object]）
          o.total  是数字，要能 .toFixed(2)
          o.time   是 `HH:MM:SS`（原工程只存时分秒，没有日期）
          o.paid   是布尔
          o.method 是字符串
        """
        parts = []
        for item in order.get("items") or []:
            qty = float(item.get("qty") or 0)
            parts.append("%sx%s" % (item.get("name") or "", "%g" % qty))
        created = order.get("created_at") or ""
        view = {
            "items": ", ".join(parts),
            "total": round(float(order.get("total") or 0), 2),
            "time": created[11:19] if len(created) >= 19 else created,
            "paid": order.get("status") == "PAID",
            "method": order.get("method") or "",
        }
        # 下面两个页面用不到，但扩展层自己（和退款）需要稳定标识
        view["id"] = order.get("id")
        view["status"] = order.get("status")
        return view

    def h_admin_refund(self, req):
        """退款。原工程收的 `orderId` 是 `orders[]` 的**数组下标**。

        页面 `refundOrder(i)` 传的就是下标（`orders[orderId]`、`Product_IsPaid`），
        所以 `orderId` 按「页面看到的窗口里的位置」解释。下标→主键必须用
        `order_window()` 这同一个窗口翻译，否则会退到别的单上。

        想按真实主键退，用 `order_id`（扩展层自己的客户端走这条）。
        """
        real_id = req.arg("order_id", "id")
        index = req.arg("orderId")
        if real_id is not None:
            payload = req.merged({"order_id": real_id})
        elif index is not None:
            try:
                position = int(index)
            except (TypeError, ValueError):
                return json_reply({"ok": False, "msg": "订单不存在或未支付"}, 400)
            window = self.order_window(req.arg("limit"))
            if position < 0 or position >= len(window):
                return json_reply({"ok": False, "msg": "订单不存在或未支付"}, 400)
            order = window[position]
            payload = req.merged({"order_id": order.get("id"),
                                  "index": position})
        else:
            return json_reply({"ok": False, "msg": "订单不存在或未支付"}, 400)
        return triple_to_reply(self.ext.refund_order(payload))

    # ============================================================ 诊断
    def h_ext_status(self, req):
        data = self.ext.status()
        payload = {"ok": True,
                   "device_token": "set" if self.device_token else "unset(开放)",
                   "self_register": self.allow_self_register,
                   "rfid_login": self.allow_rfid_login,
                   "upstreams": dict(self.upstreams),
                   "pages": [p.get("path") for p in self.manifest().get("pages", [])]}
        payload.update(data)
        return json_reply(payload)

    def h_qr_svg(self, req):
        text = req.arg("text") or ""
        if not text:
            raise BadRequest("缺少 text")
        try:
            import qr_svg
        except ImportError:
            return Reply(501, "二维码模块 qr_svg.py 不在同目录，无法生成",
                         "text/plain; charset=utf-8")
        scale = int(req.arg("scale") or 4)
        try:
            svg = qr_svg.to_svg(text, scale=scale)
        except qr_svg.QrError as exc:
            return Reply(400, "无法生成二维码：%s" % exc, "text/plain; charset=utf-8")
        return Reply(200, svg, "image/svg+xml; charset=utf-8")


# ---------------------------------------------------------------- 路由表
# (方法, 路径, 处理方法名, 权限)。方法名对应 ExtRouter 上的 `h_<名字>`。
ROUTES = (
    # ---- 页面 / 静态资源
    ("GET", "/", "page_index", "open"),
    ("GET", "/index.html", "page_index", "open"),
    ("GET", "/admin", "page_admin", "open"),
    ("GET", "/login", "page_login", "open"),
    ("GET", "/logout", "page_logout", "open"),
    ("GET", "/pay", "page_pay", "open"),
    ("GET", "/bigscreen", "page_bigscreen", "open"),
    ("GET", "/customer", "page_customer", "open"),
    ("GET", "/customer/qr", "page_customer_qr", "open"),
    ("GET", "/manifest.webmanifest", "asset_manifest", "open"),
    ("GET", "/icon.svg", "asset_icon", "open"),
    ("GET", "/sw.js", "asset_sw", "open"),
    ("GET", "/qr-svg", "qr_svg", "open"),

    # ---- 登录 / 用户
    ("POST", "/admin/login", "admin_login", "open"),
    ("GET", "/admin/qr-init", "admin_qr_init", "open"),
    ("GET", "/admin/qr-status", "admin_qr_status", "open"),
    ("GET", "/admin/qr-confirm", "admin_qr_confirm_page", "open"),
    ("POST", "/admin/qr-confirm", "admin_qr_confirm", "open"),
    ("GET", "/admin/qr-do-confirm", "admin_qr_do_confirm", "open"),
    ("GET", "/api/admin/list-users", "list_users", "admin"),
    ("POST", "/api/admin/create-user", "create_user", "admin"),
    ("POST", "/api/admin/delete-user", "delete_user", "admin"),

    # ---- 会员 / RFID
    ("GET", "/api/member/list", "member_list", "read"),
    ("GET", "/api/member/lookup", "member_lookup", "read"),
    ("POST", "/api/member/register", "member_register", "write"),
    ("POST", "/api/member/add-points", "member_add_points", "write"),
    ("POST", "/api/member/delete", "member_delete", "admin"),
    ("POST", "/api/member/bind-rfid", "member_bind_rfid", "write"),
    ("POST", "/api/member/unbind-rfid", "member_unbind_rfid", "write"),
    ("GET", "/api/member/rfid-poll", "rfid_poll", "open"),
    ("GET", "/api/rfid-poll", "rfid_poll", "open"),
    # 从机（ESP32-S3）刷卡上报。原工程没有这条路：主控自己读卡自己写库，
    # 降级成从机后必须走 HTTP，所以补一个接收端点。见 h_rfid_report 的说明。
    ("POST", "/api/rfid/report", "rfid_report", "open"),
    ("POST", "/api/admin/bind-rfid", "admin_bind_rfid", "admin"),
    ("POST", "/api/admin/unbind-rfid", "admin_unbind_rfid", "admin"),
    ("POST", "/api/customer/member-session", "customer_member_session", "open"),

    # ---- 临期改价 / 补货审批
    ("POST", "/api/admin/pricing-proposal", "pricing_proposal", "write"),
    ("POST", "/api/admin/pricing-review", "pricing_review", "admin"),
    ("POST", "/api/admin/pricing-apply", "pricing_apply", "admin"),
    ("POST", "/api/admin/update-price", "update_price", "admin"),
    ("POST", "/api/admin/restock-proposal", "restock_proposal", "write"),
    ("POST", "/api/admin/restock-review", "restock_review", "admin"),
    ("POST", "/api/admin/restock-apply", "restock_apply", "admin"),
    ("GET", "/api/admin/proposals", "list_proposals", "read"),

    # ---- 打印队列
    ("POST", "/api/printer/submit", "printer_submit", "write"),
    ("GET", "/api/printer/job-meta", "printer_job_meta", "device"),
    ("GET", "/api/printer/job", "printer_job", "device"),
    ("POST", "/api/printer/ack", "printer_ack", "device"),

    # ---- 支付
    ("POST", "/api/mobile-pay", "mobile_pay", "write"),
    ("GET", "/api/pay-status", "pay_status", "open"),
    ("POST", "/api/pay-confirm", "pay_confirm", "write"),
    ("GET", "/api/pay-confirm", "pay_confirm", "write"),
    ("GET", "/api/checkout", "checkout_get", "write"),

    # ---- 报表 / 大屏 / 分析
    ("GET", "/api/admin/export-report", "export_report", "admin"),
    ("GET", "/api/admin/analysis", "admin_analysis", "admin"),
    ("GET", "/api/admin/customer-analytics", "customer_analytics", "admin"),
    ("GET", "/api/bigscreen", "bigscreen_data", "open"),
    ("POST", "/api/admin/reset-trend", "reset_trend", "admin"),

    # ---- 工单
    ("POST", "/api/service-request", "service_request_create", "write"),
    ("POST", "/api/service-request/update", "service_request_update", "write"),
    ("GET", "/api/service-request", "service_request_list", "read"),
    ("POST", "/api/customer/service-request", "service_request_create", "open"),

    # ---- 顾客端
    ("GET", "/api/customer/chat", "customer_chat", "open"),
    ("POST", "/api/customer/chat", "customer_chat", "open"),
    ("POST", "/api/customer/session", "customer_session", "open"),
    ("GET", "/api/customer/session", "customer_session", "open"),
    ("POST", "/api/customer/analytics-event", "customer_analytics_event", "open"),
    ("POST", "/api/customer/scan-event", "customer_scan_event", "open"),

    # ---- 配置
    ("GET", "/api/admin/storecfg", "storecfg_get", "admin"),
    ("POST", "/api/admin/storecfg", "storecfg_set", "admin"),

    # ---- 其它服务转发
    ("GET", "/api/cam-status", "cam_status", "open"),
    ("GET", "/api/admin-cam-start", "cam_start", "open"),
    ("GET", "/api/admin-cam-stop", "cam_stop", "open"),
    ("GET", "/api/weight", "weight", "open"),
    ("GET", "/api/voice-subtitles", "voice_subtitles", "open"),
    ("GET", "/api/env", "env", "open"),
    # 从机（ESP32-S3）上报 DHT22 读数。原工程没有这条路 —— 见 h_env_report。
    ("POST", "/api/env/report", "env_report", "open"),

    # ---- 扫码枪 / 订单
    ("GET", "/api/scan-gun-result", "scan_gun_result", "read"),
    ("GET", "/api/scan-gun-clear", "scan_gun_clear", "write"),
    ("GET", "/api/scan-gun", "scan_gun_result", "read"),
    ("GET", "/api/order-history", "order_history", "read"),
    # 原工程这条收的是 orders[] 的数组下标，页面 refundOrder(i) 就是这么传的
    ("POST", "/api/admin/refund", "admin_refund", "admin"),

    # ---- 诊断
    ("GET", "/api/ext/status", "ext_status", "open"),
)

# 带前缀的路由：(前缀, 处理方法名, 权限, 允许的方法)
PREFIX_ROUTES = (
    ("/printjob.bin", "printer_job_bin", "device", ("GET",)),
    ("/printjob.meta", "printer_job_meta", "device", ("GET",)),
    ("/printjob.tmp", "printer_job", "device", ("GET",)),
)

ROUTES_GET = {}
ROUTES_POST = {}
for _method, _path, _handler, _permission in ROUTES:
    _table = ROUTES_GET if _method == "GET" else ROUTES_POST
    if _path in _table:
        raise AssertionError("路由表里 %s %s 重复了" % (_method, _path))
    _table[_path] = (_handler, _permission)
del _method, _path, _handler, _permission, _table


def route_count():
    """给自检用：路由表里到底有多少条。"""
    return len(ROUTES)
