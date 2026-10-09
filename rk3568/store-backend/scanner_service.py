#!/usr/bin/env python3
"""RK3568 USB barcode scanner service.

Handoff section 10.6 makes the barcode the *primary* SKU confirmation source, with
the camera model demoted to a fallback.  This service owns the scanner and forwards
every decoded code to the store backend, which owns the catalog.

Two transports are supported, because a USB scanner can present itself either way
and the choice is a barcode printed in the scanner's manual:

``evdev``
    The usual factory default: the scanner emulates a USB HID keyboard and "types"
    the code.  We read the raw ``/dev/input/eventN`` stream.  No third-party module
    is required (python-evdev is not packaged for this Debian 10 image); the kernel
    ``struct input_event`` is unpacked directly.

``serial``
    The scanner is configured for "USB virtual serial" mode and appears as a CDC
    device.  **This must not be confused with the lidar**, which owns
    ``/dev/ttyACM0`` via the ``/dev/lidar`` symlink -- see handoff section 6.

A scanner that is not plugged in is not an error: the service reports
``waiting_device`` and keeps re-scanning, so the board can boot without it.
"""

import argparse
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8095
DEFAULT_STORE_URL = "http://127.0.0.1:8094"
DEFAULT_BAUD = 9600
# A barcode arrives as a burst of key events; anything slower than this starts a new
# code.  Real scanners emit characters only a few milliseconds apart.
GAP_TIMEOUT = 0.15
MAX_CODE_LEN = 64

# Names that identify a scanner (or a device that must never be treated as one).
SCANNER_HINTS = ("barcode", "bar code", "scanner", "scan", "symbol", "zebra",
                 "honeywell", "newland", "datalogic", "opticon", "扫码")
IGNORE_HINTS = ("keyboard", "mouse", "touchscreen", "touchpad", "power button",
                "sleep button", "lid switch", "video bus", "hdmi", "gpio",
                "rockchip", "rk8", "adc", "headphone", "jack")

# Linux input-event-codes -> ASCII, for the unshifted and shifted layers.
_KEYCODE_BASE = {
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    12: "-", 13: "=", 16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u",
    23: "i", 24: "o", 25: "p", 26: "[", 27: "]", 30: "a", 31: "s", 32: "d", 33: "f",
    34: "g", 35: "h", 36: "j", 37: "k", 38: "l", 39: ";", 40: "'", 41: "`",
    43: "\\", 44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m",
    51: ",", 52: ".", 53: "/", 55: "*", 57: " ",
}
_KEYCODE_SHIFTED = {
    2: "!", 3: "@", 4: "#", 5: "$", 6: "%", 7: "^", 8: "&", 9: "*", 10: "(", 11: ")",
    12: "_", 13: "+", 26: "{", 27: "}", 39: ":", 40: '"', 41: "~", 43: "|",
    51: "<", 52: ">", 53: "?",
}
_KEY_ENTER = {28, 96}          # KEY_ENTER, KEY_KPENTER
_KEY_SHIFT = {42, 54}          # KEY_LEFTSHIFT, KEY_RIGHTSHIFT
KEY_BACKSPACE = 14             # KEY_BACKSPACE, handled explicitly
_KEY_IGNORE = {1, 15, 29, 97, 100, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 119}

# struct input_event is (long sec, long usec, __u16 type, __u16 code, __s32 value).
# "l" is the native long, so the same format string is correct on armv7 and aarch64.
INPUT_EVENT = struct.Struct("llHHi")
EV_KEY = 0x01
EV_SYN = 0x00
KEY_PRESS = 1

# EVIOCGNAME(len) = _IOC(_IOC_READ, 'E', 0x06, len)
EVIOCGNAME = (2 << 30) | (256 << 16) | (ord("E") << 8) | 0x06


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _truthy(value):
    """按真值语义解析开关，兼容 JSON 的 true/false 和 form 的 "1"/"0"/"true"。

    直接 bool() 会在 form 编码下出错：`add_to_cart=0` 拿到字符串 "0"，
    bool("0") 是 True —— 想关掉反而打开。ESP32-S3 从机走 form 编码。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def device_name(path):
    """Return the kernel name of an input device, or '' when unreadable."""
    try:
        import fcntl
    except ImportError:
        return ""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        buf = bytearray(256)
        fcntl.ioctl(fd, EVIOCGNAME, buf, True)
        return bytes(buf).split(b"\x00")[0].decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


# Enumerating input devices costs an open+ioctl per /dev/input/eventN (~35 ms each
# on this board, so ~290 ms for eight devices).  The status page polls every few
# seconds, so the snapshot is cached; the hotplug check forces a refresh.
_DEVICE_CACHE = {"at": 0.0, "devices": []}
_DEVICE_CACHE_TTL = 2.0
_DEVICE_CACHE_LOCK = threading.Lock()


def _enumerate_input_devices():
    try:
        names = sorted(os.listdir("/dev/input"))
    except OSError:
        return []
    devices = []
    for name in names:
        if not name.startswith("event"):
            continue
        path = os.path.join("/dev/input", name)
        devices.append({"path": path, "name": device_name(path)})
    return devices


def list_input_devices(force=False):
    """Enumerate input devices, cached for a couple of seconds.

    A missing /dev/input is reported as empty, never an error, so the status
    endpoint keeps answering on a host without evdev.
    """
    now = time.monotonic()
    if not force:
        with _DEVICE_CACHE_LOCK:
            if (now - _DEVICE_CACHE["at"]) < _DEVICE_CACHE_TTL:
                return list(_DEVICE_CACHE["devices"])
    devices = _enumerate_input_devices()
    with _DEVICE_CACHE_LOCK:
        _DEVICE_CACHE["at"] = time.monotonic()
        _DEVICE_CACHE["devices"] = devices
    return list(devices)


def detect_scanner():
    """Pick the most likely scanner among the input devices.

    Only an explicit name match is accepted.  Guessing from "it is not a keyboard"
    would eventually steal the touchscreen on a kiosk build, and a wrong device is
    worse than reporting ``waiting_device``.
    """
    candidates = list_input_devices(force=True)
    for device in candidates:
        lowered = device["name"].lower()
        if any(hint in lowered for hint in SCANNER_HINTS):
            if not any(bad in lowered for bad in IGNORE_HINTS):
                return device
    return None


def detect_serial_scanner():
    """Find a CDC/serial scanner that is not the lidar."""
    try:
        lidar_target = os.path.realpath("/dev/lidar")
    except OSError:
        lidar_target = ""
    for path in ("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2", "/dev/ttyACM3",
                 "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2", "/dev/ttyUSB3"):
        if not os.path.exists(path):
            continue
        if os.path.realpath(path) == lidar_target:
            continue  # this is the radar, handoff section 6 forbids touching it
        return path
    return None


class Scanner(object):
    """Owns the reader thread and the decode buffer."""

    def __init__(self, store_url, mode="auto", device="", baud=DEFAULT_BAUD):
        self.store_url = store_url.rstrip("/")
        self.mode = mode
        self.device = device
        self.baud = baud
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.buffer = ""
        self.overflow = False
        self.last_char_at = 0.0
        self.state = "starting"
        self.active_mode = ""
        self.active_device = ""
        self.events = []
        self.last_error = ""
        self.stats = {"scans": 0, "matched": 0, "unmatched": 0, "dropped": 0}
        self.thread = None

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self.thread = threading.Thread(target=self._run, name="scanner", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                if self.active_mode == "evdev":
                    self._read_evdev(self.active_device)
                elif self.active_mode == "serial":
                    self._read_serial(self.active_device)
                else:
                    self._acquire()
            except Exception as exc:
                self._set_state("error", error=str(exc))
                time.sleep(2.0)
            time.sleep(0.2)

    def _acquire(self):
        """(Re)discover a scanner according to the configured mode."""
        mode, device = self.mode, self.device
        if mode in ("auto", "evdev"):
            found = detect_scanner() if mode == "auto" else (
                {"path": device, "name": device_name(device)} if device else None)
            if found and found.get("path"):
                self.active_mode = "evdev"
                self.active_device = found["path"]
                self._set_state("connected", device=found["path"],
                                detail=found.get("name", ""))
                return
        if mode in ("auto", "serial"):
            found_path = detect_serial_scanner() if mode == "auto" else device
            if found_path:
                self.active_mode = "serial"
                self.active_device = found_path
                self._set_state("connected", device=found_path, detail="serial")
                return
        self.active_mode = ""
        self.active_device = ""
        self._set_state("waiting_device",
                        detail="未检测到扫码枪（USB HID 键盘模式或串口模式）")
        time.sleep(2.0)

    def _set_state(self, state, device="", detail="", error=""):
        with self.lock:
            self.state = state
            if device:
                self.active_device = device
            if error:
                self.last_error = error
            if detail:
                self.detail = detail

    # ---------------------------------------------------------------- readers
    def _read_evdev(self, path):
        self._set_state("connected", device=path)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            self._set_state("error", error="无法打开 %s: %s" % (path, exc))
            self.active_mode = ""
            time.sleep(2.0)
            return

        os.set_blocking(fd, False)
        shift = False
        try:
            while not self.stop_event.is_set():
                try:
                    chunk = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    self._flush_if_stale()
                    time.sleep(0.01)
                    continue
                except OSError as exc:
                    self._set_state("error", error="读取 %s 失败: %s" % (path, exc))
                    break
                if not chunk:
                    self._flush_if_stale()
                    time.sleep(0.01)
                    continue
                usable = len(chunk) - (len(chunk) % INPUT_EVENT.size)
                for offset in range(0, usable, INPUT_EVENT.size):
                    _, _, etype, code, value = INPUT_EVENT.unpack_from(chunk, offset)
                    if etype == EV_KEY:
                        if code in _KEY_SHIFT:
                            shift = value in (1, 2)
                        elif value == KEY_PRESS:
                            self._on_key(code, shift)
                self._flush_if_stale()
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self.active_mode = ""

    def _read_serial(self, path):
        import termios
        self._set_state("connected", device=path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            self._set_state("error", error="无法打开 %s: %s" % (path, exc))
            self.active_mode = ""
            time.sleep(2.0)
            return
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            attrs[4] = self.baud
            attrs[5] = self.baud
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, OSError) as exc:
            self._set_state("error", error="配置 %s 失败: %s" % (path, exc))
            os.close(fd)
            self.active_mode = ""
            time.sleep(2.0)
            return

        try:
            while not self.stop_event.is_set():
                try:
                    data = os.read(fd, 256)
                except BlockingIOError:
                    self._flush_if_stale()
                    time.sleep(0.01)
                    continue
                except OSError as exc:
                    self._set_state("error", error="读取 %s 失败: %s" % (path, exc))
                    break
                if data:
                    for char in data.decode("ascii", "replace"):
                        if char in ("\r", "\n"):
                            self._commit()
                        elif char.isprintable():
                            self._push(char)
                self._flush_if_stale()
        finally:
            os.close(fd)
            self.active_mode = ""

    # --------------------------------------------------------------- decoding
    def _on_key(self, code, shift):
        if code in _KEY_ENTER:
            self._commit()
            return
        if code == KEY_BACKSPACE:
            with self.lock:
                self.buffer = self.buffer[:-1]
            return
        if code in _KEY_IGNORE:
            return
        # Letters live only in the base table; shift changes their case rather than
        # mapping to a different keycode, so the lookup must fall back in both layers.
        char = (_KEYCODE_SHIFTED if shift else _KEYCODE_BASE).get(code)
        if char is None:
            char = _KEYCODE_BASE.get(code)
        if char is None:
            return
        if char.isalpha():
            char = char.upper() if shift else char.lower()
        self._push(char)

    def _push(self, char):
        with self.lock:
            self.last_char_at = time.monotonic()
            if self.overflow:
                return
            if len(self.buffer) >= MAX_CODE_LEN:
                # A runaway device (or a stuck key) must not emit a truncated code
                # that looks like a valid barcode, so drop the whole burst.
                self.overflow = True
                self.stats["dropped"] += 1
                self.buffer = ""
                return
            self.buffer += char

    def _flush_if_stale(self):
        """Commit a code that arrived without a trailing Enter."""
        with self.lock:
            pending = bool(self.buffer)
            stale = pending and (time.monotonic() - self.last_char_at) > GAP_TIMEOUT
        if stale:
            self._commit()

    def _commit(self):
        with self.lock:
            overflow = self.overflow
            code = self.buffer.strip()
            self.buffer = ""
            self.overflow = False
        if overflow or not code:
            return
        self.emit(code, source="scanner")

    # -------------------------------------------------------------- forwarding
    def emit(self, code, source="scanner", add_to_cart=False, session="default"):
        """Forward a code to the store backend and keep a local event log."""
        result = {"code": code, "source": source, "at": now_iso(), "ok": False,
                  "message": "", "product": None}
        payload = {"code": code, "source": source}
        if add_to_cart:
            payload["add_to_cart"] = True
            payload["session"] = session
        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                self.store_url + "/api/scan", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            # Loopback only, so bypass any inherited proxy configuration.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
            result["ok"] = bool(body.get("ok"))
            result["message"] = body.get("message", "")
            result["product"] = body.get("product")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            result["message"] = "无法连接收银服务: %s" % exc

        with self.lock:
            self.stats["scans"] += 1
            if result["ok"]:
                self.stats["matched"] += 1
            else:
                self.stats["unmatched"] += 1
            self.events.insert(0, result)
            del self.events[40:]
        return result

    def status(self):
        with self.lock:
            return {
                "ok": True,
                "service": "rk3568-scanner",
                "state": self.state,
                "mode": self.mode,
                "transport": self.active_mode or "(none)",
                "device": self.active_device or "(none)",
                "baud": self.baud,
                "store_url": self.store_url,
                "connected": self.state == "connected",
                "stats": dict(self.stats),
                "last_error": self.last_error,
                "available_input_devices": list_input_devices(),
                "time": now_iso(),
            }

    def recent(self, limit=20):
        with self.lock:
            return list(self.events[:int(limit)])

    def configure(self, mode=None, device=None, baud=None):
        with self.lock:
            if mode:
                self.mode = mode
            if device is not None:
                self.device = device
            if baud:
                self.baud = int(baud)
        # Drop the current reader so the new configuration takes effect immediately.
        self.active_mode = ""
        self.active_device = ""
        self._set_state("reconfiguring")
        return True, "配置已更新"


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RK3568 扫码枪服务</title>
<style>
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#1f2328}
header{background:#fff;border-bottom:1px solid #e3e6ea;padding:12px 18px;
display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.pill{background:#eef1f4;border-radius:999px;padding:3px 10px;font-size:12px;color:#4a5158}
.pill.ok{background:#e6f4ea;color:#1c6b34}.pill.bad{background:#fdecea;color:#a5271c}
main{padding:14px;display:grid;gap:14px;grid-template-columns:1fr 1fr}
@media(max-width:820px){main{grid-template-columns:1fr}}
section{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px}
h2{font-size:14px;margin:0 0 10px;font-weight:600;color:#3a4149}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f2}
th{color:#6b7280;font-weight:500;font-size:12px}
button{border:1px solid #d0d5da;background:#fff;border-radius:6px;padding:5px 10px;
cursor:pointer;font-size:13px}
button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
input,select{border:1px solid #d0d5da;border-radius:6px;padding:6px 8px;font-size:13px}
.row{display:flex;gap:8px;margin-bottom:8px}.row>*{flex:1}.row>button{flex:0 0 auto}
pre{margin:0;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace;
background:#fafbfc;border:1px solid #eef0f2;border-radius:8px;padding:10px;max-height:300px;overflow:auto}
#msg{margin:0 14px 12px;padding:8px 12px;border-radius:8px;display:none;font-size:13px}
#msg.ok{background:#e6f4ea;color:#1c6b34;display:block}
#msg.bad{background:#fdecea;color:#a5271c;display:block}
.empty{color:#8b939b;padding:12px;text-align:center;font-size:13px}
</style></head><body>
<header><h1>RK3568 扫码枪服务</h1>
<span class="pill" id="s_state">…</span>
<span class="pill" id="s_dev"></span>
<span class="pill" id="s_stat"></span></header>
<div id="msg"></div>
<main>
<section><h2>测试 / 手动录入</h2>
<div class="row"><input id="code" placeholder="条码" onkeydown="if(event.key==='Enter')inject()">
<button class="primary" onclick="inject()">提交</button></div>
<h2 style="margin-top:14px">最近扫码</h2>
<div id="events"><div class="empty">暂无</div></div>
</section>
<section><h2>设备与状态</h2><pre id="status">加载中…</pre></section>
</main>
<script>
function toast(t,ok){var m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'bad';
clearTimeout(window._t);window._t=setTimeout(function(){m.className=''},4000);}
function inject(){var c=document.getElementById('code').value.trim();if(!c)return;
fetch('/api/scanner/inject',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:c})}).then(function(r){return r.json()}).then(function(d){
toast(d.message,d.ok);document.getElementById('code').value='';refresh();});}
function refresh(){fetch('/api/scanner/status').then(function(r){return r.json()}).then(function(d){
var e=document.getElementById('s_state');
e.textContent=d.state;e.className='pill '+(d.connected?'ok':'bad');
document.getElementById('s_dev').textContent=d.transport+' '+d.device;
document.getElementById('s_stat').textContent='扫码 '+d.stats.scans+' · 命中 '+d.stats.matched;
document.getElementById('status').textContent=JSON.stringify(d,null,2);});
fetch('/api/scanner/events?limit=15').then(function(r){return r.json()}).then(function(rows){
var el=document.getElementById('events');
if(!rows.length){el.innerHTML='<div class="empty">暂无</div>';return;}
var h='<table><tr><th>时间</th><th>条码</th><th>结果</th></tr>';
rows.forEach(function(r){h+='<tr><td>'+(r.at||'').slice(11)+'</td><td>'+r.code+'</td><td>'+
(r.ok?('✓ '+(r.product?r.product.name:'')):r.message)+'</td></tr>';});
el.innerHTML=h+'</table>';});}
refresh();setInterval(refresh,3000);
</script></body></html>
"""


class ScannerHandler(BaseHTTPRequestHandler):
    scanner = None
    server_version = "rk3568-scanner/1.0"

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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/scanner/status":
                return self._json(self.scanner.status())
            if path == "/api/scanner/events":
                return self._json(self.scanner.recent(params.get("limit") or 20))
            if path == "/api/scanner/devices":
                return self._json({"ok": True, "devices": list_input_devices(force=True),
                                   "serial": detect_serial_scanner()})
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._body()
        try:
            if path == "/api/scanner/inject":
                # ⚠️ `add_to_cart` 不能直接 bool()：本接口同时收 JSON 和
                # form-urlencoded（见 _body），form 传 `add_to_cart=0` 时
                # 拿到的是字符串 "0"，bool("0") == True —— 想关掉反而打开。
                # ESP32-S3 从机走的就是 form，必须按真值语义解析。
                result = self.scanner.emit(payload.get("code", ""),
                                           source=payload.get("source") or "inject",
                                           add_to_cart=_truthy(payload.get("add_to_cart")),
                                           session=payload.get("session") or "default")
                return self._json(result)
            if path == "/api/scanner/config":
                ok, message = self.scanner.configure(
                    mode=payload.get("mode"), device=payload.get("device"),
                    baud=payload.get("baud"))
                return self._json({"ok": ok, "message": message})
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)


def main():
    parser = argparse.ArgumentParser(description="RK3568 barcode scanner service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store-url", default=DEFAULT_STORE_URL)
    parser.add_argument("--mode", default="auto", choices=["auto", "evdev", "serial"])
    parser.add_argument("--device", default="")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--list-devices", action="store_true",
                        help="print detected input devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        print(json.dumps({"input": list_input_devices(),
                          "serial": detect_serial_scanner(),
                          "auto_detected": detect_scanner()},
                         ensure_ascii=False, indent=2))
        return

    scanner = Scanner(args.store_url, mode=args.mode, device=args.device, baud=args.baud)
    ScannerHandler.scanner = scanner
    scanner.start()
    server = ThreadingHTTPServer((args.host, args.port), ScannerHandler)
    server.daemon_threads = True
    print("scanner service listening on %d (mode=%s, store=%s)"
          % (args.port, args.mode, args.store_url), flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        server.server_close()


if __name__ == "__main__":
    main()
