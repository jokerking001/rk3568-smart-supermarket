#!/usr/bin/env python3
"""RK3568 fruit detection service (runs alongside the COCO vision service).

Design decision
---------------
The COCO model on port 8088 must stay: the fusion service (8090) consumes its
``person`` detections to drive the customer-session state machine, and swapping in a
three-class fruit model would silently break that.  So fruit detection runs as a
**separate service on its own port**.

It also does **not** open the camera.  A UVC camera generally cannot be consumed by
two processes at once, and the handoff already solved this problem for the dataset
capture service by adding ``/raw.jpg`` to the vision service.  This service reuses
that same overlay-free frame, so there is exactly one camera consumer on the board.

Outputs mirror the vision service so existing tooling and habits transfer:
    GET /                  live MJPEG page with boxes
    GET /stream.mjpg       raw MJPEG stream
    GET /fruit.jpg         latest annotated frame
    GET /api/fruit/result  detections + latency
    GET /api/fruit/status  health, model info, counters
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

# Reuse the board's proven YOLO11 decode path (letterbox + DFL/NMS post-process)
# rather than duplicating it; only the class list differs from COCO.
sys.path.insert(0, "/home/linaro/ai/yolo11")
try:
    from yolo11_infer import letterbox, post_process, OBJ_THRESH, NMS_THRESH
except ImportError:  # pragma: no cover - only when deployed without the vision dir
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from yolo11_infer import letterbox, post_process, OBJ_THRESH, NMS_THRESH

DEFAULT_MODEL = "/home/linaro/ai/models/fruits_yolo11s_i8.rknn"
DEFAULT_PORT = 8089
DEFAULT_RAW_URL = "http://127.0.0.1:8088/raw.jpg"
FRUIT_CLASSES = ("apple", "carrot", "orange")
JPEG_QUALITY = 80

# Per-class colours (BGR) so the page reads at a glance.
CLASS_COLORS = {
    "apple": (60, 60, 220),
    "carrot": (40, 140, 240),
    "orange": (30, 165, 255),
}


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class FruitEngine(object):
    """Fetches frames, runs the fruit model, keeps the latest annotated JPEG."""

    def __init__(self, model_path, raw_url, classes, conf=OBJ_THRESH, nms=NMS_THRESH):
        self.model_path = model_path
        self.raw_url = raw_url
        self.classes = tuple(classes)
        self.conf = conf
        self.nms_thresh = nms
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.runtime = None
        self.model_loaded = False
        self.model_error = ""
        self.latest_jpeg = None
        self.latest_result = {
            "ok": False, "status": "starting", "detections": [],
            "frame_age_ms": None, "inference_ms": None, "pipeline_ms": None,
            "loop_fps": None, "model": os.path.basename(model_path),
            "classes": list(self.classes), "timestamp": None,
        }
        self.counters = {"frames": 0, "detections": 0, "fetch_errors": 0,
                         "infer_errors": 0, "per_class": {}}
        # Tracked even before the model exists, so "model missing" and
        # "vision service unreachable" stay distinguishable when debugging.
        self.frame_source = {"ok": None, "checked_at": None}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.thread = None

    # ------------------------------------------------------------------ model
    def load_model(self):
        if not os.path.exists(self.model_path):
            self.model_loaded = False
            self.model_error = "模型文件不存在: %s" % self.model_path
            return False
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            self.model_loaded = False
            self.model_error = "rknnlite 不可用: %s" % exc
            return False
        try:
            runtime = RKNNLite()
            if runtime.load_rknn(self.model_path) != 0:
                self.model_error = "load_rknn 失败（模型与运行时版本不匹配？）"
                return False
            if runtime.init_runtime() != 0:
                self.model_error = "init_runtime 失败"
                return False
            self.runtime = runtime
            self.model_loaded = True
            self.model_error = ""
            return True
        except Exception as exc:
            self.model_loaded = False
            self.model_error = "加载异常: %s" % exc
            return False

    # ----------------------------------------------------------------- frames
    def fetch_frame(self):
        request = urllib.request.Request(self.raw_url)
        with self.opener.open(request, timeout=5) as response:
            return response.read()

    def annotate(self, frame, detections):
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            color = CLASS_COLORS.get(det["class"], (200, 200, 200))
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            label = "%s %.2f" % (det["class"], det["confidence"])
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(frame, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), color, -1)
            cv2.putText(frame, label, (x1 + 2, max(12, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return frame

    # ------------------------------------------------------------------- loop
    def _probe_frame_source(self):
        """Check that /raw.jpg is reachable and decodable, without a model loaded."""
        try:
            blob = self.fetch_frame()
            probe = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
            with self.lock:
                self.frame_source = {
                    "ok": probe is not None,
                    "bytes": len(blob),
                    "shape": list(probe.shape) if probe is not None else None,
                    "checked_at": now_iso(),
                }
        except Exception as exc:
            with self.lock:
                self.frame_source = {"ok": False, "error": str(exc),
                                     "checked_at": now_iso()}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="fruit-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        if not self.load_model():
            with self.lock:
                self.latest_result["status"] = "model_unavailable"
                self.latest_result["error"] = self.model_error
            print("[fruit] model unavailable: %s" % self.model_error, flush=True)

        period = 1.0 / 10.0
        last = time.monotonic()
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                if not self.model_loaded:
                    # Keep retrying: the model is usually installed after the
                    # service starts (deploy_model.sh restarts things).
                    self.load_model()
                    self._probe_frame_source()
                    time.sleep(2.0)
                    continue

                blob = self.fetch_frame()
                buffer = np.frombuffer(blob, dtype=np.uint8)
                frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError("无法解码原始帧")

                inp, ratio, dw, dh = letterbox(frame)
                inp = cv2.cvtColor(inp, cv2.COLOR_BGR2RGB)
                infer_started = time.monotonic()
                outputs = self.runtime.inference(
                    inputs=[np.expand_dims(inp, 0)], data_format=["nhwc"])
                infer_ms = (time.monotonic() - infer_started) * 1000.0

                boxes, classes, scores = post_process(outputs)
                detections = []
                if boxes is not None:
                    boxes = boxes.astype(np.float32)
                    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - dw) / ratio
                    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - dh) / ratio
                    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, frame.shape[1] - 1)
                    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, frame.shape[0] - 1)
                    for box, cls, score in zip(boxes, classes, scores):
                        index = int(cls)
                        name = (self.classes[index] if index < len(self.classes)
                                else "class_%d" % index)
                        detections.append({"class": name, "confidence": round(float(score), 3),
                                           "box": [int(v) for v in box]})

                annotated = self.annotate(frame.copy(), detections)
                ok, encoded = cv2.imencode(".jpg", annotated,
                                           [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                pipeline_ms = (time.monotonic() - started) * 1000.0

                with self.lock:
                    if ok:
                        self.latest_jpeg = encoded.tobytes()
                    self.counters["frames"] += 1
                    self.counters["detections"] += len(detections)
                    for det in detections:
                        key = det["class"]
                        self.counters["per_class"][key] = self.counters["per_class"].get(key, 0) + 1
                    self.latest_result = {
                        "ok": True,
                        "status": "running",
                        "detections": detections,
                        "inference_ms": round(infer_ms, 2),
                        "pipeline_ms": round(pipeline_ms, 2),
                        "loop_fps": round(1.0 / max(1e-3, time.monotonic() - last), 2),
                        "model": os.path.basename(self.model_path),
                        "classes": list(self.classes),
                        "counters": dict(self.counters),
                        "timestamp": now_iso(),
                    }
            except urllib.error.URLError as exc:
                with self.lock:
                    self.counters["fetch_errors"] += 1
                    self.latest_result["status"] = "vision_unavailable"
                    self.latest_result["error"] = "取原始帧失败: %s" % exc
                    self.latest_result["timestamp"] = now_iso()
            except Exception as exc:
                with self.lock:
                    self.counters["infer_errors"] += 1
                    self.latest_result["status"] = "error"
                    self.latest_result["error"] = str(exc)
                    self.latest_result["timestamp"] = now_iso()

            last = time.monotonic()
            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

    # ------------------------------------------------------------------ access
    def snapshot(self):
        with self.lock:
            return dict(self.latest_result)

    def jpeg(self):
        with self.lock:
            return self.latest_jpeg

    def status(self):
        with self.lock:
            return {
                "ok": True,
                "service": "rk3568-fruit",
                "model_loaded": self.model_loaded,
                "model": self.model_path,
                "model_error": self.model_error,
                "classes": list(self.classes),
                "raw_frame_url": self.raw_url,
                "frame_source": dict(self.frame_source),
                "conf_threshold": self.conf,
                "nms_threshold": self.nms_thresh,
                "counters": dict(self.counters),
                "last_result": dict(self.latest_result),
                "time": now_iso(),
            }


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RK3568 水果识别</title>
<style>
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#1f2328}
header{background:#fff;border-bottom:1px solid #e3e6ea;padding:12px 18px;display:flex;
gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.pill{background:#eef1f4;border-radius:999px;padding:3px 10px;font-size:12px;color:#4a5158}
.pill.ok{background:#e6f4ea;color:#1c6b34}.pill.bad{background:#fdecea;color:#a5271c}
main{padding:14px;display:grid;gap:14px;grid-template-columns:2fr 1fr}
@media(max-width:900px){main{grid-template-columns:1fr}}
section{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px}
h2{font-size:14px;margin:0 0 10px;font-weight:600;color:#3a4149}
img{width:100%;border-radius:8px;background:#111;display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f2}
th{color:#6b7280;font-weight:500;font-size:12px}
td.num,th.num{text-align:right}
.empty{color:#8b939b;padding:12px;text-align:center;font-size:13px}
pre{margin:10px 0 0;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace;
background:#fafbfc;border:1px solid #eef0f2;border-radius:8px;padding:10px;max-height:260px;overflow:auto}
</style></head><body>
<header><h1>RK3568 水果识别</h1>
<span class="pill" id="p_status">连接中…</span>
<span class="pill" id="p_model"></span>
<span class="pill" id="p_perf"></span></header>
<main>
<section><h2>实时画面</h2>
<img id="stream" src="/stream.mjpg" alt="fruit stream">
</section>
<section><h2>识别结果</h2>
<div id="dets"><div class="empty">暂无目标</div></div>
<h2 style="margin-top:14px">状态</h2>
<pre id="status">加载中…</pre>
</section>
</main>
<script>
function refresh(){
fetch('/api/fruit/result').then(function(r){return r.json()}).then(function(d){
var e=document.getElementById('p_status');
e.textContent=d.status||'-';e.className='pill '+((d.status==='running')?'ok':'bad');
document.getElementById('p_model').textContent=d.model||'-';
document.getElementById('p_perf').textContent=
  (d.inference_ms!=null?('推理 '+d.inference_ms+'ms '):'')+
  (d.loop_fps!=null?('· '+d.loop_fps+' FPS'):'');
var el=document.getElementById('dets');
if(!d.detections||!d.detections.length){el.innerHTML='<div class="empty">暂无目标</div>';}
else{
var h='<table><tr><th>类别</th><th class="num">置信度</th><th>位置</th></tr>';
d.detections.forEach(function(x){h+='<tr><td>'+x['class']+'</td><td class="num">'+
x.confidence.toFixed(2)+'</td><td>['+x.box.join(', ')+']</td></tr>';});
el.innerHTML=h+'</table>';}
});
fetch('/api/fruit/status').then(function(r){return r.json()}).then(function(d){
document.getElementById('status').textContent=JSON.stringify(
  {model_loaded:d.model_loaded,model_error:d.model_error,classes:d.classes,
   counters:d.counters},null,2);});
}
refresh();setInterval(refresh,1000);
</script></body></html>
"""


class FruitHandler(BaseHTTPRequestHandler):
    engine = None
    server_version = "rk3568-fruit/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _mjpeg(self):
        boundary = "fruitframe"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=%s" % boundary)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                frame = self.engine.jpeg()
                if frame:
                    self.wfile.write(b"--" + boundary.encode() + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(("Content-Length: %d\r\n\r\n" % len(frame)).encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/stream.mjpg":
                return self._mjpeg()
            if path == "/fruit.jpg":
                frame = self.engine.jpeg()
                if not frame:
                    return self._json({"ok": False, "message": "暂无画面"}, 503)
                return self._send(200, frame, "image/jpeg")
            if path == "/api/fruit/result":
                return self._json(self.engine.snapshot())
            if path == "/api/fruit/status":
                return self._json(self.engine.status())
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)


def main():
    parser = argparse.ArgumentParser(description="RK3568 fruit detection service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--raw-url", default=DEFAULT_RAW_URL)
    parser.add_argument("--classes", default=",".join(FRUIT_CLASSES),
                        help="comma separated class names, index order must match training")
    parser.add_argument("--conf", type=float, default=OBJ_THRESH)
    parser.add_argument("--nms", type=float, default=NMS_THRESH)
    args = parser.parse_args()

    engine = FruitEngine(args.model, args.raw_url,
                         [c.strip() for c in args.classes.split(",") if c.strip()],
                         conf=args.conf, nms=args.nms)
    FruitHandler.engine = engine
    engine.start()
    server = ThreadingHTTPServer((args.host, args.port), FruitHandler)
    server.daemon_threads = True
    print("fruit service listening on %d (model=%s)" % (args.port, args.model), flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        server.server_close()


if __name__ == "__main__":
    main()
