#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把新固件推给 ESP32-S3 从机（OTA over HTTP）。

**为什么需要它**：从机装在货架里，现场再插 USB 很麻烦。从机在
`SLAVE_HTTP_PORT`（默认 8080）上自带一套 `/api/ota/*` 端点
（实现见 firmware/store-controller/OTA_Update.cpp），本脚本是它的客户端。

    · RK3568 侧也能直接用它 —— 只依赖 Python 3 标准库，不用装任何包。
    · 也可以纯用 curl：
        curl -X POST --data-binary @Work7_20.ino.bin \\
             -H "Content-Type: application/octet-stream" \\
             "http://10.176.240.1:8080/api/ota/upload?reboot=1"
      但 curl 不给进度、也不好带 MD5，所以脚本更省事。

⚠️ 必须用 `--data-binary`（整体 Content-Length）。`curl -T` 在某些
   版本会走 `Transfer-Encoding: chunked`，而从机是手搓 socket 服务，
   **不解析 chunked**，会直接把连接读超时。

用法：
    # 看当前分区 / 上次结果
    python tools/ota_push_slave.py --status

    # 上传并自动重启（走新固件）
    python tools/ota_push_slave.py --firmware D:/789/.arduino-build/output/Work7_20/Work7_20.ino.bin --reboot

    # 上传但先不重启（想先确认写进去了）
    python tools/ota_push_slave.py --firmware <bin>

    # 新固件有问题 → 切回另一个槽
    python tools/ota_push_slave.py --rollback

    # 只重启
    python tools/ota_push_slave.py --reboot-only
"""
import argparse
import hashlib
import http.client
import json
import os
import sys
import time

DEFAULT_HOST = "10.176.240.1"      # 从机固件内静态 IP（见 Slave_Config.h / secrets.h）
DEFAULT_PORT = 8080                # SLAVE_HTTP_PORT


def _request(host, port, method, path, timeout=10, body=None, headers=None):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read().decode("utf-8", "replace")
    conn.close()
    return r.status, data


def show_status(host, port):
    try:
        code, data = _request(host, port, "GET", "/api/ota/status")
    except OSError as e:
        print("✗ 连不上 %s:%d —— %s" % (host, port, e))
        return 2
    print("HTTP %d" % code)
    try:
        print(json.dumps(json.loads(data), indent=2, ensure_ascii=False))
    except ValueError:
        print(data)
    return 0 if code == 200 else 1


def upload(host, port, path, reboot, timeout):
    size = os.path.getsize(path)
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    md5 = h.hexdigest()

    print("固件 %s" % path)
    print("大小 %d B（%.2f MB）  MD5 %s" % (size, size / 1048576.0, md5))

    url = "/api/ota/upload" + ("?reboot=1" if reboot else "")
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.putrequest("POST", url, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        # 从机侧 OTA_Update.cpp 会调 Update.setMD5()，写满后校验
        conn.putheader("X-Firmware-MD5", md5)
        conn.endheaders()

        sent = 0
        t0 = time.time()
        last = -1
        with open(path, "rb") as f:
            while True:
                blk = f.read(8192)
                if not blk:
                    break
                conn.send(blk)
                sent += len(blk)
                pct = int(sent * 100 / size)
                if pct != last:
                    last = pct
                    sys.stdout.write("\r  上传 %3d%%  %d/%d B  %.1f KB/s"
                                     % (pct, sent, size,
                                        sent / 1024.0 / max(0.001, time.time() - t0)))
                    sys.stdout.flush()
        print()

        r = conn.getresponse()
        data = r.read().decode("utf-8", "replace")
        conn.close()
    except OSError as e:
        print("✗ 上传中断：%s" % e)
        return 2

    print("HTTP %d  %s" % (r.status, data.strip()))
    if r.status != 200:
        return 1

    if reboot:
        print("已请求重启 —— 等 8 秒后回连确认新分区…")
        time.sleep(8)
        for _ in range(6):
            try:
                code, data = _request(host, port, "GET", "/api/ota/status", timeout=5)
                if code == 200:
                    info = json.loads(data)
                    print("✓ 已启动：running=%s boot=%s next=%s"
                          % (info.get("running"), info.get("boot"), info.get("next_slot")))
                    return 0
            except OSError:
                pass
            time.sleep(3)
        print("⚠️ 8+18 秒内没等到从机回来，用串口看一下启动日志")
        return 1
    else:
        print("未重启。确认无误后执行：python tools/ota_push_slave.py --reboot-only")
        return 0


def simple_post(host, port, path, timeout=10):
    try:
        code, data = _request(host, port, "POST", path, timeout=timeout, body=b"")
    except OSError as e:
        print("✗ %s —— %s" % (path, e))
        return 2
    print("HTTP %d  %s" % (code, data.strip()))
    return 0 if code == 200 else 1


def main():
    ap = argparse.ArgumentParser(description="ESP32-S3 从机 OTA 推送")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--firmware", help="要上传的 .bin")
    ap.add_argument("--reboot", action="store_true", help="上传后自动重启进新固件")
    ap.add_argument("--status", action="store_true", help="只看分区/进度")
    ap.add_argument("--reboot-only", action="store_true", help="只重启")
    ap.add_argument("--rollback", action="store_true", help="切回另一个槽并重启")
    ap.add_argument("--timeout", type=int, default=240, help="上传整体超时（秒）")
    a = ap.parse_args()

    if a.status:
        return show_status(a.host, a.port)
    if a.rollback:
        return simple_post(a.host, a.port, "/api/ota/rollback")
    if a.reboot_only:
        return simple_post(a.host, a.port, "/api/ota/reboot")
    if not a.firmware:
        ap.error("要么给 --firmware，要么用 --status / --reboot-only / --rollback")
    if not os.path.isfile(a.firmware):
        print("找不到固件：%s" % a.firmware)
        return 2
    return upload(a.host, a.port, a.firmware, a.reboot, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
