# -*- coding: utf-8 -*-
"""vision_observer 的行为测试。

不依赖 pytest，可直接在板端 Python 3.7 上跑：

    python3 test_vision_observer.py

其中 `test_end_to_end_over_http` 会真起一个 HTTP 服务托管 fruit_fusion 的
融合引擎，然后让 observer 走真实网络提交 —— 把「观测 → 融合 → 判定」
整条链路验一遍，不是打桩。
"""

import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fruit_fusion as ff
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


# ── margin ──────────────────────────────────────────────────────────────

def test_margin():
    check("margin = top1 - top2",
          approx(vo.margin({"a": 0.9, "b": 0.4, "c": 0.1}, ["a", "b", "c"]), 0.5))
    check("margin 并列第一取前两个",
          approx(vo.margin({"a": 0.5, "b": 0.5}, ["a", "b"]), 0.0))
    check("margin 只有一个非零",
          approx(vo.margin({"a": 0.7, "b": 0.0}, ["a", "b"]), 0.7))
    check("margin 缺失类别按 0",
          approx(vo.margin({"a": 0.6}, ["a", "b", "c"]), 0.6))
    check("margin 全零 -> 0", approx(vo.margin({}, ["a", "b"]), 0.0))


# ── 单帧概率构造 ────────────────────────────────────────────────────────

def test_detections_to_probabilities():
    detections = [
        {"class": "apple", "confidence": 0.71},
        {"class": "apple", "confidence": 0.93},
        {"class": "banana", "confidence": 0.20},
    ]
    top1 = vo.detections_to_probabilities(detections, LABELS3)
    check("top1 取每类最高置信度", approx(top1["apple"], 0.93)
          and approx(top1["banana"], 0.20) and approx(top1["grapes"], 0.0), str(top1))

    normalized = vo.detections_to_probabilities(detections, LABELS3,
                                                mode=vo.PROB_MODE_NORMALIZE)
    check("normalize 后和为 1", approx(sum(normalized.values()), 1.0), str(normalized))
    check("normalize 保持相对大小",
          normalized["apple"] > normalized["banana"] > normalized["grapes"])

    empty = vo.detections_to_probabilities([], LABELS3)
    check("空检测 -> 全 0", all(v == 0.0 for v in empty.values()))
    check("全零不做除零（normalize）",
          all(v == 0.0 for v in vo.detections_to_probabilities(
              [], LABELS3, mode=vo.PROB_MODE_NORMALIZE).values()))

    unknown = vo.detections_to_probabilities(
        [{"class": "durian", "confidence": 0.99}], LABELS3)
    check("未知类别被丢弃", all(v == 0.0 for v in unknown.values()))

    broken = vo.detections_to_probabilities(
        [{"class": "apple", "confidence": "NaN-ish"}, None, "junk"], LABELS3)
    check("坏数据不抛异常", approx(broken["apple"], 0.0), str(broken))


# ── 环形窗口 ────────────────────────────────────────────────────────────

def test_probability_window():
    window = vo.ProbabilityWindow(LABELS3, size=5)
    check("初始未就绪", not window.ready and window.count == 0)

    for index in range(4):
        means, spread, count = window.push({"apple": 0.5 + index * 0.1,
                                            "banana": 0.2, "grapes": 0.0})
        check("第 %d 帧后 count=%d" % (index + 1, index + 1), count == index + 1)
    check("4 帧仍未就绪", not window.ready)
    check("预热期均值是已存帧的平均",
          approx(means["apple"], (0.5 + 0.6 + 0.7 + 0.8) / 4.0), str(means))

    means, spread, count = window.push({"apple": 0.9, "banana": 0.2, "grapes": 0.0})
    check("5 帧后就绪", window.ready and count == 5)
    check("满窗均值正确",
          approx(means["apple"], (0.5 + 0.6 + 0.7 + 0.8 + 0.9) / 5.0), str(means))
    # apple 在窗口内 0.5~0.9 -> 极差 0.4，是所有类别里最大的
    check("spread 取所有类别极差的最大值", approx(spread, 0.4), str(spread))

    # 再推一帧，最旧的 0.5 被挤掉
    means, spread, count = window.push({"apple": 0.5, "banana": 0.2, "grapes": 0.0})
    check("满窗后仍是 5 帧", count == 5)
    check("最旧帧被挤出（0.5 出现两次：一次被挤、一次新进）",
          approx(means["apple"], (0.6 + 0.7 + 0.8 + 0.9 + 0.5) / 5.0), str(means))

    window.clear()
    check("clear 后回到未就绪", not window.ready and window.count == 0)

    # spread 必须对**所有**类别取最大，不能只看 top1
    window2 = vo.ProbabilityWindow(["a", "b"], size=2)
    window2.push({"a": 0.9, "b": 0.1})
    _, spread2, _ = window2.push({"a": 0.9, "b": 0.9})
    check("非 top1 类别的抖动也计入 spread", approx(spread2, 0.8), str(spread2))


# ── observation_id ──────────────────────────────────────────────────────

def test_observation_id_factory():
    factory = vo.ObservationIdFactory(boot_id="deadbeef")
    first = factory.next()
    second = factory.next()
    check("id 格式 bootId-序号", first == "deadbeef-1" and second == "deadbeef-2")
    check("id 通过融合引擎的校验", ff.valid_observation_id(first))
    check("id 长度远小于 40", len(first) <= 40)

    auto = vo.ObservationIdFactory()
    check("自动 bootId 是 8 位十六进制", len(auto.boot_id) == 8)
    check("自动 bootId 通过校验", ff.valid_observation_id(auto.next()))
    check("两次自动 bootId 不同", vo.ObservationIdFactory().boot_id
          != vo.ObservationIdFactory().boot_id)


# ── 提交前的门禁 ────────────────────────────────────────────────────────

class RecordingClient(object):
    """记录提交内容，不发网络请求。"""

    def __init__(self, status=200, body='{"ok":true,"accepted":true}'):
        self.status = status
        self.body = body
        self.calls = []

    def post(self, fields):
        self.calls.append(list(fields))
        return self.status, self.body


def _fields_to_dict(fields):
    return dict(fields)


def test_submit_gating():
    client = RecordingClient()
    observer = vo.VisionObserver(LABELS3, client=client, window_size=5,
                                 boot_id="aa11bb22", now_ms=lambda: 12345)
    for _ in range(4):
        result = observer.submit_frame({"apple": 0.9, "banana": 0.1, "grapes": 0.0})
        check("窗口未满不提交", result is None)
    check("窗口未满时一次都没发出去", len(client.calls) == 0)

    observer.submit_frame({"apple": 0.9, "banana": 0.1, "grapes": 0.0})
    check("窗口满了才提交", len(client.calls) == 1)

    fields = _fields_to_dict(client.calls[0])
    check("提交含 observation_id", fields.get("observation_id") == "aa11bb22-1", str(fields))
    check("提交含 sample_count=5", fields.get("sample_count") == 5, str(fields))
    check("提交含 captured_ms", fields.get("captured_ms") == 12345, str(fields))
    check("提交含 spread", "spread" in fields)
    check("提交含 margin", "margin" in fields)
    check("字段名就是类别名（原工程约定）",
          all(label in fields for label in LABELS3), str(sorted(fields)))
    check("不含未知类别字段",
          set(fields) == set(["observation_id", "captured_ms", "sample_count",
                              "spread", "margin"] + LABELS3), str(sorted(fields)))


def test_response_handling():
    ok = vo.VisionObserver(LABELS3, client=RecordingClient(
        200, '{"ok":true,"accepted":true,"label":"apple"}'), window_size=1)
    response = ok.submit_frame({"apple": 0.9})
    check("200 + JSON 正常解析", response.get("accepted") is True
          and response.get("label") == "apple", str(response))
    check("latest 返回同一份", ok.latest().get("label") == "apple")

    bad_json = vo.VisionObserver(LABELS3, client=RecordingClient(200, "<html>500</html>"),
                                 window_size=1)
    response = bad_json.submit_frame({"apple": 0.9})
    check("200 但非 JSON -> ok=false", response.get("ok") is False, str(response))
    check("非 JSON 时保留原文便于排查", "html" in response.get("raw", ""))

    down = vo.VisionObserver(LABELS3, client=RecordingClient(-1, "connection refused"),
                             window_size=1)
    response = down.submit_frame({"apple": 0.9})
    check("不可达 -> ok=false", response.get("ok") is False, str(response))
    check("不可达时带 observation_id", response.get("observation_id") == down.ids.boot_id + "-1",
          str(response))

    err = vo.VisionObserver(LABELS3, client=RecordingClient(500, "boom"), window_size=1)
    check("5xx 也当失败处理", err.submit_frame({"apple": 0.9}).get("ok") is False)


def test_submit_detections():
    client = RecordingClient()
    observer = vo.VisionObserver(LABELS3, client=client, window_size=1)
    observer.submit_detections([{"class": "banana", "confidence": 0.88}])
    fields = _fields_to_dict(client.calls[0])
    check("detections 入口把最高置信度送出去",
          approx(float(fields["banana"]), 0.88), str(fields))
    check("detections 入口其它类别为 0",
          approx(float(fields["apple"]), 0.0) and approx(float(fields["grapes"]), 0.0))


# ── 与规则表对齐 ────────────────────────────────────────────────────────

def test_labels_from_rules():
    labels = vo.labels_from_rules()
    check("默认类别取自 fruit_rules.json", labels[:3] == ["apple", "banana", "grapes"],
          str(labels))
    check("类别数与规则数一致", len(labels) == 8, str(labels))
    check("没有重复 label", len(labels) == len(set(labels)))


# ── 端到端：真起 HTTP 服务托管融合引擎 ──────────────────────────────────

def _build_fusion_server(engine, now_ms):
    class FusionHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(raw, keep_blank_values=True)

            def one(key, default=""):
                values = form.get(key)
                return values[0] if values else default

            probabilities = {}
            for label in engine.labels():
                if label in form:
                    probabilities[label] = float(form[label][0])
            response = engine.observe(
                one("observation_id"),
                probabilities,
                int(float(one("sample_count", "0"))),
                float(one("spread", "0")),
                float(one("margin", "0")),
                now_ms)
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return FusionHandler


def test_end_to_end_over_http():
    """整条链路：observer --HTTP--> 融合引擎（含重量）--> 判定。"""
    engine = ff.FusionEngine(scale=ff.ScaleBuffer())
    now_ms = 100000
    for index in range(8):
        engine.scale.add_sample(180.0, now_ms - (7 - index) * 500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _build_fusion_server(engine, now_ms))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = vo.FusionClient("http://127.0.0.1:%d/api/vision-fusion" % port,
                                 timeout_ms=3000)
        observer = vo.VisionObserver(LABELS3, client=client, window_size=5,
                                     boot_id="e2e00001")

        # 前 4 帧只预热，不提交
        for _ in range(4):
            check("端到端：预热期不提交",
                  observer.submit_frame({"apple": 0.90, "banana": 0.05,
                                         "grapes": 0.02}) is None)

        # 第 5 帧：稳定苹果 180 g，应该被自动确认
        response = observer.submit_frame({"apple": 0.90, "banana": 0.05, "grapes": 0.02})
        check("端到端：返回 ok", response.get("ok") is True, str(response))
        check("端到端：自动确认", response.get("accepted") is True, str(response))
        check("端到端：判定为苹果", response.get("label") == "apple"
              and response.get("name") == "苹果", str(response))
        check("端到端：重量读到 180 g", approx(response.get("weight_g", 0), 180.0, 0.05),
              str(response))
        # 0.90*0.78 + 1.0*0.22 = 0.702 + 0.22 = 0.922
        check("端到端：融合置信度 = 0.922",
              approx(response.get("confidence", 0), 0.922, 1e-3), str(response))
        check("端到端：spread 在阈值内", response.get("vision_stable") is True,
              str(response))

        # 同一 observation_id 再提交一次：融合引擎幂等，不该重复计数
        first_id = observer.ids.boot_id + "-1"
        repeat = engine.observe(first_id, {"apple": 0.90, "banana": 0.05, "grapes": 0.02},
                                5, 0.0, 0.85, now_ms)
        check("端到端：observation_id 幂等",
              repeat.get("observation_id") == first_id
              and repeat.get("accepted") is True, str(repeat))

        # 抖动大的窗口：spread 超标 -> 不确认
        shaky = vo.VisionObserver(LABELS3, client=client, window_size=3,
                                  boot_id="e2e00002")
        shaky.submit_frame({"apple": 0.95, "banana": 0.02, "grapes": 0.01})
        shaky.submit_frame({"apple": 0.40, "banana": 0.50, "grapes": 0.05})
        rejected = shaky.submit_frame({"apple": 0.90, "banana": 0.05, "grapes": 0.02})
        check("端到端：概率抖动大 -> 不自动确认",
              rejected.get("accepted") is False, str(rejected))
        check("端到端：抖动大时 vision_stable=false",
              rejected.get("vision_stable") is False, str(rejected))

        # 重量不匹配：视觉说苹果，秤上是 600 g 的西瓜
        engine.scale.clear()
        for index in range(8):
            engine.scale.add_sample(600.0, now_ms - (7 - index) * 500)
        heavy = vo.VisionObserver(LABELS3, client=client, window_size=5,
                                  boot_id="e2e00003")
        for _ in range(5):
            last = heavy.submit_frame({"apple": 0.95, "banana": 0.02, "grapes": 0.01})
        check("端到端：视觉对但重量严重不符 -> 不自动确认",
              last.get("accepted") is False, str(last))
        check("端到端：重量不匹配时仍报出最可能的类别",
              last.get("label") == "apple", str(last))
    finally:
        server.shutdown()
        server.server_close()


def test_fusion_client_retry():
    """连一个没人监听的端口，确认按原工程重试 2 次后放弃。"""
    client = vo.FusionClient("http://127.0.0.1:1/api/vision-fusion",
                             timeout_ms=300, retries=2, retry_delay_ms=10)
    status, text = client.post([("observation_id", "x-1")])
    check("不可达返回 -1", status == -1, "%s %s" % (status, text))


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
