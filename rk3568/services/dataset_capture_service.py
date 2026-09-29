#!/usr/bin/env python3
"""Collect unannotated, deployment-camera images for supermarket SKU training.

The service deliberately asks the running vision service for /raw.jpg instead
of opening /dev/video9 itself.  Thus it cannot add a second V4L2 consumer or
reintroduce camera-frame latency while the RKNN detector is running.
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


RAW_CAMERA_URL = "http://127.0.0.1:8088/raw.jpg"
ROOT = "/home/linaro/ai/dataset"
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
LOCK = threading.Lock()
STATE = {"ok": True, "status": "ready", "last_capture": None, "error": ""}


def safe_label(value):
    value = str(value or "").strip()
    if not LABEL_RE.fullmatch(value):
        raise ValueError("SKU 标签只能使用英文字母、数字、下划线和短横线，长度 1-64")
    return value


def fetch_raw_image(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with opener.open(request, timeout=3.0) as response:
        content_type = response.headers.get("Content-Type", "")
        image = response.read(4 * 1024 * 1024)
    if "image/jpeg" not in content_type.lower() or not image.startswith(b"\xff\xd8"):
        raise RuntimeError("vision-service did not return a JPEG raw frame")
    return image


def label_counts(root):
    images_root = os.path.join(root, "images")
    result = {}
    try:
        for label in sorted(os.listdir(images_root)):
            path = os.path.join(images_root, label)
            if os.path.isdir(path):
                result[label] = len([name for name in os.listdir(path) if name.lower().endswith(".jpg")])
    except OSError:
        pass
    return result


def capture(root, raw_url, label, count, interval_ms):
    label = safe_label(label)
    count = max(1, min(int(count), 30))
    interval_ms = max(250, min(int(interval_ms), 5000))
    destination = os.path.join(root, "images", label)
    os.makedirs(destination, exist_ok=True)
    manifest_path = os.path.join(root, "manifest.jsonl")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    saved = []
    with LOCK:
        STATE.update({"status": "capturing", "error": ""})
        for index in range(count):
            image = fetch_raw_image(raw_url)
            filename = "%s_%03d.jpg" % (stamp, index + 1)
            path = os.path.join(destination, filename)
            with open(path, "wb") as handle:
                handle.write(image)
            item = {
                "captured_at": datetime.now().isoformat(timespec="milliseconds"),
                "label": label,
                "path": os.path.relpath(path, root),
                "source": raw_url,
                "annotated": False,
            }
            with open(manifest_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            saved.append(item)
            if index + 1 < count:
                time.sleep(interval_ms / 1000.0)
        STATE.update({"status": "ready", "last_capture": saved[-1], "error": ""})
    return saved


class Handler(BaseHTTPRequestHandler):
    root = ROOT
    raw_url = RAW_CAMERA_URL

    def log_message(self, *_args):
        return

    def reply_json(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/dataset/status") or self.path.startswith("/health"):
            self.reply_json(200, dict(STATE, root=self.root, labels=label_counts(self.root)))
            return
        page = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>智慧超市 SKU 图像采集</title>
<style>body{font-family:Arial,"Microsoft YaHei";max-width:880px;margin:auto;padding:22px;background:#101418;color:#e8eef2}.card{background:#1b232a;border:1px solid #2d3a44;border-radius:10px;padding:16px;margin:12px 0}input,button{font-size:16px;padding:9px;margin:5px 0}input{box-sizing:border-box;width:100%;background:#0e151a;color:#fff;border:1px solid #52616d;border-radius:5px}button{cursor:pointer;background:#1976d2;color:#fff;border:0;border-radius:5px}button:disabled{opacity:.5}.hint{color:#9eb0bd;line-height:1.55}pre{white-space:pre-wrap;word-break:break-word}</style>
<h2>智慧超市 SKU 图像采集</h2><div class="card"><p class="hint">保存的是没有检测框和文字叠加的原始相机图。每次采集后仍需在 CVAT/Label Studio 中标注商品框；不要只采正面白底图，要包含不同距离、角度、光照、遮挡和真实货架背景。</p><label>SKU/类别标签（例如 <code>coke_500ml</code>）</label><input id="label" value="product_demo"><label>本次张数（1–30）</label><input id="count" type="number" min="1" max="30" value="5"><label>间隔毫秒（至少250）</label><input id="interval" type="number" min="250" max="5000" value="500"><button id="go" onclick="cap()">采集原始图片</button></div><div class="card"><b>当前数据集</b><pre id="s">读取中…</pre></div>
<script>async function status(){let d=await(await fetch('/api/dataset/status?t='+Date.now())).json();s.textContent=JSON.stringify(d,null,2)}async function cap(){go.disabled=true;try{let r=await fetch('/api/dataset/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:label.value,count:+count.value,interval_ms:+interval.value})});let d=await r.json();alert(r.ok?'已保存 '+d.saved.length+' 张图片':d.error);await status()}catch(e){alert(e)}finally{go.disabled=false}}status();setInterval(status,1500)</script>'''
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.startswith("/api/dataset/capture"):
            self.reply_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("请求体长度无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            saved = capture(self.root, self.raw_url, payload.get("label"),
                            payload.get("count", 1), payload.get("interval_ms", 500))
            self.reply_json(200, {"ok": True, "saved": saved, "labels": label_counts(self.root)})
        except (ValueError, urllib.error.URLError, RuntimeError, OSError) as exc:
            STATE.update({"status": "error", "error": str(exc)})
            self.reply_json(400, {"ok": False, "error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--raw-url", default=RAW_CAMERA_URL)
    parser.add_argument("--port", type=int, default=8093)
    args = parser.parse_args()
    Handler.root = args.root
    Handler.raw_url = args.raw_url
    os.makedirs(os.path.join(args.root, "images"), exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
