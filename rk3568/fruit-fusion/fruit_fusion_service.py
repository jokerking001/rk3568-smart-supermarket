#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水果视觉 + 重量融合服务（RK3568 侧）。

这是原工程 ESP32 主控里 `/api/vision-fusion` 那一块在 RK 上的落点。

**为什么单独起一个服务，而不是塞进 8094 收银后端：**

    原工程只有一个主控，所以融合判定和购物车在同一个 WebServer 里。
    换到 RK 之后 8094 的 store_service.py 已经跑着一套完整的收银逻辑
    （1156 行，77 个回归测试在守着），并且**本地副本和板端副本是否一致
    还没核对过**。直接改它风险太大。
    所以这里做成独立服务：融合判定单独跑，判定通过后**复用 8094 已有的
    `/api/vision/observe`** 把结果交出去，一行都不碰 store_service.py。
    等以后核对完本地/板端一致，再决定要不要并进去。

**它补上了 RK 侧缺的东西：**

    8094 现在的 observe_vision(label, confidence) 只有单帧标签+置信度，
    没有重量、没有多帧稳定性、没有幂等。这个服务补齐：
      - 多帧聚合（sample_count / spread / margin），判据与原工程一致
      - 重量融合（vision×0.78 + weight×0.22）与四重门禁
      - observation_id 幂等，且**跨服务边界也只转发一次**

**端口：8099**

    8090 是「雷达背景变化 + 视觉 person」的顾客会话状态机，端点是
    /api/fusion/status，和这个不是一回事，别搞混。
    8099 是原来干跑水果服务用的临时端口，已关闭，现在收回来给融合服务。

接口：

    POST /api/vision-fusion          ← 视觉节点提交观测（原工程同款表单）
    GET  /api/vision-fusion/latest   ← 最近一次判定
    POST /api/scale/sample           ← ESP32-S3 从机喂重量样本（grams）
    GET  /api/scale/status           ← 称重缓冲状态
    GET  /api/fusion/status          ← 健康检查
    GET  /                           ← 页面

板端是 Python 3.7.3，本文件不使用 walrus、dict 合并、PEP 585 泛型下标。
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fruit_fusion

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8099
DEFAULT_STORE_URL = "http://127.0.0.1:8094"
DEFAULT_SESSION = "default"
HTTP_TIMEOUT_S = 3.0
# observation_id 去重表的上限。满了就淘汰最早的一条（dict 在 3.7 保序）。
FORWARD_DEDUPE_LIMIT = 512


def now_ms():
    return int(time.monotonic() * 1000)


def no_proxy_opener():
    """显式不走代理。

    环境里只要存在一个 http_proxy，访问 127.0.0.1 也会被绕出去然后超时。
    板子上踩过一次，这里写死。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalFusionClient(object):
    """把观测**直接交给本进程的 FusionService**，不走 loopback HTTP。

    桥接线程开在融合服务内部时用它。走 HTTP 也能通，但那是自己请求自己，
    多一次序列化和一次连接，没有任何好处。
    接口形状对齐 vision_observer.FusionClient.post()：返回 (status, text)。
    """

    def __init__(self, service):
        self.service = service

    def post(self, fields):
        response = self.service.observe(dict(fields))
        return 200, json.dumps(response, ensure_ascii=False)


class StoreForwarder(object):
    """把判定结果交给 8094 收银后端，走它已有的 /api/vision/observe。"""

    def __init__(self, base_url=DEFAULT_STORE_URL, timeout_s=HTTP_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = no_proxy_opener()

    def post_observe(self, label, confidence, session=DEFAULT_SESSION):
        body = json.dumps({"label": label, "confidence": confidence,
                           "session": session}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/vision/observe", data=body,
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            response = self._opener.open(request, timeout=self.timeout_s)
            try:
                text = response.read().decode("utf-8", "replace")
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            return False, "收银后端返回 %s" % exc.code, text
        except Exception as exc:
            return False, "收银后端不可达: %s" % exc, ""
        try:
            parsed = json.loads(text)
        except ValueError:
            return False, "收银后端返回的不是 JSON", text[:400]
        if isinstance(parsed, dict):
            return bool(parsed.get("ok")), parsed.get("message") or "", text
        return False, "收银后端返回结构异常", text[:400]


class FusionService(object):
    """观测入口 + 称重入口 + 幂等转发。"""

    def __init__(self, rules=None, forwarder=None, store_url=DEFAULT_STORE_URL,
                 forward_enabled=True, clock=now_ms):
        self.engine = fruit_fusion.FusionEngine(rules=rules)
        self.forwarder = forwarder if forwarder is not None else StoreForwarder(store_url)
        self.forward_enabled = forward_enabled
        self.clock = clock
        self._forwarded = {}
        self._accepted = 0
        self._forwarded_ok = 0
        self._lock = threading.Lock()
        # 由 main() 在开了 --bridge-url 时挂上，status() 会把它一起报出去
        self.bridge = None

    # ------------------------------------------------------------- 观测入口
    def observe(self, fields):
        """处理一次视觉观测。fields 是表单字典，字段名就是类别名。"""
        observation_id = (fields.get("observation_id") or "").strip()
        if not fruit_fusion.valid_observation_id(observation_id):
            return {"ok": False, "msg": "invalid observation_id"}

        probabilities = {}
        for label in self.engine.labels():
            if label in fields:
                probabilities[label] = fields[label]

        def number(key, default):
            raw = fields.get(key)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        response = self.engine.observe(
            observation_id,
            probabilities,
            int(number("sample_count", 0)),
            number("spread", 0.0),
            number("margin", 0.0),
            self.clock())

        session = (fields.get("session") or DEFAULT_SESSION).strip() or DEFAULT_SESSION
        response["session"] = session
        self._maybe_forward(response, session)
        return response

    def _maybe_forward(self, response, session):
        """只在「判定通过」且「这个 observation_id 还没转发过」时转发。

        engine.observe 本身对 observation_id 幂等，会直接返回上次结果；
        但收银后端的 /api/vision/observe **不幂等**，重放会重复加购。
        所以这层必须自己去重 —— 这是跨服务边界唯一的新增职责。
        """
        observation_id = response.get("observation_id")
        response["forwarded"] = False
        response["forward_message"] = ""
        if not response.get("accepted"):
            return

        with self._lock:
            if observation_id in self._forwarded:
                previous = self._forwarded[observation_id]
                response["forwarded"] = previous[0]
                response["forward_message"] = previous[1] + "（去重，未重复转发）"
                response["duplicate"] = True
                return
            # 只有「首次通过判定」才计数，重放不算
            self._accepted += 1
            if not self.forward_enabled:
                message = "转发已关闭（--no-forward）"
                response["forward_message"] = message
                self._remember(observation_id, False, message)
                return

        ok, message, _raw = self.forwarder.post_observe(
            response.get("label"), response.get("confidence", 0.0), session)
        response["forwarded"] = ok
        response["forward_message"] = message
        response["duplicate"] = False
        with self._lock:
            if ok:
                self._forwarded_ok += 1
            self._remember(observation_id, ok, message)

    def _remember(self, observation_id, ok, message):
        """调用方需持有 self._lock。"""
        if observation_id in self._forwarded:
            del self._forwarded[observation_id]
        self._forwarded[observation_id] = (bool(ok), str(message))
        while len(self._forwarded) > FORWARD_DEDUPE_LIMIT:
            oldest = next(iter(self._forwarded))
            del self._forwarded[oldest]

    # ------------------------------------------------------------- 称重入口
    def add_scale_sample(self, grams, timestamp_ms=None):
        """ESP32-S3 从机喂一个重量样本。timestamp_ms 给了就用，没给就用本地时钟。"""
        try:
            grams = float(grams)
        except (TypeError, ValueError):
            return False, "grams 不是数字"
        stamp = self.clock() if timestamp_ms is None else int(timestamp_ms)
        self.engine.scale.add_sample(grams, stamp)
        return True, "ok"

    def scale_status(self):
        grams, stable, age_ms, ok = self.engine.scale.snapshot(self.clock())
        return {
            "ok": ok,
            "weight_g": round(grams, 1) if grams >= 0 else None,
            "stable": stable,
            "age_ms": age_ms,
            "samples": len(self.engine.scale._samples),
            "sample_target": self.engine.scale._size,
            "max_age_ms": self.engine.scale._max_age_ms,
            "stable_span_g": self.engine.scale._stable_span_g,
        }

    # ---------------------------------------------------------------- 状态
    def status(self):
        with self._lock:
            dedupe_entries = len(self._forwarded)
            accepted = self._accepted
            forwarded_ok = self._forwarded_ok
        return {
            "ok": True,
            "service": "rk3568-fruit-fusion",
            "labels": self.engine.labels(),
            "forward_enabled": self.forward_enabled,
            "store_url": self.forwarder.base_url,
            # 三个口径分清楚，别用一个数糊弄：
            #   accepted_observations 首次通过判定的观测数（重放不计）
            #   forwarded_ok          其中成功交给收银后端的
            #   dedupe_entries        去重表当前条数（有上限，会淘汰）
            "accepted_observations": accepted,
            "forwarded_ok": forwarded_ok,
            "dedupe_entries": dedupe_entries,
            "latest": self.engine.latest(),
            "scale": self.scale_status(),
            "bridge": self.bridge.status() if self.bridge is not None else None,
        }


DASHBOARD = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>水果视觉与重量融合</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f7;color:#18202a;
font-family:Arial,"Microsoft YaHei",sans-serif}.wrap{max-width:820px;margin:auto;padding:18px}
.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.head h1{font-size:20px;margin:0}.head span{font-size:12px;color:#007a5c;
background:#e8f6f1;padding:5px 8px;border-radius:4px}
.card{margin-top:14px;background:#fff;border:1px solid #e1e6ea;border-radius:8px;padding:16px}
.status{font-size:18px;font-weight:700}
.meta{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.meta div{background:#f7f9fa;padding:10px;border-radius:6px}
.meta span{display:block;font-size:11px;color:#687386}.meta b{font-size:16px}
.raw{font:12px monospace;color:#687386;white-space:pre-wrap;margin-top:12px}
</style><body><main class="wrap">
<header class="head"><h1>RK3568 水果视觉与重量融合</h1><span id="state">连接中</span></header>
<section class="card"><div class="status" id="decision">等待识别结果</div>
<div class="meta">
<div><span>识别商品</span><b id="name">-</b></div>
<div><span>融合置信度</span><b id="confidence">-</b></div>
<div><span>HX711 重量</span><b id="weight">-</b></div>
<div><span>重量稳定</span><b id="stable">-</b></div>
<div><span>视觉有效</span><b id="vstable">-</b></div>
<div><span>转发收银</span><b id="fwd">-</b></div>
</div><div class="raw" id="raw"></div></section></main><script>
async function pull(){try{
let d=await (await fetch('/api/vision-fusion/latest?t='+Date.now())).json();
let s=await (await fetch('/api/scale/status?t='+Date.now())).json();
document.getElementById('state').textContent=d.ok?'已连接':'等待视觉节点';
document.getElementById('decision').textContent=
  d.accepted?('已确认：'+d.name):(d.msg||'请人工确认');
document.getElementById('name').textContent=d.name||'-';
document.getElementById('confidence').textContent=
  (d.confidence!==undefined)?(d.confidence*100).toFixed(1)+'%':'-';
document.getElementById('weight').textContent=
  (s.weight_g!==null&&s.weight_g!==undefined)?s.weight_g.toFixed(1)+' g':'-';
document.getElementById('stable').textContent=s.stable?'稳定':'待稳定';
document.getElementById('vstable').textContent=
  (d.vision_stable===undefined)?'-':(d.vision_stable?'有效':'无效');
document.getElementById('fwd').textContent=
  (d.forwarded===undefined)?'-':(d.forwarded?'已加购':(d.forward_message||'未加购'));
document.getElementById('raw').textContent=JSON.stringify(d,null,2)}
catch(e){document.getElementById('state').textContent='等待服务'}}
pull();setInterval(pull,1000)</script></body></html>"""


def build_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code=200):
            self._send(code, json.dumps(payload, ensure_ascii=False))

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            text = raw.decode("utf-8", "replace")
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                try:
                    parsed = json.loads(text)
                    return parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    return {}
            return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}

        def _path(self):
            return urllib.parse.urlparse(self.path).path

        def do_GET(self):
            path = self._path()
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, DASHBOARD, "text/html; charset=utf-8")
                if path == "/api/vision-fusion/latest":
                    return self._json(service.engine.latest())
                if path == "/api/scale/status":
                    return self._json(service.scale_status())
                if path in ("/api/fusion/status", "/health"):
                    return self._json(service.status())
                return self._json({"ok": False, "msg": "未知接口: %s" % path}, 404)
            except Exception as exc:
                return self._json({"ok": False, "msg": "内部错误: %s" % exc}, 500)

        def do_POST(self):
            path = self._path()
            fields = self._read_body()
            try:
                if path == "/api/vision-fusion":
                    return self._json(service.observe(fields))
                if path == "/api/scale/sample":
                    ok, message = service.add_scale_sample(
                        fields.get("grams"), fields.get("timestamp_ms"))
                    return self._json({"ok": ok, "message": message,
                                       "scale": service.scale_status()})
                return self._json({"ok": False, "msg": "未知接口: %s" % path}, 404)
            except Exception as exc:
                return self._json({"ok": False, "msg": "内部错误: %s" % exc}, 500)

    return Handler


STOP = {"flag": False}


def _signal(*_args):
    STOP["flag"] = True


def main():
    parser = argparse.ArgumentParser(description="RK3568 fruit vision+weight fusion")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store-url", default=DEFAULT_STORE_URL)
    parser.add_argument("--rules", default=None, help="fruit_rules.json 路径")
    parser.add_argument("--no-forward", action="store_true",
                        help="只判定不加购（联调时用，避免误加购物车）")
    parser.add_argument("--bridge-url", default=None,
                        help="开启后内置桥接线程，按原工程节奏轮询水果识别服务。"
                             "默认 http://127.0.0.1:8089/api/fruit/result")
    parser.add_argument("--bridge-interval-ms", type=int, default=None,
                        help="桥接轮询间隔，默认 1400（原工程 INFERENCE_INTERVAL_MS）")
    parser.add_argument("--prob-mode", default=None,
                        choices=["top1", "normalize"],
                        help="检测框置信度转概率的方式，默认 top1")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    service = FusionService(store_url=args.store_url,
                            forward_enabled=not args.no_forward)

    bridge = None
    if args.bridge_url:
        import fruit_fusion_bridge
        import vision_observer as vo

        result_url = (fruit_fusion_bridge.DEFAULT_FRUIT_RESULT_URL
                      if args.bridge_url == "auto" else args.bridge_url)
        interval_ms = (args.bridge_interval_ms
                       if args.bridge_interval_ms
                       else fruit_fusion_bridge.POLL_INTERVAL_MS)
        prob_mode = args.prob_mode or vo.PROB_MODE_TOP1
        # 进程内直连，不走 loopback HTTP（自己请求自己没意义）
        observer = vo.VisionObserver(service.engine.labels(),
                                     client=LocalFusionClient(service))
        bridge = fruit_fusion_bridge.FruitBridge(
            observer, result_url=result_url, interval_ms=interval_ms,
            prob_mode=prob_mode)
        service.bridge = bridge
        bridge.start()

    server = ThreadingHTTPServer((args.host, args.port), build_handler(service))
    print("水果融合服务已启动 http://%s:%d/" % (args.host, args.port))
    print("  类别        : %s" % ", ".join(service.engine.labels()))
    print("  收银后端    : %s (%s)" % (args.store_url,
                                      "转发已关闭" if args.no_forward else "判定通过后转发"))
    if bridge is not None:
        print("  内置桥接    : %s 每 %d ms 一次（窗口 %.1f 秒填满）"
              % (bridge.result_url, bridge.interval_ms,
                 bridge.observer.window.size * bridge.interval_ms / 1000.0))
    else:
        print("  视觉节点应提交到: http://<板端IP>:%d/api/vision-fusion" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if bridge is not None:
            bridge.stop()
        server.server_close()
        print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
