# -*- coding: utf-8 -*-
"""fruit_fusion_service 的行为测试。

不依赖 pytest，可直接在板端 Python 3.7 上跑：

    python3 test_fruit_fusion_service.py

其中 `test_three_hop_end_to_end` 会把三跳全串起来真跑一遍：

    vision_observer  --HTTP-->  融合服务(8099)  --HTTP-->  假的收银后端
"""

import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fruit_fusion as ff
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


class FakeForwarder(object):
    """假装是 8094 收银后端。"""

    def __init__(self, ok=True, message="已加入购物车"):
        self.base_url = "http://fake-store:8094"
        self.ok = ok
        self.message = message
        self.calls = []

    def post_observe(self, label, confidence, session=svc.DEFAULT_SESSION):
        self.calls.append({"label": label, "confidence": confidence, "session": session})
        return self.ok, self.message, "{}"


def _service(grams=180.0, samples=8, forwarder=None, clock=None):
    service = svc.FusionService(
        rules=[ff.FruitRule("apple", "苹果", 180.0, 90.0),
               ff.FruitRule("banana", "香蕉", 140.0, 80.0),
               ff.FruitRule("grapes", "葡萄", 300.0, 220.0)],
        forwarder=forwarder if forwarder is not None else FakeForwarder(),
        clock=clock or (lambda: 100000))
    for index in range(samples):
        service.add_scale_sample(grams, 100000 - (samples - 1 - index) * 500)
    return service


def _observation(observation_id, apple=0.90, banana=0.05, grapes=0.02,
                 sample_count=5, spread=0.01, margin=0.85):
    return {
        "observation_id": observation_id,
        "captured_ms": "12345",
        "sample_count": str(sample_count),
        "spread": "%.5f" % spread,
        "margin": "%.5f" % margin,
        "apple": "%.5f" % apple,
        "banana": "%.5f" % banana,
        "grapes": "%.5f" % grapes,
    }


# ── 判定 + 转发 ─────────────────────────────────────────────────────────

def test_accept_and_forward():
    forwarder = FakeForwarder()
    service = _service(forwarder=forwarder)
    response = service.observe(_observation("boot-1"))
    check("稳定苹果 180g -> 自动确认", response.get("accepted") is True, str(response))
    check("判定为苹果", response.get("label") == "apple", str(response))
    check("融合置信度 0.922", approx(response.get("confidence", 0), 0.922, 1e-3),
          str(response))
    check("已转发收银后端", response.get("forwarded") is True, str(response))
    check("转发只发生一次", len(forwarder.calls) == 1, str(forwarder.calls))
    check("转发带上 label/confidence",
          forwarder.calls[0]["label"] == "apple"
          and approx(forwarder.calls[0]["confidence"], 0.922, 1e-3),
          str(forwarder.calls[0]))


def test_idempotent_no_double_add():
    """同一个 observation_id 重放不能重复加购 —— 跨服务边界的去重。"""
    forwarder = FakeForwarder()
    service = _service(forwarder=forwarder)
    first = service.observe(_observation("boot-7"))
    second = service.observe(_observation("boot-7"))
    check("首次已转发", first.get("forwarded") is True)
    check("重放不重复转发", len(forwarder.calls) == 1, str(forwarder.calls))
    check("重放标记 duplicate", second.get("duplicate") is True, str(second))
    check("重放结果与首次一致",
          second.get("label") == first.get("label")
          and second.get("confidence") == first.get("confidence"), str(second))

    third = service.observe(_observation("boot-8"))
    check("换 observation_id 会再次转发", len(forwarder.calls) == 2, str(forwarder.calls))
    check("新 id 不算 duplicate", third.get("duplicate") is False, str(third))


def test_rejections_do_not_forward():
    forwarder = FakeForwarder()

    # 视觉无效：spread 超标
    service = _service(forwarder=forwarder)
    response = service.observe(_observation("r-1", spread=0.40))
    check("spread 超标 -> 不确认", response.get("accepted") is False, str(response))
    check("不确认就不转发", len(forwarder.calls) == 0 and response.get("forwarded") is False)

    # 视觉无效：margin 太小
    response = service.observe(_observation("r-2", apple=0.40, banana=0.38, margin=0.02))
    check("margin 太小 -> 不确认", response.get("accepted") is False, str(response))

    # 视觉无效：样本数不足
    response = service.observe(_observation("r-3", sample_count=3))
    check("sample_count < 5 -> 不确认", response.get("accepted") is False, str(response))

    # 视觉无效：概率和越界
    response = service.observe(_observation("r-4", apple=0.50, banana=0.05, grapes=0.02))
    check("概率和 < 0.80 -> 不确认", response.get("accepted") is False, str(response))

    # 重量不匹配：苹果的视觉 + 西瓜的重量
    heavy = _service(grams=600.0, forwarder=forwarder)
    response = heavy.observe(_observation("r-5"))
    check("重量严重不符 -> 不确认", response.get("accepted") is False, str(response))
    check("仍然报出最可能的类别", response.get("label") == "apple", str(response))

    # 太轻
    light = _service(grams=10.0, forwarder=forwarder)
    response = light.observe(_observation("r-6"))
    check("重量 < 25g -> 不确认", response.get("accepted") is False, str(response))

    check("以上全部没有转发", len(forwarder.calls) == 0, str(forwarder.calls))


def test_bad_observation_id():
    forwarder = FakeForwarder()
    service = _service(forwarder=forwarder)
    check("空 id 拒绝", service.observe(_observation("")).get("ok") is False)
    check("含斜杠拒绝", service.observe(_observation("a/b")).get("ok") is False)
    check("超 40 字符拒绝", service.observe(_observation("x" * 41)).get("ok") is False)
    check("非法 id 不转发", len(forwarder.calls) == 0)


def test_store_unreachable():
    forwarder = FakeForwarder(ok=False, message="收银后端不可达")
    service = _service(forwarder=forwarder)
    response = service.observe(_observation("d-1"))
    check("收银后端挂了，判定结果仍然返回", response.get("ok") is True
          and response.get("accepted") is True, str(response))
    check("明确标出没转发成功", response.get("forwarded") is False
          and "不可达" in response.get("forward_message", ""), str(response))

    # 关键：第一次转发失败后，重放**不应该**再打一次收银后端
    service.observe(_observation("d-1"))
    check("失败的转发也去重（避免重试风暴）", len(forwarder.calls) == 1,
          str(forwarder.calls))


def test_forward_disabled():
    forwarder = FakeForwarder()
    service = svc.FusionService(
        rules=[ff.FruitRule("apple", "苹果", 180.0, 90.0),
               ff.FruitRule("banana", "香蕉", 140.0, 80.0),
               ff.FruitRule("grapes", "葡萄", 300.0, 220.0)],
        forwarder=forwarder, forward_enabled=False, clock=lambda: 100000)
    for index in range(8):
        service.add_scale_sample(180.0, 100000 - (7 - index) * 500)
    response = service.observe(_observation("n-1"))
    check("--no-forward 时仍判定", response.get("accepted") is True, str(response))
    check("--no-forward 时不加购", len(forwarder.calls) == 0
          and response.get("forwarded") is False, str(response))


# ── 称重入口 ────────────────────────────────────────────────────────────

def test_scale_endpoint():
    service = svc.FusionService(forwarder=FakeForwarder(), clock=lambda: 50000)
    status = service.scale_status()
    check("没样本时 ok=false", status["ok"] is False, str(status))
    check("没样本时重量为 None", status["weight_g"] is None, str(status))

    for index in range(5):
        ok, _ = service.add_scale_sample(180.0, 50000 - (4 - index) * 500)
    status = service.scale_status()
    check("够 5 个样本后 ok=true", status["ok"] is True, str(status))
    check("重量正确", approx(status["weight_g"], 180.0, 0.05), str(status))
    check("稳定判定正确", status["stable"] is True, str(status))
    check("样本数正确", status["samples"] == 5, str(status))

    ok, message = service.add_scale_sample("abc")
    check("非数字 grams 被拒", ok is False and "数字" in message, message)
    ok, _ = service.add_scale_sample("181.5")
    check("字符串数字能接受", ok is True)

    # 样本过期：时钟跳到 10 秒后
    stale = svc.FusionService(forwarder=FakeForwarder(), clock=lambda: 60000)
    for index in range(5):
        stale.add_scale_sample(180.0, 50000 - (4 - index) * 500)
    check("样本过期 -> ok=false", stale.scale_status()["ok"] is False,
          str(stale.scale_status()))


def test_dedupe_table_is_bounded():
    forwarder = FakeForwarder()
    service = _service(forwarder=forwarder)
    for index in range(svc.FORWARD_DEDUPE_LIMIT + 50):
        service.observe(_observation("bulk-%d" % index))
    check("去重表不会无限增长",
          len(service._forwarded) <= svc.FORWARD_DEDUPE_LIMIT,
          str(len(service._forwarded)))
    check("每次都是新 id，转发次数对得上",
          len(forwarder.calls) == svc.FORWARD_DEDUPE_LIMIT + 50, str(len(forwarder.calls)))


# ── 端到端：三跳真跑 ────────────────────────────────────────────────────

def _fake_store_server():
    """假收银后端：实现 /api/vision/observe，记录收到的每一次调用。"""
    received = []

    class StoreHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw) if raw else {}
            received.append(payload)
            body = json.dumps({"ok": True, "message": "已加入购物车"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), StoreHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


def test_three_hop_end_to_end():
    store_server, received = _fake_store_server()
    store_url = "http://127.0.0.1:%d" % store_server.server_address[1]

    service = svc.FusionService(store_url=store_url, clock=lambda: 200000)
    for index in range(8):
        service.add_scale_sample(180.0, 200000 - (7 - index) * 500)

    fusion_server = ThreadingHTTPServer(("127.0.0.1", 0), svc.build_handler(service))
    fusion_url = "http://127.0.0.1:%d/api/vision-fusion" % fusion_server.server_address[1]
    threading.Thread(target=fusion_server.serve_forever, daemon=True).start()

    try:
        client = vo.FusionClient(fusion_url, timeout_ms=3000)
        observer = vo.VisionObserver(LABELS3, client=client, window_size=5,
                                     boot_id="hop00001")
        for _ in range(4):
            check("三跳：预热期不提交",
                  observer.submit_frame({"apple": 0.90, "banana": 0.05,
                                         "grapes": 0.02}) is None)
        response = observer.submit_frame({"apple": 0.90, "banana": 0.05, "grapes": 0.02})

        check("三跳：观测被确认", response.get("accepted") is True, str(response))
        check("三跳：判定为苹果", response.get("label") == "apple", str(response))
        check("三跳：已转发到收银后端", response.get("forwarded") is True, str(response))
        check("三跳：收银后端收到 1 次", len(received) == 1, str(received))
        check("三跳：收银后端收到的 label 正确",
              received[0].get("label") == "apple", str(received[0]))
        check("三跳：收银后端收到的置信度正确",
              approx(received[0].get("confidence", 0), 0.922, 1e-3), str(received[0]))

        # 视觉节点重试同一批观测（observation_id 会递增，但模拟网络重传同一 id）
        service.observe(_observation(observer.ids.boot_id + "-1"))
        check("三跳：重放同一 id 不重复加购", len(received) == 1, str(received))

        # 查询接口
        latest = _get_json("http://127.0.0.1:%d/api/vision-fusion/latest"
                           % fusion_server.server_address[1])
        check("三跳：/latest 能读到判定", latest.get("label") == "apple", str(latest))
        scale = _get_json("http://127.0.0.1:%d/api/scale/status"
                          % fusion_server.server_address[1])
        check("三跳：/scale/status 能读到重量",
              approx(scale.get("weight_g", 0), 180.0, 0.05), str(scale))
        status = _get_json("http://127.0.0.1:%d/api/fusion/status"
                           % fusion_server.server_address[1])
        # 注意：这里服务端用的是 fruit_rules.json 的完整 8 类表，
        # 而 observer 只发了 3 类 —— 其余 5 类按 0 处理，判定照样成立。
        # 这条断言同时守着「类别表可以比视觉节点宽」这个容忍度。
        check("三跳：/status 报出完整 8 类", len(status.get("labels", [])) == 8,
              str(status.get("labels")))
        check("三跳：/status 前 3 类与视觉节点一致",
              status.get("labels", [])[:3] == LABELS3, str(status.get("labels")))
        check("三跳：/status 统计到 1 条通过判定",
              status.get("accepted_observations") == 1, str(status))
        check("三跳：/status 统计到 1 条成功转发",
              status.get("forwarded_ok") == 1, str(status))
    finally:
        fusion_server.shutdown()
        fusion_server.server_close()
        store_server.shutdown()
        store_server.server_close()


def _get_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    response = opener.open(url, timeout=5)
    try:
        return json.loads(response.read().decode("utf-8"))
    finally:
        response.close()


def test_form_parsing_matches_original_contract():
    """原工程表单字段名就是类别名，这里验一遍解析确实按名字取。"""
    forwarder = FakeForwarder()
    service = _service(forwarder=forwarder)
    fields = _observation("form-1")
    response = service.observe(fields)
    check("表单按类别名解析出概率",
          approx(response.get("vision", 0), 0.90, 1e-6), str(response))

    # 顺序打乱 + 多一个未知类别字段，不该受影响
    shuffled = {"grapes": "0.02000", "unknownfruit": "0.99", "apple": "0.90000",
                "observation_id": "form-2", "margin": "0.85000", "banana": "0.05000",
                "spread": "0.01000", "sample_count": "5"}
    response = service.observe(shuffled)
    check("字段顺序无关", response.get("label") == "apple", str(response))
    check("未知类别字段被忽略", response.get("vision") == 0.90, str(response))

    # 缺失类别按 0 处理
    partial = {"observation_id": "form-3", "sample_count": "5",
               "spread": "0.01", "margin": "0.85", "apple": "0.90"}
    response = service.observe(partial)
    check("缺失类别按 0 处理", response.get("ok") is True, str(response))

    # 数值字段缺失时用默认值，不抛异常
    minimal = {"observation_id": "form-4", "apple": "0.90"}
    response = service.observe(minimal)
    check("缺 sample_count 不抛异常", response.get("ok") is True
          and response.get("accepted") is False, str(response))

    bad_number = {"observation_id": "form-5", "sample_count": "five",
                  "spread": "x", "margin": "y", "apple": "0.90"}
    check("数值字段是垃圾也不抛异常",
          service.observe(bad_number).get("ok") is True)


def test_service_status_shape():
    service = _service()
    status = service.status()
    for key in ("ok", "service", "labels", "forward_enabled", "store_url",
                "accepted_observations", "forwarded_ok", "dedupe_entries",
                "latest", "scale"):
        check("status 含字段 %s" % key, key in status, str(sorted(status)))
    check("status 类别顺序与规则一致", status["labels"] == LABELS3, str(status["labels"]))


def test_status_counters_are_distinct():
    """三个计数口径必须分开：通过判定 / 成功转发 / 去重表条数。"""
    forwarder = FakeForwarder()
    service = _service(forwarder=forwarder)
    for index in range(3):
        service.observe(_observation("c-%d" % index))
    status = service.status()
    check("3 条通过判定", status["accepted_observations"] == 3, str(status))
    check("3 条成功转发", status["forwarded_ok"] == 3, str(status))
    check("去重表 3 条", status["dedupe_entries"] == 3, str(status))

    # 重放不涨
    service.observe(_observation("c-0"))
    status = service.status()
    check("重放不涨通过判定数", status["accepted_observations"] == 3, str(status))

    # 转发失败时：通过判定数涨，成功转发数不涨
    failing = svc.FusionService(
        rules=[ff.FruitRule("apple", "苹果", 180.0, 90.0),
               ff.FruitRule("banana", "香蕉", 140.0, 80.0),
               ff.FruitRule("grapes", "葡萄", 300.0, 220.0)],
        forwarder=FakeForwarder(ok=False), clock=lambda: 100000)
    for index in range(8):
        failing.add_scale_sample(180.0, 100000 - (7 - index) * 500)
    failing.observe(_observation("f-1"))
    status = failing.status()
    check("转发失败时通过判定仍计数", status["accepted_observations"] == 1, str(status))
    check("转发失败时成功转发不计数", status["forwarded_ok"] == 0, str(status))


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
