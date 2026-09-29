#!/usr/bin/env python3
"""RK3568 OCR fallback service (brand / spec / capacity).

Handoff section 10.7 asks for OCR as the *fallback* when the barcode fails: read the
brand, specification and capacity off the packaging and let the store backend
resolve it.  The barcode stays the primary source (section 10.6).

Status of this component
------------------------
``tesseract`` is **not installed** on the board image, and the board has no package
mirror reachable from the hotspot, so this service cannot actually read pixels yet.
Rather than pretend otherwise, it:

  * reports ``engine_available: false`` with the exact install command, and
  * still ships the part that is genuinely useful and fully testable offline: the
    label parser and the catalog matcher, reachable via ``POST /api/ocr/parse``.

That means the pipeline can be wired up and validated now, and swapping in a real
engine later is a one-line change once ``tesseract-ocr`` and the Chinese language
data are installed.

Install when a mirror is reachable:
    sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8096
DEFAULT_STORE_URL = "http://127.0.0.1:8094"
DEFAULT_RAW_URL = "http://127.0.0.1:8088/raw.jpg"
OCR_TIMEOUT = 25

# Capacity: number + unit, in the spellings that actually appear on packaging.
CAPACITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ml|mL|ML|Ml|毫升|L|升|l|g|G|克|kg|KG|千克|片|包|袋|瓶|罐)")
# A leading volume like "330ml" with no unit word after it.
BARE_VOLUME_RE = re.compile(r"\b(\d{2,4})\s*(?:ml|mL|ML)\b")

NOISE_TOKENS = (
    "净含量", "规格", "配料", "生产日期", "保质期", "贮存", "营养成分", "食品添加剂",
    "生产许可", "执行标准", "地址", "电话", "网址", "www", "http", "条码", "价格",
)


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def engine_info():
    path = shutil.which("tesseract")
    info = {"available": bool(path), "path": path or "", "languages": [],
            "install_hint": "sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim"}
    if not path:
        return info
    try:
        output = subprocess.run([path, "--list-langs"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10).stdout
        languages = [line.strip() for line in output.decode("utf-8", "replace").splitlines()
                     if line.strip() and " " not in line.strip()]
        info["languages"] = languages
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def normalize(text):
    """Strip packaging boilerplate while keeping the product text.

    The labels are removed rather than the whole line, because on real packaging the
    brand and the net content share a line ("可口可乐 净含量 330ml") and OCR often
    returns the whole label as a single line.  Dropping the line would throw away the
    brand along with the boilerplate.
    """
    if not text:
        return ""
    cleaned = []
    for line in str(text).replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        for token in NOISE_TOKENS:
            line = line.replace(token, " ")
        line = re.sub(r"[：:，,。、|；;！!？?]+", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        # Keep only lines that still carry a letter, digit or CJK character.
        if not re.search(r"[0-9a-zA-Z\u4e00-\u9fff]", line):
            continue
        cleaned.append(line)
    return " ".join(cleaned).strip()


def extract_capacity(text):
    """Return (value, unit, raw) for the first capacity-looking token."""
    match = CAPACITY_RE.search(text or "")
    if not match:
        return None, "", ""
    value = match.group(1)
    unit = match.group(2)
    canonical = {"毫升": "ml", "ML": "ml", "mL": "ml", "Ml": "ml",
                 "升": "L", "l": "L", "克": "g", "G": "g", "千克": "kg", "KG": "kg"}.get(unit, unit)
    return value, canonical, match.group(0).strip()


def _normalize_for_match(text):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def longest_common_substring(a, b):
    """Length of the longest shared run of characters, used as a cheap similarity."""
    if not a or not b:
        return 0
    best = 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best = current[j]
        previous = current
    return best


def match_catalog(text, products, limit=5, min_score=0.34):
    """Rank catalog rows by how much of the product name appears in the OCR text.

    A plain substring score is used on purpose: OCR of a curved bottle yields noisy
    text, and fuzzy token overlap would produce confident nonsense.  A low score is
    returned as "no confident match" so the caller falls back to asking the shopper.
    """
    haystack = _normalize_for_match(normalize(text))
    if not haystack:
        return []
    results = []
    for product in products:
        name = _normalize_for_match(product.get("name", ""))
        if not name:
            continue
        if name in haystack:
            score = 1.0
        else:
            score = longest_common_substring(name, haystack) / float(len(name))
        # A matched capacity is strong evidence, so it is allowed to lift the score.
        if score >= min_score:
            results.append({"code": product.get("code"), "name": product.get("name"),
                            "price": product.get("price"), "score": round(score, 3)})
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def parse_label(text, products=None, capacity_hint=None):
    """Turn raw OCR text into the fields the store backend cares about."""
    cleaned = normalize(text)
    value, unit, raw = extract_capacity(cleaned)
    if value is None and capacity_hint:
        value, unit, raw = extract_capacity(capacity_hint)
    if value is None:
        bare = BARE_VOLUME_RE.search(cleaned)
        if bare:
            value, unit, raw = bare.group(1), "ml", bare.group(0)

    brand = ""
    candidates = []
    if products:
        candidates = match_catalog(cleaned, products)
        if candidates:
            brand = candidates[0]["name"]
    if not brand:
        # Fall back to the first line, which on packaging is normally the brand.
        brand = cleaned.split(" ")[0][:24] if cleaned else ""

    return {
        "text": cleaned,
        "brand": brand,
        "capacity_value": value,
        "capacity_unit": unit,
        "capacity_raw": raw,
        "candidates": candidates,
        "confident": bool(candidates and candidates[0]["score"] >= 0.5),
    }


class OcrService(object):
    def __init__(self, store_url, raw_url):
        self.store_url = store_url.rstrip("/")
        self.raw_url = raw_url
        self.lock = threading.Lock()
        self.stats = {"recognize": 0, "parse": 0, "engine_missing": 0, "errors": 0}

    def _fetch_json(self, url, payload=None, timeout=8):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def catalog(self):
        try:
            return self._fetch_json(self.store_url + "/api/products")
        except Exception:
            return []

    def fetch_raw_frame(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(self.raw_url)
        with opener.open(request, timeout=8) as response:
            return response.read()

    def recognize(self, image_path=None, use_raw=False, language=None):
        """Run the OCR engine over an image and parse the result."""
        with self.lock:
            self.stats["recognize"] += 1
        info = engine_info()
        if not info["available"]:
            with self.lock:
                self.stats["engine_missing"] += 1
            return False, "未安装 OCR 引擎（tesseract），无法识别图像。" \
                          "可先用 POST /api/ocr/parse 验证解析逻辑。", None

        temp_path = None
        try:
            if use_raw or not image_path:
                blob = self.fetch_raw_frame()
                handle = tempfile.NamedTemporaryFile(prefix="ocr-", suffix=".jpg", delete=False)
                handle.write(blob)
                handle.close()
                temp_path = handle.name
                image_path = temp_path
            if not os.path.exists(image_path):
                return False, "图片不存在: %s" % image_path, None

            languages = language or ("chi_sim+eng" if "chi_sim" in info["languages"] else "eng")
            command = [info["path"], image_path, "stdout", "-l", languages]
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       timeout=OCR_TIMEOUT)
            if completed.returncode != 0:
                with self.lock:
                    self.stats["errors"] += 1
                return False, "tesseract 失败: %s" % completed.stderr.decode("utf-8", "replace")[:200], None
            raw_text = completed.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            with self.lock:
                self.stats["errors"] += 1
            return False, "OCR 超时（>%ds）" % OCR_TIMEOUT, None
        except Exception as exc:
            with self.lock:
                self.stats["errors"] += 1
            return False, "OCR 执行异常: %s" % exc, None
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        parsed = parse_label(raw_text, self.catalog())
        parsed["raw_text"] = raw_text
        parsed["engine"] = info
        return True, "识别完成", parsed

    def status(self):
        info = engine_info()
        with self.lock:
            stats = dict(self.stats)
        return {
            "ok": True,
            "service": "rk3568-ocr",
            "role": "barcode 失败时的辅助识别（handoff 10.7）",
            "engine_available": info["available"],
            "engine": info,
            "store_url": self.store_url,
            "raw_frame_url": self.raw_url,
            "stats": stats,
            "note": "" if info["available"] else
                    "本机未安装 tesseract，识别接口会明确报错；解析逻辑可用 /api/ocr/parse 验证。",
            "time": now_iso(),
        }


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RK3568 OCR 辅助识别</title>
<style>
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#1f2328}
header{background:#fff;border-bottom:1px solid #e3e6ea;padding:12px 18px;display:flex;
gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.pill{background:#eef1f4;border-radius:999px;padding:3px 10px;font-size:12px;color:#4a5158}
.pill.ok{background:#e6f4ea;color:#1c6b34}.pill.bad{background:#fdecea;color:#a5271c}
main{padding:14px;display:grid;gap:14px;grid-template-columns:1fr 1fr}
@media(max-width:820px){main{grid-template-columns:1fr}}
section{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px}
h2{font-size:14px;margin:0 0 10px;font-weight:600;color:#3a4149}
textarea,input,select{width:100%;border:1px solid #d0d5da;border-radius:6px;padding:8px;
font-size:13px;font-family:inherit;background:#fff;color:#1f2328}
textarea{min-height:110px;resize:vertical}
button{border:1px solid #d0d5da;background:#fff;border-radius:6px;padding:6px 12px;
cursor:pointer;font-size:13px;margin-top:8px}
button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
pre{margin:10px 0 0;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace;
background:#fafbfc;border:1px solid #eef0f2;border-radius:8px;padding:10px;max-height:340px;overflow:auto}
.hint{color:#6b7280;font-size:12px;margin:6px 0 0}
</style></head><body>
<header><h1>RK3568 OCR 辅助识别</h1>
<span class="pill" id="engine">…</span><span class="pill" id="role"></span></header>
<main>
<section><h2>解析测试（无需引擎）</h2>
<textarea id="text" placeholder="粘贴 OCR 文本，例如：&#10;可口可乐 净含量 330ml&#10;生产日期 见瓶身"></textarea>
<button class="primary" onclick="parse()">解析</button>
<p class="hint">解析逻辑是条码失败后的兜底：提取品牌 / 规格 / 容量，并在商品目录中匹配候选。</p>
<pre id="parsed">等待输入…</pre>
</section>
<section><h2>状态</h2><pre id="status">加载中…</pre>
<button onclick="recognize()">对当前原始帧执行识别</button>
<pre id="ocr">未执行</pre>
</section>
</main>
<script>
function parse(){var t=document.getElementById('text').value;
fetch('/api/ocr/parse',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({text:t})}).then(function(r){return r.json()}).then(function(d){
document.getElementById('parsed').textContent=JSON.stringify(d,null,2);});}
function recognize(){fetch('/api/ocr/recognize',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({use_raw:true})}).then(function(r){return r.json()}).then(function(d){
document.getElementById('ocr').textContent=JSON.stringify(d,null,2);});}
fetch('/api/ocr/status').then(function(r){return r.json()}).then(function(d){
var e=document.getElementById('engine');
e.textContent=d.engine_available?'引擎就绪 '+d.engine.languages.join(','):'引擎未安装';
e.className='pill '+(d.engine_available?'ok':'bad');
document.getElementById('role').textContent=d.role;
document.getElementById('status').textContent=JSON.stringify(d,null,2);});
</script></body></html>
"""


class OcrHandler(BaseHTTPRequestHandler):
    service = None
    server_version = "rk3568-ocr/1.0"

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

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        text = raw.decode("utf-8", "replace")
        if "json" in (self.headers.get("Content-Type") or "").lower():
            try:
                return json.loads(text)
            except ValueError:
                return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/ocr/status":
                return self._json(self.service.status())
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        payload = self._body()
        try:
            if path == "/api/ocr/parse":
                with self.service.lock:
                    self.service.stats["parse"] += 1
                parsed = parse_label(payload.get("text", ""), self.service.catalog())
                parsed["ok"] = True
                return self._json(parsed)
            if path == "/api/ocr/recognize":
                ok, message, parsed = self.service.recognize(
                    image_path=payload.get("image_path"),
                    use_raw=bool(payload.get("use_raw")),
                    language=payload.get("language"))
                return self._json({"ok": ok, "message": message, "result": parsed})
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)


def main():
    parser = argparse.ArgumentParser(description="RK3568 OCR fallback service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store-url", default=DEFAULT_STORE_URL)
    parser.add_argument("--raw-url", default=DEFAULT_RAW_URL)
    parser.add_argument("--parse", help="parse a text string and exit (no server)")
    args = parser.parse_args()

    if args.parse:
        print(json.dumps(parse_label(args.parse), ensure_ascii=False, indent=2))
        return

    OcrHandler.service = OcrService(args.store_url, args.raw_url)
    server = ThreadingHTTPServer((args.host, args.port), OcrHandler)
    server.daemon_threads = True
    print("ocr service listening on %d (engine=%s)"
          % (args.port, "yes" if engine_info()["available"] else "MISSING"), flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
