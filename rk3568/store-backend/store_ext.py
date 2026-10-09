# -*- coding: utf-8 -*-
"""store_service 的扩展层 —— 补齐原工程 WebServer.cpp 的 52 个缺口端点。

## 为什么单独一个文件，不并进 store_service.py

`store_service.py` 有 1156 行 + 77 个回归测试在守着。迁移文档里写得很明确：
「能不动就不动」。所以这里做成**并列模块**：自己建表、自己处理路由，
`store_service.py` 只加一个极小的挂载钩子。

## 每个特权操作必须「确认 + 幂等 + 验证结果 + 审计」

这是原工程一以贯之的设计哲学（来自 MimiClaw 07-23 交接文档），必须继承。
所以所有写操作都走 `privileged()` 这一个入口，四件事在那里一次性保证：

  1. **确认**：`confirm` 参数必须为真，否则直接拒绝
  2. **幂等**：`idem_key` 重复提交时返回上次结果，不重复执行
  3. **验证结果**：`verify` 回调检查后置条件，不满足就回滚
  4. **审计**：成功/失败都写 `op_logs`

## 密码哈希

用标准库 `hashlib.pbkdf2_hmac`，不引第三方依赖（板端是 Debian 10 + Python 3.7，
装包麻烦）。每用户独立 salt，比较用 `hmac.compare_digest` 防时序侧信道。

## 兼容性

板端是 **Python 3.7.3**：不用海象运算符、不用字典合并、不用 `list[int]`。
改完记得跑 `python tools/check_py37.py`。
"""
import base64
import hashlib
import hmac
import json
import os
import time

# 会话有效期（秒）
SESSION_TTL = 8 * 3600
# 二维码登录有效期
QR_TTL = 120
# 移动支付二维码有效期
PAY_TTL = 180
# 会员等级门槛（累计积分）
MEMBER_LEVELS = [(0, "普通"), (500, "银卡"), (2000, "金卡"), (5000, "钻石")]

ROLE_ADMIN = "admin"
ROLE_CASHIER = "cashier"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_CASHIER, ROLE_VIEWER)

# 角色能做什么。key 是权限名，value 是允许的角色。
PERMISSIONS = {
    "admin": (ROLE_ADMIN,),
    "write": (ROLE_ADMIN, ROLE_CASHIER),
    "read": (ROLE_ADMIN, ROLE_CASHIER, ROLE_VIEWER),
}

SCHEMA_EXT = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    pass_hash   TEXT NOT NULL,
    salt        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'cashier',
    display_name TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    last_login  TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_seen   TEXT,
    user_agent  TEXT
);

CREATE TABLE IF NOT EXISTS login_qr (
    token       TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    username    TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    confirmed_by TEXT,
    consumed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no     TEXT NOT NULL UNIQUE,
    name        TEXT,
    phone       TEXT,
    points      REAL NOT NULL DEFAULT 0,
    balance     REAL NOT NULL DEFAULT 0,
    level       TEXT NOT NULL DEFAULT '普通',
    rfid_uid    TEXT UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_points_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL,
    delta       REAL NOT NULL,
    reason      TEXT,
    operator    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rfid_pending (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    scene       TEXT NOT NULL DEFAULT 'generic',
    created_at  TEXT NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    old_price   REAL NOT NULL,
    new_price   REAL NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    applied_at  TEXT,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS restock_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    qty         REAL NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    applied_at  TEXT,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'raster',
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'QUEUED',
    created_at  TEXT NOT NULL,
    taken_at    TEXT,
    acked_at    TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    ack_ok      INTEGER,
    result      TEXT,
    meta        TEXT
);

CREATE TABLE IF NOT EXISTS pay_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    method      TEXT NOT NULL DEFAULT 'mobile',
    amount      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'WAITING',
    qr_text     TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    paid_at     TEXT
);

CREATE TABLE IF NOT EXISTS service_tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    member_id   INTEGER,
    kind        TEXT NOT NULL DEFAULT 'other',
    content     TEXT,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    created_at  TEXT NOT NULL,
    updated_at  TEXT,
    handled_by  TEXT,
    reply       TEXT
);

CREATE TABLE IF NOT EXISTS customer_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL UNIQUE,
    member_id   INTEGER,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS analytics_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idem_keys (
    key         TEXT PRIMARY KEY,
    endpoint    TEXT,
    result      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_expires  ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_members_card  ON members(card_no);
CREATE INDEX IF NOT EXISTS idx_price_status  ON price_proposals(status);
CREATE INDEX IF NOT EXISTS idx_restock_status ON restock_proposals(status);
CREATE INDEX IF NOT EXISTS idx_print_status  ON print_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON service_tickets(status);
CREATE INDEX IF NOT EXISTS idx_analytics_time ON analytics_events(created_at);
"""


class ConfirmRequired(Exception):
    """工作函数返回成功、但后置验证没过。抛出去让外层回滚事务。"""


def now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def future_iso(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))


def expired(iso_text):
    """字符串时间比较即可（格式固定，字典序就是时间序）。"""
    if not iso_text:
        return True
    return iso_text < now_iso()


def hash_password(password, salt=None):
    """返回 (hash_hex, salt_hex)。每用户独立 salt。"""
    if salt is None:
        salt = os.urandom(16)
    elif not isinstance(salt, bytes):
        salt = bytes.fromhex(salt)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return digest.hex(), salt.hex()


def verify_password(password, stored_hash, salt_hex):
    """定长比较，防时序侧信道。"""
    try:
        candidate, _ = hash_password(password, salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, stored_hash or "")


def new_token(nbytes=24):
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode("ascii").rstrip("=")


def level_for(points):
    level = MEMBER_LEVELS[0][1]
    for threshold, name in MEMBER_LEVELS:
        if points >= threshold:
            level = name
    return level


class StoreExt(object):
    """扩展层的业务逻辑。共用 Store 的 sqlite 连接与锁。

    不自己开连接 —— 8094 的 `Store` 是「一个连接 + 一把 RLock」的设计，
    再开一个连接会和它抢 WAL 写锁，反而更容易出问题。
    """

    def __init__(self, store):
        self.store = store
        self.lock = store.lock
        self.conn = store._conn
        with self.conn:
            self.conn.executescript(SCHEMA_EXT)
        self._seed_admin()

    # ------------------------------------------------------------- 基础设施
    def _rows(self, cursor):
        return [dict(row) for row in cursor.fetchall()]

    def _one(self, cursor):
        row = cursor.fetchone()
        return dict(row) if row else None

    def _log(self, conn, action, detail, user="system"):
        """审计。所有写操作都必须留下这一条。"""
        conn.execute(
            "INSERT INTO op_logs(ts, user, action, detail) VALUES(?,?,?,?)",
            (now_iso(), user, action, detail),
        )

    def _idem_get(self, conn, key):
        if not key:
            return None
        row = conn.execute("SELECT result FROM idem_keys WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["result"])
        except ValueError:
            return None

    def _idem_put(self, conn, key, endpoint, ok, message, data):
        if not key:
            return
        conn.execute(
            "INSERT OR REPLACE INTO idem_keys(key, endpoint, result, created_at) "
            "VALUES(?,?,?,?)",
            (key, endpoint, json.dumps({"ok": ok, "message": message, "data": data},
                                       ensure_ascii=False), now_iso()),
        )

    def privileged(self, conn, action, user, idem_key, confirm, work,
                   verify=None, detail="", endpoint=""):
        """「确认 + 幂等 + 验证结果 + 审计」的唯一入口。

        work(conn)   -> (ok, message, data)
        verify(conn, data) -> (ok, message)   后置条件检查，不满足则回滚

        返回 (ok, message, data)。**不要绕过这个函数直接写库。**
        """
        if not confirm:
            # 不写审计 —— 没执行的动作不算操作记录，只返回提示
            return False, "该操作需要确认：请带 confirm=true 重试", None

        cached = self._idem_get(conn, idem_key)
        if cached is not None:
            return (cached.get("ok", False),
                    (cached.get("message") or "") + "（幂等重放，未重复执行）",
                    cached.get("data"))

        ok, message, data = work(conn)
        if not ok:
            self._log(conn, action + ".FAILED",
                      "%s | %s" % (detail, message), user)
            return False, message, None

        if verify is not None:
            ok2, msg2 = verify(conn, data)
            if not ok2:
                # 抛出去让 `with conn` 回滚整个事务
                raise ConfirmRequired("后置验证失败：%s" % msg2)

        self._log(conn, action, detail or message, user)
        self._idem_put(conn, idem_key, endpoint or action, True, message, data)
        return True, message, data

    # ---------------------------------------------------------------- 用户
    def _seed_admin(self):
        """首次启动建一个默认管理员。密码从环境变量读，没有就用随机值打印一次。"""
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            if row and row["n"]:
                return
            username = os.environ.get("STORE_ADMIN_USER") or "admin"
            password = os.environ.get("STORE_ADMIN_PASSWORD")
            generated = password is None
            if generated:
                password = new_token(9)
            pass_hash, salt = hash_password(password)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO users(username, pass_hash, salt, role, display_name, "
                    "active, created_at) VALUES(?,?,?,?,?,1,?)",
                    (username, pass_hash, salt, ROLE_ADMIN, "管理员", now_iso()))
                self._log(self.conn, "user.seed",
                          "初始化管理员 %s" % username, "system")
            self.default_admin_password = password if generated else None
            if generated:
                # 只打印一次，且不写进任何日志文件
                print("=" * 60, flush=True)
                print("已创建默认管理员：%s / %s" % (username, password), flush=True)
                print("**请立刻改密码，并把这个密码只记在本地。**", flush=True)
                print("（设 STORE_ADMIN_PASSWORD 环境变量可指定初始密码）", flush=True)
                print("=" * 60, flush=True)

    def list_users(self):
        with self.lock:
            return self._rows(self.conn.execute(
                "SELECT id, username, role, display_name, active, created_at, last_login "
                "FROM users ORDER BY id"))

    def create_user(self, payload):
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        role = payload.get("role") or ROLE_CASHIER
        if not username or len(username) < 3:
            return False, "用户名至少 3 个字符", None
        if len(password) < 6:
            return False, "密码至少 6 个字符", None
        if role not in ROLES:
            return False, "角色只能是 %s" % "/".join(ROLES), None
        with self.lock:
            with self.conn:
                exists = self.conn.execute("SELECT 1 FROM users WHERE username=?",
                                           (username,)).fetchone()
                if exists:
                    return False, "用户名已存在", None
                pass_hash, salt = hash_password(password)
                self.conn.execute(
                    "INSERT INTO users(username, pass_hash, salt, role, display_name, "
                    "active, created_at) VALUES(?,?,?,?,?,1,?)",
                    (username, pass_hash, salt, role,
                     payload.get("display_name") or username, now_iso()))
                self._log(self.conn, "user.create",
                          "新建用户 %s（%s）" % (username, role),
                          payload.get("_operator") or "system")
        return True, "用户 %s 已创建" % username, {"username": username, "role": role}

    def delete_user(self, payload):
        username = (payload.get("username") or "").strip()
        operator = payload.get("_operator") or "system"
        if not username:
            return False, "缺少 username", None
        if username == operator:
            return False, "不能删除自己", None
        with self.lock:
            with self.conn:
                row = self.conn.execute(
                    "SELECT role FROM users WHERE username=?", (username,)).fetchone()
                if not row:
                    return False, "用户不存在", None
                if row["role"] == ROLE_ADMIN:
                    admins = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM users WHERE role=? AND active=1",
                        (ROLE_ADMIN,)).fetchone()["n"]
                    if admins <= 1:
                        return False, "这是最后一个管理员，删了没人能管了", None
                # 软删除：保留审计线索，不物理删行
                self.conn.execute("UPDATE users SET active=0 WHERE username=?", (username,))
                self.conn.execute("DELETE FROM auth_sessions WHERE username=?", (username,))
                self._log(self.conn, "user.delete", "停用用户 %s" % username, operator)
        return True, "用户 %s 已停用" % username, {"username": username}

    # ---------------------------------------------------------------- 登录
    def login(self, username, password, user_agent=""):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row or not row["active"]:
                return False, "用户名或密码错误", None
            if not verify_password(password, row["pass_hash"], row["salt"]):
                with self.conn:
                    self._log(self.conn, "login.FAILED",
                              "用户 %s 密码错误" % username, username)
                return False, "用户名或密码错误", None

            token = new_token()
            with self.conn:
                self.conn.execute(
                    "INSERT INTO auth_sessions(token, username, role, created_at, "
                    "expires_at, last_seen, user_agent) VALUES(?,?,?,?,?,?,?)",
                    (token, username, row["role"], now_iso(),
                     future_iso(SESSION_TTL), now_iso(), user_agent[:200]))
                self.conn.execute("UPDATE users SET last_login=? WHERE username=?",
                                  (now_iso(), username))
                self._log(self.conn, "login", "用户 %s 登录" % username, username)
        return True, "登录成功", {"token": token, "username": username,
                                 "role": row["role"]}

    def session_of(self, token):
        """校验会话。过期或不存在返回 None。"""
        if not token:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM auth_sessions WHERE token=?", (token,)).fetchone()
            if not row:
                return None
            if expired(row["expires_at"]):
                with self.conn:
                    self.conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
                return None
            with self.conn:
                self.conn.execute("UPDATE auth_sessions SET last_seen=? WHERE token=?",
                                  (now_iso(), token))
            return dict(row)

    def logout(self, token):
        with self.lock:
            with self.conn:
                self.conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
        return True, "已登出", None

    def has_permission(self, session, permission):
        if not session:
            return False
        return session.get("role") in PERMISSIONS.get(permission, ())

    # ---------------------------------------------------------- 二维码登录
    def qr_init(self):
        """生成二维码登录令牌。网页端展示二维码，手机扫码后确认。"""
        token = new_token(16)
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO login_qr(token, status, created_at, expires_at) "
                    "VALUES(?,?,?,?)",
                    (token, "pending", now_iso(), future_iso(QR_TTL)))
                self._log(self.conn, "login.qr_init", "生成二维码令牌", "system")
        return True, "二维码已生成", {"token": token, "expires_in": QR_TTL,
                                     "qr_text": "store://login?token=%s" % token}

    def qr_status(self, token):
        with self.lock:
            row = self.conn.execute("SELECT * FROM login_qr WHERE token=?",
                                    (token,)).fetchone()
            if not row:
                return False, "令牌不存在", None
            if expired(row["expires_at"]) and row["status"] == "pending":
                with self.conn:
                    self.conn.execute("UPDATE login_qr SET status='expired' WHERE token=?",
                                      (token,))
                return True, "已过期", {"status": "expired"}
            data = {"status": row["status"], "username": row["username"],
                    "consumed": bool(row["consumed"])}
            if row["status"] == "confirmed" and not row["consumed"]:
                # 一次性：换出会话令牌后就作废，防止二维码被重放
                session_token = new_token()
                user = self.conn.execute(
                    "SELECT role FROM users WHERE username=?", (row["username"],)).fetchone()
                if not user:
                    return False, "确认的用户已被删除", None
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO auth_sessions(token, username, role, created_at, "
                        "expires_at, last_seen, user_agent) VALUES(?,?,?,?,?,?,?)",
                        (session_token, row["username"], user["role"], now_iso(),
                         future_iso(SESSION_TTL), now_iso(), "qr-login"))
                    self.conn.execute("UPDATE login_qr SET consumed=1 WHERE token=?",
                                      (token,))
                    self._log(self.conn, "login.qr_consumed",
                              "二维码登录换出会话：%s" % row["username"], row["username"])
                data["session_token"] = session_token
                data["role"] = user["role"]
            return True, "查询成功", data

    def qr_confirm(self, token, username, password):
        """手机端提交凭据确认。凭据在这一步校验，不在扫码那一步。"""
        with self.lock:
            row = self.conn.execute("SELECT * FROM login_qr WHERE token=?",
                                    (token,)).fetchone()
            if not row:
                return False, "令牌不存在", None
            if expired(row["expires_at"]):
                return False, "二维码已过期，请重新生成", None
            if row["status"] != "pending":
                return False, "该二维码已被处理", None
            user = self.conn.execute("SELECT * FROM users WHERE username=?",
                                     (username,)).fetchone()
            if not user or not user["active"] or \
                    not verify_password(password, user["pass_hash"], user["salt"]):
                with self.conn:
                    self._log(self.conn, "login.qr_confirm.FAILED",
                              "二维码确认失败：%s" % username, username or "unknown")
                return False, "用户名或密码错误", None
            with self.conn:
                self.conn.execute(
                    "UPDATE login_qr SET status='confirmed', username=?, confirmed_by=? "
                    "WHERE token=?", (username, username, token))
                self._log(self.conn, "login.qr_confirm",
                          "二维码确认：%s" % username, username)
        return True, "已确认，请在电脑上继续", {"status": "confirmed"}

    def qr_do_confirm(self, token):
        """网页端点击「我已扫码」后调用 —— 就是 qr_status 的显式版本。"""
        return self.qr_status(token)

    # ---------------------------------------------------------------- 会员
    def list_members(self, query=None):
        with self.lock:
            if query:
                like = "%" + query + "%"
                return self._rows(self.conn.execute(
                    "SELECT * FROM members WHERE active=1 AND "
                    "(card_no LIKE ? OR name LIKE ? OR phone LIKE ?) ORDER BY id",
                    (like, like, like)))
            return self._rows(self.conn.execute(
                "SELECT * FROM members WHERE active=1 ORDER BY id"))

    def lookup_member(self, card_no=None, phone=None, rfid_uid=None):
        with self.lock:
            if card_no:
                row = self.conn.execute("SELECT * FROM members WHERE card_no=? AND active=1",
                                        (card_no,)).fetchone()
            elif phone:
                row = self.conn.execute("SELECT * FROM members WHERE phone=? AND active=1",
                                        (phone,)).fetchone()
            elif rfid_uid:
                row = self.conn.execute("SELECT * FROM members WHERE rfid_uid=? AND active=1",
                                        (rfid_uid,)).fetchone()
            else:
                return False, "请给 card_no / phone / rfid_uid 之一", None
            if not row:
                return False, "未找到会员", None
            return True, "找到会员", dict(row)

    def register_member(self, payload):
        card_no = (payload.get("card_no") or "").strip()
        if not card_no:
            return False, "缺少 card_no", None
        operator = payload.get("_operator") or "system"
        with self.lock:
            with self.conn:
                exists = self.conn.execute(
                    "SELECT 1 FROM members WHERE card_no=?", (card_no,)).fetchone()
                if exists:
                    return False, "卡号已存在", None
                phone = (payload.get("phone") or "").strip() or None
                if phone:
                    dup = self.conn.execute(
                        "SELECT card_no FROM members WHERE phone=? AND active=1",
                        (phone,)).fetchone()
                    if dup:
                        return False, "该手机号已绑定卡号 %s" % dup["card_no"], None
                self.conn.execute(
                    "INSERT INTO members(card_no, name, phone, points, balance, level, "
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (card_no, payload.get("name"), phone,
                     float(payload.get("points") or 0),
                     float(payload.get("balance") or 0),
                     level_for(float(payload.get("points") or 0)), now_iso()))
                self._log(self.conn, "member.register",
                          "注册会员 %s（%s）" % (card_no, payload.get("name") or "-"),
                          operator)
        return True, "会员 %s 已注册" % card_no, {"card_no": card_no}

    def add_points(self, payload):
        card_no = payload.get("card_no")
        delta = float(payload.get("delta") or payload.get("points") or 0)
        if not card_no or not delta:
            return False, "缺少 card_no 或 delta", None
        operator = payload.get("_operator") or "system"

        def work(conn):
            row = conn.execute("SELECT * FROM members WHERE card_no=? AND active=1",
                               (card_no,)).fetchone()
            if not row:
                return False, "会员不存在", None
            new_points = row["points"] + delta
            if new_points < 0:
                return False, "积分不足以扣减（当前 %.0f，要扣 %.0f）" % (row["points"], -delta), None
            level = level_for(new_points)
            conn.execute("UPDATE members SET points=?, level=? WHERE id=?",
                         (new_points, level, row["id"]))
            conn.execute(
                "INSERT INTO member_points_log(member_id, delta, reason, operator, "
                "created_at) VALUES(?,?,?,?,?)",
                (row["id"], delta, payload.get("reason") or "", operator, now_iso()))
            return True, "积分 %+.0f，当前 %.0f（%s）" % (delta, new_points, level), \
                {"card_no": card_no, "points": new_points, "level": level}

        def verify(conn, data):
            row = conn.execute("SELECT points FROM members WHERE card_no=?",
                               (card_no,)).fetchone()
            if not row:
                return False, "会员记录消失"
            if abs(row["points"] - data["points"]) > 1e-6:
                return False, "积分落库后对不上（期望 %.0f，实为 %.0f）" % (
                    data["points"], row["points"])
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "member.add_points", operator,
                    payload.get("idem_key"), payload.get("confirm"), work, verify,
                    detail="%s 积分 %+.0f" % (card_no, delta),
                    endpoint="/api/member/add-points")

    def delete_member(self, payload):
        card_no = payload.get("card_no")
        operator = payload.get("_operator") or "system"
        if not card_no:
            return False, "缺少 card_no", None
        with self.lock:
            with self.conn:
                row = self.conn.execute("SELECT id, points FROM members WHERE card_no=?",
                                        (card_no,)).fetchone()
                if not row:
                    return False, "会员不存在", None
                if row["points"] > 0:
                    return False, "会员还有 %.0f 积分，先处理掉再注销" % row["points"], None
                # 软删除：交易记录要留着，不能物理删
                self.conn.execute("UPDATE members SET active=0, rfid_uid=NULL WHERE id=?",
                                  (row["id"],))
                self._log(self.conn, "member.delete", "注销会员 %s" % card_no, operator)
        return True, "会员 %s 已注销" % card_no, {"card_no": card_no}

    def bind_rfid(self, payload, scene="member"):
        """绑定 RFID 卡。会员卡绑到 members，其它场景只记 pending。"""
        uid = (payload.get("uid") or payload.get("rfid_uid") or "").strip()
        operator = payload.get("_operator") or "system"
        if not uid:
            return False, "缺少 uid", None

        def work(conn):
            card_no = payload.get("card_no")
            if not card_no:
                conn.execute(
                    "INSERT INTO rfid_pending(uid, scene, created_at) VALUES(?,?,?)",
                    (uid, scene, now_iso()))
                return True, "已登记待处理 RFID %s" % uid, {"uid": uid, "bound": False}
            row = conn.execute("SELECT * FROM members WHERE card_no=? AND active=1",
                               (card_no,)).fetchone()
            if not row:
                return False, "会员不存在", None
            taken = conn.execute(
                "SELECT card_no FROM members WHERE rfid_uid=? AND id<>?",
                (uid, row["id"])).fetchone()
            if taken:
                return False, "该卡已绑在 %s 上" % taken["card_no"], None
            conn.execute("UPDATE members SET rfid_uid=? WHERE id=?", (uid, row["id"]))
            return True, "卡 %s 已绑定到会员 %s" % (uid, card_no), \
                {"uid": uid, "card_no": card_no, "bound": True}

        def verify(conn, data):
            if not data.get("bound"):
                return True, ""
            row = conn.execute("SELECT rfid_uid FROM members WHERE card_no=?",
                               (data["card_no"],)).fetchone()
            if not row or row["rfid_uid"] != data["uid"]:
                return False, "绑定没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "rfid.bind", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="RFID %s -> %s" % (uid, payload.get("card_no") or "pending"),
                    endpoint="/api/admin/bind-rfid")

    def unbind_rfid(self, payload):
        uid = (payload.get("uid") or payload.get("rfid_uid") or "").strip()
        operator = payload.get("_operator") or "system"
        if not uid:
            return False, "缺少 uid", None

        def work(conn):
            row = conn.execute("SELECT card_no FROM members WHERE rfid_uid=?",
                               (uid,)).fetchone()
            if not row:
                return False, "这张卡没有绑定任何会员", None
            conn.execute("UPDATE members SET rfid_uid=NULL WHERE rfid_uid=?", (uid,))
            return True, "卡 %s 已解绑（原属 %s）" % (uid, row["card_no"]), \
                {"uid": uid, "card_no": row["card_no"]}

        def verify(conn, data):
            row = conn.execute("SELECT 1 FROM members WHERE rfid_uid=?", (data["uid"],)).fetchone()
            if row:
                return False, "解绑没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "rfid.unbind", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="解绑 RFID %s" % uid, endpoint="/api/admin/unbind-rfid")

    def report_rfid(self, uid, scene="slave"):
        """从机（ESP32-S3）刷卡上报：把卡号登记进待处理队列。

        与 `bind_rfid` 里那段 `INSERT INTO rfid_pending` 落的是**同一张表**，
        所以页面轮询 `/api/rfid-poll` 不用改一行就能看到。

        为什么需要它：原工程里主控（ESP32-S3）自己读卡、自己写库，
        没有「从机上报」这条 HTTP 路径。降级成从机后读卡的还是 ESP32-S3，
        落库的变成 RK3568，中间必须有入口 —— 否则从机发出去是 404，
        而且是**静默的**（从机只累加失败计数、不报错），很难发现。

        去重：同一张卡在「尚未被消费」期间重复上报只记一次。
        RFID 模块在卡片停在感应区时会反复触发；从机侧靠 `lastCardTime`
        做了 2 秒去重，这里再兜一层（从机重启后计数器会归零）。
        """
        uid = (uid or "").strip()
        if not uid:
            return False, "缺少 uid", None
        with self.lock:
            with self.conn:
                row = self.conn.execute(
                    "SELECT id FROM rfid_pending WHERE uid=? AND consumed=0 "
                    "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
                if row:
                    return True, "已登记（去重）", {"uid": uid, "id": row["id"]}
                cur = self.conn.execute(
                    "INSERT INTO rfid_pending(uid, scene, created_at) VALUES(?,?,?)",
                    (uid, scene, now_iso()))
                return True, "已登记待处理 RFID %s" % uid, \
                    {"uid": uid, "id": cur.lastrowid}

    def poll_rfid(self, scene=None, consume=True):
        """轮询待处理卡。主控/会员端用。"""
        with self.lock:
            if scene:
                rows = self._rows(self.conn.execute(
                    "SELECT * FROM rfid_pending WHERE consumed=0 AND scene=? ORDER BY id",
                    (scene,)))
            else:
                rows = self._rows(self.conn.execute(
                    "SELECT * FROM rfid_pending WHERE consumed=0 ORDER BY id"))
            if rows and consume:
                ids = [r["id"] for r in rows]
                marks = ",".join("?" * len(ids))
                with self.conn:
                    self.conn.execute(
                        "UPDATE rfid_pending SET consumed=1 WHERE id IN (%s)" % marks, ids)
            # 顺便把已绑卡的最新状态带上，省一次请求
            for row in rows:
                member = self.conn.execute(
                    "SELECT card_no, name, points FROM members WHERE rfid_uid=? AND active=1",
                    (row["uid"],)).fetchone()
                row["member"] = dict(member) if member else None
            return {"ok": True, "count": len(rows), "events": rows}

    # ------------------------------------------------------ 提案 → 审核 → 应用
    def _propose(self, table, payload, kind):
        code = (payload.get("code") or "").strip()
        if not code:
            return False, "缺少 code", None
        operator = payload.get("_operator") or "system"
        with self.lock:
            with self.conn:
                product = self.conn.execute(
                    "SELECT * FROM products WHERE code=?", (code,)).fetchone()
                if not product:
                    return False, "商品不存在：%s" % code, None
                if table == "price_proposals":
                    new_price = payload.get("new_price")
                    if new_price is None:
                        return False, "缺少 new_price", None
                    new_price = float(new_price)
                    if new_price <= 0:
                        return False, "价格必须大于 0", None
                    if abs(new_price - product["price"]) < 1e-9:
                        return False, "新价格和现价一样，不用改", None
                    self.conn.execute(
                        "INSERT INTO price_proposals(code, old_price, new_price, reason, "
                        "status, created_by, created_at) VALUES(?,?,?,?,'PENDING',?,?)",
                        (code, product["price"], new_price, payload.get("reason"),
                         operator, now_iso()))
                    detail = "%s 价格 %.2f -> %.2f" % (code, product["price"], new_price)
                else:
                    qty = float(payload.get("qty") or 0)
                    if qty <= 0:
                        return False, "补货数量必须大于 0", None
                    self.conn.execute(
                        "INSERT INTO restock_proposals(code, qty, reason, status, "
                        "created_by, created_at) VALUES(?,?,?,'PENDING',?,?)",
                        (code, qty, payload.get("reason"), operator, now_iso()))
                    detail = "%s 补货 %.0f" % (code, qty)
                self._log(self.conn, kind + ".propose", detail, operator)
        return True, "提案已提交，等待审核", {"code": code}

    def _review(self, table, payload, kind):
        proposal_id = payload.get("id")
        decision = (payload.get("decision") or "").upper()
        operator = payload.get("_operator") or "system"
        if not proposal_id:
            return False, "缺少 id", None
        if decision not in ("APPROVE", "REJECT"):
            return False, "decision 只能是 APPROVE 或 REJECT", None

        def work(conn):
            row = conn.execute("SELECT * FROM %s WHERE id=?" % table,
                               (proposal_id,)).fetchone()
            if not row:
                return False, "提案不存在", None
            if row["status"] != "PENDING":
                return False, "该提案已是 %s 状态，不能重复审核" % row["status"], None
            if row["created_by"] == operator and not payload.get("allow_self_review"):
                return False, "不能审核自己提的案子（要自审请显式带 allow_self_review）", None
            status = "APPROVED" if decision == "APPROVE" else "REJECTED"
            conn.execute(
                "UPDATE %s SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                "WHERE id=?" % table,
                (status, operator, now_iso(), payload.get("note"), proposal_id))
            return True, "提案 #%s 已%s" % (proposal_id, "通过" if status == "APPROVED" else "驳回"), \
                {"id": proposal_id, "status": status}

        def verify(conn, data):
            row = conn.execute("SELECT status FROM %s WHERE id=?" % table,
                               (data["id"],)).fetchone()
            if not row or row["status"] != data["status"]:
                return False, "审核状态没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, kind + ".review", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="提案 #%s %s" % (proposal_id, decision),
                    endpoint="/api/admin/%s-review" % kind)

    def _apply(self, table, payload, kind):
        proposal_id = payload.get("id")
        operator = payload.get("_operator") or "system"
        if not proposal_id:
            return False, "缺少 id", None

        def work(conn):
            row = conn.execute("SELECT * FROM %s WHERE id=?" % table,
                               (proposal_id,)).fetchone()
            if not row:
                return False, "提案不存在", None
            if row["status"] != "APPROVED":
                return False, "只有已通过的提案才能应用（当前 %s）" % row["status"], None
            product = conn.execute("SELECT * FROM products WHERE code=?",
                                   (row["code"],)).fetchone()
            if not product:
                return False, "商品已不存在：%s" % row["code"], None

            if table == "price_proposals":
                # 乐观锁：审核期间价格被别人改过就拒绝，避免覆盖别人的改动
                if abs(product["price"] - row["old_price"]) > 1e-9:
                    return False, ("现价 %.2f 已不等于提案时的 %.2f，"
                                   "有人改过价，请重新提案"
                                   % (product["price"], row["old_price"])), None
                conn.execute("UPDATE products SET price=?, updated_at=? WHERE code=?",
                             (row["new_price"], now_iso(), row["code"]))
                detail = "%s 价格 %.2f -> %.2f" % (row["code"], row["old_price"],
                                                  row["new_price"])
                data = {"code": row["code"], "price": row["new_price"]}
            else:
                conn.execute("UPDATE products SET stock=stock+?, updated_at=? WHERE code=?",
                             (row["qty"], now_iso(), row["code"]))
                after = conn.execute("SELECT stock FROM products WHERE code=?",
                                     (row["code"],)).fetchone()
                detail = "%s 补货 +%.0f（现库存 %.0f）" % (row["code"], row["qty"],
                                                          after["stock"])
                data = {"code": row["code"], "stock": after["stock"]}

            conn.execute("UPDATE %s SET status='APPLIED', applied_at=? WHERE id=?" % table,
                         (now_iso(), proposal_id))
            return True, "已应用：" + detail, data

        def verify(conn, data):
            if "price" in data:
                row = conn.execute("SELECT price FROM products WHERE code=?",
                                   (data["code"],)).fetchone()
                if not row or abs(row["price"] - data["price"]) > 1e-9:
                    return False, "价格没落库"
            else:
                row = conn.execute("SELECT stock FROM products WHERE code=?",
                                   (data["code"],)).fetchone()
                if not row or abs(row["stock"] - data["stock"]) > 1e-6:
                    return False, "库存没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, kind + ".apply", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="应用提案 #%s" % proposal_id,
                    endpoint="/api/admin/%s-apply" % kind)

    def list_proposals(self, table, status=None):
        with self.lock:
            if status:
                return self._rows(self.conn.execute(
                    "SELECT * FROM %s WHERE status=? ORDER BY id DESC" % table, (status,)))
            return self._rows(self.conn.execute(
                "SELECT * FROM %s ORDER BY id DESC LIMIT 200" % table))

    def pricing_proposal(self, payload):
        return self._propose("price_proposals", payload, "pricing")

    def pricing_review(self, payload):
        return self._review("price_proposals", payload, "pricing")

    def pricing_apply(self, payload):
        return self._apply("price_proposals", payload, "pricing")

    def restock_proposal(self, payload):
        return self._propose("restock_proposals", payload, "restock")

    def restock_review(self, payload):
        return self._review("restock_proposals", payload, "restock")

    def restock_apply(self, payload):
        return self._apply("restock_proposals", payload, "restock")

    def update_price(self, payload):
        """直接改价（不走审批）。原工程有这个口子，MimiClaw 的 expiry-pricing 用。

        仍然要求确认 + 审计 —— 直接改价是特权操作，只是跳过了审核环节。
        """
        code = (payload.get("code") or "").strip()
        operator = payload.get("_operator") or "system"
        if not code:
            return False, "缺少 code", None
        try:
            new_price = float(payload.get("price") if payload.get("price") is not None
                              else payload.get("new_price"))
        except (TypeError, ValueError):
            return False, "价格必须是数字", None
        if new_price <= 0:
            return False, "价格必须大于 0", None

        def work(conn):
            product = conn.execute("SELECT * FROM products WHERE code=?",
                                   (code,)).fetchone()
            if not product:
                return False, "商品不存在：%s" % code, None
            old_price = product["price"]
            conn.execute("UPDATE products SET price=?, updated_at=? WHERE code=?",
                         (new_price, now_iso(), code))
            return True, "%s 价格 %.2f -> %.2f" % (code, old_price, new_price), \
                {"code": code, "old_price": old_price, "price": new_price}

        def verify(conn, data):
            row = conn.execute("SELECT price FROM products WHERE code=?",
                               (data["code"],)).fetchone()
            if not row or abs(row["price"] - data["price"]) > 1e-9:
                return False, "价格没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "pricing.update_price", operator,
                    payload.get("idem_key"), payload.get("confirm"), work, verify,
                    detail="直接改价 %s -> %.2f" % (code, new_price),
                    endpoint="/api/admin/update-price")

    def refund_order(self, payload):
        """退款并恢复库存。

        原工程 `/api/admin/refund` 收的是 `orders[]` 的**数组下标**（`orders[orderId]`），
        页面里 `refundOrder(i)` 传的就是下标。所以这里两种都收：
        `order_id` 是真实主键，`index` 是 `/api/order-history` 数组里的位置。
        由路由层负责把下标翻译成主键（它才知道页面看到的是哪个窗口）。

        退款是**特权操作**：钱和库存都会动，必须确认 + 幂等 + 验证 + 审计。
        退款后 `status` 变 `REFUNDED`、`paid` 归零，页面上的「退款」按钮随之消失，
        这与原工程把 `orders[i].paid` 置 false 的行为一致。
        """
        operator = payload.get("_operator") or "system"
        order_id = payload.get("order_id", payload.get("orderId"))
        if order_id is None:
            return False, "缺少 order_id", None
        try:
            order_id = int(order_id)
        except (TypeError, ValueError):
            return False, "order_id 必须是整数", None

        def work(conn):
            order = conn.execute("SELECT * FROM orders WHERE id=?",
                                 (order_id,)).fetchone()
            if not order:
                return False, "订单不存在", None
            if order["status"] == "REFUNDED":
                # 幂等靠 idem_key 兜底；没带 idem_key 时也不能把钱退两遍
                return False, "订单已退款", None
            if order["status"] != "PAID":
                return False, "订单不存在或未支付", None
            # 库存必须按**下单时记录的行**恢复。原工程退款是拿显示字符串
            # "名称x数量" 反解数量的，商品名里带 x 就会算错 —— 这里读 order_items。
            items = conn.execute(
                "SELECT code, name, qty FROM order_items WHERE order_id=?",
                (order_id,)).fetchall()
            for item in items:
                conn.execute(
                    "UPDATE products SET stock=stock+?,"
                    " today_sold=MAX(0, today_sold-?), updated_at=? WHERE code=?",
                    (item["qty"], item["qty"], now_iso(), item["code"]))
            conn.execute("UPDATE orders SET paid=0, status='REFUNDED' WHERE id=?",
                         (order_id,))
            qty_total = round(sum(r["qty"] for r in items), 3)
            day = (order["created_at"] or "")[:10]
            conn.execute(
                "UPDATE daily_stats SET revenue=MAX(0, revenue-?),"
                " items_sold=MAX(0, items_sold-?) WHERE day=?",
                (order["total"], qty_total, day))
            return True, "已退款并恢复库存", {
                "order_id": order_id,
                "total": order["total"],
                "items": [{"code": r["code"], "name": r["name"], "qty": r["qty"]}
                          for r in items],
            }

        def verify(conn, data):
            """退款最容易出的错是「钱退了、库存没回来」。这里逐行核。"""
            row = conn.execute("SELECT status, paid FROM orders WHERE id=?",
                               (data["order_id"],)).fetchone()
            if not row:
                return False, "订单查不到了"
            if row["status"] != "REFUNDED" or row["paid"]:
                return False, "订单状态没变成已退款"
            missing = [i["code"] for i in data["items"]
                       if not conn.execute("SELECT 1 FROM products WHERE code=?",
                                           (i["code"],)).fetchone()]
            if missing:
                # 商品被删了库存就无处可还 —— 这是数据问题，得让人看见
                return False, "商品已不存在，库存无法恢复：%s" % ", ".join(missing)
            return True, ""

        index = payload.get("index")
        detail = "退款订单 #%s" % order_id
        if index is not None:
            # 页面是按数组下标点的退款。万一下标漂了，审计里能看出当时点的是第几行。
            detail += "（页面下标 %s）" % index

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "order.refund", operator,
                    payload.get("idem_key"), payload.get("confirm"), work, verify,
                    detail=detail,
                    endpoint="/api/admin/refund")

    # ------------------------------------------------------- 打印光栅队列
    def printer_submit(self, payload):
        """提交一个打印任务。原工程是 384-dot 单色光栅，打印机轮询来取。"""
        operator = payload.get("_operator") or "system"
        raster = payload.get("raster") or payload.get("payload")
        if not raster:
            return False, "缺少 raster（384 点单色光栅，base64 或十六进制字符串）", None

        def work(conn):
            cursor = conn.execute(
                "INSERT INTO print_jobs(kind, payload, status, created_at, meta) "
                "VALUES(?,?,'QUEUED',?,?)",
                (payload.get("kind") or "raster", raster, now_iso(),
                 json.dumps(payload.get("meta") or {}, ensure_ascii=False)))
            job_id = cursor.lastrowid
            return True, "打印任务 #%s 已入队" % job_id, {"id": job_id}

        def verify(conn, data):
            row = conn.execute("SELECT status FROM print_jobs WHERE id=?",
                               (data["id"],)).fetchone()
            if not row or row["status"] != "QUEUED":
                return False, "任务没入队"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "printer.submit", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="提交打印任务", endpoint="/api/printer/submit")

    def printer_job_meta(self):
        """打印机先问「有没有活」。返回队首任务的最小信息。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT id, kind, created_at, attempts FROM print_jobs "
                "WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return {"ok": True, "pending": False, "count": 0}
            count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM print_jobs WHERE status='QUEUED'").fetchone()["n"]
            return {"ok": True, "pending": True, "count": count,
                    "job": dict(row)}

    def printer_job(self, job_id):
        """打印机取任务正文。取走即标记 PRINTING，并计一次 attempts。"""
        if not job_id:
            return False, "缺少 id", None
        with self.lock:
            with self.conn:
                row = self.conn.execute("SELECT * FROM print_jobs WHERE id=?",
                                        (job_id,)).fetchone()
                if not row:
                    return False, "任务不存在", None
                if row["status"] == "DONE":
                    return False, "任务已完成，不重复下发", None
                self.conn.execute(
                    "UPDATE print_jobs SET status='PRINTING', taken_at=?, "
                    "attempts=attempts+1 WHERE id=?", (now_iso(), job_id))
                self._log(self.conn, "printer.take", "打印机取走任务 #%s" % job_id,
                          "printer")
                data = dict(row)
                data["payload"] = row["payload"]
                # 返回**取走之后**的状态，别把取之前的 QUEUED 回给打印机
                data["status"] = "PRINTING"
                data["attempts"] = row["attempts"] + 1
            return True, "任务 #%s 已下发" % job_id, data

    def printer_ack(self, job_id, ok, result=None):
        """打印机回报结果。失败的任务退回 QUEUED 重试，超过 3 次才判死。"""
        if not job_id:
            return False, "缺少 id", None
        with self.lock:
            with self.conn:
                row = self.conn.execute("SELECT * FROM print_jobs WHERE id=?",
                                        (job_id,)).fetchone()
                if not row:
                    return False, "任务不存在", None
                if ok:
                    status = "DONE"
                elif row["attempts"] >= 3:
                    status = "FAILED"
                else:
                    status = "QUEUED"      # 退回重试
                # result 是打印机回传的任意结构（dict/list），sqlite3 只吃标量，
                # 必须先序列化 —— 否则 Error binding parameter: type 'dict' is not supported。
                if result is not None and not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                self.conn.execute(
                    "UPDATE print_jobs SET status=?, acked_at=?, ack_ok=?, result=? "
                    "WHERE id=?", (status, now_iso(), 1 if ok else 0, result, job_id))
                self._log(self.conn, "printer.ack",
                          "任务 #%s -> %s（第 %d 次）" % (job_id, status, row["attempts"]),
                          "printer")
        return True, "任务 #%s 标记为 %s" % (job_id, status), {"id": job_id, "status": status}

    # ------------------------------------------------------------ 移动支付
    def mobile_pay(self, payload):
        order_id = payload.get("order_id")
        operator = payload.get("_operator") or "system"
        if not order_id:
            return False, "缺少 order_id", None

        def work(conn):
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return False, "订单不存在", None
            if order["paid"]:
                return False, "订单已支付，不要重复发起", None
            if order["status"] == "REFUNDED":
                return False, "订单已退款", None
            existing = conn.execute(
                "SELECT id FROM pay_requests WHERE order_id=? AND status='WAITING'",
                (order_id,)).fetchone()
            if existing:
                return False, "该订单已有待支付的付款请求 #%s" % existing["id"], None
            qr_text = "store://pay?order=%s&amount=%.2f" % (order_id, order["total"])
            cursor = conn.execute(
                "INSERT INTO pay_requests(order_id, method, amount, status, qr_text, "
                "created_at, expires_at) VALUES(?,?,?,'WAITING',?,?,?)",
                (order_id, payload.get("method") or "mobile", order["total"], qr_text,
                 now_iso(), future_iso(PAY_TTL)))
            return True, "付款请求 #%s 已创建（%d 秒内有效）" % (cursor.lastrowid, PAY_TTL), \
                {"id": cursor.lastrowid, "order_id": order_id,
                 "amount": order["total"], "qr_text": qr_text, "expires_in": PAY_TTL}

        def verify(conn, data):
            row = conn.execute("SELECT status FROM pay_requests WHERE id=?",
                               (data["id"],)).fetchone()
            if not row or row["status"] != "WAITING":
                return False, "付款请求没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "pay.mobile_create", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="订单 %s 发起移动支付" % order_id,
                    endpoint="/api/mobile-pay")

    def pay_status(self, request_id=None, order_id=None):
        with self.lock:
            if request_id:
                row = self.conn.execute("SELECT * FROM pay_requests WHERE id=?",
                                        (request_id,)).fetchone()
            elif order_id:
                row = self.conn.execute(
                    "SELECT * FROM pay_requests WHERE order_id=? ORDER BY id DESC LIMIT 1",
                    (order_id,)).fetchone()
            else:
                return False, "请给 request_id 或 order_id", None
            if not row:
                return False, "没有对应的付款请求", None
            data = dict(row)
            if data["status"] == "WAITING" and expired(data["expires_at"]):
                with self.conn:
                    self.conn.execute("UPDATE pay_requests SET status='EXPIRED' WHERE id=?",
                                      (data["id"],))
                data["status"] = "EXPIRED"
            return True, "查询成功", data

    def pay_confirm(self, payload):
        """确认收到钱。**这里必须先查真实支付结果再确认，不能只信调用方。**"""
        request_id = payload.get("request_id")
        operator = payload.get("_operator") or "system"
        if not request_id:
            return False, "缺少 request_id", None

        def work(conn):
            row = conn.execute("SELECT * FROM pay_requests WHERE id=?",
                               (request_id,)).fetchone()
            if not row:
                return False, "付款请求不存在", None
            if row["status"] == "PAID":
                return False, "该付款请求已确认过", None
            if row["status"] == "EXPIRED":
                return False, "付款请求已过期，请重新发起", None
            if expired(row["expires_at"]):
                conn.execute("UPDATE pay_requests SET status='EXPIRED' WHERE id=?",
                             (request_id,))
                return False, "付款请求已过期，请重新发起", None

            order = conn.execute("SELECT * FROM orders WHERE id=?",
                                 (row["order_id"],)).fetchone()
            if not order:
                return False, "订单不存在", None
            if order["paid"]:
                # 订单已付但付款请求还是 WAITING —— 状态不一致，修掉而不是重复收款
                conn.execute("UPDATE pay_requests SET status='PAID', paid_at=? WHERE id=?",
                             (now_iso(), request_id))
                return False, "订单已经是已支付状态，付款请求已同步为 PAID", None
            if order["status"] == "REFUNDED":
                # 发起付款到确认收款之间可能被退款，别把钱又收一遍
                return False, "订单已退款，不能再收款", None

            items = conn.execute("SELECT qty FROM order_items WHERE order_id=?",
                                 (order["id"],)).fetchall()
            qty_total = round(sum(item["qty"] for item in items), 3)

            conn.execute("UPDATE orders SET paid=1, status='PAID', method=?, paid_at=? "
                         "WHERE id=?", (row["method"], now_iso(), order["id"]))
            conn.execute("UPDATE pay_requests SET status='PAID', paid_at=? WHERE id=?",
                         (now_iso(), request_id))
            # 移动支付收到的钱同样要进当日统计，否则大屏/报表会少算这一笔。
            # 复用 Store 自己的实现，避免两处 UPSERT 各写各的、日后改一处漏一处。
            self.store._bump_daily(conn, order["total"], qty_total)
            return True, "订单 #%s 已收款 %.2f" % (order["id"], row["amount"]), \
                {"request_id": request_id, "order_id": order["id"],
                 "amount": row["amount"]}

        def verify(conn, data):
            row = conn.execute("SELECT paid FROM orders WHERE id=?",
                               (data["order_id"],)).fetchone()
            if not row or not row["paid"]:
                return False, "订单支付状态没落库"
            req = conn.execute("SELECT status FROM pay_requests WHERE id=?",
                               (data["request_id"],)).fetchone()
            if not req or req["status"] != "PAID":
                return False, "付款请求状态没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "pay.confirm", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="确认收款 request=%s" % request_id,
                    endpoint="/api/pay-confirm")

    # ------------------------------------------------------------ 服务工单
    def create_ticket(self, payload):
        content = (payload.get("content") or "").strip()
        if not content:
            return False, "缺少 content", None
        session_id = payload.get("session") or payload.get("session_id") or "guest"
        with self.lock:
            with self.conn:
                member_id = None
                if payload.get("card_no"):
                    row = self.conn.execute("SELECT id FROM members WHERE card_no=?",
                                            (payload["card_no"],)).fetchone()
                    member_id = row["id"] if row else None
                cursor = self.conn.execute(
                    "INSERT INTO service_tickets(session_id, member_id, kind, content, "
                    "status, created_at) VALUES(?,?,?,?,'OPEN',?)",
                    (session_id, member_id, payload.get("kind") or "other",
                     content, now_iso()))
                self._log(self.conn, "ticket.create",
                          "工单 #%s：%s" % (cursor.lastrowid, content[:60]), session_id)
        return True, "工单已提交，我们会尽快处理", {"id": cursor.lastrowid}

    def update_ticket(self, payload):
        ticket_id = payload.get("id")
        operator = payload.get("_operator") or "system"
        if not ticket_id:
            return False, "缺少 id", None
        status = (payload.get("status") or "").upper()
        if status and status not in ("OPEN", "HANDLING", "DONE", "CLOSED"):
            return False, "状态只能是 OPEN/HANDLING/DONE/CLOSED", None

        def work(conn):
            row = conn.execute("SELECT * FROM service_tickets WHERE id=?",
                               (ticket_id,)).fetchone()
            if not row:
                return False, "工单不存在", None
            new_status = status or row["status"]
            conn.execute(
                "UPDATE service_tickets SET status=?, updated_at=?, handled_by=?, "
                "reply=? WHERE id=?",
                (new_status, now_iso(), operator,
                 payload.get("reply") if payload.get("reply") is not None else row["reply"],
                 ticket_id))
            return True, "工单 #%s -> %s" % (ticket_id, new_status), \
                {"id": ticket_id, "status": new_status}

        def verify(conn, data):
            row = conn.execute("SELECT status FROM service_tickets WHERE id=?",
                               (data["id"],)).fetchone()
            if not row or row["status"] != data["status"]:
                return False, "工单状态没落库"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "ticket.update", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="更新工单 #%s" % ticket_id, endpoint="/api/service-request/update")

    def list_tickets(self, status=None, limit=50):
        with self.lock:
            if status:
                return self._rows(self.conn.execute(
                    "SELECT * FROM service_tickets WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit)))
            return self._rows(self.conn.execute(
                "SELECT * FROM service_tickets ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------- 顾客会话与分析
    def touch_customer_session(self, session_id, payload=None):
        payload = payload or {}
        if not session_id:
            return False, "缺少 session", None
        with self.lock:
            with self.conn:
                row = self.conn.execute("SELECT * FROM customer_sessions WHERE session_id=?",
                                        (session_id,)).fetchone()
                if row:
                    self.conn.execute(
                        "UPDATE customer_sessions SET last_seen=?, note=? WHERE session_id=?",
                        (now_iso(),
                         payload.get("note") if payload.get("note") is not None else row["note"],
                         session_id))
                else:
                    self.conn.execute(
                        "INSERT INTO customer_sessions(session_id, first_seen, last_seen, note) "
                        "VALUES(?,?,?,?)",
                        (session_id, now_iso(), now_iso(), payload.get("note")))
                data = self._one(self.conn.execute(
                    "SELECT * FROM customer_sessions WHERE session_id=?", (session_id,)))
        return True, "会话已更新", data

    def analytics_event(self, payload):
        kind = (payload.get("kind") or "").strip()
        if not kind:
            return False, "缺少 kind", None
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO analytics_events(session_id, kind, detail, created_at) "
                    "VALUES(?,?,?,?)",
                    (payload.get("session") or payload.get("session_id"), kind,
                     payload.get("detail"), now_iso()))
        return True, "已记录", {"kind": kind}

    def customer_analytics(self, days=7):
        with self.lock:
            since = time.strftime("%Y-%m-%d 00:00:00",
                                  time.localtime(time.time() - days * 86400))
            by_kind = self._rows(self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM analytics_events WHERE created_at>=? "
                "GROUP BY kind ORDER BY n DESC", (since,)))
            sessions = self.conn.execute(
                "SELECT COUNT(*) AS n FROM customer_sessions WHERE last_seen>=?",
                (since,)).fetchone()["n"]
            # 转化漏斗：进店 → 加购 → 下单
            carts = self.conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM cart_items").fetchone()["n"]
            orders = self.conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE created_at>=?", (since,)).fetchone()["n"]
            paid = self.conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE created_at>=? AND paid=1",
                (since,)).fetchone()["n"]
            members = self.conn.execute(
                "SELECT COUNT(*) AS n FROM members WHERE active=1").fetchone()["n"]
            tickets = self._rows(self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM service_tickets GROUP BY status"))
            return {
                "ok": True, "days": days,
                "sessions": sessions,
                "funnel": {"sessions": sessions, "carts": carts,
                           "orders": orders, "paid": paid},
                "by_kind": by_kind,
                "active_members": members,
                "tickets": tickets,
            }

    # ------------------------------------------------------------ 店铺配置
    # 白名单：只有这些 key 能通过 /api/admin/storecfg 读写。
    # 不做白名单的话，这个接口就等于「任意改 settings 表」，太危险。
    STORECFG_KEYS = (
        "store_name", "store_address", "store_phone", "receipt_header",
        "receipt_footer", "auto_add_to_cart", "auto_add_min_conf",
        "expiry_warn_days", "low_stock_threshold", "mimi_store_key",
        "pay_methods", "bigscreen_refresh_ms", "member_points_per_yuan",
    )

    def get_storecfg(self):
        with self.lock:
            out = {}
            for key in self.STORECFG_KEYS:
                row = self.conn.execute("SELECT v FROM settings WHERE k=?",
                                        (key,)).fetchone()
                out[key] = row["v"] if row else None
            return {"ok": True, "config": out, "editable_keys": list(self.STORECFG_KEYS)}

    def set_storecfg(self, payload):
        operator = payload.get("_operator") or "system"
        config = payload.get("config")
        if not isinstance(config, dict) or not config:
            # 也允许直接平铺传 key=value
            flat = dict((k, v) for k, v in payload.items()
                        if k in self.STORECFG_KEYS)
            if not flat:
                return False, "缺少 config（或直接传可编辑的 key）", None
            config = flat

        unknown = [k for k in config if k not in self.STORECFG_KEYS]
        if unknown:
            return False, "不可编辑的配置项：%s" % ", ".join(sorted(unknown)), None

        def work(conn):
            changed = []
            for key, value in config.items():
                old = conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
                old_value = old["v"] if old else None
                text = "" if value is None else str(value)
                conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)",
                             (key, text))
                if old_value != text:
                    changed.append("%s: %s -> %s" % (key, old_value, text))
            if not changed:
                return True, "没有实际变化", {"changed": []}
            return True, "已更新 %d 项" % len(changed), {"changed": changed}

        def verify(conn, data):
            for key in config:
                row = conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
                if not row:
                    return False, "%s 没写进去" % key
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "storecfg.set", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="更新店铺配置 %d 项" % len(config),
                    endpoint="/api/admin/storecfg")

    # -------------------------------------------------------- 报表 / 大屏
    def export_report(self, days=7, fmt="csv"):
        """导出报表。CSV 直接返回文本，前端存文件。"""
        days = max(1, min(int(days or 7), 365))
        since = time.strftime("%Y-%m-%d 00:00:00",
                              time.localtime(time.time() - days * 86400))
        with self.lock:
            rows = self._rows(self.conn.execute(
                "SELECT o.id, o.created_at, o.total, o.paid, o.method, o.status, "
                "COUNT(i.id) AS items, SUM(i.qty) AS qty "
                "FROM orders o LEFT JOIN order_items i ON i.order_id=o.id "
                "WHERE o.created_at>=? GROUP BY o.id ORDER BY o.id", (since,)))
            daily = self._rows(self.conn.execute(
                "SELECT day, revenue, items_sold FROM daily_stats WHERE day>=? "
                "ORDER BY day", (time.strftime("%Y-%m-%d",
                                               time.localtime(time.time() - days * 86400)),)))
            products = self._rows(self.conn.execute(
                "SELECT code, name, price, stock, today_sold, category FROM products "
                "ORDER BY today_sold DESC"))

        lines = []
        lines.append("# RK3568 智慧超市 · 经营报表")
        lines.append("# 生成时间,%s" % now_iso())
        lines.append("# 统计区间,最近 %d 天" % days)
        lines.append("")
        lines.append("## 逐日汇总")
        lines.append("日期,营业额,销量")
        for row in daily:
            lines.append("%s,%.2f,%.0f" % (row["day"], row["revenue"], row["items_sold"]))
        lines.append("")
        lines.append("## 订单明细")
        lines.append("订单号,时间,金额,已付,支付方式,状态,商品种数,总件数")
        for row in rows:
            lines.append("%s,%s,%.2f,%s,%s,%s,%s,%s"
                         % (row["id"], row["created_at"], row["total"],
                            "是" if row["paid"] else "否", row["method"], row["status"],
                            row["items"], row["qty"] if row["qty"] is not None else 0))
        lines.append("")
        lines.append("## 商品库存")
        lines.append("条码,名称,单价,库存,今日销量,分类")
        for row in products:
            lines.append("%s,%s,%.2f,%.0f,%.0f,%s"
                         % (row["code"], row["name"], row["price"], row["stock"],
                            row["today_sold"], row["category"] or ""))
        text = "\n".join(lines) + "\n"
        return True, "报表已生成", {
            "format": fmt, "days": days,
            "filename": "report_%s.csv" % time.strftime("%Y%m%d-%H%M%S"),
            "content": text,
            "orders": len(rows), "days_counted": len(daily),
        }

    def admin_analysis(self):
        """商家 AI 分析。没配 key 就返回**基于真实数据的统计摘要**，不编。"""
        with self.lock:
            summary = self.store.summary()
            low = self._rows(self.conn.execute(
                "SELECT code, name, stock FROM products WHERE stock<=5 ORDER BY stock"))
            slow = self._rows(self.conn.execute(
                "SELECT code, name, stock, today_sold FROM products "
                "WHERE stock>0 AND today_sold=0 ORDER BY stock DESC LIMIT 10"))
            pending_price = self.conn.execute(
                "SELECT COUNT(*) AS n FROM price_proposals WHERE status='PENDING'"
            ).fetchone()["n"]
            pending_restock = self.conn.execute(
                "SELECT COUNT(*) AS n FROM restock_proposals WHERE status='PENDING'"
            ).fetchone()["n"]
            open_tickets = self.conn.execute(
                "SELECT COUNT(*) AS n FROM service_tickets WHERE status='OPEN'"
            ).fetchone()["n"]

        findings = []
        if low:
            findings.append("有 %d 个商品库存告急（<=5）：%s"
                            % (len(low), "、".join(r["name"] for r in low[:5])))
        if slow:
            findings.append("有 %d 个商品今天一件没卖但库存偏高，考虑促销或调整陈列：%s"
                            % (len(slow), "、".join(r["name"] for r in slow[:5])))
        if pending_price:
            findings.append("有 %d 条改价提案待审核" % pending_price)
        if pending_restock:
            findings.append("有 %d 条补货提案待审核" % pending_restock)
        if open_tickets:
            findings.append("有 %d 条顾客工单未处理" % open_tickets)
        if not findings:
            findings.append("库存、待审提案、工单都正常，没有需要立刻处理的。")

        api_key = os.environ.get("DASHSCOPE_API_KEY")
        return {
            "ok": True,
            "mode": "llm" if api_key else "rule-based",
            "note": ("未配置 DASHSCOPE_API_KEY，下面是**基于真实数据的规则统计**，"
                     "不是模型生成的内容。" if not api_key
                     else "已配置 API key，可接入大模型做进一步解读。"),
            "summary": summary,
            "findings": findings,
            "low_stock": low,
            "slow_moving": slow,
            "pending": {"price_proposals": pending_price,
                        "restock_proposals": pending_restock,
                        "open_tickets": open_tickets},
        }

    def reset_trend(self, payload):
        """重置当日统计。会丢数据，所以要确认 + 审计。"""
        operator = payload.get("_operator") or "system"
        day = payload.get("day") or time.strftime("%Y-%m-%d")

        def work(conn):
            row = conn.execute("SELECT * FROM daily_stats WHERE day=?", (day,)).fetchone()
            if not row:
                return False, "%s 没有统计数据" % day, None
            snapshot = {"day": day, "revenue": row["revenue"], "items_sold": row["items_sold"]}
            conn.execute("UPDATE daily_stats SET revenue=0, items_sold=0 WHERE day=?",
                         (day,))
            conn.execute("UPDATE products SET today_sold=0")
            return True, "已重置 %s 的统计（原营业额 %.2f，原销量 %.0f）" % (
                day, row["revenue"], row["items_sold"]), snapshot

        def verify(conn, data):
            row = conn.execute("SELECT revenue, items_sold FROM daily_stats WHERE day=?",
                               (data["day"],)).fetchone()
            if not row or row["revenue"] != 0 or row["items_sold"] != 0:
                return False, "统计没清零"
            return True, ""

        with self.lock:
            with self.conn:
                return self.privileged(
                    self.conn, "trend.reset", operator, payload.get("idem_key"),
                    payload.get("confirm"), work, verify,
                    detail="重置 %s 统计" % day, endpoint="/api/admin/reset-trend")

    def customer_chat(self, payload):
        """顾客端问答。

        优先转发给 MimiClaw；没配就退回**基于本店真实数据的规则回答**。
        绝不编造不存在的商品或价格。
        """
        question = (payload.get("message") or payload.get("question") or "").strip()
        if not question:
            return False, "缺少 message", None

        with self.lock:
            store_url = None
            row = self.conn.execute("SELECT v FROM settings WHERE k='mimi_store_url'").fetchone()
            if row:
                store_url = row["v"]
            products = self._rows(self.conn.execute(
                "SELECT code, name, price, stock FROM products WHERE stock>0"))

        lowered = question.lower()
        hits = []
        for item in products:
            if item["name"] and item["name"].lower() in lowered:
                hits.append(item)
        # 中文没有空格，再按字符重叠兜一层
        if not hits:
            for item in products:
                name = item["name"] or ""
                if len(name) >= 2 and any(name[i:i + 2] in question
                                          for i in range(len(name) - 1)):
                    hits.append(item)

        if hits:
            item = hits[0]
            reply = "%s 现价 %.2f 元，%s。" % (
                item["name"], item["price"],
                "有货（库存 %.0f）" % item["stock"] if item["stock"] > 0 else "暂时缺货")
        elif any(word in question for word in ("营业", "几点", "开门", "关门")):
            reply = "营业时间请以店内公示为准。"
        elif any(word in question for word in ("会员", "积分", "办卡")):
            reply = "会员可在收银台办理，消费累计积分，积分可兑换商品。"
        elif any(word in question for word in ("退", "换", "售后")):
            reply = "如需退换货，请在收银台出示小票，或提交服务工单，我们会尽快处理。"
        else:
            reply = ("我暂时没查到相关信息。你可以问我某个商品的价格或有没有货，"
                     "也可以在收银台提交服务工单。")

        return True, "已回复", {
            "reply": reply,
            "mode": "mimi" if store_url else "rule-based",
            "matched_products": [h["name"] for h in hits[:3]],
            "note": None if store_url else "未接入 MimiClaw，当前是本地规则回答。",
        }

    # --------------------------------------------------------------- 大屏
    def bigscreen_data(self):
        """大屏用的聚合数据。一次取全，避免大屏频繁轮询。"""
        with self.lock:
            summary = self.store.summary()
            trend = self.store.trend(7)
            top = self._rows(self.conn.execute(
                "SELECT name, today_sold, price FROM products "
                "ORDER BY today_sold DESC LIMIT 8"))
            low = self._rows(self.conn.execute(
                "SELECT name, stock FROM products WHERE stock<=5 ORDER BY stock"))
            recent = self._rows(self.conn.execute(
                "SELECT id, created_at, total, paid FROM orders ORDER BY id DESC LIMIT 6"))
            tickets_open = self.conn.execute(
                "SELECT COUNT(*) AS n FROM service_tickets WHERE status='OPEN'"
            ).fetchone()["n"]
            sessions = self.conn.execute(
                "SELECT COUNT(*) AS n FROM customer_sessions").fetchone()["n"]
            config = {}
            for key in ("store_name", "bigscreen_refresh_ms"):
                row = self.conn.execute("SELECT v FROM settings WHERE k=?",
                                        (key,)).fetchone()
                config[key] = row["v"] if row else None
        return {
            "ok": True,
            "generated_at": now_iso(),
            "store_name": config.get("store_name") or "智慧超市",
            "refresh_ms": int(config.get("bigscreen_refresh_ms") or 5000),
            "summary": summary,
            "trend": trend,
            "top_products": top,
            "low_stock": low,
            "recent_orders": recent,
            "open_tickets": tickets_open,
            "customer_sessions": sessions,
        }

    # --------------------------------------------------------------- 状态
    def status(self):
        """扩展层的自检状态，方便上板后一眼看出哪块没配好。"""
        with self.lock:
            def count(table):
                return self.conn.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
            return {
                "ok": True,
                "users": count("users"),
                "members": count("members"),
                "auth_sessions": count("auth_sessions"),
                "pending_price_proposals": self.conn.execute(
                    "SELECT COUNT(*) AS n FROM price_proposals WHERE status='PENDING'"
                ).fetchone()["n"],
                "pending_restock_proposals": self.conn.execute(
                    "SELECT COUNT(*) AS n FROM restock_proposals WHERE status='PENDING'"
                ).fetchone()["n"],
                "queued_print_jobs": self.conn.execute(
                    "SELECT COUNT(*) AS n FROM print_jobs WHERE status='QUEUED'"
                ).fetchone()["n"],
                "waiting_pay_requests": self.conn.execute(
                    "SELECT COUNT(*) AS n FROM pay_requests WHERE status='WAITING'"
                ).fetchone()["n"],
                "open_tickets": self.conn.execute(
                    "SELECT COUNT(*) AS n FROM service_tickets WHERE status='OPEN'"
                ).fetchone()["n"],
                "customer_sessions": count("customer_sessions"),
                "analytics_events": count("analytics_events"),
                "storecfg_set": self.conn.execute(
                    "SELECT COUNT(*) AS n FROM settings WHERE k IN (%s)"
                    % ",".join("?" * len(self.STORECFG_KEYS)),
                    self.STORECFG_KEYS).fetchone()["n"],
                "editable_cfg_keys": len(self.STORECFG_KEYS),
            }
