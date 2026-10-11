#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电脑侧视觉大模型（VLM）服务 —— 一键启动。

为什么需要这个脚本（而不是直接 `python vlm_service.py`）
--------------------------------------------------------
1. 板端 8092 只是个**代理**，真正跑模型的是这台电脑。启动前必须把
   `VLM_API_KEY` / `VLM_BACKEND` / `VLM_OPENAI_MODEL` / `VLM_BOARD_IP`
   都准备好 —— 手敲很容易漏一个，而漏了的表现是「板端点识图问答没反应」，
   从现象上完全看不出是环境变量的问题。
2. 现场换网段后，板端 `/home/linaro/ai/vlm/vlm_client_config.json` 的
   `pc_url` 必须指向**这台电脑的当前 IP**。本脚本会把本机 IP 打出来，
   照着改即可（2026-10-11 就是两边都停在旧网段，整条链是死的）。

用法
----
    python rk3568/vlm/pc_start.py              # 起服务，监听 0.0.0.0:8097
    python rk3568/vlm/pc_start.py --port 8097
    python rk3568/vlm/pc_start.py --check      # 只做连通性自检，不起服务

密钥来源（按顺序找，都不写进版本库）
------------------------------------
    VLM_API_KEY   →  DASHSCOPE_API_KEY

⚠️ 模型名别照抄文档里的 `qwen-vl-max`。2026-10-11 实测这台机器上的 key 是
   **国内账号**（`sk-ws-` 前缀），该账号下**没有 qwen-vl-max**，调用直接返回
   `Model not exist.`。可用的视觉模型是 `qwen3-vl-plus`。换 key 后先跑：

       python rk3568/vlm/pc_start.py --check

   它会列出该 key 实际可用的视觉模型。
"""
import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = "qwen3-vl-plus"
DEFAULT_BOARD = "10.176.240.215"
DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_PORT = 8097


def local_ips():
    """列出本机所有 IPv4，最可能被板端用到的排前面。"""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    # 兜底：对外探一次，拿到默认出口网卡的 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.insert(0, ip)
        else:
            ips.remove(ip)
            ips.insert(0, ip)
    except OSError:
        pass
    return ips


def pick_lan_ip(ips, board_ip):
    """挑一个和板端同网段的 IP；挑不出就用第一个非虚拟网段。"""
    if board_ip:
        prefix = board_ip.rsplit(".", 1)[0] + "."
        for ip in ips:
            if ip.startswith(prefix):
                return ip
    for ip in ips:
        if not ip.startswith(("127.", "169.254.", "192.168.136.", "192.168.190.")):
            return ip
    return ips[0] if ips else "(未知)"


def resolve_key():
    for name in ("VLM_API_KEY", "DASHSCOPE_API_KEY"):
        val = os.environ.get(name)
        if val:
            return val, name
    return "", ""


def probe_models(base, key):
    """列出该 key 实际可用的视觉模型 —— 模型名写错是最常见的失败。"""
    req = urllib.request.Request(base.rstrip("/") + "/models",
                                 headers={"Authorization": "Bearer " + key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, "HTTP %d %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:
        return None, repr(exc)
    ids = [m.get("id") for m in (data.get("data") or [])]
    return ids, ""


def main():
    ap = argparse.ArgumentParser(description="电脑侧 VLM 服务一键启动")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--board", default=DEFAULT_BOARD, help="板端 IP")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--check", action="store_true", help="只做自检，不起服务")
    args = ap.parse_args()

    key, key_src = resolve_key()

    print("=" * 66)
    print("电脑侧 VLM 服务")
    print("=" * 66)

    ips = local_ips()
    lan = pick_lan_ip(ips, args.board)
    print("本机 IPv4      : %s" % ", ".join(ips) if ips else "本机 IPv4      : (取不到)")
    print("板端应指向     : http://%s:%d" % (lan, args.port))
    print("板端 IP        : %s" % args.board)
    print("后端 / 模型    : openai / %s" % args.model)
    print("接口地址       : %s" % args.base)
    if key:
        print("密钥           : 来自 %s（%d 字符）" % (key_src, len(key)))
    else:
        print("密钥           : ❌ 没有！请设置 VLM_API_KEY 或 DASHSCOPE_API_KEY")
    print()

    if not key:
        print("设置办法（任选其一）：")
        print("    setx DASHSCOPE_API_KEY sk-xxxxxxxx      # 永久，需重开终端")
        print("    set VLM_API_KEY=sk-xxxxxxxx             # 仅当前窗口")
        return 2

    # ---- 自检：模型名对不对 --------------------------------------
    ids, err = probe_models(args.base, key)
    if ids is None:
        print("⚠️ 列模型失败：%s" % err)
        print("   继续尝试启动；若报 'Model not exist.' 就是这个原因。")
    else:
        vl = [i for i in ids if i and "vl" in i]
        print("该 key 可用模型共 %d 个，其中视觉模型：" % len(ids))
        for i in vl[:20]:
            print("    %s%s" % (i, "   ← 本脚本在用" if i == args.model else ""))
        if args.model not in ids:
            print()
            print("❌ 模型 %r 不在可用列表里 —— 调用会返回 'Model not exist.'。" % args.model)
            print("   请改用上面列出的某个视觉模型，例如：")
            if vl:
                print("       python %s --model %s" % (os.path.basename(__file__), vl[0]))
            return 2
        print()
        print("✅ 模型可用")

    if args.check:
        print()
        print("（--check：不自检完不起服务）")
        return 0

    # ---- 起服务 --------------------------------------------------
    os.environ["VLM_BACKEND"] = "openai"
    os.environ["VLM_OPENAI_MODEL"] = args.model
    os.environ["VLM_OPENAI_BASE"] = args.base
    os.environ["VLM_API_KEY"] = key
    os.environ["VLM_BOARD_IP"] = args.board

    sys.path.insert(0, HERE)
    import vlm_service

    print()
    print("=" * 66)
    print("启动中… 板端 8092 的 /api/vlm/ask 会转发到这里。")
    print("换网段后记得同步改板端 vlm_client_config.json 的 pc_url。")
    print("=" * 66)
    print()
    # ⚠️ vlm_service.main() **不接受参数**，它自己解析 sys.argv。
    # 所以这里得先把 sys.argv 摆好，不能写成 main(["--port", ...])。
    sys.argv = ["vlm_service.py", "--port", str(args.port), "--host", args.host]
    return vlm_service.main()


if __name__ == "__main__":
    sys.exit(main() or 0)
