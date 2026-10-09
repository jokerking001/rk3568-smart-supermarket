# -*- coding: utf-8 -*-
"""称重链路的**真端到端**测试：真起 8099 融合服务，模拟从机按节奏喂样本。

从机最核心的功能就是称重。这条链上有个反直觉的设计约束：

    **从机不能只在「读数变化时」上报** —— 融合服务判「样本过期」的阈值是
    1500ms，只在变化时上报的话秤会一直显示过期，等于没接。

这条约束以前只写在文档里，现在变成可执行的断言（见 [5]）。

用法：
    python rk3568/fruit-fusion/test_scale_chain.py
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

PORT = 18099
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


def port_open(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
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


def _json(url, form=None, method="GET"):
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=8) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


def feed(grams, extra=True):
    """**完全按 Slave_Link.cpp 的 body 格式**发一个样本。

    从机发的是 grams / stable / age_ms 三个字段；融合服务只认 grams，
    多出来的字段必须被安静忽略（不能报错）。
    """
    form = {"grams": "%.1f" % grams}
    if extra:
        form["stable"] = "1"
        form["age_ms"] = "0"
    return _json("http://127.0.0.1:%d/api/scale/sample" % PORT, form, "POST")


def status():
    return _json("http://127.0.0.1:%d/api/scale/status" % PORT)


def main():
    work = tempfile.mkdtemp(prefix="scale-chain-")
    log = open(os.path.join(work, "fusion.log"), "wb")
    proc = None
    env = dict(os.environ)
    env.pop("DASHSCOPE_API_KEY", None)
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.Popen(
            [PY, os.path.join(HERE, "fruit_fusion_service.py"),
             "--host", "127.0.0.1", "--port", str(PORT), "--no-forward"],
            cwd=HERE, stdout=log, stderr=subprocess.STDOUT, env=env)
        check_true("8099 融合服务起来了", wait_port(PORT))

        if not port_open(PORT):
            return

        base = "http://127.0.0.1:%d" % PORT

        print("[1] 样本不足时必须是「无效」，不能拿半个数骗人")
        feed(120.0)
        s = status()[1]
        check("1 个样本 -> ok=False", s.get("ok"), False)
        check("1 个样本 -> weight 为 None", s.get("weight_g"), None)

        print("[2] 连续 5 个稳定样本 -> 判稳定（从机 500ms 节奏）")
        for _ in range(4):
            feed(120.0)
            time.sleep(0.12)
        s = status()[1]
        check("样本数", s.get("samples"), 5)
        check("stable=True", s.get("stable"), True)
        check("ok=True（新鲜）", s.get("ok"), True)
        check("重量 120.0g", s.get("weight_g"), 120.0)

        print("[3] 抖动超过 4g -> 判不稳定（原工程判据）")
        for g in (126.0, 118.0, 125.0):
            feed(g)
            time.sleep(0.05)
        s = status()[1]
        check("span>4g -> stable=False", s.get("stable"), False)
        check_true("但样本仍是新鲜的", s.get("ok") is True, str(s))

        print("[4] 多出来的 stable / age_ms 字段被安静忽略（从机就发这三个）")
        st, b = feed(100.0)
        check("带多余字段仍 200", st, 200)
        check("返回 ok", b.get("ok"), True)

        print("[5] ⭐ 关键：从机「只在变化时上报」会怎样")
        time.sleep(1.7)                     # 静默 1.7s，模拟「读数没变就不发」
        s = status()[1]
        check("停报 1.7s -> ok=False（样本过期）", s.get("ok"), False)
        check_true("样本还在（samples 未清零）", (s.get("samples") or 0) > 0, str(s))
        check_true("age_ms 超过 1500", (s.get("age_ms") or 0) > 1500, str(s))
        print("       => 所以从机必须按 ~500ms 持续上报，不能等读数变化。")

        print("[6] 恢复上报 -> 立刻又能用")
        # 缓冲区是**环形**的（原工程 HX711_Scale.cpp 一样，保留最近 8 个），
        # 前面那些 126/118/125/100 还留在窗口里，所以这里喂满 8 个把它冲干净，
        # 顺带验证「上限就是 8」。
        for _ in range(8):
            feed(120.0)
            time.sleep(0.05)
        s = status()[1]
        check("恢复后 ok=True", s.get("ok"), True)
        check("环形缓冲上限 8", s.get("samples"), 8)
        check("窗口全是 120 -> weight 120.0g", s.get("weight_g"), 120.0)

        print("[7] 异常输入要如实报错，不能静默当 0g")
        try:
            _st, b2 = _json(base + "/api/scale/sample", {"grams": "abc"}, "POST")
            check("grams=abc -> ok=False", b2.get("ok"), False)
        except urllib.error.HTTPError as exc:
            check_true("grams=abc 被拒（HTTP %d）" % exc.code, True)

        print("[8] 负值（HX711 读取失败）按原工程语义忽略")
        before = status()[1].get("samples")
        _json(base + "/api/scale/sample", {"grams": "-1"}, "POST")
        after = status()[1].get("samples")
        check("负值不增加样本数", after, before)

    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        log.close()
        text = open(os.path.join(work, "fusion.log"),
                    encoding="utf-8", errors="replace").read()
        if "Traceback" in text:
            print("\n--- fusion.log ---")
            print("\n".join(text.splitlines()[-15:]))
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        sys.exit(1)
    print("✅ %d 项全部通过 —— 称重链路端到端通了" % PASS)


if __name__ == "__main__":
    main()
