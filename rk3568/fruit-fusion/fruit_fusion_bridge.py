# -*- coding: utf-8 -*-
"""水果识别服务（8089）→ 融合服务（8099）的桥接层。

**为什么要有这一层：**

    板端 8089 的 `fruit_service.py` 已经在跑了，10 Hz 推理、输出 detections。
    但原工程的视觉节点是**每 1400 ms 推一帧**给融合判定的，不是每帧都推：

        原工程 INFERENCE_INTERVAL_MS = 1400，窗口 5 帧
        → 一个判定窗口横跨约 7 秒，也就是「水果必须在秤上稳住 7 秒」

    如果直接把 10 Hz 的检测结果全喂进观测窗口，窗口 0.5 秒就填满了，
    判定会变得极其敏感；而且秤那边（500 ms 一次采样，要 5 个样本）根本还没稳。
    所以**必须按 1400 ms 的节奏喂**，这一层就是干这个的。

**为什么不改 fruit_service.py：**

    它已经支持 `--model` 和 `--classes` 参数了，换成 8 类水果模型只需要改
    systemd unit 的启动参数，**一行代码都不用动**。
    这一层则通过它已有的 `GET /api/fruit/result` 拿结果，
    对已部署的服务是零侵入的 —— 板端本地副本与线上副本是否一致还没核对过，
    能不动就不动。

链路：

    8089 fruit_service.py（10 Hz 推理）
         ↓  GET /api/fruit/result   每 1400 ms 轮询一次
    本模块 FruitBridge
         ↓  observer.submit_detections()
    8099 fruit_fusion_service.py（融合判定）
         ↓  POST /api/vision/observe
    8094 store_service.py（加购）

板端是 Python 3.7.3，本文件不使用 walrus、dict 合并、PEP 585 泛型下标。
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vision_observer as vo

DEFAULT_FRUIT_RESULT_URL = "http://127.0.0.1:8089/api/fruit/result"
# 与 vision_observer.INFERENCE_INTERVAL_MS 保持同源，别各写一份
POLL_INTERVAL_MS = vo.INFERENCE_INTERVAL_MS
FETCH_TIMEOUT_S = 3.0


def no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FruitBridge(object):
    """把 8089 的检测结果按原工程节奏喂给观测器。"""

    def __init__(self, observer, result_url=DEFAULT_FRUIT_RESULT_URL,
                 interval_ms=POLL_INTERVAL_MS, prob_mode=vo.PROB_MODE_TOP1,
                 fetch=None, sleep=None, clock=None):
        self.observer = observer
        self.result_url = result_url
        self.interval_ms = int(interval_ms)
        self.prob_mode = prob_mode
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._fetch = fetch if fetch is not None else self._http_fetch
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._counters = {"polls": 0, "fed": 0, "skipped": 0, "errors": 0,
                          "submitted": 0, "accepted": 0}
        self._last = {"detections": [], "response": None, "error": "",
                      "source_status": None}

    # ------------------------------------------------------------------ 取数
    def _http_fetch(self):
        request = urllib.request.Request(self.result_url)
        response = no_proxy_opener().open(request, timeout=FETCH_TIMEOUT_S)
        try:
            return json.loads(response.read().decode("utf-8", "replace"))
        finally:
            response.close()

    # ------------------------------------------------------------------ 单次
    def poll_once(self):
        """轮询一次。返回观测响应；窗口预热期或源不可用时返回 None。"""
        try:
            payload = self._fetch()
        except urllib.error.URLError as exc:
            self._record_error("水果识别服务不可达: %s" % exc, status=None)
            return None
        except Exception as exc:
            self._record_error("取水果识别结果失败: %s" % exc, status=None)
            return None

        if not isinstance(payload, dict):
            self._record_error("水果识别服务返回结构异常", status=None)
            return None

        status = payload.get("status")
        if status != "running":
            # 模型没加载 / 取原始帧失败 / 正在启动，都不是「台面上没东西」，
            # 所以**不能**喂空检测进窗口，那会污染判定。
            self._record_error("水果识别服务状态=%s" % status, status=status, skip=True)
            return None

        detections = payload.get("detections") or []
        response = self.observer.submit_detections(detections, self.prob_mode)

        with self._lock:
            self._counters["polls"] += 1
            self._counters["fed"] += 1
            if response is not None:
                self._counters["submitted"] += 1
                if response.get("accepted"):
                    self._counters["accepted"] += 1
            self._last = {"detections": detections, "response": response,
                          "error": "", "source_status": status}
        return response

    def _record_error(self, message, status=None, skip=False):
        with self._lock:
            self._counters["errors"] += 1
            if skip:
                self._counters["skipped"] += 1
            self._last = {"detections": self._last["detections"],
                          "response": self._last["response"],
                          "error": message, "source_status": status}

    # ------------------------------------------------------------------ 循环
    def run(self):
        """按 interval_ms 的节奏轮询，直到 stop()。"""
        while not self._stop.is_set():
            started = self._clock()
            self.poll_once()
            elapsed_ms = (self._clock() - started) * 1000.0
            remaining_ms = self.interval_ms - elapsed_ms
            if remaining_ms > 0:
                self._sleep(remaining_ms / 1000.0)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="fruit-bridge", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    # ------------------------------------------------------------------ 状态
    def status(self):
        with self._lock:
            counters = dict(self._counters)
            last = dict(self._last)
            last["detections"] = list(last.get("detections") or [])
        return {
            "source_url": self.result_url,
            "interval_ms": self.interval_ms,
            "prob_mode": self.prob_mode,
            "running": bool(self._thread is not None and self._thread.is_alive()),
            "counters": counters,
            "window": {"count": self.observer.window.count,
                       "size": self.observer.window.size},
            "last_detections": last["detections"],
            "last_response": last["response"],
            "last_error": last["error"],
            "last_source_status": last["source_status"],
        }


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="8089 水果识别 → 8099 融合服务 的桥接（可单独跑，用于板端联调）")
    parser.add_argument("--fruit-url", default=DEFAULT_FRUIT_RESULT_URL)
    parser.add_argument("--fusion-url", default=vo.DEFAULT_FUSION_URL)
    parser.add_argument("--rules", default=None)
    parser.add_argument("--interval-ms", type=int, default=POLL_INTERVAL_MS)
    parser.add_argument("--prob-mode", default=vo.PROB_MODE_TOP1,
                        choices=[vo.PROB_MODE_TOP1, vo.PROB_MODE_NORMALIZE])
    parser.add_argument("--steps", type=int, default=0,
                        help="轮询 N 次就退出（默认一直跑）")
    args = parser.parse_args()

    labels = vo.labels_from_rules(args.rules)
    observer = vo.VisionObserver(labels, client=vo.FusionClient(args.fusion_url))
    bridge = FruitBridge(observer, result_url=args.fruit_url,
                         interval_ms=args.interval_ms, prob_mode=args.prob_mode)

    print("桥接：%s -> %s" % (args.fruit_url, args.fusion_url))
    print("类别：%s" % ", ".join(labels))
    print("节奏：每 %d ms 一次（原工程 INFERENCE_INTERVAL_MS）" % args.interval_ms)
    print("窗口：%d 帧，填满约 %.1f 秒" % (
        observer.window.size, observer.window.size * args.interval_ms / 1000.0))
    print("")

    step = 0
    try:
        while True:
            response = bridge.poll_once()
            step += 1
            if response is None:
                print("[%02d] 未提交（窗口 %d/%d 或源不可用）"
                      % (step, observer.window.count, observer.window.size))
            else:
                print("[%02d] accepted=%s label=%s confidence=%s weight=%sg"
                      % (step, response.get("accepted"), response.get("label"),
                         response.get("confidence"), response.get("weight_g")))
            if args.steps and step >= args.steps:
                break
            time.sleep(args.interval_ms / 1000.0)
    except KeyboardInterrupt:
        pass
    print("")
    print(json.dumps(bridge.status()["counters"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
