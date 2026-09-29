#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
假 VLM 后端：一个最小的 OpenAI 兼容多模态接口，用来在没有真实模型时
验证「板端 → 电脑 → 大模型」整条链路。

它会把收到的图片字节数写进回答里，这样一眼就能确认图片真的传到了。

  python mock_vlm_backend.py --port 18098
  # 然后把电脑侧服务切过来：
  #   POST http://127.0.0.1:8097/api/vlm/config
  #   {"backend":"openai","openai_base":"http://127.0.0.1:18098/v1",
  #    "openai_model":"mock-vl","api_key":"test"}
"""

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNT = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock] %s\n" % (fmt % args))

    def do_GET(self):
        if self.path.startswith("/stats"):
            body = json.dumps({"requests": COUNT["n"]}).encode()
            self.send_response(200)
        else:
            body = json.dumps({"ok": True, "mock": "vlm"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            body = {}
        COUNT["n"] += 1

        # 从 data URI 里量出图片真实大小
        image_bytes = 0
        question = ""
        for msg in body.get("messages") or []:
            content = msg.get("content")
            if isinstance(content, str):
                question = content
                continue
            for part in content or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    question = part.get("text") or question
                elif part.get("type") == "image_url":
                    url = str((part.get("image_url") or {}).get("url") or "")
                    if "," in url:
                        image_bytes = len(url.split(",", 1)[1]) * 3 // 4

        answer = ("假后端回答：收到图片约 %d KB，问题是「%s」。"
                  "（这条链路已经通了，换成真模型即可。）" % (image_bytes // 1024, question))
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": answer}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description="假 VLM 后端（OpenAI 兼容）")
    parser.add_argument("--port", type=int, default=18098)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("假 VLM 后端已启动: http://%s:%d/v1  (OpenAI 兼容)" % (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
