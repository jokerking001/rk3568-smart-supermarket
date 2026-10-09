# -*- coding: utf-8 -*-
"""扫码枪链路的**真端到端**测试：真起两个服务、真发 HTTP。

    从机（模拟） --form--> 8095 /api/scanner/inject
                            --JSON--> 8094 /api/scan
                                        --> scan_events 表
                                            --> 页面 GET /api/scan-gun-result

这是原工程「USB 扫码枪 → 页面取码」的等价物。中间任何一跳断掉，
表现都是「扫了码页面没反应」而且不报错，所以必须真跑一遍。

顺带守住两个语义：

  * 扫码**不加购** —— 原工程 `WebServer_SubmitScanGunCode()` 只是把码存进
    「最近扫码结果」等页面来取，加购是页面拿到码之后的事。
  * `add_to_cart` 在 form 编码下必须按真值解析 —— `add_to_cart=0` 传上来是
    字符串 "0"，`bool("0")` 是 True，想关掉反而打开。

用法：
    python rk3568/store-backend/test_scan_chain.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

STORE_PORT = 18094
SCANNER_PORT = 18095
COKE = "6901234567890"          # 真实种子商品：可口可乐 330ml

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


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.2)
    return False


def http(method, url, form=None, timeout=8, cookie=None):
    data = None
    hdrs = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        hdrs["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        raw_cookie = r.headers.get("Set-Cookie")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"_raw": body}
        return r.status, parsed, raw_cookie


def login(base):
    """收银页面是登录态才看得到扫码结果 —— 照做，不然 401 会误判成链路断了。"""
    status, _body, raw = http("POST", base + "/admin/login",
                              {"user": "admin", "pwd": "admin123"})
    if status != 200 or not raw:
        return None
    return raw.split(";", 1)[0]


def main():
    work = tempfile.mkdtemp(prefix="scan-chain-")
    store_log = open(os.path.join(work, "store.log"), "wb")
    scanner_log = open(os.path.join(work, "scanner.log"), "wb")
    procs = []
    env = dict(os.environ)
    env.pop("DASHSCOPE_API_KEY", None)      # 否则 /api/admin/analysis 走 llm 分支
    env["PYTHONIOENCODING"] = "utf-8"
    env["STORE_ADMIN_PASSWORD"] = "admin123"
    env.pop("STORE_DEVICE_TOKEN", None)

    try:
        store_proc = subprocess.Popen(
            [PY, os.path.join(HERE, "store_service.py"),
             "--host", "127.0.0.1", "--port", str(STORE_PORT),
             "--db", os.path.join(work, "s.db"),
             "--receipt-dir", os.path.join(work, "receipts")],
            cwd=HERE, stdout=store_log, stderr=subprocess.STDOUT, env=env)
        procs.append(store_proc)
        check_true("8094 收银后端起来了", wait_port(STORE_PORT), "见下方日志尾部")

        scanner_proc = subprocess.Popen(
            [PY, os.path.join(HERE, "scanner_service.py"),
             "--host", "127.0.0.1", "--port", str(SCANNER_PORT),
             "--store-url", "http://127.0.0.1:%d" % STORE_PORT],
            cwd=HERE, stdout=scanner_log, stderr=subprocess.STDOUT, env=env)
        procs.append(scanner_proc)
        check_true("8095 扫码枪服务起来了", wait_port(SCANNER_PORT), "见下方日志尾部")

        if not (port_open(STORE_PORT) and port_open(SCANNER_PORT)):
            return

        base = "http://127.0.0.1:%d" % STORE_PORT
        scan_base = "http://127.0.0.1:%d" % SCANNER_PORT
        cookie = login(base)
        check_true("收银端登录成功（拿得到会话）", cookie is not None)

        print("[1] 从机发 form 到 8095 /api/scanner/inject（与 Slave_Link.cpp 一致）")
        status, body, _ = http("POST", scan_base + "/api/scanner/inject",
                               {"code": COKE, "source": "usb-host"})
        check("inject 返回 200", status, 200)
        check("inject ok", body.get("ok"), True)
        check_true("inject 解析出商品", body.get("product") is not None, str(body))
        if body.get("product"):
            check("商品名", body["product"].get("name"), "可口可乐 330ml")

        print("[2] 页面侧取码：GET 8094 /api/scan-gun-result")
        status, page, _ = http("GET", base + "/api/scan-gun-result", cookie=cookie)
        check("取码返回 200", status, 200)
        events = page.get("events") or []
        check("页面拿到 1 条扫码事件", len(events), 1)
        if events:
            check("条码正确", events[0].get("code"), COKE)
            check("source 保持原工程口径", events[0].get("source"), "usb-host")
            check("匹配到商品", events[0].get("matched"), 1)

        print("[3] 不该加购（原工程扫码枪只通知、不加购）")
        status, cart, _ = http("GET", base + "/api/cart", cookie=cookie)
        items = cart.get("items") or cart.get("cart") or []
        check("购物车仍为空", len(items), 0)

        print("[4] form 编码下的 add_to_cart 真值语义")
        status, _b2, _ = http("POST", scan_base + "/api/scanner/inject",
                              {"code": COKE, "source": "usb-host",
                               "add_to_cart": "0"})
        check("add_to_cart=0 不报错", status, 200)
        status, cart2, _ = http("GET", base + "/api/cart", cookie=cookie)
        items2 = cart2.get("items") or cart2.get("cart") or []
        check("add_to_cart=0 -> 仍然不加购", len(items2), 0)

        print("[5] add_to_cart=1 应该真加购")
        status, _b3, _ = http("POST", scan_base + "/api/scanner/inject",
                              {"code": COKE, "source": "usb-host",
                               "add_to_cart": "1"})
        check("add_to_cart=1 不报错", status, 200)
        status, cart3, _ = http("GET", base + "/api/cart", cookie=cookie)
        items3 = cart3.get("items") or cart3.get("cart") or []
        check("add_to_cart=1 -> 加购 1 件", len(items3), 1)

        print("[6] 未录入的条码要如实报未匹配，不能静默丢弃")
        status, body4, _ = http("POST", scan_base + "/api/scanner/inject",
                                {"code": "0000000000000", "source": "usb-host"})
        check("未录入条码 ok=False", body4.get("ok"), False)
        check_true("给了原因", "未录入" in (body4.get("message") or ""), str(body4))

        print("[7] 从机写错的旧路径应当 404（防止有人改回去）")
        try:
            status, _, _ = http("POST", scan_base + "/api/scan", {"code": COKE})
            check("8095 /api/scan -> 404", status, 404)
        except urllib.error.HTTPError as exc:
            check("8095 /api/scan -> 404", exc.code, 404)

    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        store_log.close()
        scanner_log.close()
        for name in ("store.log", "scanner.log"):
            path = os.path.join(work, name)
            if os.path.exists(path):
                text = open(path, encoding="utf-8", errors="replace").read()
                if "Traceback" in text or FAIL:
                    print("\n--- %s ---" % name)
                    print("\n".join(text.splitlines()[-15:]))
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        sys.exit(1)
    print("✅ %d 项全部通过 —— 扫码链路端到端通了" % PASS)


if __name__ == "__main__":
    main()
