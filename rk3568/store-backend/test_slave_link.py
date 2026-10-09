# -*- coding: utf-8 -*-
"""从机（ESP32-S3）↔ RK3568 的上报链路契约测试。

不需要板子、不需要 ESP32、不需要起服务 —— 纯内存跑。

**为什么需要这个**：从机上报用的是硬编码 URL，写错了 RK 侧就是 404，
而从机只累加失败计数、不报错 —— 属于最难发现的那类问题。
本测试把「URL 能通」变成可执行断言。

历史上踩过的两个坑（本测试就是防它们复发）：

  * RFID 发到 `/api/rfid-poll` —— 那是**页面轮询用**的 GET 接口，
    语义是「有没有新卡事件」，方向相反；POST 上去必然 404。
  * 条码发到 8095 的 `/api/scan` —— 8095 没有这个端点，
  `/api/scan` 是 8094 收银后端的。

用法：
    python rk3568/store-backend/test_slave_link.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.normpath(os.path.join(HERE, "..", "..",
                                        "firmware", "store-controller"))
sys.path.insert(0, HERE)

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s\n       实际 %r / 期望 %r" % (label, got, want))


def check_true(label, value, hint=""):
    global PASS, FAIL
    if value:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, ("  —— " + hint) if hint else ""))


# ============================================================ 1. 静态：路径对齐
def test_paths_match():
    print("[1] 从机写的路径 vs RK 侧注册的路径")
    cfg = open(os.path.join(FIRMWARE, "Slave_Config.h"),
               encoding="utf-8").read()
    routes = open(os.path.join(HERE, "store_ext_routes.py"),
                  encoding="utf-8").read()
    scanner = open(os.path.join(HERE, "scanner_service.py"),
                   encoding="utf-8").read()

    # 从机宏 -> RK 侧应存在的 (method, path, 在哪个服务里)
    want = {
        "RK_PATH_SCALE_SAMPLE": ("POST", "/api/scale/sample", "fruit_fusion"),
        "RK_PATH_RFID_REPORT": ("POST", "/api/rfid/report", "store_ext"),
        "RK_PATH_SCANNER_INJECT": ("POST", "/api/scanner/inject", "scanner"),
    }
    for macro, (method, path, where) in want.items():
        m = re.search(r'#define\s+%s\s+"([^"]+)"' % macro, cfg)
        check_true("从机定义了 %s" % macro, m is not None)
        if not m:
            continue
        check("%s 的值" % macro, m.group(1), path)
        if where == "store_ext":
            pat = r'\("%s",\s*"%s"' % (method, re.escape(path))
            check_true("8094 路由表里有 %s %s" % (method, path),
                       re.search(pat, routes) is not None)
        elif where == "scanner":
            pat = r'path\s*==\s*"%s"' % re.escape(path)
            check_true("8095 有 %s" % path,
                       re.search(pat, scanner) is not None)
        elif where == "fruit_fusion":
            fusion = os.path.normpath(os.path.join(HERE, "..", "fruit-fusion",
                                                   "fruit_fusion_service.py"))
            text = open(fusion, encoding="utf-8").read()
            check_true("8099 有 %s" % path,
                       re.search(r'path\s*==\s*"%s"' % re.escape(path),
                                 text) is not None)

    # 反向：从机 .cpp 里不应该再出现裸路径（防止有人改回去）
    cpp = open(os.path.join(FIRMWARE, "Slave_Link.cpp"),
               encoding="utf-8").read()
    for bad in ('"/api/rfid-poll"', '"/api/scan"'):
        check_true("Slave_Link.cpp 里已无裸路径 %s" % bad, bad not in cpp,
                   "写错的端点又回来了")
    for macro in want:
        check_true("Slave_Link.cpp 用了 %s" % macro, macro in cpp)


# ============================================================ 2. 动态：真跑一遍
def test_rfid_roundtrip():
    print("[2] RFID 上报 -> 页面轮询 端到端")
    import store_ext as sx
    import store_ext_routes as sr
    import store_service as ss

    d = tempfile.mkdtemp(prefix="slave-link-")
    os.environ["STORE_ADMIN_PASSWORD"] = "admin123"
    os.environ.pop("STORE_DEVICE_TOKEN", None)
    try:
        store = ss.Store(os.path.join(d, "s.db"), os.path.join(d, "receipts"))
        ext = sx.StoreExt(store)
        router = sr.ExtRouter(ext)

        def body_of(reply):
            body = reply.body
            return body.decode("utf-8") if isinstance(body, bytes) else body

        def post(path, params=None):
            return router.handle("POST", path, params or {}, {}, b"", {}, {})

        def get(path, params=None):
            return router.handle("GET", path, params or {}, {}, b"", {}, {})

        # 从机的 httpPostForm 发的是 application/x-www-form-urlencoded，
        # store_service 解析后就是 params —— 这里直接给 params 等价。
        card = "A1B2C3D4"
        r = post("/api/rfid/report", {"uid": card})
        check("上报返回 200", r.status, 200)
        b = json.loads(body_of(r))
        check("上报 ok", b.get("ok"), True)
        check_true("上报回带 id", "id" in b, str(b))

        # 关键：页面轮询能不能看到（页面代码一行没改）
        b2 = json.loads(body_of(get("/api/rfid-poll")))
        check("轮询 count", b2.get("count"), 1)
        check("轮询 card", b2.get("card"), card)
        check("轮询 scene", b2.get("scene"), "slave")

        # 去重：同一张卡再报一次，未消费期间不应产生第二条
        post("/api/rfid/report", {"uid": card})
        b3 = json.loads(body_of(get("/api/rfid-poll")))
        check("重复上报被去重（count 仍为 1）", b3.get("count"), 1)

        # 消费之后再报，应该记新的一条
        b4 = json.loads(body_of(get("/api/rfid-poll")))
        check("消费后队列空", b4.get("count"), 0)
        post("/api/rfid/report", {"uid": card})
        b5 = json.loads(body_of(get("/api/rfid-poll")))
        check("消费后再报 -> 新事件", b5.get("count"), 1)

        # 缺 uid 要报错，不能静默吞掉
        r6 = post("/api/rfid/report", {})
        check("缺 uid -> 400", r6.status, 400)

        # 另一张卡应互不影响
        post("/api/rfid/report", {"uid": "DEADBEEF"})
        b7 = json.loads(body_of(get("/api/rfid-poll")))
        check("第二张卡也能进队列", b7.get("count"), 1)
        check("第二张卡 uid 正确", b7.get("card"), "DEADBEEF")

        # 未登录也能上报（从机没有会话）—— 权限必须是 open
        check_true("未登录可上报（open 权限）", r.status == 200)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 3. 路由表自洽
def test_route_table():
    print("[3] 路由表自洽（新增路由不能撞车）")
    import store_ext_routes as sr
    seen = {}
    dup = []
    for method, path, handler, _perm in sr.ROUTES:
        key = (method, path)
        if key in seen:
            dup.append(key)
        seen[key] = handler
    check("没有重复的 (method, path)", dup, [])
    check("新路由已注册", seen.get(("POST", "/api/rfid/report")), "rfid_report")
    check_true("handler 方法存在", hasattr(sr.ExtRouter, "h_rfid_report"))
    print("     路由总数 %d" % sr.route_count())


# ============================================================ 4. 真值语义
def test_truthy():
    print("[4] form 编码下的开关真值语义")
    import scanner_service as sc
    check('"0" -> False', sc._truthy("0"), False)
    check('"1" -> True', sc._truthy("1"), True)
    check('"false" -> False', sc._truthy("false"), False)
    check('"true" -> True', sc._truthy("true"), True)
    check("None -> False", sc._truthy(None), False)
    check("bool True -> True", sc._truthy(True), True)
    check("0 -> False", sc._truthy(0), False)
    check("1 -> True", sc._truthy(1), True)
    print("     （bool(\"0\") 是 True，所以不能直接 bool()）")


if __name__ == "__main__":
    test_paths_match()
    test_rfid_roundtrip()
    test_route_table()
    test_truthy()
    print()
    print("=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        sys.exit(1)
    print("✅ %d 项全部通过 —— 从机上报链路对得上" % PASS)
