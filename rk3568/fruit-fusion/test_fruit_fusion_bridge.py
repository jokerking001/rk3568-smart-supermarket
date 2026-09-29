# -*- coding: utf-8 -*-
"""fruit_fusion_bridge 的行为测试。

不依赖 pytest，可直接在板端 Python 3.7 上跑：

    python3 test_fruit_fusion_bridge.py

重点守两件事：
  1. **节奏**：必须按 1400 ms 喂，不能每帧都喂（否则窗口 0.5 秒填满，判定过敏）
  2. **源异常时不能污染窗口**：8089 状态不是 running 时（模型没加载 / 取帧失败）
     要跳过，不能把「空检测」当成「台面上没东西」喂进去
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fruit_fusion as ff
import fruit_fusion_bridge as ffb
import fruit_fusion_service as svc
import vision_observer as vo

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
    else:
        FAILED.append("%s  %s" % (name, detail))


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


LABELS3 = ["apple", "banana", "grapes"]


class FakeFetch(object):
    """按脚本返回 payload；脚本用完就返回最后一个。"""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0]


def _observer():
    """观测器直连融合服务（进程内），避免测试里再起 HTTP。"""
    service = svc.FusionService(
        rules=[ff.FruitRule("apple", "苹果", 180.0, 90.0),
               ff.FruitRule("banana", "香蕉", 140.0, 80.0),
               ff.FruitRule("grapes", "葡萄", 300.0, 220.0)],
        forwarder=_NoopForwarder(), clock=lambda: 300000)
    for index in range(8):
        service.add_scale_sample(180.0, 300000 - (7 - index) * 500)
    observer = vo.VisionObserver(service.engine.labels(),
                                 client=svc.LocalFusionClient(service), window_size=5)
    return service, observer


class _NoopForwarder(object):
    def __init__(self):
        self.base_url = "http://noop"
        self.calls = []

    def post_observe(self, label, confidence, session=svc.DEFAULT_SESSION):
        self.calls.append(label)
        return True, "已加入购物车", "{}"


def _running(detections):
    return {"ok": True, "status": "running", "detections": detections,
            "model": "fruit8_yolo11n_i8.rknn"}


APPLE = [{"class": "apple", "confidence": 0.93, "box": [10, 10, 100, 100]}]


# ── 正常喂入 ────────────────────────────────────────────────────────────

def test_feed_until_window_full():
    service, observer = _observer()
    fetch = FakeFetch([_running(APPLE)])
    bridge = ffb.FruitBridge(observer, fetch=fetch)

    for index in range(4):
        check("第 %d 次轮询窗口未满不提交" % (index + 1),
              bridge.poll_once() is None)
    response = bridge.poll_once()
    check("第 5 次轮询拿到判定", response is not None and response.get("ok") is True,
          str(response))
    check("判定为苹果", response.get("label") == "apple", str(response))
    check("融合置信度 = 0.93×0.78 + 1.0×0.22 = 0.9454",
          approx(response.get("confidence", 0), 0.9454, 1e-3), str(response))
    check("自动确认", response.get("accepted") is True, str(response))
    check("已转发收银", response.get("forwarded") is True, str(response))

    counters = bridge.status()["counters"]
    check("轮询计数 5", counters["polls"] == 5, str(counters))
    check("喂入计数 5", counters["fed"] == 5, str(counters))
    check("提交计数 1", counters["submitted"] == 1, str(counters))
    check("通过计数 1", counters["accepted"] == 1, str(counters))
    check("无错误", counters["errors"] == 0, str(counters))


def test_prob_mode_passed_through():
    service, observer = _observer()
    fetch = FakeFetch([_running([{"class": "apple", "confidence": 0.9},
                                 {"class": "banana", "confidence": 0.9}])])
    bridge = ffb.FruitBridge(observer, fetch=fetch, prob_mode=vo.PROB_MODE_NORMALIZE)
    for _ in range(5):
        response = bridge.poll_once()
    check("normalize 模式传到了观测器",
          approx(response.get("vision", 0), 0.5, 1e-6), str(response))
    check("两类别并列 -> 视觉概率 0.5 < 0.55，不确认",
          response.get("accepted") is False, str(response))


# ── 源异常不能污染窗口 ──────────────────────────────────────────────────

def test_source_not_running_is_skipped():
    service, observer = _observer()
    fetch = FakeFetch([
        {"ok": False, "status": "model_unavailable", "detections": []},
        {"ok": True, "status": "vision_unavailable", "detections": []},
        {"ok": False, "status": "starting", "detections": []},
    ])
    bridge = ffb.FruitBridge(observer, fetch=fetch)

    for _ in range(3):
        check("源非 running 时返回 None", bridge.poll_once() is None)
    check("窗口一点没动（没被空检测污染）", observer.window.count == 0,
          str(observer.window.count))

    counters = bridge.status()["counters"]
    check("记为跳过", counters["skipped"] == 3, str(counters))
    check("跳过不算喂入", counters["fed"] == 0, str(counters))
    check("记下最后一次源状态",
          bridge.status()["last_source_status"] == "starting", str(bridge.status()))


def test_empty_detections_still_fed():
    """running 但没检测到东西 —— 这是「台面上没东西」，要喂 0，不能跳过。"""
    service, observer = _observer()
    fetch = FakeFetch([_running([])])
    bridge = ffb.FruitBridge(observer, fetch=fetch)
    for _ in range(5):
        response = bridge.poll_once()
    check("空检测照常喂入", bridge.status()["counters"]["fed"] == 5,
          str(bridge.status()["counters"]))
    check("空检测 -> 概率和 0 -> 视觉无效", response.get("vision_stable") is False,
          str(response))
    check("空检测 -> 不确认", response.get("accepted") is False, str(response))


def test_fetch_failure():
    def boom():
        raise OSError("connection refused")

    service, observer = _observer()
    bridge = ffb.FruitBridge(observer, fetch=boom)
    check("取数异常不抛出", bridge.poll_once() is None)
    status = bridge.status()
    check("记下错误", "取水果识别结果失败" in status["last_error"], str(status))
    check("异常不计入喂入", status["counters"]["fed"] == 0, str(status))
    check("窗口未受影响", observer.window.count == 0, str(observer.window.count))

    import urllib.error

    def url_boom():
        raise urllib.error.URLError("refused")

    bridge2 = ffb.FruitBridge(observer, fetch=url_boom)
    bridge2.poll_once()
    check("URLError 走专门分支",
          "不可达" in bridge2.status()["last_error"], str(bridge2.status()))


def test_bad_payload_shape():
    service, observer = _observer()
    for payload in ([1, 2, 3], "just a string", None):
        bridge = ffb.FruitBridge(observer, fetch=lambda p=payload: p)
        check("非 dict payload 不抛出 (%r)" % (payload,), bridge.poll_once() is None)
        check("非 dict payload 不喂入 (%r)" % (payload,),
              bridge.status()["counters"]["fed"] == 0)
    check("窗口未受影响", observer.window.count == 0)


# ── 节奏 ────────────────────────────────────────────────────────────────

def test_interval_matches_original():
    """桥接节奏必须等于原工程的 INFERENCE_INTERVAL_MS，别各写一份。"""
    check("POLL_INTERVAL_MS == vision_observer.INFERENCE_INTERVAL_MS",
          ffb.POLL_INTERVAL_MS == vo.INFERENCE_INTERVAL_MS,
          "%s vs %s" % (ffb.POLL_INTERVAL_MS, vo.INFERENCE_INTERVAL_MS))
    check("默认 1400 ms", ffb.POLL_INTERVAL_MS == 1400, str(ffb.POLL_INTERVAL_MS))

    service, observer = _observer()
    bridge = ffb.FruitBridge(observer, fetch=FakeFetch([_running(APPLE)]))
    check("默认间隔取常量", bridge.interval_ms == vo.INFERENCE_INTERVAL_MS,
          str(bridge.interval_ms))
    check("5 帧窗口约 7 秒填满",
          approx(observer.window.size * bridge.interval_ms / 1000.0, 7.0),
          str(observer.window.size * bridge.interval_ms / 1000.0))


def test_run_respects_interval():
    """run() 要按 interval 睡够，且跑满 N 次后能停。"""
    service, observer = _observer()
    fetch = FakeFetch([_running(APPLE)])
    slept = []
    clock_state = {"t": 0.0}

    def fake_clock():
        return clock_state["t"]

    def fake_sleep(seconds):
        slept.append(seconds)
        clock_state["t"] += seconds

    bridge = ffb.FruitBridge(observer, fetch=fetch, sleep=fake_sleep, clock=fake_clock)
    bridge.interval_ms = 1400

    # 手动跑 3 轮，避免依赖线程调度
    for _ in range(3):
        started = fake_clock()
        bridge.poll_once()
        remaining = bridge.interval_ms - (fake_clock() - started) * 1000.0
        if remaining > 0:
            fake_sleep(remaining / 1000.0)

    check("每轮都睡满 1.4 秒", len(slept) == 3 and all(approx(s, 1.4, 1e-6)
                                                      for s in slept), str(slept))
    check("轮询了 3 次", fetch.calls == 3, str(fetch.calls))


def test_thread_start_stop():
    service, observer = _observer()
    bridge = ffb.FruitBridge(observer, fetch=FakeFetch([_running(APPLE)]),
                             sleep=lambda s: None)
    check("未启动时 running=False", bridge.status()["running"] is False)
    bridge.start()
    check("启动后 running=True", bridge.status()["running"] is True)
    bridge.stop()
    check("停止后 running=False", bridge.status()["running"] is False)
    check("重复 stop 不炸", bridge.stop() is None)


# ── 端到端：假 8089 → 桥接 → 真融合服务 ─────────────────────────────────

def test_end_to_end_bridge_to_fusion():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    service = svc.FusionService(forwarder=_NoopForwarder(), clock=lambda: 400000)
    for index in range(8):
        service.add_scale_sample(180.0, 400000 - (7 - index) * 500)

    fusion_server = ThreadingHTTPServer(("127.0.0.1", 0), svc.build_handler(service))
    fusion_url = ("http://127.0.0.1:%d/api/vision-fusion"
                  % fusion_server.server_address[1])
    threading.Thread(target=fusion_server.serve_forever, daemon=True).start()

    # 假的 8089 水果识别服务
    class FruitHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            body = json.dumps(_running(APPLE)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    fruit_server = ThreadingHTTPServer(("127.0.0.1", 0), FruitHandler)
    fruit_url = "http://127.0.0.1:%d/api/fruit/result" % fruit_server.server_address[1]
    threading.Thread(target=fruit_server.serve_forever, daemon=True).start()

    try:
        observer = vo.VisionObserver(service.engine.labels(),
                                     client=vo.FusionClient(fusion_url), window_size=5)
        bridge = ffb.FruitBridge(observer, result_url=fruit_url)
        for _ in range(4):
            check("端到端：预热期不提交", bridge.poll_once() is None)
        response = bridge.poll_once()
        check("端到端：走真实 HTTP 拿到判定",
              response is not None and response.get("accepted") is True, str(response))
        check("端到端：判定为苹果", response.get("label") == "apple", str(response))
        check("端到端：重量 180 g", approx(response.get("weight_g", 0), 180.0, 0.05),
              str(response))
        check("端到端：收银后端被调了一次", len(service.forwarder.calls) == 1,
              str(service.forwarder.calls))
        status = bridge.status()
        check("端到端：源地址记对了", status["source_url"] == fruit_url, str(status))
        check("端到端：窗口满了", status["window"]["count"] == 5, str(status["window"]))
    finally:
        fruit_server.shutdown()
        fruit_server.server_close()
        fusion_server.shutdown()
        fusion_server.server_close()


def test_local_fusion_client_adapter():
    """LocalFusionClient 的返回形状必须和 FusionClient.post 一致。"""
    service = svc.FusionService(forwarder=_NoopForwarder(), clock=lambda: 500000)
    for index in range(8):
        service.add_scale_sample(180.0, 500000 - (7 - index) * 500)
    client = svc.LocalFusionClient(service)
    status, text = client.post([("observation_id", "local-1"),
                                ("sample_count", "5"), ("spread", "0.01"),
                                ("margin", "0.85"), ("apple", "0.93"),
                                ("banana", "0.02"), ("grapes", "0.01")])
    check("状态码 200", status == 200, str(status))
    parsed = json.loads(text)
    check("返回可解析 JSON", isinstance(parsed, dict), text[:200])
    check("判定通过", parsed.get("accepted") is True, str(parsed))
    check("类别正确", parsed.get("label") == "apple", str(parsed))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILED.append("%s 抛异常: %r" % (fn.__name__, exc))
    total = len(PASSED) + len(FAILED)
    for name in FAILED:
        print("FAIL  " + name)
    print("")
    print("%d passed, %d failed (共 %d 项)" % (len(PASSED), len(FAILED), total))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
