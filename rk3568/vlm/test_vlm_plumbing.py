#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
不依赖大模型的「识图问答」管线自测。

思路沿用主项目里验证水果服务时的做法：先用假后端把整条链路跑通，
这样真模型一到位，出问题就必定在权重而不在代码。

本脚本用一个本地 OpenAI 兼容假后端替换真实模型，验证：
  1. 图片上传 → base64 → data URI 的构造是否正确；
  2. 请求体字段（model / messages / image_url / max_tokens / temperature）是否齐全；
  3. 回答解析、历史记录、统计是否正常；
  4. 错误路径（后端 500、缺图）是否返回可读的失败原因。

用法：
  python test_vlm_plumbing.py                      # 只测 upload 路径
  python test_vlm_plumbing.py --board 10.181.229.215   # 额外测真实板端取帧路径
  python test_vlm_plumbing.py --service http://127.0.0.1:8097
"""

import argparse
import base64
import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 1x1 的最小合法 JPEG，用来当上传素材，避免依赖 PIL
TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

CANNED_ANSWER = "假后端回答：我看到一件商品。"
MOCK_PORT = 18098

RECEIVED = []          # 假后端收到的请求体


class MockHandler(BaseHTTPRequestHandler):
    """极简 OpenAI 兼容多模态接口，只用来验证请求形状。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            body = {}
        RECEIVED.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
        })

        if "/fail" in self.path:
            payload = json.dumps({"error": {"message": "故意失败"}}).encode()
            self.send_response(500)
        else:
            payload = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": CANNED_ANSWER}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def post_json(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=timeout)
    try:
        return json.loads(resp.read().decode("utf-8", "replace"))
    finally:
        resp.close()


def get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=timeout)
    try:
        return json.loads(resp.read().decode("utf-8", "replace"))
    finally:
        resp.close()


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def main():
    parser = argparse.ArgumentParser(description="识图问答管线自测（不需要真实模型）")
    parser.add_argument("--service", default="http://127.0.0.1:8097", help="电脑侧 VLM 服务地址")
    parser.add_argument("--board", default=None, help="额外测试真实板端取帧，传板端 IP")
    parser.add_argument("--restore-ollama", action="store_true",
                        help="测完把配置恢复成 ollama 后端（默认会恢复）")
    args = parser.parse_args()

    service = args.service.rstrip("/")
    print("=" * 64)
    print("识图问答管线自测")
    print("  被测服务: %s" % service)
    print("=" * 64)

    # 服务在线
    try:
        health = get_json(service + "/health")
        check("服务存活 /health", health.get("ok") is True)
    except Exception as exc:
        check("服务存活 /health", False, str(exc))
        return report()

    original = get_json(service + "/api/vlm/status").get("config", {})
    print("  原配置: %s" % json.dumps(original, ensure_ascii=False))

    # 起假后端
    mock_port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % mock_port
    print("  假后端: %s" % base)

    try:
        # 切到假后端
        cfg = post_json(service + "/api/vlm/config", {
            "backend": "openai",
            "openai_base": base,
            "openai_model": "mock-vl",
            "api_key": "test-key",
        })
        check("切换到假后端 /api/vlm/config", cfg.get("ok") is True, json.dumps(cfg, ensure_ascii=False)[:160])

        status = get_json(service + "/api/vlm/status")
        check("状态显示假后端就绪", status.get("backend", {}).get("ready") is True,
              status.get("backend", {}).get("detail", ""))

        # ---- 1. upload 路径 ----
        RECEIVED.clear()
        t0 = time.time()
        res = post_json(service + "/api/vlm/ask", {
            "question": "画面里有什么？",
            "source": "upload",
            "image_b64": TINY_JPEG_B64,
        }, timeout=90)
        dt = int((time.time() - t0) * 1000)

        check("upload 路径返回 ok", res.get("ok") is True, str(res.get("error", ""))[:200])
        check("回答内容与假后端一致", res.get("answer") == CANNED_ANSWER, str(res.get("answer"))[:80])
        check("返回 image_bytes 合理", isinstance(res.get("image_bytes"), int) and res["image_bytes"] > 0,
              str(res.get("image_bytes")))
        check("返回 latency_ms", isinstance(res.get("latency_ms"), int), "%s (实测 %d ms)" % (res.get("latency_ms"), dt))

        # ---- 2. 请求形状 ----
        check("假后端收到 1 个请求", len(RECEIVED) == 1, "收到 %d" % len(RECEIVED))
        if RECEIVED:
            req = RECEIVED[0]
            body = req["body"]
            check("路径是 /v1/chat/completions", req["path"].endswith("/v1/chat/completions"), req["path"])
            check("带 Bearer 鉴权", (req.get("auth") or "") == "Bearer test-key", str(req.get("auth")))
            check("model 字段正确", body.get("model") == "mock-vl", str(body.get("model")))
            check("max_tokens 已传", body.get("max_tokens") is not None, str(body.get("max_tokens")))
            check("temperature 已传", body.get("temperature") is not None, str(body.get("temperature")))

            msgs = body.get("messages") or []
            check("含 system + user 两条消息", len(msgs) == 2, "实际 %d" % len(msgs))
            user_content = (msgs[1].get("content") if len(msgs) > 1 else []) or []
            has_text = any(p.get("type") == "text" for p in user_content if isinstance(p, dict))
            has_image = any(
                p.get("type") == "image_url"
                and str(p.get("image_url", {}).get("url", "")).startswith("data:image/jpeg;base64,")
                for p in user_content if isinstance(p, dict)
            )
            check("user 消息含 text 分段", has_text)
            check("user 消息含 data URI 图片", has_image)

        # ---- 3. 历史与统计 ----
        hist = get_json(service + "/api/vlm/history")
        items = hist.get("items") or []
        check("历史已记录本次问答", len(items) >= 1 and items[0].get("answer") == CANNED_ANSWER,
              "历史 %d 条" % len(items))

        status2 = get_json(service + "/api/vlm/status")
        check("统计 ask_total 已累加", (status2.get("stats") or {}).get("ask_total", 0) >= 1,
              json.dumps(status2.get("stats"), ensure_ascii=False))

        # ---- 4. 错误路径：后端 500 ----
        post_json(service + "/api/vlm/config", {"openai_base": base + "/fail"})
        res_fail = post_json(service + "/api/vlm/ask", {
            "question": "会失败的问题",
            "source": "upload",
            "image_b64": TINY_JPEG_B64,
        }, timeout=90)
        check("后端 500 时返回 ok=false", res_fail.get("ok") is False, json.dumps(res_fail, ensure_ascii=False)[:200])
        check("失败原因可读", "500" in str(res_fail.get("error", "")), str(res_fail.get("error"))[:160])
        post_json(service + "/api/vlm/config", {"openai_base": base})

        # ---- 5. 错误路径：缺图 ----
        res_noimg = post_json(service + "/api/vlm/ask", {"question": "没有图", "source": "upload"}, timeout=60)
        check("缺图时返回 ok=false", res_noimg.get("ok") is False, str(res_noimg.get("error"))[:160])

        # ---- 6. 可选：真实板端取帧 ----
        if args.board:
            print("  --- 真实板端取帧路径（热点慢，请耐心）---")
            post_json(service + "/api/vlm/config", {"backend": "openai", "board_ip": args.board})
            t0 = time.time()
            res_board = post_json(service + "/api/vlm/ask", {
                "question": "画面里有什么？",
                "source": "board",
                "board_ip": args.board,
            }, timeout=300)
            dt = int((time.time() - t0) * 1000)
            check("board 路径返回 ok", res_board.get("ok") is True, str(res_board.get("error", ""))[:200])
            check("board 路径拿到真实图片",
                  isinstance(res_board.get("image_bytes"), int) and res_board["image_bytes"] > 10000,
                  "image_bytes=%s, 端到端 %d ms" % (res_board.get("image_bytes"), dt))

    finally:
        # 恢复原后端配置
        restore = {
            "backend": original.get("backend", "ollama"),
            "openai_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "openai_model": "qwen-vl-max",
            "api_key": "",
        }
        if original.get("board_ip"):
            restore["board_ip"] = original["board_ip"]
        try:
            post_json(service + "/api/vlm/config", restore)
            print("  已恢复原配置: %s" % json.dumps(restore, ensure_ascii=False))
        except Exception as exc:
            print("  [warn] 恢复配置失败: %s" % exc)
        httpd.shutdown()
        httpd.server_close()

    return report()


def report():
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("-" * 64)
    print("%d passed, %d failed" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
