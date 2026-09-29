#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RK3568 智慧超市 - 板端「识图问答」服务

板子自己跑不动视觉大模型（内存约 2.7 GB 可用、根分区约 1.4 GB），
所以这里只做三件事：

  1. 复用视觉服务的 http://127.0.0.1:8088/raw.jpg 拿一帧（绝不自己开摄像头）；
  2. 把「问题 + 板端 IP」发给电脑上的 vlm_service.py；
  3. 把电脑返回的中文回答展示在网页 / API 上。

图片由电脑侧主动来板子拉，板端不传 base64，省掉 33% 的传输量
（这条热点链路带宽很紧张）。

板端环境：Debian 10 / aarch64 / Python 3.7.3 —— 不能用 walrus、
不能用 dict 合并运算符、不能用 list[str] 标注。

用法：
  python3 vlm_client_service.py --port 8092 --pc http://10.181.229.84:8097
  curl -s --noproxy '*' http://127.0.0.1:8092/api/vlm/status
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "vlm_client_config.json")

DEFAULT_CONFIG = {
    "pc_url": "http://10.181.229.84:8097",   # 电脑侧 VLM 服务，IP 随热点变化
    "vision_url": "http://127.0.0.1:8088",   # 板端视觉服务，取帧用
    "host": "0.0.0.0",
    "port": 8092,
    "pc_timeout_s": 240,                     # 大模型首次加载会慢
    "frame_timeout_s": 20,
}

STATS = {
    "ask_total": 0,
    "ask_failed": 0,
    "last_latency_ms": None,
    "last_error": None,
    "last_answer": None,
}
HISTORY = []
HISTORY_LOCK = threading.Lock()
MAX_HISTORY = 20

# 进程内生效的配置：默认值 + 配置文件 + 环境变量 + 命令行参数。
# 请求处理器必须用 get_config()，否则命令行参数会被磁盘旧配置覆盖。
RUNTIME = {"cfg": None}
RUNTIME_LOCK = threading.Lock()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            fh = open(CONFIG_PATH, "r")
            try:
                cfg.update(json.load(fh) or {})
            finally:
                fh.close()
        except Exception as exc:
            print("[warn] 读取配置失败，使用默认值: %s" % exc)
    for env_key, cfg_key in (("VLM_PC_URL", "pc_url"), ("VLM_VISION_URL", "vision_url")):
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val
    return cfg


def save_config(cfg):
    try:
        fh = open(CONFIG_PATH, "w")
        try:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        finally:
            fh.close()
        return True
    except Exception as exc:
        print("[warn] 保存配置失败: %s" % exc)
        return False


def set_runtime_config(cfg):
    with RUNTIME_LOCK:
        RUNTIME["cfg"] = cfg


def get_config():
    with RUNTIME_LOCK:
        cfg = RUNTIME["cfg"]
    if cfg is None:
        cfg = get_config()
        set_runtime_config(cfg)
    return cfg


# --------------------------------------------------------------------------
# HTTP 工具：板端 shell 里可能有 http_proxy，访问本机/局域网必须显式绕过
# --------------------------------------------------------------------------

def no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json_post(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    opener = no_proxy_opener()
    resp = opener.open(req, timeout=timeout)
    try:
        body = resp.read().decode("utf-8", "replace")
    finally:
        resp.close()
    return json.loads(body)


def http_get_bytes(url, timeout):
    req = urllib.request.Request(url, method="GET")
    opener = no_proxy_opener()
    resp = opener.open(req, timeout=timeout)
    try:
        return resp.read()
    finally:
        resp.close()


def guess_local_ip():
    """拿本机在热点网段上的 IP（用于兜底，正常走 Host 头）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))       # 不发包，只为让内核选出出口地址
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


# --------------------------------------------------------------------------
# 网页
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RK3568 识图问答</title>
<style>
  :root{ --bg:#111418; --panel:#1a1f26; --line:#2b333d; --text:#e8ecf1;
         --muted:#8f9aa8; --accent:#3d8bfd; --ok:#39b26b; --bad:#e05c4b; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{padding:14px 20px;background:var(--panel);border-bottom:1px solid var(--line);
         display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{font-size:17px;margin:0;font-weight:600}
  .meta{font-size:13px;color:var(--muted)}
  main{display:grid;grid-template-columns:minmax(300px,1fr) minmax(340px,1fr);
       gap:16px;padding:16px 20px;max-width:1240px;margin:0 auto}
  @media (max-width:900px){main{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:14px;display:flex;flex-direction:column;gap:10px}
  .card h2{font-size:13px;margin:0;color:var(--muted);font-weight:600}
  img{width:100%;aspect-ratio:4/3;background:#000;border-radius:8px;object-fit:contain}
  .row{display:flex;gap:8px;flex-wrap:wrap}
  input[type=text]{flex:1;min-width:110px;padding:9px 11px;border:1px solid var(--line);
                   border-radius:8px;font:inherit;background:#0e1216;color:var(--text)}
  button{padding:9px 14px;border:1px solid var(--line);background:#222a33;color:var(--text);
         border-radius:8px;font:inherit;cursor:pointer}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.primary:disabled{opacity:.5;cursor:default}
  .chip{font-size:13px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);
        color:var(--muted);cursor:pointer;background:#0e1216}
  .answer{min-height:130px;white-space:pre-wrap;background:#0e1216;border:1px solid var(--line);
          border-radius:8px;padding:12px}
  .status{font-size:13px;color:var(--muted)}
  .ok{color:var(--ok)} .bad{color:var(--bad)}
  .hist{font-size:13px;color:var(--muted);max-height:150px;overflow:auto}
  .hist div{padding:5px 0;border-bottom:1px dashed var(--line)}
</style>
</head>
<body>
<header>
  <h1>RK3568 识图问答</h1>
  <span class="meta" id="head">正在读取状态…</span>
</header>
<main>
  <section class="card">
    <h2>摄像头画面（视觉服务 8088）</h2>
    <img id="cam" alt="等待视觉服务">
    <div class="row">
      <button onclick="reload()">刷新</button>
      <label class="status"><input type="checkbox" id="auto" checked> 自动刷新</label>
    </div>
    <div class="status" id="boardLine">—</div>
  </section>
  <section class="card">
    <h2>提问</h2>
    <div class="row">
      <input type="text" id="q" placeholder="例如：画面里有什么商品？">
      <button class="primary" id="go" onclick="ask()">识图回答</button>
    </div>
    <div class="row" id="chips"></div>
    <div class="answer" id="a">回答会显示在这里。</div>
    <div class="status" id="line">—</div>
    <h2>最近问答</h2>
    <div class="hist" id="hist"></div>
  </section>
</main>
<script>
var PRESETS = ["画面里有什么商品？", "图里的文字写了什么？", "这是什么东西？", "画面里有几个人？"];
function el(i){ return document.getElementById(i); }
function renderChips(){
  el("chips").innerHTML = "";
  PRESETS.forEach(function(q){
    var b = document.createElement("span");
    b.className = "chip"; b.textContent = q;
    b.onclick = function(){ el("q").value = q; };
    el("chips").appendChild(b);
  });
}
function reload(){ el("cam").src = "http://" + location.hostname + ":8088/raw.jpg?t=" + Date.now(); }
function loadStatus(){
  fetch("/api/vlm/status").then(function(r){ return r.json(); }).then(function(s){
    el("head").textContent = "电脑侧 " + s.pc_url + " · " + (s.pc_reachable ? "可达" : "不可达")
      + " · 模型 " + (s.model || "-");
    el("head").className = "meta " + (s.pc_reachable ? "ok" : "bad");
    el("boardLine").textContent = s.detail || "—";
  }).catch(function(e){ el("head").textContent = "状态读取失败: " + e; });
}
function ask(){
  var q = el("q").value.trim();
  if (!q) { el("a").textContent = "请先输入问题。"; return; }
  var b = el("go"); b.disabled = true;
  el("a").textContent = "思考中…（首次调用要加载模型，可能较慢）";
  el("line").textContent = "—";
  fetch("/api/vlm/ask", {method:"POST", headers:{"Content-Type":"application/json"},
                         body: JSON.stringify({question:q})})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.ok){
        el("a").textContent = d.answer;
        el("line").textContent = "耗时 " + d.latency_ms + " ms · 图片 " + Math.round((d.image_bytes||0)/1024) + " KB";
        el("line").className = "status ok";
        refreshHistory();
      } else {
        el("a").textContent = "失败：" + (d.error || "未知错误");
        el("line").className = "status bad";
      }
    })
    .catch(function(e){ el("a").textContent = "请求异常：" + e; })
    .then(function(){ b.disabled = false; });
}
function refreshHistory(){
  fetch("/api/vlm/history").then(function(r){ return r.json(); }).then(function(h){
    el("hist").innerHTML = "";
    (h.items||[]).forEach(function(it){
      var d = document.createElement("div");
      d.textContent = "[" + it.ts + "] " + it.question + " → " + it.answer;
      el("hist").appendChild(d);
    });
  }).catch(function(){});
}
renderChips(); loadStatus(); reload(); refreshHistory();
setInterval(function(){ if (el("auto").checked) reload(); }, 5000);
setInterval(loadStatus, 15000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RK3568VLMClient/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def _board_ip(self):
        """浏览器访问的是板子的 IP，Host 头就是板端地址。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip()
        if host and host not in ("127.0.0.1", "localhost"):
            return host
        return guess_local_ip()

    # ---- 路由 ----
    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/health":
                self._json(200, {"ok": True, "ts": int(time.time())})
            elif path == "/api/vlm/status":
                self._status()
            elif path == "/api/vlm/history":
                with HISTORY_LOCK:
                    self._json(200, {"ok": True, "items": list(HISTORY)})
            else:
                self._json(404, {"ok": False, "error": "no route: %s" % path})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            body = self._read_json()
            if path == "/api/vlm/ask":
                self._ask(body)
            elif path == "/api/vlm/config":
                self._config(body)
            else:
                self._json(404, {"ok": False, "error": "no route: %s" % path})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    # ---- 处理 ----
    def _status(self):
        cfg = get_config()
        detail = ""
        reachable = False
        model = ""
        try:
            req = urllib.request.Request(cfg["pc_url"].rstrip("/") + "/api/vlm/status")
            resp = no_proxy_opener().open(req, timeout=15)
            try:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                resp.close()
            reachable = True
            model = ((data or {}).get("backend") or {}).get("model", "")
            detail = ((data or {}).get("backend") or {}).get("detail", "")
        except Exception as exc:
            detail = "电脑侧 VLM 服务不可达: %s" % exc
        self._json(200, {
            "ok": True,
            "pc_url": cfg["pc_url"],
            "pc_reachable": reachable,
            "model": model,
            "detail": detail,
            "vision_url": cfg["vision_url"],
            "stats": dict(STATS, history_len=len(HISTORY)),
        })

    def _ask(self, body):
        cfg = get_config()
        question = (body.get("question") or "").strip()
        if not question:
            self._json(400, {"ok": False, "error": "question 不能为空"})
            return

        board_ip = (body.get("board_ip") or "").strip() or self._board_ip()
        t0 = time.time()
        image_bytes = 0

        # 先确认板端视觉服务有帧（这样失败时能给出更准确的报错）
        try:
            frame = http_get_bytes(cfg["vision_url"].rstrip("/") + "/raw.jpg",
                                   timeout=int(cfg["frame_timeout_s"]))
            image_bytes = len(frame)
        except Exception as exc:
            self._fail("取板端画面失败: %s" % exc)
            return

        try:
            payload = {"question": question, "board_ip": board_ip, "source": "board"}
            data = http_json_post(cfg["pc_url"].rstrip("/") + "/api/vlm/ask",
                                  payload, timeout=int(cfg["pc_timeout_s"]))
        except Exception as exc:
            self._fail("调用电脑侧 VLM 失败: %s" % exc)
            return

        if not data.get("ok"):
            self._fail(data.get("error") or "电脑侧返回失败")
            return

        latency_ms = int((time.time() - t0) * 1000)
        answer = data.get("answer") or ""
        STATS["ask_total"] += 1
        STATS["last_latency_ms"] = latency_ms
        STATS["last_error"] = None
        STATS["last_answer"] = answer

        with HISTORY_LOCK:
            HISTORY.insert(0, {
                "ts": time.strftime("%H:%M:%S"),
                "question": question,
                "answer": answer,
                "latency_ms": latency_ms,
            })
            del HISTORY[MAX_HISTORY:]

        self._json(200, {
            "ok": True,
            "answer": answer,
            "latency_ms": latency_ms,
            "image_bytes": image_bytes,
            "board_ip": board_ip,
            "pc_url": cfg["pc_url"],
        })

    def _fail(self, message):
        STATS["ask_total"] += 1
        STATS["ask_failed"] += 1
        STATS["last_error"] = message
        print("[ask-fail] %s" % message)
        self._json(200, {"ok": False, "error": message})

    def _config(self, body):
        cfg = get_config()
        changed = {}
        for key in ("pc_url", "vision_url", "pc_timeout_s", "frame_timeout_s"):
            if body.get(key) is not None:
                cfg[key] = body[key]
                changed[key] = body[key]
        if not changed:
            self._json(400, {"ok": False, "error": "没有可更新的字段"})
            return
        save_config(cfg)
        self._json(200, {"ok": True, "changed": changed})


def serve(cfg):
    host = cfg.get("host") or "0.0.0.0"
    port = int(cfg.get("port") or 8092)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print("=" * 60)
    print("RK3568 识图问答服务已启动")
    print("  监听    : http://%s:%d/" % (host, port))
    print("  电脑侧  : %s" % cfg["pc_url"])
    print("  取帧    : %s/raw.jpg" % cfg["vision_url"])
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，退出。")
    finally:
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="RK3568 板端识图问答服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--pc", default=None, help="电脑侧 VLM 服务地址")
    parser.add_argument("--check", action="store_true", help="只做连通性自检")
    args = parser.parse_args()

    cfg = load_config()
    if args.host: cfg["host"] = args.host
    if args.port: cfg["port"] = args.port
    if args.pc: cfg["pc_url"] = args.pc
    set_runtime_config(cfg)          # 命令行参数对请求处理器生效

    if args.check:
        ok = True
        try:
            frame = http_get_bytes(cfg["vision_url"].rstrip("/") + "/raw.jpg", timeout=15)
            print("视觉服务: OK, /raw.jpg %d 字节" % len(frame))
        except Exception as exc:
            print("视觉服务: 失败 %s" % exc)
            ok = False
        try:
            req = urllib.request.Request(cfg["pc_url"].rstrip("/") + "/api/vlm/status")
            resp = no_proxy_opener().open(req, timeout=15)
            try:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                resp.close()
            backend = data.get("backend") or {}
            print("电脑侧 VLM: OK, 后端=%s 模型=%s 就绪=%s"
                  % (backend.get("backend"), backend.get("model"), backend.get("ready")))
        except Exception as exc:
            print("电脑侧 VLM: 失败 %s" % exc)
            ok = False
        return 0 if ok else 1

    serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
