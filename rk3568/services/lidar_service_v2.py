#!/usr/bin/env python3
"""S2-YJ/YDLIDAR compatible scanner service for the ATK-DLRK3568.

The parser follows the checksum + intensity packet layout observed in the
vendor FAI upper-host log:
  AA 55 | CT | LSN | FSA | LSA | CS | (quality, distance_lo, distance_hi)*LSN
"""
import json
import math
import os
import signal
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial


PORT = os.environ.get("LIDAR_PORT", "/dev/lidar")
BAUD = int(os.environ.get("LIDAR_BAUD", "150000"))
HTTP_PORT = int(os.environ.get("LIDAR_HTTP_PORT", "8091"))
MIN_RANGE_M = 0.10
MAX_RANGE_M = 8.0
PRESENT_RANGE_M = 3.0

STOP = threading.Event()
LOCK = threading.Lock()
SCAN = []
STATE = {
    "ok": False,
    "connected": False,
    "status": "waiting_device",
    "port": PORT,
    "baudrate": BAUD,
    "protocol": "ydlidar_intensity_checksum",
    "present": False,
    "points": 0,
    "raw_points": 0,
    "scan_hz": 0.0,
    "sample_rate_khz": 0.0,
    "nearest_m": None,
    "average_m": None,
    "stddev_m": None,
    "minimum_m": None,
    "maximum_m": None,
    "front_m": None,
    "left_m": None,
    "right_m": None,
    "packets": 0,
    "valid_packets": 0,
    "checksum_errors": 0,
    "framing_errors": 0,
    "bytes_received": 0,
    "frames": 0,
    "last_update_ms": 0,
    "data_age_ms": None,
    "error": "",
}


def update(**values):
    with LOCK:
        STATE.update(values)


def snapshot():
    with LOCK:
        state = dict(STATE)
        scan = list(SCAN)
    if state["last_update_ms"]:
        state["data_age_ms"] = max(0, int(time.time() * 1000) - state["last_update_ms"])
    return state, scan


def corrected_angle(raw_angle_deg, distance_mm):
    if distance_mm <= 0:
        return raw_angle_deg % 360.0
    correction = math.degrees(math.atan(21.8 * (155.3 - distance_mm) /
                                        (155.3 * distance_mm)))
    return (raw_angle_deg + correction) % 360.0


def packet_checksum(packet):
    ct = packet[2]
    lsn = packet[3]
    fsa = packet[4] | (packet[5] << 8)
    lsa = packet[6] | (packet[7] << 8)
    checksum = 0x55AA ^ fsa ^ lsa ^ ((lsn << 8) | (ct & 0x01))
    for index in range(lsn):
        offset = 10 + index * 3
        quality_low = packet[offset]
        packed_distance = packet[offset + 1] | (packet[offset + 2] << 8)
        intensity = ((packed_distance & 0x03) << 8) | quality_low
        checksum ^= intensity
        checksum ^= packed_distance
    return checksum & 0xFFFF


def parse_packet(packet):
    if len(packet) < 13 or packet[:2] != b"\xAA\x55":
        raise ValueError("bad packet header or length")
    ct = packet[2]
    lsn = packet[3]
    expected = 10 + 3 * lsn
    if not (1 <= lsn <= 120) or len(packet) != expected:
        raise ValueError("invalid sample count")
    expected_checksum = packet[8] | (packet[9] << 8)
    actual_checksum = packet_checksum(packet)
    if actual_checksum != expected_checksum:
        raise ValueError("checksum %04x != %04x" % (actual_checksum, expected_checksum))

    fsa = packet[4] | (packet[5] << 8)
    lsa = packet[6] | (packet[7] << 8)
    first_angle = (fsa >> 1) / 64.0
    last_angle = (lsa >> 1) / 64.0
    span = (last_angle - first_angle) % 360.0
    points = []
    for index in range(lsn):
        offset = 10 + index * 3
        quality_low = packet[offset]
        packed_distance = packet[offset + 1] | (packet[offset + 2] << 8)
        intensity = ((packed_distance & 0x03) << 8) | quality_low
        distance_mm = packed_distance >> 2
        angle = first_angle if lsn == 1 else first_angle + span * index / (lsn - 1)
        angle = corrected_angle(angle, distance_mm)
        points.append((angle, distance_mm / 1000.0, intensity))

    advertised_hz = (ct >> 1) / 10.0 if (ct >> 1) else None
    return {
        "ct": ct,
        "start_of_scan": bool(ct & 0x01),
        "advertised_hz": advertised_hz,
        "points": points,
    }


class StreamParser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        errors = 0
        while True:
            marker = self.buffer.find(b"\xAA\x55")
            if marker < 0:
                if len(self.buffer) > 1:
                    del self.buffer[:-1]
                break
            if marker:
                del self.buffer[:marker]
                errors += 1
            if len(self.buffer) < 4:
                break
            lsn = self.buffer[3]
            if not (1 <= lsn <= 120):
                del self.buffer[0]
                errors += 1
                continue
            packet_length = 10 + 3 * lsn
            if len(self.buffer) < packet_length:
                break
            candidate = bytes(self.buffer[:packet_length])
            try:
                parsed = parse_packet(candidate)
            except ValueError:
                del self.buffer[0]
                errors += 1
                continue
            del self.buffer[:packet_length]
            packets.append(parsed)
        return packets, errors


def sector_min(points, center_deg, half_width=45):
    values = []
    for angle, distance, _quality in points:
        delta = (angle - center_deg + 180.0) % 360.0 - 180.0
        if abs(delta) <= half_width and MIN_RANGE_M <= distance <= MAX_RANGE_M:
            values.append(distance)
    return min(values) if values else None


def stable_points(points):
    """Drop zeros and isolated angular spikes without inventing new geometry."""
    valid = sorted((a % 360.0, d, q) for a, d, q in points
                   if MIN_RANGE_M <= d <= MAX_RANGE_M)
    if len(valid) < 3:
        return valid
    filtered = []
    count = len(valid)
    for index, point in enumerate(valid):
        _angle, distance, _quality = point
        before = valid[(index - 1) % count]
        after = valid[(index + 1) % count]
        if min(abs(distance - before[1]), abs(distance - after[1])) <= max(0.18, distance * 0.12):
            filtered.append(point)
    return filtered if len(filtered) >= 20 else valid


def publish_scan(raw_points, frame_started, advertised_hz):
    global SCAN
    if len(raw_points) < 20:
        return frame_started
    now = time.time()
    filtered = stable_points(raw_points)
    distances = [point[1] for point in filtered]
    measured_hz = 0.0 if not frame_started else 1.0 / max(now - frame_started, 1e-6)
    scan_hz = advertised_hz if advertised_hz and 1.0 <= advertised_hz <= 20.0 else measured_hz
    sample_rate = len(raw_points) * scan_hz / 1000.0
    nearest = min(distances) if distances else None
    result_scan = [
        {"index": index, "angle_deg": round(angle, 3),
         "distance_m": round(distance, 4), "intensity": int(intensity)}
        for index, (angle, distance, intensity) in enumerate(filtered)
    ]
    with LOCK:
        SCAN = result_scan
        STATE.update({
            "ok": True,
            "connected": True,
            "status": "running",
            "present": nearest is not None and nearest <= PRESENT_RANGE_M,
            "points": len(filtered),
            "raw_points": len(raw_points),
            "scan_hz": round(scan_hz, 2),
            "sample_rate_khz": round(sample_rate, 2),
            "nearest_m": None if nearest is None else round(nearest, 4),
            "average_m": None if not distances else round(statistics.mean(distances), 4),
            "stddev_m": None if len(distances) < 2 else round(statistics.pstdev(distances), 4),
            "minimum_m": None if not distances else round(min(distances), 4),
            "maximum_m": None if not distances else round(max(distances), 4),
            "front_m": sector_min(filtered, 0),
            "left_m": sector_min(filtered, 90),
            "right_m": sector_min(filtered, 270),
            "frames": STATE["frames"] + 1,
            "last_update_ms": int(now * 1000),
            "error": "",
        })
    return now


def open_lidar():
    device = serial.Serial(PORT, BAUD, timeout=0.10, write_timeout=0.5,
                           exclusive=True)
    # YDLidar SDK's common single-channel path uses clearDTR to start the motor.
    device.dtr = False
    device.rts = False
    time.sleep(0.50)
    device.reset_input_buffer()
    device.write(b"\xA5\x60")
    device.flush()
    return device


def serial_loop():
    parser = StreamParser()
    current_scan = []
    frame_started = 0.0
    current_hz = None
    device = None
    last_data = 0.0
    while not STOP.is_set():
        if device is None:
            if not os.path.exists(PORT):
                update(ok=False, connected=False, status="waiting_device", present=False)
                STOP.wait(0.5)
                continue
            try:
                device = open_lidar()
                parser = StreamParser()
                current_scan = []
                frame_started = time.time()
                last_data = frame_started
                update(connected=True, status="starting_scan", port=os.path.realpath(PORT), error="")
            except Exception as exc:
                update(ok=False, connected=False, status="open_error", error=str(exc), present=False)
                device = None
                STOP.wait(1.0)
                continue
        try:
            data = device.read(8192)
            if data:
                last_data = time.time()
                with LOCK:
                    STATE["bytes_received"] += len(data)
                packets, errors = parser.feed(data)
                if errors:
                    with LOCK:
                        STATE["framing_errors"] += errors
                for packet in packets:
                    with LOCK:
                        STATE["packets"] += 1
                        STATE["valid_packets"] += 1
                    if packet["start_of_scan"] and current_scan:
                        frame_started = publish_scan(current_scan, frame_started, current_hz)
                        current_scan = []
                    current_hz = packet["advertised_hz"] or current_hz
                    if (current_scan and packet["points"] and
                            packet["points"][0][0] < current_scan[-1][0] - 180.0):
                        frame_started = publish_scan(current_scan, frame_started, current_hz)
                        current_scan = []
                    current_scan.extend(packet["points"])
            elif time.time() - last_data > 2.0:
                update(ok=False, status="no_data", present=False,
                       error="No scan bytes. Check motor rotation, DTR and USB power.")
        except (serial.SerialException, OSError) as exc:
            update(ok=False, connected=False, status="read_error", error=str(exc), present=False)
            try:
                device.close()
            except Exception:
                pass
            device = None
            STOP.wait(0.5)
    if device is not None:
        try:
            device.write(b"\xA5\x65\xA5\x00")
            device.close()
        except Exception:
            pass


DASHBOARD = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RK3568 LiDAR</title>
<style>body{margin:0;background:#292d33;color:#e9eef3;font:14px Arial,"Microsoft YaHei"}.wrap{max-width:1180px;margin:auto;padding:14px}.head{display:flex;justify-content:space-between;align-items:center}.grid{display:grid;grid-template-columns:minmax(520px,1fr) 330px;gap:14px}.panel{background:#1d2126;border:1px solid #46505a;border-radius:5px;padding:10px}canvas{width:100%;background:#20252b}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#3b444d}.m{background:#22282e;padding:8px}.m b{display:block;color:#55d6ff;font-size:17px;margin-top:4px}.ok{color:#52df84}.bad{color:#ff6b6b}pre{white-space:pre-wrap;font-size:11px;color:#aebbc6}.hint{color:#aebbc6;line-height:1.55}@media(max-width:850px){.grid{display:block}.panel{margin-bottom:12px}}</style>
<div class="wrap"><div class="head"><h2>FAI / S2-YJ 激光雷达 — RK3568</h2><b id="status">连接中</b></div><div class="grid"><div class="panel"><canvas id="radar" width="780" height="780"></canvas></div><div><div class="panel metrics"><div class="m">Scan Frequency<b id="hz">—</b></div><div class="m">Sample Rate<b id="rate">—</b></div><div class="m">Points<b id="points">—</b></div><div class="m">Point Count<b id="counts">—</b></div><div class="m">Min<b id="min">—</b></div><div class="m">Max<b id="max">—</b></div><div class="m">Average<b id="avg">—</b></div><div class="m">Std<b id="std">—</b></div><div class="m">Front<b id="front">—</b></div><div class="m">Nearest<b id="near">—</b></div></div><div class="panel"><p class="hint">显示最新一整圈数据；不是历史点累计。鼠标指向点可查看 Index、Angle、Distance、Intensity。环距 1 m，最大 8 m。</p><pre id="raw"></pre></div></div></div></div>
<script>const c=document.getElementById('radar'),g=c.getContext('2d');let last=[];function n(v,d=2){return v==null?'—':Number(v).toFixed(d)}function draw(scan){last=scan;let w=c.width,h=c.height,cx=w/2,cy=h/2,R=Math.min(w,h)*.46,k=R/8;g.fillStyle='#20252b';g.fillRect(0,0,w,h);g.strokeStyle='#69747e';g.fillStyle='#b8c2ca';g.font='12px Arial';for(let r=1;r<=8;r++){g.beginPath();g.arc(cx,cy,r*k,0,7);g.stroke();g.fillText(r+'m',cx+4,cy-r*k+13)}for(let a=0;a<360;a+=30){let t=a*Math.PI/180;g.beginPath();g.moveTo(cx,cy);g.lineTo(cx+Math.sin(t)*R,cy-Math.cos(t)*R);g.stroke();g.fillText(a+'°',cx+Math.sin(t)*(R+12)-10,cy-Math.cos(t)*(R+12)+4)}for(const p of scan){if(p.distance_m>8)continue;let t=p.angle_deg*Math.PI/180,q=Math.min(1023,p.intensity||0)/1023;g.fillStyle=`hsl(${120-120*q},90%,60%)`;g.fillRect(cx+Math.sin(t)*p.distance_m*k-1.5,cy-Math.cos(t)*p.distance_m*k-1.5,3,3)}g.fillStyle='#ff5252';g.beginPath();g.arc(cx,cy,4,0,7);g.fill()}async function tick(){try{let d=await(await fetch('/api/radar/scan?t='+Date.now(),{cache:'no-store'})).json(),s=d.state;status.textContent=s.status;status.className=s.ok?'ok':'bad';hz.textContent=n(s.scan_hz,1)+' Hz';rate.textContent=n(s.sample_rate_khz,1)+' K/s';points.textContent=s.points||0;counts.textContent=(s.points||0)+' / '+(s.raw_points||0);min.textContent=n(s.minimum_m)+' m';max.textContent=n(s.maximum_m)+' m';avg.textContent=n(s.average_m)+' m';std.textContent=n(s.stddev_m)+' m';front.textContent=n(s.front_m)+' m';near.textContent=n(s.nearest_m)+' m';raw.textContent=JSON.stringify(s,null,2);draw(d.scan)}catch(e){status.textContent='HTTP disconnected';status.className='bad'}setTimeout(tick,100)}tick();c.onmousemove=e=>{let r=c.getBoundingClientRect(),x=(e.clientX-r.left)*c.width/r.width,y=(e.clientY-r.top)*c.height/r.height,cx=c.width/2,cy=c.height/2,k=Math.min(c.width,c.height)*.46/8,b=null,bd=14;for(const p of last){let t=p.angle_deg*Math.PI/180,px=cx+Math.sin(t)*p.distance_m*k,py=cy-Math.cos(t)*p.distance_m*k,d=Math.hypot(px-x,py-y);if(d<bd){bd=d;b=p}}c.title=b?`Index: ${b.index}  Angle: ${b.angle_deg}°  Distance: ${b.distance_m}m  Intensity: ${b.intensity}`:''}</script>'''.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send_json(self, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state, scan = snapshot()
        if self.path.startswith("/api/radar/scan"):
            self.send_json({"state": state, "scan": scan})
        elif self.path.startswith("/api/radar/status") or self.path.startswith("/health"):
            self.send_json(state)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(DASHBOARD)))
            self.end_headers()
            self.wfile.write(DASHBOARD)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    worker = threading.Thread(target=serial_loop, daemon=True)
    worker.start()

    def shutdown(_signum, _frame):
        STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        STOP.set()
        server.server_close()
        worker.join(timeout=2.0)


if __name__ == "__main__":
    main()
