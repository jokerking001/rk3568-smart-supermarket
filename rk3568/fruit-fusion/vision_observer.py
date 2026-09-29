# -*- coding: utf-8 -*-
"""水果视觉节点：多帧概率聚合 → 提交给融合引擎（RK3568 侧实现）。

原工程这一层跑在 XIAO ESP32-S3 上（`D:\\789\\xiao_ov3660_weight_fusion\\
xiao_ov3660_weight_fusion.ino`），用 Edge Impulse 做三分类，然后把聚合结果
POST 给主控的 `/api/vision-fusion`。换主控后 RK3568 自己拍图自己推理，
但**聚合与提交这一层必须原样保留**——融合引擎要的不是单帧结果，
而是「多帧稳定之后的概率」。

照搬的原工程行为（逐条对应）：

    VISION_WINDOW_SIZE = 5          5 帧滑动窗口
    INFERENCE_INTERVAL_MS = 1400    每 1400 ms 推一帧（窗口约 7 秒填满）
    均值   means[label] = 窗口内该类别概率的算术平均
    spread = max over labels( 窗口内该类别 max - min )   ← 注意是对所有类别取最大
    margin = top1(means) - top2(means)                   ← 注意是在均值上算，不是单帧
    窗口不满 5 帧不提交（所以 sample_count 恒等于 5）
    observation_id = <bootId>-<递增序号>，bootId 8 位十六进制
    POST application/x-www-form-urlencoded，**字段名就是类别名**
         observation_id / captured_ms / sample_count / spread / margin / <label>=...
    提交失败重试 2 次，间隔 250 ms，超时 3000 ms

与 ESP32 版的差异（都是搬移带来的，不是行为改变）：

1. 类别数从 3 扩到可配置（默认从 `fruit_rules.json` 读），字段名跟着类别名走。
2. 单帧概率的来源不同：原工程是 Edge Impulse 的 softmax（天然和为 1），
   RK 侧是 YOLO 检测框的置信度。两者语义不同，见 `detections_to_probabilities()`
   的说明——这是本次移植**唯一需要你拍板**的地方。
3. 提交目标从主控的 80 端口改成 RK 侧融合服务（默认 8090）。

板端是 Python 3.7.3，本文件不使用 walrus、dict 合并、PEP 585 泛型下标。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import fruit_fusion

# ── 与原工程一致的常量 ────────────────────────────────────────────────────

VISION_WINDOW_SIZE = 5
INFERENCE_INTERVAL_MS = 1400
FUSION_HTTP_TIMEOUT_MS = 3000
FUSION_RETRIES = 2
FUSION_RETRY_DELAY_MS = 250

# 板端服务端口约定（来自 rk3568-smart-supermarket-handoff-v2.md 第 54-62 行）：
#   8088 视觉推理 / 8089 水果识别 / 8090 融合(雷达+视觉，**不是这个**) /
#   8091 雷达 / 8092 VLM / 8093 数据采集 / 8094 收银后端 / 8095 扫码枪 / 8096 OCR
# 8090 已经被「雷达背景变化 + 视觉 person」的顾客会话状态机占用，
# 端点是 /api/fusion/status，**没有** /api/vision-fusion。
# 所以水果视觉+重量融合另起服务，落在 8099（原来干跑水果服务用的临时端口，已关闭）。
DEFAULT_FUSION_URL = "http://127.0.0.1:8099/api/vision-fusion"

# 单帧概率的构造方式，见 detections_to_probabilities()
PROB_MODE_TOP1 = "top1"
PROB_MODE_NORMALIZE = "normalize"


def margin(means, labels):
    """原工程 `margin` 的计算：在**均值**上取 top1 - top2。

    原工程用 first/second 两个变量扫一遍，不关心是哪个类别，只要差值。
    """
    first = 0.0
    second = 0.0
    for label in labels:
        value = means.get(label, 0.0)
        if value > first:
            second = first
            first = value
        elif value > second:
            second = value
    return first - second


def detections_to_probabilities(detections, labels, mode=PROB_MODE_TOP1):
    """把 RK 侧的单帧 YOLO 检测结果转成「类别 -> 概率」。

    这里有个**语义差异**必须讲清楚：

        原工程用 Edge Impulse 分类器，`result.classification[i].value` 是
        softmax 输出，天然和为 1。融合引擎里 `prob_sum ∈ [0.80, 1.20]`
        那条判据，本质是在检查「分类器给了一个像样的分布」。

        RK 侧用 YOLO 检测框置信度，没有这种分布。所以：

    mode="top1"（默认）
        每个类别取其**最高**检测框置信度，不做归一化。
        - 台面上只有一个水果、模型有信心时，sum ≈ 0.85~0.95 → 通过 sum 判据
        - 台面上同时出现多个水果时，sum 会明显 > 1.2 → **判定视觉无效**
          这恰好符合原工程「一个秤盘一个商品」的假设，是个有用的副作用，
          但它不是原工程显式设计的行为，得知道。
        - 模型对单帧没信心（置信度 < 0.80）时会因为 sum 太小被判无效。

    mode="normalize"
        先取每类最高置信度，再归一化到和为 1。
        sum 判据恒成立，判别力全部落在 `margin`、`spread` 和单类概率上
        （融合还要 `vision >= 0.55`）。想更贴近原工程 softmax 语义就用这个。

    两种都保留，等你拿板子在真实台面上跑一批数据再定。默认 top1，
    因为它保留了「多物体 / 低置信度 → 不自动确认」这个安全边界。
    """
    values = {}
    for label in labels:
        values[label] = 0.0
    for det in detections or []:
        if not isinstance(det, dict):
            continue
        label = det.get("class")
        if label not in values:
            continue
        try:
            confidence = float(det.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence > values[label]:
            values[label] = confidence

    if mode == PROB_MODE_NORMALIZE:
        total = 0.0
        for label in labels:
            total += values[label]
        if total > 0.0:
            for label in labels:
                values[label] = values[label] / total

    return values


class ProbabilityWindow(object):
    """原工程 `updateProbabilityWindow()` 的等价实现。

    固定长度的环形窗口，保存最近 N 帧的「类别 -> 概率」。
    原工程是先写数组再按写入顺序取前 count 项求均值；
    因为均值与顺序无关，这里直接用实际存下的帧求均值，结果一致。
    """

    def __init__(self, labels, size=VISION_WINDOW_SIZE):
        self.labels = list(labels)
        self.size = int(size)
        self._frames = []
        self._index = 0
        self._count = 0
        self._lock = threading.Lock()

    def push(self, probabilities):
        """喂入一帧，返回 (means, spread, count)。

        means / spread 的算法与原工程逐条对应：
            means[label] = 窗口内该类别概率的平均
            spread       = 所有类别里「窗口内极差」的最大值
        """
        frame = {}
        for label in self.labels:
            try:
                frame[label] = float(probabilities.get(label, 0.0))
            except (TypeError, ValueError, AttributeError):
                frame[label] = 0.0

        with self._lock:
            if len(self._frames) < self.size:
                self._frames.append(frame)
            else:
                self._frames[self._index] = frame
            self._index = (self._index + 1) % self.size
            if self._count < self.size:
                self._count += 1
            frames = list(self._frames)
            count = self._count

        means = {}
        spread = 0.0
        for label in self.labels:
            values = [f[label] for f in frames]
            means[label] = sum(values) / float(len(values))
            span = max(values) - min(values)
            if span > spread:
                spread = span
        return means, spread, count

    @property
    def count(self):
        with self._lock:
            return self._count

    @property
    def ready(self):
        with self._lock:
            return self._count >= self.size

    def clear(self):
        with self._lock:
            self._frames = []
            self._index = 0
            self._count = 0


class ObservationIdFactory(object):
    """原工程 `observation_id = bootId + "-" + String(++observationSequence)`。

    bootId 取 8 位十六进制（原工程是 `esp_random()`），
    加上序号后长度远小于 40，字符集只有字母数字和 '-'，
    正好满足融合引擎 `valid_observation_id()` 的约束。
    """

    def __init__(self, boot_id=None):
        if boot_id:
            self.boot_id = str(boot_id)
        else:
            self.boot_id = os.urandom(4).hex()
        self._sequence = 0
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            self._sequence += 1
            return "%s-%d" % (self.boot_id, self._sequence)


class FusionClient(object):
    """把观测 POST 给融合服务，重试策略与原工程一致（2 次，间隔 250 ms）。"""

    def __init__(self, url=DEFAULT_FUSION_URL, timeout_ms=FUSION_HTTP_TIMEOUT_MS,
                 retries=FUSION_RETRIES, retry_delay_ms=FUSION_RETRY_DELAY_MS):
        self.url = url
        self.timeout_s = float(timeout_ms) / 1000.0
        self.retries = int(retries)
        self.retry_delay_s = float(retry_delay_ms) / 1000.0
        # 显式禁用代理：环境里只要有一个 http_proxy，访问 127.0.0.1 也会被绕出去。
        # 板子上踩过一次（沙箱注入 http_proxy），这里直接写死不走代理。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, fields):
        """fields 是 (key, value) 列表，保证同名类别字段顺序稳定。"""
        body = urllib.parse.urlencode(fields).encode("utf-8")
        last_error = None
        for attempt in range(self.retries):
            request = urllib.request.Request(
                self.url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                response = self._opener.open(request, timeout=self.timeout_s)
                try:
                    return response.getcode(), response.read().decode("utf-8", "replace")
                finally:
                    response.close()
            except urllib.error.HTTPError as exc:
                # 4xx/5xx 也是有效响应，读出来当正文返回，与原工程 status>0 一致
                try:
                    return exc.code, exc.read().decode("utf-8", "replace")
                except Exception:
                    last_error = exc
            except Exception as exc:
                last_error = exc
            if attempt + 1 < self.retries:
                time.sleep(self.retry_delay_s)
        return -1, str(last_error)


class VisionObserver(object):
    """聚合 + 提交 + 保存最近一次结果。"""

    def __init__(self, labels, client=None, window_size=VISION_WINDOW_SIZE,
                 boot_id=None, now_ms=None):
        self.labels = list(labels)
        self.window = ProbabilityWindow(self.labels, window_size)
        self.ids = ObservationIdFactory(boot_id)
        self.client = client if client is not None else FusionClient()
        self._now_ms = now_ms if now_ms is not None else _monotonic_ms
        self._last = {"ok": False, "msg": "等待第一次识别"}
        self._lock = threading.Lock()

    def submit_frame(self, probabilities):
        """喂一帧概率。窗口没满返回 None（与原工程一样不提交）。"""
        means, spread, count = self.window.push(probabilities)
        if count < self.window.size:
            return None

        observation_id = self.ids.next()
        fields = [
            ("observation_id", observation_id),
            ("captured_ms", self._now_ms()),
            ("sample_count", count),
            ("spread", "%.5f" % spread),
            ("margin", "%.5f" % margin(means, self.labels)),
        ]
        for label in self.labels:
            fields.append((label, "%.5f" % means[label]))

        status, text = self.client.post(fields)
        response = self._parse_response(status, text, observation_id)
        with self._lock:
            self._last = response
        return response

    def submit_detections(self, detections, mode=PROB_MODE_TOP1):
        """给 RK 侧视觉服务的便捷入口：单帧 detections 直接喂进来。"""
        return self.submit_frame(detections_to_probabilities(detections, self.labels, mode))

    def _parse_response(self, status, text, observation_id):
        if status == 200 and text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
            return {"ok": False, "observation_id": observation_id,
                    "msg": "融合服务返回的不是 JSON", "raw": text[:400]}
        return {"ok": False, "observation_id": observation_id,
                "msg": "融合服务不可达或异常（status=%s）" % status,
                "raw": (text or "")[:400]}

    def latest(self):
        with self._lock:
            return dict(self._last)

    def set_last(self, response):
        """融合服务把结果推回来时用（比如带重量变化的重判）。"""
        with self._lock:
            self._last = dict(response)


def _monotonic_ms():
    return int(time.monotonic() * 1000)


def labels_from_rules(path=None):
    """默认类别集合直接取自融合规则表 —— 两边天然对齐，不会漏改。"""
    return [rule.label for rule in fruit_fusion.load_rules(path)]


# ── 独立运行模式（台面联调用，生产环境应该嵌进 8088 视觉服务） ─────────────
#
# 板子上 8088 已经被 vision_service.py 占用。这个独立入口的用途是：
#   1. 不接摄像头，用合成概率流把「观测 → 融合 → 判定」整条链路先跑通
#   2. 提供一个和原工程同形状的 /result 页面，方便肉眼确认
# 生产做法：在 vision_service.py 的推理循环里直接调
#   observer.submit_detections(detections)
# 不要另起一个服务去抢摄像头。

DASHBOARD = (
    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>水果视觉与重量融合</title><style>'
    'body{margin:0;background:#f3f5f7;color:#18202a;font-family:Arial,'
    '"Microsoft YaHei",sans-serif}.wrap{max-width:760px;margin:auto;padding:18px}'
    '.card{margin-top:14px;background:#fff;border:1px solid #e1e6ea;'
    'border-radius:8px;padding:16px}.status{font-size:18px;font-weight:700}'
    '.meta{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}'
    '.meta div{background:#f7f9fa;padding:10px;border-radius:6px}'
    '.meta span{display:block;font-size:11px;color:#687386}.meta b{font-size:16px}'
    '.raw{font:12px monospace;color:#687386;white-space:pre-wrap;margin-top:12px}'
    '</style><body><main class="wrap"><h2>RK3568 水果视觉与重量融合</h2>'
    '<section class="card"><div class="status" id="decision">等待识别结果</div>'
    '<div class="meta"><div><span>识别商品</span><b id="name">-</b></div>'
    '<div><span>融合置信度</span><b id="confidence">-</b></div>'
    '<div><span>重量</span><b id="weight">-</b></div>'
    '<div><span>重量稳定</span><b id="stable">-</b></div></div>'
    '<div class="raw" id="raw"></div></section></main><script>'
    'async function pull(){try{let d=await (await fetch("/result?t="+Date.now())).json();'
    'document.getElementById("decision").textContent=d.accepted?("已确认："+d.name):"请人工确认";'
    'document.getElementById("name").textContent=d.name||"-";'
    'document.getElementById("confidence").textContent='
    '(d.confidence!==undefined)?(d.confidence*100).toFixed(1)+"%":"-";'
    'document.getElementById("weight").textContent='
    '(d.weight_g!==undefined)?d.weight_g.toFixed(1)+" g":"-";'
    'document.getElementById("stable").textContent=d.stable?"稳定":"待稳定";'
    'document.getElementById("raw").textContent=JSON.stringify(d,null,2)}'
    'catch(e){}}pull();setInterval(pull,1000)</script></body></html>'
)


class ObserverState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.payload = {"ok": False, "msg": "启动中"}

    def set(self, payload):
        with self.lock:
            self.payload = payload

    def get(self):
        with self.lock:
            return dict(self.payload)


def build_handler(state):
    """用闭包拿 state，避免依赖 http.server 的类属性 hack。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/result") or self.path.startswith("/health"):
                body = json.dumps(state.get(), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            else:
                self._send(200, "text/html; charset=utf-8", DASHBOARD.encode("utf-8"))

    return Handler


def synthetic_probabilities(labels, step):
    """合成概率流：模拟一个「先晃动后稳定」的苹果，用来空跑整条链路。

    step 0..2 主类别概率大幅抖动（spread 会超过 0.25，触发视觉无效），
    step 3 起收敛 —— 正好能验证融合引擎的 spread / margin 判据真的在起作用。

    注意概率和必须落在 [0.80, 1.20]：
        原工程是 softmax 输出，天然和为 1。这里主类别给 ~0.88，
        剩下的**平均分给其余类别**，总和精确为 1.0。
        早期版本给每个非主类别固定 0.05，8 类时和是 1.23，会被
        prob_sum 门禁判无效 —— 那是我合成数据的错，不是判据的错。
    """
    import math
    if not labels:
        return {}
    primary = labels[0]
    others = [label for label in labels if label != primary]

    wobble = 0.30 * math.sin(step * 1.7)
    if step >= 3:
        wobble = 0.01 * math.sin(step * 1.7)
    top = 0.88 + wobble
    if top < 0.0:
        top = 0.0
    if top > 0.98:
        top = 0.98

    rest = (1.0 - top) / float(len(others)) if others else 0.0
    values = {}
    for label in labels:
        values[label] = rest
    values[primary] = top
    return values


def main():
    import argparse
    import signal
    from http.server import ThreadingHTTPServer

    parser = argparse.ArgumentParser(description="水果视觉节点（观测聚合 → 融合提交）")
    parser.add_argument("--fusion-url", default=DEFAULT_FUSION_URL)
    parser.add_argument("--rules", default=None, help="fruit_rules.json 路径")
    parser.add_argument("--labels", default=None, help="逗号分隔的类别，默认从规则表读")
    parser.add_argument("--port", type=int, default=8098,
                        help="独立模式监听端口（默认 8098，避开 8088 视觉服务）")
    parser.add_argument("--interval", type=float, default=INFERENCE_INTERVAL_MS / 1000.0)
    parser.add_argument("--window", type=int, default=VISION_WINDOW_SIZE)
    parser.add_argument("--prob-mode", default=PROB_MODE_TOP1,
                        choices=[PROB_MODE_TOP1, PROB_MODE_NORMALIZE])
    parser.add_argument("--synthetic", action="store_true",
                        help="不接摄像头，用合成概率流空跑（联调用）")
    parser.add_argument("--once", action="store_true",
                        help="拿到第一次判定就退出（自检用）")
    parser.add_argument("--steps", type=int, default=0,
                        help="合成模式跑满 N 帧就退出（默认一直跑）。"
                             "前 3 帧故意抖动大，跑 9 帧以上才能看到「通过」")
    args = parser.parse_args()

    if args.labels:
        labels = [item.strip() for item in args.labels.split(",") if item.strip()]
    else:
        labels = labels_from_rules(args.rules)

    client = FusionClient(args.fusion_url)
    observer = VisionObserver(labels, client=client, window_size=args.window)
    state = ObserverState()
    state.set({"ok": False, "msg": "窗口预热中", "labels": labels,
               "fusion_url": args.fusion_url})

    stop = {"flag": False}

    def handle_signal(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    server = None
    if not args.once:
        handler = build_handler(state)
        server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("视觉节点独立模式：http://0.0.0.0:%d/  -> 融合 %s"
              % (args.port, args.fusion_url))

    print("类别：%s" % ", ".join(labels))
    if not args.synthetic:
        print("")
        print("注意：这个入口不接摄像头。生产做法是在 8088 视觉服务的推理循环里")
        print("      直接调 observer.submit_detections(detections)。")
        print("      只想空跑链路的话加 --synthetic。")
        if server is not None:
            print("（仍然起了 /result 页面，供外部把结果推进来）")
            while not stop["flag"]:
                time.sleep(0.2)
            server.shutdown()
        return 0

    step = 0
    while not stop["flag"]:
        probabilities = synthetic_probabilities(labels, step)
        response = observer.submit_frame(probabilities)
        if response is None:
            print("[%02d] 窗口 %d/%d 预热中"
                  % (step, observer.window.count, observer.window.size))
            state.set({"ok": False, "msg": "窗口预热中",
                       "window": observer.window.count, "window_size": observer.window.size,
                       "labels": labels})
        else:
            state.set(response)
            print("[%02d] accepted=%s label=%s confidence=%s weight=%sg  %s"
                  % (step, response.get("accepted"), response.get("label"),
                     response.get("confidence"), response.get("weight_g"),
                     response.get("msg") or response.get("name") or ""))
            if args.once:
                break
        step += 1
        if args.steps and step >= args.steps:
            break
        time.sleep(args.interval)

    if server is not None:
        server.shutdown()
    print("退出。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
