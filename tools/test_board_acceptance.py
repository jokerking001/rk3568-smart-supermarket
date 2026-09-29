# -*- coding: utf-8 -*-
"""board_acceptance 的自测 —— 不需要板子。

思路：起一个**假的板端**（本机 HTTP 服务），把 10 个端口里的一个填上，
验证健康检查能准确分辨 UP / DOWN；再用假的 manifest 目录验证哈希核对
能分辨 SAME / DRIFT / MISSING。

用法：
    python tools/test_board_acceptance.py

    # 换临时目录（默认在系统 temp 下）
    BA_TEST_DIR=/e/tmp/ba-test python tools/test_board_acceptance.py

退出码：0 = 全过；1 = 有失败。
"""
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import board_acceptance as ba  # noqa: E402

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:                                  # pragma: no cover
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

PASS = 0
FAIL = 0
FAILURES = []


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        FAILURES.append(label)
        print("  FAIL %s\n         实得 %r\n         期望 %r" % (label, got, want))


def check_true(label, value):
    check(label, bool(value), True)


# --------------------------------------------------------------- 假板端
class FakeBoard(BaseHTTPRequestHandler):
    """只认一个路径，返回一段带诊断字段的状态 JSON。"""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/store/status"):
            body = json.dumps({"ok": True, "model_loaded": False, "fps": 4.6912}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def start_fake_board():
    server = HTTPServer(("127.0.0.1", 0), FakeBoard)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, server.server_address[1], thread


# --------------------------------------------------------------- 纯函数
def test_pure_functions():
    print("\n[1] parse_passed —— 必须认得出真实测试输出，认不出时返回 None")
    check("e2e 格式", ba.parse_passed("== 38 passed, 0 failed =="), 38)
    check("unittest 格式",
          ba.parse_passed("OK\nRan 19 tests in 0.1s\n\n== 19 passed, 0 failed =="), 19)
    check("两位数", ba.parse_passed("== 20 passed, 0 failed =="), 20)
    check("空串 -> None（不是 0）", ba.parse_passed(""), None)
    check("失败输出 -> None", ba.parse_passed("FAILED (errors=2)"), None)

    print("\n[2] short_error —— 拒绝和超时必须分得开")
    check("端口拒绝",
          ba.short_error("<urlopen error [WinError 10061] 由于目标计算机积极拒绝>"),
          "端口未监听（服务没起来）")
    check("超时", ba.short_error("timed out"), "超时（网络不通或被代理绕出去）")
    check("DNS", ba.short_error("getaddrinfo failed"), "域名解析失败")
    check("未知错误原样透出", ba.short_error("weird thing"), "weird thing")

    print("\n[3] summarize —— 只挑诊断字段，且不炸")
    got = ba.summarize('{"ok": true, "model_loaded": false, "fps": 4.6912}')
    check_true("含 model_loaded", "model_loaded=False" in got)
    check_true("含 ok", "ok=True" in got)
    check_true("浮点被截到两位", "fps=4.69" in got)
    check("非 JSON 原样返回", ba.summarize("not json at all"), "not json at all")
    check("空串返回空", ba.summarize(""), "")


# --------------------------------------------------------------- 健康检查
def test_services(port):
    print("\n[4] check_services —— 假板端只有 8094 活着")
    # 把 SERVICES 临时改成「只探一个假端口」，避免真去扫 10 个端口
    original = ba.SERVICES
    ba.SERVICES = [
        (port, "fake-store", ["/api/store/status", "/health"]),
        (port + 1, "fake-dead", ["/api/nope/status", "/health"]),
    ]
    try:
        results, failures = ba.check_services("127.0.0.1", timeout=3)
    finally:
        ba.SERVICES = original

    states = dict((name, state) for _p, name, state, _pa, _s, _d in results)
    check("活着的报 UP", states.get("fake-store"), "UP")
    check("没起的报 DOWN", states.get("fake-dead"), "DOWN")
    check("失败列表只含 DOWN 那个", failures, ["fake-dead (%d)" % (port + 1)])

    detail = dict((name, d) for _p, name, _s, _pa, _st, d in results)
    check_true("UP 的详情里带上了状态字段", "model_loaded" in detail.get("fake-store", ""))

    dead_detail = detail.get("fake-dead", "")
    check_true("DOWN 的原因是「端口未监听」而不是超时",
               "端口未监听" in dead_detail)


def test_tcp_reachable(port):
    print("\n[5] tcp_reachable —— 板子不在线要能立刻判出来")
    check("活着的端口", ba.tcp_reachable("127.0.0.1", port, timeout=3), True)
    check("没人听的端口", ba.tcp_reachable("127.0.0.1", port + 1, timeout=2), False)


# --------------------------------------------------------------- 哈希核对
def test_sync(tmpdir):
    print("\n[6] check_sync —— 缺文件要报 LOCAL_MISSING，不能静默放过")

    # 造一个假的仓库根，manifest 指向它
    fake_repo = os.path.join(tmpdir, "repo")
    os.makedirs(os.path.join(fake_repo, "sub"))
    present = os.path.join(fake_repo, "sub", "present.py")
    with open(present, "w") as fh:
        fh.write("x = 1\n")
    boardgone = os.path.join(fake_repo, "sub", "boardgone.py")
    with open(boardgone, "w") as fh:
        fh.write("y = 1\n")

    import check_board_sync as cbs
    original_repo = ba.REPO
    original_manifest = cbs.MANIFEST

    manifest_path = os.path.join(tmpdir, "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump({"entries": [
            {"local": "sub/present.py", "remote": "/board/present.py", "required": True},
            {"local": "sub/gone.py", "remote": "/board/gone.py", "required": True},
            {"local": "sub/boardgone.py", "remote": "/board/boardgone.py", "required": True},
        ]}, fh)

    ba.REPO = fake_repo
    cbs.MANIFEST = manifest_path

    # probe_board 会去 ssh，这里换成一个假的远端结果
    original_probe = cbs.probe_board
    cbs.probe_board = lambda entries, board, user, key, timeout: {
        "/board/present.py": ("ok", cbs.sha256_of(present)),
        "/board/gone.py": ("ok", "0" * 64),
        "/board/boardgone.py": ("missing", None),
    }
    try:
        rows, failures = ba.check_sync("127.0.0.1", "linaro", "", 3)
    finally:
        ba.REPO = original_repo
        cbs.MANIFEST = original_manifest
        cbs.probe_board = original_probe

    states = dict((r[0], r[1]) for r in rows)
    check("内容一致 -> SAME", states.get("sub/present.py"), "SAME")
    check("本地文件不在 -> LOCAL_MISSING", states.get("sub/gone.py"), "LOCAL_MISSING")
    check("板端文件不在 -> MISSING", states.get("sub/boardgone.py"), "MISSING")
    check("两处必检失败都报出来（本地缺 + 板端缺）", len(failures), 2)

    # 反向断言：内容改一个字，必须变成 DRIFT
    with open(present, "w") as fh:
        fh.write("x = 2\n")
    cbs.probe_board = lambda entries, board, user, key, timeout: {
        "/board/present.py": ("ok", "0" * 64),
        "/board/gone.py": ("ok", "0" * 64),
        "/board/boardgone.py": ("missing", None),
    }
    ba.REPO = fake_repo
    cbs.MANIFEST = manifest_path
    try:
        rows2, _f = ba.check_sync("127.0.0.1", "linaro", "", 3)
    finally:
        ba.REPO = original_repo
        cbs.MANIFEST = original_manifest
        cbs.probe_board = original_probe
    states2 = dict((r[0], r[1]) for r in rows2)
    check("哈希不同 -> DRIFT", states2.get("sub/present.py"), "DRIFT")

    # ssh 挂掉时不能抛出去，要收敛成失败项
    cbs.probe_board = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssh 失败"))
    ba.REPO = fake_repo
    cbs.MANIFEST = manifest_path
    try:
        rows3, failures3 = ba.check_sync("127.0.0.1", "linaro", "", 3)
    finally:
        ba.REPO = original_repo
        cbs.MANIFEST = original_manifest
        cbs.probe_board = original_probe
    check("ssh 失败 -> 空 rows + 一条失败", (rows3, len(failures3)), ([], 1))


def main():
    tmpdir = os.environ.get("BA_TEST_DIR") or tempfile.mkdtemp(prefix="ba-test-")
    if not os.path.isdir(tmpdir):
        os.makedirs(tmpdir)

    server, port, thread = start_fake_board()
    print("假板端起在 127.0.0.1:%d，临时目录 %s" % (port, tmpdir))
    try:
        test_pure_functions()
        test_services(port)
        test_tcp_reachable(port)
        test_sync(tmpdir)
    finally:
        server.shutdown()
        server.server_close()
        if not os.environ.get("BA_TEST_DIR"):
            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        for label in FAILURES:
            print("    - %s" % label)
        return 1
    print("✅ %d 项全部通过 —— 验收脚本本身可信" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
