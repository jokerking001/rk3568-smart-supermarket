#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RK3568 智慧超市 - 电脑侧视觉大模型（VLM）服务

定位
----
板子（ATK-DLRK3568）只有约 2.7 GB 可用内存、根分区约 1.4 GB，跑不动可用的
视觉语言模型。所以模型放在电脑上，板子只负责「拍图 + 展示」：

    RK3568 摄像头 -> 8088 /raw.jpg
                          |
                          v
        电脑 vlm_service.py --(图片+中文问题)--> 视觉大模型
                          |
                          v
                    中文回答 -> 板端页面 / API

后端可切换（见 vlm_config.json）
-------------------------------
  ollama  : 本地 Ollama，默认 http://127.0.0.1:11434，模型 qwen2.5vl:3b
            —— 离线可用，模型在电脑的显卡上跑。
  openai  : 任意 OpenAI 兼容多模态接口，例如
              阿里云百炼 compatible-mode : https://dashscope.aliyuncs.com/compatible-mode/v1
              vLLM / LM Studio / SGLang 等本地服务
            —— 需要 api_key，零下载即可用。

只用 Python 标准库，不需要 pip 安装任何东西。

用法
----
  python vlm_service.py                      # 默认 0.0.0.0:8097
  python vlm_service.py --port 8097 --board 10.181.229.215
  python vlm_service.py --check              # 只做后端连通性自检，不起服务
  python vlm_service.py --backend openai --model qwen-vl-max --api-key sk-xxx

网页：http://<电脑IP>:8097/
"""

import argparse
import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "vlm_config.json")

DEFAULT_CONFIG = {
    # ---- 后端选择 ----
    "backend": "ollama",              # ollama | openai
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5vl:3b",
    "openai_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai_model": "qwen-vl-max",
    "api_key": "",                    # openai 后端必填
    "proxy": "",                      # 外部接口需要代理时才填，例如 http://127.0.0.1:58829
    # ---- 业务 ----
    "board_ip": "10.181.229.215",     # 板端 IP，随热点变化
    "board_vision_port": 8088,
    "max_tokens": 512,
    "temperature": 0.2,
    "timeout_s": 180,                 # 单次推理超时（CPU 兜底时可能要几分钟）
    # ---- 服务 ----
    "host": "0.0.0.0",
    "port": 8097,
    "system_prompt": (
        "你是一个超市自助结算台的视觉助手，运行在正点原子 RK3568 开发板上。"
        "用户会给你一张摄像头实拍图和一个中文问题。请用简洁、口语化的中文回答。"
        "只描述你在图中真实看到的内容；看不清就直说看不清，不要编造。"
        "价格、库存、订单这类数据一律以系统数据库为准，绝对不要靠猜测回答。"
        "回答控制在 3 句话以内。"
    ),
}

HISTORY = []          # 最近若干次问答
HISTORY_LOCK = threading.Lock()
MAX_HISTORY = 20
STATS = {"ask_total": 0, "ask_failed": 0, "last_latency_ms": None, "last_error": None}

# 进程内生效的配置：= 默认值 + 配置文件 + 环境变量 + 命令行参数。
# 请求处理器必须用 get_config()，不能重新 load_config()，
# 否则命令行参数会被磁盘上的旧配置覆盖掉。
RUNTIME = {"cfg": None}
RUNTIME_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh) or {})
        except Exception as exc:      # 配置坏了不能拖死服务
            print("[warn] 读取配置失败，使用默认值: %s" % exc)
    # 环境变量可以覆盖，方便临时切后端
    env_map = {
        "VLM_BACKEND": "backend",
        "VLM_OLLAMA_URL": "ollama_url",
        "VLM_OLLAMA_MODEL": "ollama_model",
        "VLM_OPENAI_BASE": "openai_base",
        "VLM_OPENAI_MODEL": "openai_model",
        "VLM_API_KEY": "api_key",
        "VLM_BOARD_IP": "board_ip",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        print("[warn] 保存配置失败: %s" % exc)
        return False


def set_runtime_config(cfg):
    with RUNTIME_LOCK:
        RUNTIME["cfg"] = cfg


def get_config():
    """请求处理器统一入口：返回进程内生效的配置。"""
    with RUNTIME_LOCK:
        cfg = RUNTIME["cfg"]
    if cfg is None:                       # 直接 import 调用时兜底
        cfg = get_config()
        set_runtime_config(cfg)
    return cfg


# --------------------------------------------------------------------------
# HTTP 小工具（本机 shell 里可能有 http_proxy，必须显式控制）
# --------------------------------------------------------------------------

def make_opener(proxy=""):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))   # 显式绕过环境代理
    return urllib.request.build_opener(*handlers)


def http_json(url, payload, headers=None, timeout=60, proxy=""):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    opener = make_opener(proxy)
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body)


def http_bytes(url, timeout=15, proxy=""):
    req = urllib.request.Request(url, method="GET")
    opener = make_opener(proxy)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "image/jpeg")


# --------------------------------------------------------------------------
# 后端：Ollama
# --------------------------------------------------------------------------

def call_ollama(cfg, image_b64, question):
    url = cfg["ollama_url"].rstrip("/") + "/api/chat"
    payload = {
        "model": cfg["ollama_model"],
        "messages": [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": question, "images": [image_b64]},
        ],
        "stream": False,
        "options": {
            "temperature": float(cfg["temperature"]),
            "num_predict": int(cfg["max_tokens"]),
        },
    }
    data = http_json(url, payload, timeout=int(cfg["timeout_s"]))
    msg = (data.get("message") or {}).get("content") or data.get("response") or ""
    msg = msg.strip()
    if not msg:
        raise RuntimeError("Ollama 返回了空回答: %s" % json.dumps(data, ensure_ascii=False)[:300])
    return msg, {
        "eval_count": data.get("eval_count"),
        "total_duration_ms": (data.get("total_duration") or 0) // 1_000_000,
    }


# --------------------------------------------------------------------------
# 后端：OpenAI 兼容多模态
# --------------------------------------------------------------------------

def call_openai(cfg, image_b64, question):
    if not cfg.get("api_key"):
        raise RuntimeError("openai 后端需要在 vlm_config.json 里填 api_key（或设 VLM_API_KEY）")
    url = cfg["openai_base"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["openai_model"],
        "messages": [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
            ]},
        ],
        "max_tokens": int(cfg["max_tokens"]),
        "temperature": float(cfg["temperature"]),
    }
    headers = {"Authorization": "Bearer " + cfg["api_key"]}
    data = http_json(url, payload, headers=headers,
                     timeout=int(cfg["timeout_s"]), proxy=cfg.get("proxy") or "")
    try:
        msg = data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("OpenAI 兼容接口返回异常: %s" % json.dumps(data, ensure_ascii=False)[:300])
    if isinstance(msg, list):          # 有些服务返回分段内容
        msg = "".join(part.get("text", "") for part in msg if isinstance(part, dict))
    return msg.strip(), {"usage": data.get("usage")}


def ask_vlm(cfg, image_b64, question):
    backend = (cfg.get("backend") or "ollama").lower()
    if backend == "ollama":
        return call_ollama(cfg, image_b64, question)
    if backend in ("openai", "openai_compatible", "dashscope"):
        return call_openai(cfg, image_b64, question)
    raise RuntimeError("未知后端: %s（可选 ollama / openai）" % backend)


# --------------------------------------------------------------------------
# 板端取图
# --------------------------------------------------------------------------

def fetch_board_frame(cfg, board_ip=None, timeout=20):
    ip = (board_ip or cfg.get("board_ip") or "").strip()
    if not ip:
        raise RuntimeError("没有配置板端 IP")
    port = int(cfg.get("board_vision_port") or 8088)
    url = "http://%s:%d/raw.jpg" % (ip, port)
    return http_bytes(url, timeout=timeout)


def probe_board(cfg, board_ip=None, timeout=6):
    ip = (board_ip or cfg.get("board_ip") or "").strip()
    port = int(cfg.get("board_vision_port") or 8088)
    result = {"board_ip": ip, "reachable": False, "detail": ""}
    if not ip:
        result["detail"] = "未配置 IP"
        return result
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        result["reachable"] = True
        result["detail"] = "%s:%d 可连接" % (ip, port)
    except Exception as exc:
        result["detail"] = "%s:%d 不可达 (%s)" % (ip, port, exc)
    return result


def probe_backend(cfg, timeout=8):
    backend = (cfg.get("backend") or "ollama").lower()
    info = {"backend": backend, "ready": False, "detail": "", "models": []}
    try:
        if backend == "ollama":
            raw, _ = http_bytes(cfg["ollama_url"].rstrip("/") + "/api/tags", timeout=timeout)
            tags = json.loads(raw.decode("utf-8", "replace"))
            names = [m.get("name", "") for m in tags.get("models", [])]
            info["models"] = names
            want = cfg["ollama_model"]
            hit = [n for n in names if n == want or n.split(":")[0] == want.split(":")[0]]
            if hit:
                info["ready"] = True
                info["detail"] = "已加载模型: %s" % ", ".join(hit)
            else:
                info["detail"] = "Ollama 在线，但还没有 %s（先执行 ollama pull %s）" % (want, want)
        else:
            if not cfg.get("api_key"):
                info["detail"] = "缺少 api_key"
            else:
                info["ready"] = True
                info["detail"] = "OpenAI 兼容后端已配置: %s / %s" % (
                    cfg["openai_base"], cfg["openai_model"])
    except Exception as exc:
        info["detail"] = "后端不可用: %s" % exc
    return info


# --------------------------------------------------------------------------
# 网页
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RK3568 视觉问答</title>
<style>
  :root{
    --bg:#f5f6f8; --panel:#ffffff; --line:#e3e6ea; --text:#1c1f23;
    --muted:#6b7280; --accent:#1f6feb; --ok:#1a7f4b; --bad:#c0392b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{padding:16px 22px;background:var(--panel);border-bottom:1px solid var(--line);
         display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{font-size:17px;margin:0;font-weight:600}
  .meta{font-size:13px;color:var(--muted)}
  main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1fr);
       gap:18px;padding:18px 22px;max-width:1280px;margin:0 auto}
  @media (max-width:900px){main{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:14px;display:flex;flex-direction:column;gap:10px}
  .card h2{font-size:14px;margin:0;color:var(--muted);font-weight:600;
           letter-spacing:.02em;text-transform:none}
  .frame{width:100%;aspect-ratio:4/3;background:#eceff3;border-radius:8px;
         object-fit:contain;display:block}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  input[type=text]{flex:1;min-width:120px;padding:9px 11px;border:1px solid var(--line);
                   border-radius:8px;font:inherit;background:#fff;color:var(--text)}
  button{padding:9px 14px;border:1px solid var(--line);background:#fff;color:var(--text);
         border-radius:8px;font:inherit;cursor:pointer}
  button:hover{border-color:#c7ccd3}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.primary:disabled{opacity:.55;cursor:default}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{font-size:13px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);
        background:#fff;color:var(--muted);cursor:pointer}
  .chip:hover{color:var(--text);border-color:#c7ccd3}
  .answer{min-height:120px;white-space:pre-wrap;background:#fafbfc;border:1px solid var(--line);
          border-radius:8px;padding:12px;font-size:15px}
  .status{font-size:13px;color:var(--muted)}
  .ok{color:var(--ok)} .bad{color:var(--bad)}
  .hist{font-size:13px;color:var(--muted);max-height:180px;overflow:auto}
  .hist div{padding:6px 0;border-bottom:1px dashed var(--line)}
</style>
</head>
<body>
<header>
  <h1>RK3568 视觉问答（电脑侧 VLM）</h1>
  <span class="meta" id="backendMeta">正在读取状态…</span>
</header>

<main>
  <section class="card">
    <h2>板端摄像头画面（8088 /raw.jpg）</h2>
    <img class="frame" id="frame" alt="等待板端画面">
    <div class="row">
      <input type="text" id="boardIp" placeholder="板端 IP">
      <button onclick="reloadFrame()">刷新画面</button>
      <label class="status"><input type="checkbox" id="auto" checked> 自动刷新</label>
    </div>
    <div class="status" id="boardStatus">—</div>
  </section>

  <section class="card">
    <h2>提问</h2>
    <div class="row">
      <input type="text" id="question" placeholder="例如：画面里有什么商品？">
      <button class="primary" id="askBtn" onclick="ask()">识图回答</button>
    </div>
    <div class="chips" id="chips"></div>
    <div class="answer" id="answer">回答会显示在这里。</div>
    <div class="status" id="askStatus">—</div>
    <h2>最近问答</h2>
    <div class="hist" id="hist"></div>
  </section>
</main>

<script>
const PRESETS = [
  "画面里有什么商品？",
  "这件商品是什么包装、什么口味？",
  "图里的文字写了什么？",
  "这个商品大概是什么类别的食品？",
  "画面里有几个人？",
];

function el(id){ return document.getElementById(id); }

function renderChips(){
  el("chips").innerHTML = "";
  PRESETS.forEach(function(q){
    const b = document.createElement("span");
    b.className = "chip"; b.textContent = q;
    b.onclick = function(){ el("question").value = q; };
    el("chips").appendChild(b);
  });
}

async function loadStatus(){
  try{
    const r = await fetch("/api/vlm/status");
    const s = await r.json();
    const b = s.backend || {};
    el("backendMeta").textContent =
      "后端 " + b.backend + " · 模型 " + (b.model||"-") + " · " + (b.ready ? "就绪" : "未就绪");
    el("backendMeta").className = "meta " + (b.ready ? "ok" : "bad");
    if (!el("boardIp").value) el("boardIp").value = s.config.board_ip || "";
    el("boardStatus").textContent = s.board.detail || "—";
    el("boardStatus").className = "status " + (s.board.reachable ? "ok" : "bad");
  }catch(e){
    el("backendMeta").textContent = "状态读取失败: " + e;
  }
}

function reloadFrame(){
  const ip = el("boardIp").value.trim();
  el("frame").src = "/api/vlm/frame?t=" + Date.now() + (ip ? "&board=" + encodeURIComponent(ip) : "");
}

async function ask(){
  const q = el("question").value.trim();
  if (!q) { el("answer").textContent = "请先输入问题。"; return; }
  const btn = el("askBtn");
  btn.disabled = true;
  el("answer").textContent = "思考中…（首次调用需要加载模型，可能较慢）";
  el("askStatus").textContent = "—";
  const t0 = Date.now();
  try{
    const r = await fetch("/api/vlm/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: q, board_ip: el("boardIp").value.trim(), source: "board"})
    });
    const data = await r.json();
    if (data.ok){
      el("answer").textContent = data.answer;
      el("askStatus").textContent =
        "耗时 " + data.latency_ms + " ms · 图片 " + Math.round((data.image_bytes||0)/1024) + " KB";
      el("askStatus").className = "status ok";
      refreshHistory();
    }else{
      el("answer").textContent = "失败：" + (data.error || "未知错误");
      el("askStatus").textContent = "—";
      el("askStatus").className = "status bad";
    }
  }catch(e){
    el("answer").textContent = "请求异常：" + e;
  }finally{
    btn.disabled = false;
  }
}

async function refreshHistory(){
  try{
    const r = await fetch("/api/vlm/history");
    const h = await r.json();
    el("hist").innerHTML = "";
    (h.items || []).forEach(function(it){
      const d = document.createElement("div");
      d.textContent = "[" + it.ts + "] " + it.question + " → " + it.answer;
      el("hist").appendChild(d);
    });
  }catch(e){}
}

renderChips();
loadStatus();
reloadFrame();
refreshHistory();
setInterval(function(){
  if (el("auto").checked) reloadFrame();
}, 3000);
setInterval(loadStatus, 10000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RK3568VLM/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # 收敛日志
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 响应工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    # ---- 路由 ----
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/health":
                self._json(200, {"ok": True, "ts": int(time.time())})
            elif path == "/api/vlm/status":
                self._json(200, self._status_payload(query))
            elif path == "/api/vlm/frame":
                self._proxy_frame(query)
            elif path == "/api/vlm/history":
                with HISTORY_LOCK:
                    self._json(200, {"items": list(HISTORY)})
            else:
                self._json(404, {"ok": False, "error": "no route: %s" % path})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self._read_json()

            if path == "/api/vlm/ask":
                self._handle_ask(body)
            elif path == "/api/vlm/config":
                self._handle_config(body)
            else:
                self._json(404, {"ok": False, "error": "no route: %s" % path})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    # ---- 具体处理 ----
    def _status_payload(self, query):
        cfg = get_config()
        board = probe_board(cfg, (query.get("board") or [None])[0])
        backend = probe_backend(cfg)
        with HISTORY_LOCK:
            history_len = len(HISTORY)
        return {
            "ok": True,
            "config": {
                "backend": cfg["backend"],
                "board_ip": cfg["board_ip"],
                "board_vision_port": cfg["board_vision_port"],
            },
            "backend": {
                "backend": cfg["backend"],
                "model": cfg["ollama_model"] if cfg["backend"] == "ollama" else cfg["openai_model"],
                "ready": backend["ready"],
                "detail": backend["detail"],
                "models": backend["models"],
            },
            "board": board,
            "stats": dict(STATS, history_len=history_len),
        }

    def _proxy_frame(self, query):
        cfg = get_config()
        board_ip = (query.get("board") or [None])[0]
        try:
            data, ctype = fetch_board_frame(cfg, board_ip)
            self._send(200, data, ctype or "image/jpeg")
        except Exception as exc:
            self._json(502, {"ok": False, "error": "取板端画面失败: %s" % exc})

    def _handle_ask(self, body):
        cfg = get_config()
        question = (body.get("question") or "").strip()
        if not question:
            self._json(400, {"ok": False, "error": "question 不能为空"})
            return

        source = (body.get("source") or "board").lower()
        image_b64 = body.get("image_b64")
        image_bytes = 0
        t0 = time.time()

        try:
            if source == "upload" or image_b64:
                if not image_b64:
                    raise RuntimeError("source=upload 时必须带 image_b64")
                if "," in image_b64[:80]:        # 容忍 data URI
                    image_b64 = image_b64.split(",", 1)[1]
                image_bytes = len(base64.b64decode(image_b64))
            else:
                raw, _ctype = fetch_board_frame(cfg, body.get("board_ip"))
                image_bytes = len(raw)
                image_b64 = base64.b64encode(raw).decode("ascii")

            answer, extra = ask_vlm(cfg, image_b64, question)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            self._fail("后端 HTTP %s: %s %s" % (exc.code, exc.reason, detail))
            return
        except Exception as exc:
            self._fail(str(exc))
            return

        latency_ms = int((time.time() - t0) * 1000)
        STATS["ask_total"] += 1
        STATS["last_latency_ms"] = latency_ms
        STATS["last_error"] = None

        item = {
            "ts": time.strftime("%H:%M:%S"),
            "question": question,
            "answer": answer,
            "latency_ms": latency_ms,
            "image_bytes": image_bytes,
        }
        with HISTORY_LOCK:
            HISTORY.insert(0, item)
            del HISTORY[MAX_HISTORY:]

        self._json(200, {
            "ok": True,
            "answer": answer,
            "latency_ms": latency_ms,
            "image_bytes": image_bytes,
            "backend": cfg["backend"],
            "model": cfg["ollama_model"] if cfg["backend"] == "ollama" else cfg["openai_model"],
            "extra": extra,
        })

    def _fail(self, message):
        STATS["ask_total"] += 1
        STATS["ask_failed"] += 1
        STATS["last_error"] = message
        print("[ask-fail] %s" % message)
        self._json(200, {"ok": False, "error": message})

    def _handle_config(self, body):
        cfg = get_config()
        allowed = {
            "backend", "ollama_url", "ollama_model", "openai_base", "openai_model",
            "api_key", "proxy", "board_ip", "board_vision_port", "max_tokens",
            "temperature", "timeout_s", "system_prompt",
        }
        changed = {}
        for key, val in (body or {}).items():
            if key in allowed and val is not None:
                cfg[key] = val
                changed[key] = val
        if not changed:
            self._json(400, {"ok": False, "error": "没有可更新的字段"})
            return
        save_config(cfg)
        self._json(200, {"ok": True, "changed": changed})


def serve(cfg):
    host = cfg.get("host") or "0.0.0.0"
    port = int(cfg.get("port") or 8097)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print("=" * 62)
    print("RK3568 电脑侧 VLM 服务已启动")
    print("  监听      : http://%s:%d/" % (host, port))
    print("  后端      : %s" % cfg["backend"])
    if cfg["backend"] == "ollama":
        print("  模型      : %s @ %s" % (cfg["ollama_model"], cfg["ollama_url"]))
    else:
        print("  模型      : %s @ %s" % (cfg["openai_model"], cfg["openai_base"]))
    print("  板端      : %s:%s" % (cfg["board_ip"], cfg["board_vision_port"]))
    print("  配置文件  : %s" % CONFIG_PATH)
    print("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，退出。")
    finally:
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="RK3568 电脑侧视觉大模型服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--backend", default=None, choices=["ollama", "openai"])
    parser.add_argument("--model", default=None, help="覆盖模型名")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--board", default=None, help="板端 IP")
    parser.add_argument("--check", action="store_true", help="只做连通性自检")
    args = parser.parse_args()

    cfg = load_config()
    if args.host: cfg["host"] = args.host
    if args.port: cfg["port"] = args.port
    if args.backend: cfg["backend"] = args.backend
    if args.board: cfg["board_ip"] = args.board
    if args.api_key: cfg["api_key"] = args.api_key
    if args.model:
        if (args.backend or cfg["backend"]) == "ollama":
            cfg["ollama_model"] = args.model
        else:
            cfg["openai_model"] = args.model

    set_runtime_config(cfg)          # 命令行参数对请求处理器生效

    if args.check:
        backend = probe_backend(cfg)
        board = probe_board(cfg)
        print("后端: %s -> %s" % (backend["backend"], "就绪" if backend["ready"] else "未就绪"))
        print("      %s" % backend["detail"])
        if backend["models"]:
            print("      已装模型: %s" % ", ".join(backend["models"][:10]))
        print("板端: %s -> %s" % (board["board_ip"], board["detail"]))
        return 0 if backend["ready"] else 1

    serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
