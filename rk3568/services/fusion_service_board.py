#!/usr/bin/env python3
"""Fuse the local RK3568 vision and YDLIDAR services into a customer session state.

This is a discrete event/state fusion layer, not a pose EKF: the inputs are a
classification (person present), a lidar occupancy change against a static
baseline, and service health/staleness.  It deliberately avoids using the
raw lidar nearest point as a person detector because furniture and the kiosk
itself create permanent returns.
"""
import argparse
import json
import math
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_RADAR_URL = "http://127.0.0.1:8091/api/radar/status"
DEFAULT_RADAR_SCAN_URL = "http://127.0.0.1:8091/api/radar/scan"
DEFAULT_VISION_URL = "http://127.0.0.1:8088/api/vision/result"
DEFAULT_PORT = 8090
POLL_HZ = 5.0
RADAR_SCAN_BINS = 36                 # 10-degree sectors; less sensitive to packet angle jitter
RADAR_MAX_RANGE_M = 16.0
RADAR_ZONE_MIN_M = 0.30
RADAR_ZONE_MAX_M = 2.50
RADAR_ZONE_HALF_ANGLE_DEG = 75.0
RADAR_WARMUP_SCANS = 12
BASELINE_BLOCK_TIMEOUT_S = 30.0      # give up waiting for a clear scene after this long
RADAR_CHANGE_THRESHOLD_M = 0.25
RADAR_CHANGE_MIN_BINS = 2
RADAR_NEW_TARGET_MIN_BINS = 2
RADAR_CHANGE_MIN_SCORE = 0.03
VISION_PERSON_THRESHOLD = 0.45
CANDIDATE_CONFIRM_COUNT = 3
CANDIDATE_TIMEOUT_S = 0.8
SESSION_END_GRACE_S = 3.0
HTTP_TIMEOUT_S = 0.35

STOP = False
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def now_epoch_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now().isoformat(timespec="seconds")


def local_json_get(url, timeout=HTTP_TIMEOUT_S):
    """GET JSON without inheriting the board's outbound proxy variables."""
    request = urllib.request.Request(url, headers={"Connection": "close"})
    with LOCAL_OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def finite_distance(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0 or value > RADAR_MAX_RANGE_M:
        return None
    return value


def angle_delta_deg(angle, center):
    return (angle - center + 180.0) % 360.0 - 180.0


def scan_to_bins(scan):
    """Reduce a full scan to nearest return per 10-degree angular bin."""
    bins = [None] * RADAR_SCAN_BINS
    for point in scan or []:
        try:
            angle = float(point.get("angle_deg", 0.0)) % 360.0
        except (TypeError, ValueError, AttributeError):
            continue
        distance = finite_distance(point.get("distance_m"))
        if distance is None:
            continue
        index = int(angle / 360.0 * RADAR_SCAN_BINS) % RADAR_SCAN_BINS
        if bins[index] is None or distance < bins[index]:
            bins[index] = distance
    return bins


def compare_bins(current, baseline):
    """Return a conservative occupancy-change score against the static scene."""
    changed = 0
    new_target = 0
    disappeared = 0
    total_change = 0.0
    compared_bins = 0
    for index, (current_distance, baseline_distance) in enumerate(zip(current, baseline)):
        bin_center = (index + 0.5) * 360.0 / RADAR_SCAN_BINS
        if not in_front_zone(bin_center):
            continue
        compared_bins += 1
        if current_distance is None and baseline_distance is None:
            continue
        if current_distance is None and baseline_distance is not None:
            # A previously visible static return disappeared.
            if baseline_distance <= RADAR_ZONE_MAX_M:
                changed += 1
                disappeared += 1
                total_change += 1.0
            continue
        if current_distance is not None and baseline_distance is None:
            # A new return in a previously empty sector is strong evidence.
            changed += 1
            if current_distance <= RADAR_ZONE_MAX_M:
                new_target += 1
            total_change += 1.0
            continue
        delta = abs(current_distance - baseline_distance)
        if delta >= RADAR_CHANGE_THRESHOLD_M:
            changed += 1
            total_change += min(delta / 1.0, 1.0)
            if current_distance + RADAR_CHANGE_THRESHOLD_M < baseline_distance:
                new_target += 1
    score = changed / float(max(compared_bins, 1))
    return {
        "changed_bins": changed,
        "new_target_bins": new_target,
        "disappeared_bins": disappeared,
        "dynamic_score": round(score, 4),
        "mean_change": round(total_change / max(changed, 1), 4),
        "motion": changed >= RADAR_CHANGE_MIN_BINS and score >= RADAR_CHANGE_MIN_SCORE,
    }


def in_front_zone(angle):
    return abs(angle_delta_deg(angle, 0.0)) <= RADAR_ZONE_HALF_ANGLE_DEG


def scan_zone_info(scan):
    valid = []
    zone = []
    for point in scan or []:
        distance = finite_distance(point.get("distance_m"))
        if distance is None:
            continue
        try:
            angle = float(point.get("angle_deg", 0.0)) % 360.0
        except (TypeError, ValueError):
            continue
        valid.append(distance)
        if in_front_zone(angle) and RADAR_ZONE_MIN_M <= distance <= RADAR_ZONE_MAX_M:
            zone.append(distance)
    return {
        "valid_points": len(valid),
        "zone_points": len(zone),
        "zone_occupied": bool(zone),
        "zone_nearest_m": round(min(zone), 3) if zone else None,
    }


class FusionEngine:
    def __init__(self, radar_url, radar_scan_url, vision_url, event_path):
        self.radar_url = radar_url
        self.radar_scan_url = radar_scan_url
        self.vision_url = vision_url
        self.event_path = event_path
        self.lock = threading.Lock()
        # Guards self.baseline / baseline_samples / baseline_blocked_since /
        # baseline_degraded / last_radar_frame.  These are mutated both by the
        # poll loop and by the HTTP thread (POST /api/fusion/recalibrate), so a
        # single lock must cover *reads and writes on both sides*.
        self.baseline_lock = threading.RLock()
        self.state = "IDLE"
        self.session_id = None
        self.session_number = 0
        self.candidate_count = 0
        self.candidate_since = None
        self.last_evidence = None
        self.last_radar_fetch = None
        self.last_vision_fetch = None
        self.last_radar_frame = None
        self.baseline = None
        self.baseline_samples = 0
        self.baseline_blocked_since = None
        self.baseline_degraded = False
        self.baseline_path = os.path.join(os.path.dirname(event_path), "radar_baseline.json")
        self.load_baseline()
        self.last_scan = []
        self.data = {
            "ok": True,
            "state": "IDLE",
            "customer_present": False,
            "session_id": None,
            "updated_at": iso_now(),
            "radar": {"ok": False, "connected": False, "status": "not_checked"},
            "vision": {"ok": False, "status": "not_checked", "detections": []},
            "evidence": {},
            "health": {},
            "last_event": "startup",
        }

    def load_baseline(self):
        with self.baseline_lock:
            try:
                with open(self.baseline_path, "r", encoding="utf-8") as handle:
                    values = json.load(handle).get("bins")
                if isinstance(values, list) and len(values) == RADAR_SCAN_BINS:
                    self.baseline = [finite_distance(value) if value is not None else None for value in values]
                    self.baseline_samples = RADAR_WARMUP_SCANS
            except (OSError, ValueError, AttributeError):
                self.baseline = None
                self.baseline_samples = 0

    def save_baseline(self):
        with self.baseline_lock:
            if self.baseline is None or self.baseline_samples < RADAR_WARMUP_SCANS:
                return
            try:
                os.makedirs(os.path.dirname(self.baseline_path), exist_ok=True)
                temp_path = self.baseline_path + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump({"saved_at": iso_now(), "bins": self.baseline}, handle, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.baseline_path)
            except OSError:
                pass

    def reset_baseline(self):
        with self.baseline_lock:
            self.baseline = None
            self.baseline_samples = 0
            self.last_radar_frame = None
            self.baseline_blocked_since = None
            self.baseline_degraded = False
        try:
            os.unlink(self.baseline_path)
        except OSError:
            pass
        self.log_event("radar_baseline_reset")

    def log_event(self, event, details=None):
        item = {
            "time": iso_now(),
            "epoch_ms": now_epoch_ms(),
            "event": event,
            "state": self.state,
            "session_id": self.session_id,
            "details": details or {},
        }
        try:
            os.makedirs(os.path.dirname(self.event_path), exist_ok=True)
            with open(self.event_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def transition(self, new_state, reason):
        if new_state == self.state:
            return
        old_state = self.state
        self.state = new_state
        self.data["last_event"] = "%s -> %s: %s" % (old_state, new_state, reason)
        self.log_event("state_transition", {"from": old_state, "to": new_state, "reason": reason})
        if new_state == "CUSTOMER_PRESENT" and self.session_id is None:
            self.session_number += 1
            self.session_id = "%s-%04d" % (datetime.now().strftime("%Y%m%d-%H%M%S"), self.session_number)
            self.data["session_id"] = self.session_id
            self.log_event("session_started", {"reason": reason})
        elif new_state == "IDLE" and old_state in ("CUSTOMER_PRESENT", "SESSION_END_PENDING"):
            self.log_event("session_ended", {"reason": reason})
            self.session_id = None
            self.data["session_id"] = None
        self.data["state"] = self.state
        self.data["customer_present"] = self.state in ("CUSTOMER_CANDIDATE", "CUSTOMER_PRESENT", "SESSION_END_PENDING")

    def update_radar(self, status):
        fetch_time = time.monotonic()
        self.last_radar_fetch = fetch_time
        # Keep the last derived scan fields between status polls. The radar
        # service updates a full scan at ~5-8 Hz while fusion polls at 10 Hz;
        # rebuilding from status alone on an in-between poll would erase the
        # previous dynamic evidence and make the state machine chatter.
        radar = dict(self.data.get("radar", {}))
        radar.update(status if isinstance(status, dict) else {"ok": False, "status": "invalid_json"})
        radar_ok = bool(radar.get("ok")) and bool(radar.get("connected"))
        radar["service_ok"] = radar_ok
        radar["raw_present"] = bool(radar.get("present"))
        radar.setdefault("dynamic_score", 0.0)
        radar.setdefault("changed_bins", 0)
        radar.setdefault("new_target_bins", 0)
        radar.setdefault("disappeared_bins", 0)
        radar.setdefault("mean_change", 0.0)
        radar.setdefault("motion", False)
        with self.baseline_lock:
            radar["baseline_ready"] = self.baseline is not None and self.baseline_samples >= RADAR_WARMUP_SCANS
            radar["baseline_degraded"] = self.baseline_degraded
            radar["baseline_blocked_s"] = (
                None if self.baseline_blocked_since is None
                else round(time.monotonic() - self.baseline_blocked_since, 2)
            )
        # Fusion polling and lidar scans are asynchronous. Preserve the prior
        # revolution's derived target state until a new full scan replaces it.
        radar.setdefault("target_active", False)
        if radar.get("frames") != self.last_radar_frame and radar_ok:
            with self.baseline_lock:
                self.last_radar_frame = radar.get("frames")
            try:
                scan_response = local_json_get(self.radar_scan_url)
                scan = scan_response.get("scan", []) if isinstance(scan_response, dict) else []
            except Exception:
                scan = []
            if scan:
                current = scan_to_bins(scan)
                zone = scan_zone_info(scan)
                radar.update(zone)
                vision_state = self.data.get("vision", {})
                vision_ok = bool(vision_state.get("service_ok"))
                vision_person = bool(vision_state.get("person_detected"))
                scene_clear = vision_ok and not vision_person
                zero_change = {"changed_bins": 0, "new_target_bins": 0, "disappeared_bins": 0, "dynamic_score": 0.0, "mean_change": 0.0, "motion": False}
                # Every read and write of the baseline state happens under
                # baseline_lock, because the HTTP thread may call
                # reset_baseline() concurrently via POST /api/fusion/recalibrate.
                with self.baseline_lock:
                    now_mono = time.monotonic()
                    warmup = self.baseline_samples < RADAR_WARMUP_SCANS
                    if scene_clear:
                        self.baseline_blocked_since = None
                        self.baseline_degraded = False
                    elif warmup and self.baseline_blocked_since is None:
                        self.baseline_blocked_since = now_mono
                    blocked_s = 0.0 if self.baseline_blocked_since is None else now_mono - self.baseline_blocked_since
                    # Degrade only when we have NO information about the scene, i.e.
                    # the vision service itself is down.  A positive person detection
                    # *is* information -- the scene really is not clear -- so building
                    # a baseline then would bake the person into the static scene.
                    if (not vision_ok) and warmup and blocked_s >= BASELINE_BLOCK_TIMEOUT_S:
                        self.baseline_degraded = True
                    effective_clear = scene_clear or self.baseline_degraded
                    radar["baseline_degraded"] = self.baseline_degraded
                    radar["baseline_blocked_s"] = round(blocked_s, 2) if (warmup and not scene_clear) else 0.0
                    radar["baseline_waiting_for_clear_scene"] = warmup and not effective_clear
                    if not radar["baseline_waiting_for_clear_scene"]:
                        radar["baseline_block_cause"] = None
                    elif not vision_ok:
                        radar["baseline_block_cause"] = "vision_service_down"
                    else:
                        radar["baseline_block_cause"] = "person_in_scene"
                    if self.baseline is None:
                        if effective_clear:
                            self.baseline = list(current)
                            self.baseline_samples = 1
                        change = zero_change
                    elif self.baseline_samples < RADAR_WARMUP_SCANS:
                        # The first scan may start at an arbitrary angle and adjacent
                        # scans have small packet/interpolation jitter. Build a static
                        # scene baseline before allowing lidar changes to create a
                        # customer event.
                        # While the scene is not clear we PAUSE the warmup instead of
                        # discarding it: every sample accumulated so far was taken while
                        # the scene *was* clear, so it is still valid.  The previous
                        # code reset the counter on every blocked scan, which -- once a
                        # vision outage made every scan "blocked" -- kept the lidar
                        # channel unusable for the entire run.
                        if effective_clear:
                            for index, value in enumerate(current):
                                if value is None:
                                    continue
                                if self.baseline[index] is None:
                                    self.baseline[index] = value
                                else:
                                    self.baseline[index] = 0.75 * self.baseline[index] + 0.25 * value
                            self.baseline_samples += 1
                            if self.baseline_samples >= RADAR_WARMUP_SCANS:
                                self.save_baseline()
                        change = zero_change
                    else:
                        change = compare_bins(current, self.baseline)
                        # Adapt only on a strictly clear scene while no candidate or
                        # session target is active.  This prevents a person from being
                        # absorbed into the static baseline, and it also keeps a
                        # degraded baseline (built while the scene was not clear) from
                        # adapting.
                        if self.state == "IDLE" and scene_clear and not change["motion"]:
                            for index, value in enumerate(current):
                                if value is not None:
                                    if self.baseline[index] is None:
                                        self.baseline[index] = value
                                    else:
                                        self.baseline[index] = 0.995 * self.baseline[index] + 0.005 * value
                    radar.update(change)
                    radar["baseline_ready"] = self.baseline is not None and self.baseline_samples >= RADAR_WARMUP_SCANS
                    # A customer target must introduce closer returns. Purely
                    # disappeared returns and ordinary range jitter are motion
                    # diagnostics, but must not open a shopping session alone.
                    radar["target_active"] = bool(
                        radar["baseline_ready"] and
                        change["new_target_bins"] >= RADAR_NEW_TARGET_MIN_BINS
                    )
                self.last_scan = scan
        radar["last_fetch_age_ms"] = 0
        self.data["radar"] = radar

    def update_vision(self, vision):
        self.last_vision_fetch = time.monotonic()
        result = dict(vision) if isinstance(vision, dict) else {"ok": False, "status": "invalid_json", "detections": []}
        detections = result.get("detections") if isinstance(result.get("detections"), list) else []
        people = []
        for detection in detections:
            if str(detection.get("class", "")).strip().lower() == "person":
                try:
                    confidence = float(detection.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                if confidence >= VISION_PERSON_THRESHOLD:
                    people.append(detection)
        result["person_detected"] = bool(people)
        result["person_count"] = len(people)
        result["person_confidence"] = max([float(x.get("confidence", 0.0)) for x in people] or [0.0])
        result["service_ok"] = bool(result.get("ok")) and result.get("status") == "running"
        result["last_fetch_age_ms"] = 0
        self.data["vision"] = result

    def mark_fetch_errors(self, radar_error=None, vision_error=None):
        if radar_error:
            self.last_radar_fetch = None
            self.data["radar"] = {"ok": False, "connected": False, "service_ok": False, "status": "fetch_error", "error": str(radar_error), "target_active": False, "last_fetch_age_ms": None}
        if vision_error:
            self.last_vision_fetch = None
            self.data["vision"] = {"ok": False, "service_ok": False, "status": "fetch_error", "error": str(vision_error), "detections": [], "person_detected": False, "last_fetch_age_ms": None}

    def update_health(self, loop_ms):
        now = time.monotonic()
        radar_age = None if self.last_radar_fetch is None else int((now - self.last_radar_fetch) * 1000)
        vision_age = None if self.last_vision_fetch is None else int((now - self.last_vision_fetch) * 1000)
        radar_fresh = radar_age is not None and radar_age <= 1200
        vision_fresh = vision_age is not None and vision_age <= 1200
        self.data["radar"]["last_fetch_age_ms"] = radar_age
        self.data["vision"]["last_fetch_age_ms"] = vision_age
        self.data["health"] = {
            "radar_fresh": radar_fresh,
            "vision_fresh": vision_fresh,
            "radar_age_ms": radar_age,
            "vision_age_ms": vision_age,
            "loop_ms": round(loop_ms, 2),
        }

    def step(self):
        started = time.monotonic()
        radar_error = None
        vision_error = None
        try:
            self.update_vision(local_json_get(self.vision_url))
        except Exception as error:
            vision_error = error
        try:
            self.update_radar(local_json_get(self.radar_url))
        except Exception as error:
            radar_error = error
        self.mark_fetch_errors(radar_error, vision_error)

        radar = self.data.get("radar", {})
        vision = self.data.get("vision", {})
        radar_target = bool(radar.get("target_active"))
        vision_person = bool(vision.get("person_detected"))
        radar_motion = bool(radar.get("motion")) and bool(radar.get("baseline_ready"))
        evidence = {
            "radar_target_active": radar_target,
            "radar_motion": radar_motion,
            "radar_dynamic_score": radar.get("dynamic_score", 0.0),
            "vision_person": vision_person,
            "vision_person_confidence": round(float(vision.get("person_confidence", 0.0)), 4),
            "zone_occupied_raw": bool(radar.get("zone_occupied", False)),
            "baseline_ready": bool(radar.get("baseline_ready", False)),
        }
        self.data["evidence"] = evidence
        activity = radar_target or vision_person
        now = time.monotonic()
        if activity:
            self.last_evidence = now

        if self.state == "IDLE":
            if activity:
                self.candidate_count += 1
                self.candidate_since = self.candidate_since or now
                self.transition("CUSTOMER_CANDIDATE", "activity evidence")
            else:
                self.candidate_count = 0
                self.candidate_since = None
        elif self.state == "CUSTOMER_CANDIDATE":
            if activity:
                self.candidate_count += 1
                if self.candidate_count >= CANDIDATE_CONFIRM_COUNT:
                    self.transition("CUSTOMER_PRESENT", "persistent activity")
            elif self.candidate_since is not None and now - self.candidate_since > CANDIDATE_TIMEOUT_S:
                self.candidate_count = 0
                self.candidate_since = None
                self.transition("IDLE", "candidate timeout")
        elif self.state == "CUSTOMER_PRESENT":
            if not activity and self.last_evidence is not None and now - self.last_evidence > SESSION_END_GRACE_S:
                self.transition("SESSION_END_PENDING", "no recent activity")
        elif self.state == "SESSION_END_PENDING":
            if activity:
                self.transition("CUSTOMER_PRESENT", "activity returned")
            elif self.last_evidence is not None and now - self.last_evidence > SESSION_END_GRACE_S + 0.8:
                self.transition("IDLE", "customer left")
                self.candidate_count = 0
                self.candidate_since = None
                self.last_evidence = None

        self.data["state"] = self.state
        self.data["customer_present"] = self.state in ("CUSTOMER_CANDIDATE", "CUSTOMER_PRESENT", "SESSION_END_PENDING")
        self.data["updated_at"] = iso_now()
        self.data["epoch_ms"] = now_epoch_ms()
        self.update_health((time.monotonic() - started) * 1000.0)
        with self.lock:
            snapshot = json.loads(json.dumps(self.data, ensure_ascii=False))
        return snapshot

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data, ensure_ascii=False))


class FusionHandler(BaseHTTPRequestHandler):
    engine = None
    dashboard = None

    def log_message(self, *_args):
        return

    def send_body(self, status, content_type, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/fusion/status") or self.path.startswith("/health"):
            payload = json.dumps(self.engine.get(), ensure_ascii=False, indent=2)
            self.send_body(200, "application/json; charset=utf-8", payload)
        elif self.path.startswith("/api/fusion/events"):
            try:
                with open(self.engine.event_path, "r", encoding="utf-8") as handle:
                    lines = handle.readlines()[-50:]
                payload = "[" + ",".join(line.strip() for line in lines if line.strip()) + "]"
            except OSError:
                payload = "[]"
            self.send_body(200, "application/json; charset=utf-8", payload)
        else:
            self.send_body(200, "text/html; charset=utf-8", self.dashboard)

    def do_POST(self):
        if self.path.startswith("/api/fusion/recalibrate"):
            self.engine.reset_baseline()
            self.send_body(200, "application/json; charset=utf-8", '{"ok":true,"status":"waiting_for_clear_scene"}')
        else:
            self.send_body(404, "application/json; charset=utf-8", '{"ok":false,"error":"not_found"}')


def dashboard_html():
    return """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RK3568 传感器融合</title>
<style>
body{font-family:Arial,"Microsoft YaHei",sans-serif;background:#101418;color:#e8eef2;max-width:1100px;margin:auto;padding:18px}
header{display:flex;justify-content:space-between;align-items:center;gap:10px}.state{font-size:20px;font-weight:700}.ok{color:#35d07f}.warn{color:#ffd166}.bad{color:#ff6b6b}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0}.card{background:#1b232a;border:1px solid #2d3a44;border-radius:10px;padding:14px}.label{color:#91a4b2;font-size:12px}.value{font-size:23px;font-weight:700;margin-top:5px}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:#c7d4dc;line-height:1.4}.hint{color:#91a4b2;font-size:12px;line-height:1.5}@media(max-width:720px){.grid{grid-template-columns:1fr 1fr}}@media(max-width:480px){.grid{grid-template-columns:1fr}}
</style>
<header><h2>ATK-DLRK3568 传感器融合</h2><div id="state" class="state">连接中</div></header>
<p class="hint">雷达负责“是否出现新目标/顾客状态”，视觉负责“是否看到人和目标”。静态家具不会仅凭最近点触发顾客会话。</p>
<div class="grid">
<div class="card"><div class="label">融合状态</div><div id="fusion" class="value">-</div></div>
<div class="card"><div class="label">顾客会话</div><div id="session" class="value">-</div></div>
<div class="card"><div class="label">顾客距离/雷达前方</div><div id="distance" class="value">-</div></div>
<div class="card"><div class="label">雷达动态分数</div><div id="dynamic" class="value">-</div></div>
<div class="card"><div class="label">视觉推理</div><div id="vision" class="value">-</div></div>
<div class="card"><div class="label">传感器健康</div><div id="health" class="value">-</div></div>
</div><div class="card"><div class="label">详细状态</div><pre id="raw">-</pre></div>
<script>
function text(v){return v===null||v===undefined?'—':String(v)}
async function refresh(){try{let d=await (await fetch('/api/fusion/status?t='+Date.now())).json();
let s=document.getElementById('state');s.textContent=d.ok?'服务在线':'服务异常';s.className='state '+(d.ok?'ok':'bad');
 document.getElementById('fusion').textContent=text(d.state);document.getElementById('session').textContent=d.session_id||'无';
 let r=d.radar||{},v=d.vision||{},h=d.health||{};document.getElementById('distance').textContent=text(r.zone_nearest_m)+' / '+text(r.front_m)+' m';
 document.getElementById('dynamic').textContent=Number(r.dynamic_score||0).toFixed(3)+' ('+(r.target_active?'目标变化':'静态')+')';
 document.getElementById('vision').textContent=(v.service_ok?'正常':'离线')+' '+Number(v.loop_fps||0).toFixed(1)+' FPS';
 document.getElementById('health').textContent=(h.radar_fresh?'雷达✓':'雷达×')+' '+(h.vision_fresh?'视觉✓':'视觉×')+(r.baseline_degraded?' 基线降级 '+text(r.baseline_blocked_s)+'s':'');document.getElementById('raw').textContent=JSON.stringify(d,null,2);
}catch(e){document.getElementById('state').textContent='连接失败';document.getElementById('state').className='state bad'}setTimeout(refresh,300)}refresh();
</script></html>""".encode("utf-8")


def signal_handler(*_args):
    global STOP
    STOP = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--radar-url", default=DEFAULT_RADAR_URL)
    parser.add_argument("--radar-scan-url", default=DEFAULT_RADAR_SCAN_URL)
    parser.add_argument("--vision-url", default=DEFAULT_VISION_URL)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--event-path", default="/home/linaro/ai/fusion/events.jsonl")
    args = parser.parse_args()
    global STOP
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    engine = FusionEngine(args.radar_url, args.radar_scan_url, args.vision_url, args.event_path)
    FusionHandler.engine = engine
    FusionHandler.dashboard = dashboard_html()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), FusionHandler)
    http_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    http_thread.start()
    print("fusion service listening on %d" % args.port, flush=True)
    period = 1.0 / POLL_HZ
    last_snapshot_write = 0.0
    try:
        while not STOP:
            loop_started = time.monotonic()
            snapshot = engine.step()
            # One diagnostic snapshot per second is enough and avoids 10 eMMC
            # rewrites per second on an appliance that is expected to run 24/7.
            if loop_started - last_snapshot_write >= 1.0:
                try:
                    os.makedirs(os.path.dirname(args.event_path), exist_ok=True)
                    latest_path = os.path.join(os.path.dirname(args.event_path), "latest.json")
                    temp_path = latest_path + ".tmp"
                    with open(temp_path, "w", encoding="utf-8") as handle:
                        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
                    os.replace(temp_path, latest_path)
                    last_snapshot_write = loop_started
                except OSError:
                    pass
            remaining = period - (time.monotonic() - loop_started)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2.0)


if __name__ == "__main__":
    main()

