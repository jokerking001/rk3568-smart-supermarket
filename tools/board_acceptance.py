# -*- coding: utf-8 -*-
"""板端一键验收 —— 上板后第一条命令。

把「板子一上线要做的三件事」合成一条命令：

  1. 十个服务的健康检查（8088-8096 + 8099）
  2. 本地工程 vs 板端部署副本的哈希核对（复用 check_board_sync）
  3. 77 项回归基线（store 38 + scanner 20 + ocr 19，在板端跑）

用法：

    # 最常用：板子 IP 给上，其余用默认
    python tools/board_acceptance.py --board 192.168.43.44

    # 只看服务健康，不跑回归（快）
    python tools/board_acceptance.py --board 192.168.43.44 --skip-regression

    # 跳过哈希核对（没配 SSH 时）
    python tools/board_acceptance.py --board 192.168.43.44 --skip-sync

    # 在板子本机上跑（不走 SSH，只做健康检查 + 回归）
    python tools/board_acceptance.py --local

退出码：0 = 全绿；1 = 有必检项失败。

设计要点：
  * **显式禁用代理**。本机 shell 里常常继承着 http_proxy，
    不加 ProxyHandler({}) 的话连 192.168.x.x 会被绕出去超时。
  * 健康检查用**两个候选端点**逐个试（`/health` 和 `/api/xxx/status`），
    因为各服务的状态路由不统一，硬编一个会在服务改名后假红。
  * 回归测试在**板端**跑，不在本机跑 —— 本机跑通不等于板上跑通，
    这正是要防的那类事故。
  * 全部 Python 3.7 兼容（板端是 3.7.3，这个脚本可能在板上跑）。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DEFAULT_BOARD = "10.181.229.215"
DEFAULT_USER = "linaro"
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519_rk3568")

# 端口 -> (服务名, [候选状态端点...])。顺序即尝试顺序，命中第一个就停。
SERVICES = [
    (8088, "rk3568-vision",      ["/health", "/api/vision/result"]),
    (8089, "rk3568-fruit",       ["/api/fruit/status", "/health"]),
    (8090, "rk3568-fusion",      ["/api/fusion/status", "/health"]),
    (8091, "rk3568-lidar",       ["/api/radar/status", "/health"]),
    (8092, "rk3568-vlm",         ["/api/vlm/status", "/health"]),
    (8093, "rk3568-dataset",     ["/api/dataset/status", "/health"]),
    (8094, "rk3568-store",       ["/api/store/status", "/health"]),
    (8095, "rk3568-scanner",     ["/api/scanner/status", "/health"]),
    (8096, "rk3568-ocr",         ["/api/ocr/status", "/health"]),
    (8099, "rk3568-fruit-fusion", ["/api/fusion/status", "/health"]),
]

# 回归基线：板端路径 -> (测试文件名, 期望通过数)。板端目录见 board_sync_manifest.json。
REGRESSION = [
    ("/home/linaro/ai/store",   "test_store_e2e.py",     38),
    ("/home/linaro/ai/scanner", "test_scanner_decode.py", 20),
    ("/home/linaro/ai/ocr",     "test_ocr_parse.py",      19),
]

# 8094 的 E2E 测试打的是 HTTP 接口，跑之前得保证服务在。
E2E_BASE = "http://127.0.0.1:8094"


def opener():
    """不走任何代理的 opener。

    本机 shell 常继承 http_proxy / https_proxy，不显式清掉的话
    连 192.168.x.x 会被绕出去，表现为「板子明明在线却全部超时」。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url, timeout=6):
    """返回 (ok, status, text)。任何异常都收敛成 ok=False，不往外抛。"""
    req = urllib.request.Request(url, headers={"User-Agent": "board-acceptance/1.0"})
    try:
        with opener().open(req, timeout=timeout) as resp:
            return True, resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return True, exc.code, ""      # 有响应就算活着，4xx 也是服务在跑
    except Exception as exc:
        return False, 0, str(exc)


def short_error(text):
    """把 urllib 的长错误串压成一句人话。

    区分「端口拒绝」和「超时」很重要：
      拒绝 = 服务没起来（去看 systemd）
      超时 = 网络/防火墙/被代理绕出去（去看网络）
    """
    low = (text or "").lower()
    if "10061" in low or "refused" in low or "拒绝" in text:
        return "端口未监听（服务没起来）"
    if "timed out" in low or "timeout" in low:
        return "超时（网络不通或被代理绕出去）"
    if "10060" in low:
        return "连接超时"
    if "getaddrinfo" in low or "name or service" in low:
        return "域名解析失败"
    return (text or "").strip().replace("\n", " ")[:50]


def check_services(board, timeout=4):
    """并发探活十个端口。返回 (results, failures)。

    必须并发：串行的话 10 个端口 x 2 个候选 x 超时 = 最坏 80 秒，
    板子不在线时干等两分钟，体验极差。
    """
    def probe(item):
        port, name, candidates = item
        last_err = ""
        for path in candidates:
            ok, status, text = http_get("http://%s:%d%s" % (board, port, path),
                                        timeout=timeout)
            if ok:
                return (port, name, "UP", path, status, summarize(text))
            last_err = text
        return (port, name, "DOWN", candidates[0], 0, short_error(last_err))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
        results = list(pool.map(probe, SERVICES))

    failures = ["%s (%d)" % (name, port)
                for port, name, state, _p, _s, _d in results if state != "UP"]
    return results, failures


def tcp_reachable(board, port=8094, timeout=3):
    """先做一次 TCP 连通性探测，板子整体不在线时立刻收工。"""
    import socket
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((board, port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def summarize(text):
    """从状态 JSON 里挑几个有诊断价值的字段，拼成一行。"""
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip().replace("\n", " ")[:60]
    if not isinstance(data, dict):
        return ""
    keys = ("model_loaded", "ready", "ok", "device", "fps", "inference_ms",
            "loop_fps", "running", "connected", "uptime")
    bits = []
    for key in keys:
        if key in data:
            value = data[key]
            if isinstance(value, float):
                value = round(value, 2)
            bits.append("%s=%s" % (key, value))
    return " ".join(bits)[:80]


def ssh_run(board, user, key, command, timeout=120):
    """在板端跑一条命令，返回 (returncode, stdout, stderr)。

    用 BatchMode=yes —— 免得卡在密码提示上（沙箱里看不到交互提示，会静默挂死）。
    """
    argv = [
        "ssh", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
        "-o", "LogLevel=ERROR",
    ]
    if key:
        argv += ["-i", key]
    argv += ["%s@%s" % (user, board), command]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "", "ssh 超时（%ds）" % timeout
    except Exception as exc:
        return 125, "", str(exc)


def check_sync(board, user, key, timeout):
    """复用 check_board_sync 的核对逻辑，逐条比对哈希。

    返回 (rows, failures)。rows 每项是
    (本地路径, 状态, 是否必检, 备注)，状态取 SAME / DRIFT / MISSING / LOCAL_MISSING。
    """
    try:
        import check_board_sync as cbs
    except ImportError as exc:
        return [], ["无法导入 check_board_sync: %s" % exc]

    entries, _unverified = cbs.load_manifest()
    try:
        remote = cbs.probe_board(entries, board, user, key, timeout)
    except Exception as exc:
        return [], ["核对失败: %s" % exc]

    rows = []
    failures = []
    for entry in entries:
        local_rel = entry["local"]
        remote_path = entry["remote"]
        required = bool(entry.get("required", True))
        local_abs = os.path.join(REPO, local_rel)

        if not os.path.isfile(local_abs):
            rows.append((local_rel, "LOCAL_MISSING", required, "本地文件不存在"))
            if required:
                failures.append("本地缺 %s" % local_rel)
            continue

        got = remote.get(remote_path)
        if got is None:
            # 板端没返回这一条 —— 当缺失处理
            rows.append((local_rel, "MISSING", required, remote_path))
            if required:
                failures.append("板端缺 %s" % remote_path)
            continue

        state, remote_hash = got
        if state == "missing":
            rows.append((local_rel, "MISSING", required, remote_path))
            if required:
                failures.append("板端缺 %s" % remote_path)
            continue

        local_hash = cbs.sha256_of(local_abs)
        if local_hash == remote_hash:
            note = ""
            if cbs.has_crlf(local_abs):
                note = "本地是 CRLF（上板会出 /bin/bash^M）"
            rows.append((local_rel, "SAME", required, note))
        else:
            note = "%s != %s" % (local_hash[:12], remote_hash[:12])
            rows.append((local_rel, "DRIFT", required, note))
            if required:
                failures.append("漂移 %s" % local_rel)
    return rows, failures


def check_regression(board, user, key, timeout, local=False):
    """在板端跑三个回归测试。E2E 那个需要 8094 先起来。

    local=True 时直接在**当前机器**上跑（也就是板子本机），不走 SSH。
    """
    results = []
    failures = []
    for workdir, filename, expected in REGRESSION:
        inner = "python3 %s" % filename
        if filename.endswith("e2e.py"):
            inner += " --base %s" % E2E_BASE
        if local:
            cmd = "cd %s && %s 2>&1 | tail -3" % (workdir, inner)
            code, out, err = shell_run(cmd, timeout=timeout)
        else:
            # 板端 rknnlite 装在 linaro 用户级 site-packages，root 看不到，
            # 所以一律用 sudo -u linaro -H 跑。
            cmd = "cd %s && sudo -u linaro -H %s 2>&1 | tail -3" % (workdir, inner)
            code, out, err = ssh_run(board, user, key, cmd, timeout=timeout)
        passed = parse_passed(out)
        status = "OK" if (code == 0 and passed is not None and passed >= expected) else "FAIL"
        if status == "FAIL":
            failures.append("%s (期望 %d，实得 %s)" % (filename, expected,
                                                      "?" if passed is None else passed))
        results.append((filename, expected, passed, status, (out or err).strip()[-160:]))
    return results, failures


def shell_run(command, timeout=120):
    """在本地 shell 跑一条命令（--local 模式用）。"""
    try:
        proc = subprocess.Popen(["bash", "-lc", command],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "", "命令超时（%ds）" % timeout
    except Exception as exc:
        return 125, "", str(exc)


def parse_passed(text):
    """从 unittest 输出里抠出通过数。找不到返回 None（而不是 0）。"""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("OK") or line.startswith("FAILED") or line.startswith("Ran "):
            continue
        if "passed" in line:
            digits = ""
            for ch in line:
                if ch.isdigit():
                    digits += ch
                elif digits:
                    break
            if digits:
                return int(digits)
    return None


def main():
    parser = argparse.ArgumentParser(description="板端一键验收")
    parser.add_argument("--board", default=DEFAULT_BOARD, help="板端 IP")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--key", default=DEFAULT_KEY, help="SSH 私钥路径")
    parser.add_argument("--timeout", type=int, default=12, help="单次 HTTP 超时")
    parser.add_argument("--ssh-timeout", type=int, default=180, help="单条远端命令超时")
    parser.add_argument("--local", action="store_true",
                        help="在板子本机上跑（只做健康检查 + 回归，不走 SSH）")
    parser.add_argument("--skip-regression", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
    args = parser.parse_args()

    board = "127.0.0.1" if args.local else args.board
    failures = []

    print("=" * 68)
    print("板端一键验收   目标 = %s" % board)
    print("=" * 68)

    # ------------------------------------------------- 0. 先确认板子在线
    # 不在线就别把三个检查全跑一遍干等 —— 直接告诉人去上电。
    if not tcp_reachable(board):
        print("\n✗ 连不上 %s:8094 —— 板子没上电、没接同一个网，或 IP 变了。" % board)
        print("\n上板前确认三件事：")
        print("  1. 板子插电、网口/WiFi 接的是同一个网段")
        print("  2. 板端 IP 没变（之前是 %s）" % args.board)
        print("  3. 本机没开代理绕出去（本脚本已显式禁用代理）")
        print("\n拿到 IP 后重跑：python tools/board_acceptance.py --board <新IP>")
        return 1

    # ---------------------------------------------------------- 1. 服务健康
    print("\n[1/3] 服务健康检查（%d 个端口）" % len(SERVICES))
    results, svc_fail = check_services(board, timeout=args.timeout)
    for port, name, state, path, status, detail in results:
        mark = "  " if state == "UP" else "✗ "
        print("%s%-20s :%-5d %-4s %-28s %s" % (mark, name, port, state, path, detail))
    failures += svc_fail
    print("  -> %d/%d 在线" % (len(SERVICES) - len(svc_fail), len(SERVICES)))

    # ---------------------------------------------------------- 2. 部署一致性
    if args.skip_sync or args.local:
        print("\n[2/3] 部署一致性核对 —— 已跳过")
    else:
        print("\n[2/3] 部署一致性核对（本地 vs 板端哈希）")
        rows, sync_fail = check_sync(board, args.user, args.key, args.timeout)
        if not rows and sync_fail:
            for line in sync_fail:
                print("  ✗ %s" % line)
            print("  （没配 SSH 就用 --skip-sync 显式跳过）")
        else:
            same = sum(1 for r in rows if r[1] == "SAME")
            for local_rel, state, required, note in rows:
                if state == "SAME" and not note:
                    continue
                mark = "  " if state == "SAME" else ("✗ " if required else "· ")
                print("%s%-52s %-14s %s" % (mark, local_rel, state, note))
            print("  -> %d/%d 一致" % (same, len(rows)))
            failures += sync_fail

    # ---------------------------------------------------------- 3. 回归基线
    if args.skip_regression:
        print("\n[3/3] 回归基线 —— 已跳过")
    else:
        print("\n[3/3] 回归基线（板端执行，共 77 项）")
        reg, reg_fail = check_regression(board, args.user, args.key,
                                         args.ssh_timeout, local=args.local)
        for filename, expected, passed, status, tail in reg:
            mark = "  " if status == "OK" else "✗ "
            shown = "?" if passed is None else passed
            print("%s%-24s 期望 %-3d 实得 %-4s %s" % (mark, filename, expected, shown, status))
            if tail:
                print("      %s" % tail.replace("\n", " | ")[:150])
        failures += reg_fail

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 68)
    if failures:
        print("✗ 有 %d 项没通过：" % len(failures))
        for item in failures:
            print("    - %s" % item)
        print("\n先修上面这些，再往下做。")
        return 1
    print("✅ 全部通过 —— 板子状态干净，可以开工。")
    print("\n下一步：")
    print("  python tools/stress_test_30min.py --board %s   # 30 分钟压测" % board)
    return 0


if __name__ == "__main__":
    sys.exit(main())
