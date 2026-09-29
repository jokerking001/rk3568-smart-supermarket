# -*- coding: utf-8 -*-
"""30 分钟持续压测 —— 记录温度、FPS、雷达掉线率、NPU 错误。

交接文档把它列为待办，但一直没有脚本。上板后直接跑：

    python tools/stress_test_30min.py --board 192.168.43.44

    # 冒烟（1 分钟，先确认脚本本身没问题）
    python tools/stress_test_30min.py --board 192.168.43.44 --minutes 1

    # 在板子本机上跑（温度/日志走本地，不走 SSH）
    python tools/stress_test_30min.py --local --minutes 30

产出：
  * 终端实时进度（每个采样周期一行）
  * <report-dir>/stress_<时间戳>.json  —— 原始采样，便于事后画图
  * <report-dir>/stress_<时间戳>.txt   —— 人看的结论

判定阈值（可用参数覆盖）：
  请求成功率 >= 99%   视觉 p95 延迟 <= 500ms
  温度峰值   <= 85C   雷达掉线率 <= 5%

设计要点：
  * **全程禁用代理**。本机 http_proxy 会把 192.168.x.x 绕出去。
  * 温度 / journal 走一次 SSH **批量取回**，不要每个采样点开一个 ssh 连接 ——
    30 分钟 x 每 5 秒一次 = 360 次 ssh 握手，本身就会干扰被测对象。
  * 采样点**失败不退出**，只计数。压测的意义就是看它扛不扛得住。
  * Python 3.7 兼容。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_BOARD = "10.181.229.215"
DEFAULT_USER = "linaro"
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519_rk3568")

# 被压的端点：(服务名, 端口, 路径)。挑的都是只读、幂等的查询接口 ——
# 压测绝不能往业务里写数据（不然 30 分钟后库存和订单全乱）。
TARGETS = [
    ("vision",       8088, "/api/vision/result"),
    ("fruit",        8089, "/api/fruit/status"),
    ("fusion",       8090, "/api/fusion/status"),
    ("radar",        8091, "/api/radar/status"),
    ("dataset",      8093, "/api/dataset/status"),
    ("store",        8094, "/api/store/status"),
    ("scanner",      8095, "/api/scanner/status"),
    ("ocr",          8096, "/api/ocr/status"),
    ("fruit-fusion", 8099, "/api/fusion/status"),
]

# 板端温度与日志：一条命令一次取回，避免 360 次 ssh 握手。
REMOTE_METRICS = r'''T=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1)
[ -z "$T" ] && T=0
F=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)
E=$(journalctl -u rk3568-vision --since "-90 seconds" 2>/dev/null | grep -ciE "error|fail|traceback" || echo 0)
printf 'TEMP %s\nFREQ %s\nNPUERR %s\n' "$T" "$F" "$E"'''

LOCAL_METRICS = r'''T=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1)
[ -z "$T" ] && T=0
printf 'TEMP %s\nFREQ 0\nNPUERR 0\n' "$T"'''


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def timed_get(url, timeout=5):
    """返回 (ok, 耗时毫秒, 文本)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "stress/1.0"})
    start = time.time()
    try:
        with opener().open(req, timeout=timeout) as resp:
            text = resp.read(8192).decode("utf-8", "replace")
        return True, (time.time() - start) * 1000.0, text
    except urllib.error.HTTPError:
        # 有 HTTP 响应就算服务活着
        return True, (time.time() - start) * 1000.0, ""
    except Exception:
        return False, (time.time() - start) * 1000.0, ""


def run_command(argv, timeout=20, stdin_text=None):
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(
            input=stdin_text.encode() if stdin_text else None, timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "", "timeout"
    except Exception as exc:
        return 125, "", str(exc)


def read_metrics(board, user, key, local):
    """取一次板端指标。失败返回 None —— 不能因为取不到温度就让压测崩掉。"""
    if local:
        code, out, _err = run_command(["bash", "-lc", LOCAL_METRICS], timeout=10)
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=6", "-o", "LogLevel=ERROR"]
        if key:
            argv += ["-i", key]
        argv += ["%s@%s" % (user, board), REMOTE_METRICS]
        code, out, _err = run_command(argv, timeout=20)

    if code != 0:
        return None
    result = {"temp_c": None, "freq_khz": None, "npu_errors": None}
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        key_name, value = parts[0].strip(), parts[1].strip()
        if key_name == "TEMP":
            try:
                temp = float(value)
            except ValueError:
                continue
            # 内核给的是毫摄氏度
            result["temp_c"] = round(temp / 1000.0, 1) if temp > 1000 else round(temp, 1)
        elif key_name == "FREQ":
            try:
                result["freq_khz"] = int(value)
            except ValueError:
                pass
        elif key_name == "NPUERR":
            try:
                result["npu_errors"] = int(value)
            except ValueError:
                pass
    return result


def extract_fps(text):
    """从状态 JSON 里挖 FPS。字段名各服务不统一，都试一遍。"""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("fps", "loop_fps", "fps_avg", "rate"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(index, len(ordered) - 1))]


class Sampler(object):
    """压测主循环。每 interval 秒并发打一轮所有目标，然后取一次板端指标。"""

    def __init__(self, board, timeout, interval, seconds, local, user, key):
        self.board = board
        self.timeout = timeout
        self.interval = interval
        self.seconds = seconds
        self.local = local
        self.user = user
        self.key = key
        self.latencies = {}          # 服务名 -> [毫秒]
        self.failures = {}           # 服务名 -> 失败次数
        self.attempts = {}           # 服务名 -> 总次数
        self.fps_samples = []
        self.temps = []
        self.radar_fail = 0
        self.radar_total = 0
        self.npu_errors_last = 0
        self.samples = []
        self.stop_flag = threading.Event()

    def one_round(self):
        from concurrent.futures import ThreadPoolExecutor

        def hit(target):
            name, port, path = target
            ok, ms, text = timed_get(
                "http://%s:%d%s" % (self.board, port, path), timeout=self.timeout)
            return name, ok, ms, text

        with ThreadPoolExecutor(max_workers=len(TARGETS)) as pool:
            results = list(pool.map(hit, TARGETS))

        for name, ok, ms, text in results:
            self.attempts[name] = self.attempts.get(name, 0) + 1
            if ok:
                self.latencies.setdefault(name, []).append(ms)
            else:
                self.failures[name] = self.failures.get(name, 0) + 1

            if name == "radar":
                self.radar_total += 1
                if not ok:
                    self.radar_fail += 1

            if name == "vision" and ok:
                fps = extract_fps(text)
                if fps is not None:
                    self.fps_samples.append(fps)

        metrics = read_metrics(self.board, self.user, self.key, self.local)
        if metrics and metrics.get("temp_c") is not None:
            self.temps.append(metrics["temp_c"])
        if metrics and metrics.get("npu_errors") is not None:
            self.npu_errors_last = metrics["npu_errors"]

        return metrics

    def run(self):
        started = time.time()
        deadline = started + self.seconds
        round_index = 0
        print("开始压测，目标 %s，共 %.1f 分钟，每 %.0f 秒一轮"
              % (self.board, self.seconds / 60.0, self.interval))
        print("%-8s %-8s %-9s %-8s %-8s %s"
              % ("已跑", "温度C", "FPS", "视觉ms", "失败", "NPU错误"))
        print("-" * 62)
        try:
            while time.time() < deadline and not self.stop_flag.is_set():
                round_index += 1
                metrics = self.one_round()
                temp = metrics.get("temp_c") if metrics else None
                elapsed = time.time() - started
                recent_vision = self.latencies.get("vision", [])
                self.samples.append({
                    "t": round(elapsed, 1),
                    "temp_c": temp,
                    "fps": self.fps_samples[-1] if self.fps_samples else None,
                    "vision_ms": round(recent_vision[-1], 1) if recent_vision else None,
                    "failures_total": sum(self.failures.values()),
                    "npu_errors": self.npu_errors_last,
                })
                print("%-8s %-8s %-9s %-8s %-8s %s"
                      % ("%d:%02d" % (int(elapsed) // 60, int(elapsed) % 60),
                         "-" if temp is None else temp,
                         "-" if not self.fps_samples else round(self.fps_samples[-1], 2),
                         "-" if not recent_vision else round(recent_vision[-1], 0),
                         sum(self.failures.values()),
                         self.npu_errors_last))
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(self.interval, remaining))
        except KeyboardInterrupt:
            print("\n用户中断，按已采样数据出报告。")
        return round_index


def build_report(sampler, elapsed_seconds, thresholds):
    total_attempts = sum(sampler.attempts.values())
    total_failures = sum(sampler.failures.values())
    success_rate = 0.0 if not total_attempts else \
        (total_attempts - total_failures) * 100.0 / total_attempts

    vision = sampler.latencies.get("vision", [])
    radar_rate = 0.0 if not sampler.radar_total else \
        sampler.radar_fail * 100.0 / sampler.radar_total

    report = {
        "elapsed_seconds": round(elapsed_seconds, 1),
        "attempts": sampler.attempts,
        "failures": sampler.failures,
        "success_rate_pct": round(success_rate, 3),
        "latency_ms": {},
        "fps": {
            "samples": len(sampler.fps_samples),
            "min": round(min(sampler.fps_samples), 2) if sampler.fps_samples else None,
            "avg": round(sum(sampler.fps_samples) / len(sampler.fps_samples), 2)
                   if sampler.fps_samples else None,
            "max": round(max(sampler.fps_samples), 2) if sampler.fps_samples else None,
        },
        "temperature_c": {
            "samples": len(sampler.temps),
            "min": min(sampler.temps) if sampler.temps else None,
            "max": max(sampler.temps) if sampler.temps else None,
            "end": sampler.temps[-1] if sampler.temps else None,
        },
        "radar": {
            "total": sampler.radar_total,
            "failed": sampler.radar_fail,
            "drop_rate_pct": round(radar_rate, 3),
        },
        "npu_errors_last_window": sampler.npu_errors_last,
        "samples": sampler.samples,
        "thresholds": thresholds,
    }
    for name, values in sampler.latencies.items():
        if values:
            report["latency_ms"][name] = {
                "count": len(values),
                "p50": round(percentile(values, 50), 1),
                "p95": round(percentile(values, 95), 1),
                "max": round(max(values), 1),
            }
    if vision:
        report["latency_ms"]["vision"]["p95"] = round(percentile(vision, 95), 1)

    # 三态判定：pass / fail / unknown。
    # **unknown 绝不能算 pass** —— 压测脚本报绿却什么都没测到，比不跑更危险。
    def verdict(item, measured, ok, detail):
        if not measured:
            return {"item": item, "status": "unknown", "pass": False,
                    "detail": "没测到数据 —— " + detail}
        return {"item": item, "status": "pass" if ok else "fail", "pass": ok,
                "detail": detail}

    verdicts = []
    verdicts.append(verdict(
        "请求成功率", total_attempts > 0, success_rate >= thresholds["success_rate"],
        "%.2f%% (要求 >= %.1f%%)" % (success_rate, thresholds["success_rate"])))

    vision_p95 = percentile(vision, 95)
    verdicts.append(verdict(
        "视觉 p95 延迟", vision_p95 is not None,
        vision_p95 is not None and vision_p95 <= thresholds["vision_p95_ms"],
        "%.0f ms (要求 <= %d ms)" % (vision_p95, thresholds["vision_p95_ms"])
        if vision_p95 is not None else "8088 全程没返回过有效响应"))

    temp_max = max(sampler.temps) if sampler.temps else None
    verdicts.append(verdict(
        "温度峰值", temp_max is not None,
        temp_max is not None and temp_max <= thresholds["temp_max_c"],
        "%.1f C (要求 <= %.1f C)" % (temp_max, thresholds["temp_max_c"])
        if temp_max is not None else "取不到温度（SSH 不通或 thermal_zone 不存在）"))

    verdicts.append(verdict(
        "雷达掉线率", sampler.radar_total > 0,
        radar_rate <= thresholds["radar_drop_pct"],
        "%.2f%% (要求 <= %.1f%%)" % (radar_rate, thresholds["radar_drop_pct"])))

    report["verdicts"] = verdicts
    report["passed"] = all(v["status"] == "pass" for v in verdicts)
    report["unknown_items"] = [v["item"] for v in verdicts if v["status"] == "unknown"]
    return report


def render_text(report):
    lines = []
    lines.append("=" * 66)
    lines.append("30 分钟压测报告")
    lines.append("=" * 66)
    lines.append("实际时长      : %.1f 秒 (%.1f 分钟)"
                 % (report["elapsed_seconds"], report["elapsed_seconds"] / 60.0))
    lines.append("总请求        : %d，失败 %d，成功率 %.2f%%"
                 % (sum(report["attempts"].values()),
                    sum(report["failures"].values()),
                    report["success_rate_pct"]))
    lines.append("")
    lines.append("各服务延迟 (ms)：")
    for name in sorted(report["latency_ms"]):
        item = report["latency_ms"][name]
        lines.append("  %-14s n=%-6d p50=%-8s p95=%-8s max=%s"
                     % (name, item["count"], item["p50"], item["p95"], item["max"]))
    failed = [(n, c) for n, c in report["failures"].items() if c]
    if failed:
        lines.append("")
        lines.append("失败明细：")
        for name, count in sorted(failed):
            lines.append("  %-14s %d 次" % (name, count))
    lines.append("")
    fps = report["fps"]
    lines.append("FPS           : 样本 %d，min %s / avg %s / max %s"
                 % (fps["samples"], fps["min"], fps["avg"], fps["max"]))
    temp = report["temperature_c"]
    lines.append("温度 (C)      : 样本 %d，min %s / max %s / 结束 %s"
                 % (temp["samples"], temp["min"], temp["max"], temp["end"]))
    radar = report["radar"]
    lines.append("雷达          : %d/%d 失败，掉线率 %.2f%%"
                 % (radar["failed"], radar["total"], radar["drop_rate_pct"]))
    lines.append("NPU 错误(末窗): %d" % report["npu_errors_last_window"])
    lines.append("")
    lines.append("判定：")
    marks = {"pass": "✅", "fail": "✗ ", "unknown": "? "}
    for item in report["verdicts"]:
        lines.append("  %s %-14s %s" % (marks.get(item["status"], "? "),
                                        item["item"], item["detail"]))
    lines.append("")
    if report["passed"]:
        lines.append("✅ 压测通过 —— 四项全部达标")
    else:
        unknown = report.get("unknown_items") or []
        if unknown:
            lines.append("?  有 %d 项**没测到**：%s" % (len(unknown), "、".join(unknown)))
            lines.append("   没测到不等于通过。先解决取数问题（SSH / 服务是否在线）再复测。")
        if any(v["status"] == "fail" for v in report["verdicts"]):
            lines.append("✗ 有项目未达标，见上")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="板端 30 分钟持续压测")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=5.0, help="采样间隔秒")
    parser.add_argument("--timeout", type=float, default=5.0, help="单请求超时秒")
    parser.add_argument("--local", action="store_true", help="在板子本机上跑")
    parser.add_argument("--report-dir", default=os.path.join(REPO, "artifacts"))
    parser.add_argument("--success-rate", type=float, default=99.0)
    parser.add_argument("--vision-p95", type=float, default=500.0)
    parser.add_argument("--temp-max", type=float, default=85.0)
    parser.add_argument("--radar-drop", type=float, default=5.0)
    args = parser.parse_args()

    board = "127.0.0.1" if args.local else args.board
    thresholds = {
        "success_rate": args.success_rate,
        "vision_p95_ms": args.vision_p95,
        "temp_max_c": args.temp_max,
        "radar_drop_pct": args.radar_drop,
    }

    sampler = Sampler(board, args.timeout, args.interval,
                      args.minutes * 60.0, args.local, args.user, args.key)

    # 先确认板子在线，别跑 30 分钟才发现连不上
    ok, _ms, _t = timed_get("http://%s:8094/api/store/status" % board, timeout=4)
    if not ok:
        print("✗ 连不上 %s:8094 —— 先跑 tools/board_acceptance.py 确认板子状态。" % board)
        return 1

    started = time.time()
    sampler.run()
    elapsed = time.time() - started

    report = build_report(sampler, elapsed, thresholds)
    text = render_text(report)
    print("\n" + text)

    if not os.path.isdir(args.report_dir):
        os.makedirs(args.report_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(args.report_dir, "stress_%s.json" % stamp)
    txt_path = os.path.join(args.report_dir, "stress_%s.txt" % stamp)
    with open(json_path, "w") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(txt_path, "w") as fh:
        fh.write(text + "\n")
    print("\n报告已写出：")
    print("  %s" % json_path)
    print("  %s" % txt_path)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
