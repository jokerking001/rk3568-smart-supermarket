#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对本地工程文件与板端已部署副本是否一致。

为什么需要这个：这个项目**出过「本地改了但没部署」的事故**——本地跑得好好的，
板上还是旧版，排查半天才发现服务里跑的是上一个版本。手工 scp 没有版本概念，
只能靠比对哈希兜。

用法：
    RK3568_BOARD=10.x.x.x python tools/check_board_sync.py        # 推荐：IP 走环境变量
    python tools/check_board_sync.py --board 10.x.x.x
    python tools/check_board_sync.py --board 10.x.x.x --user linaro \\
        --key ~/.ssh/id_ed25519_rk3568
    python tools/check_board_sync.py --manifest            # 只打印映射表，不连板子
    python tools/check_board_sync.py --local-root <目录>    # 拿本地目录假装板端（离线自测）

退出码：0 全部一致；1 有 required 项不一致或缺失；2 用法/环境错误。

映射表在 tools/board_sync_manifest.json，板端路径来源是各 unit 的
WorkingDirectory/ExecStart 和部署脚本里的 scp 目标 —— 不是猜的。
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "board_sync_manifest.json")

# 板端 IP 跟着热点变，支持环境变量覆盖：RK3568_BOARD=10.x.x.x
DEFAULT_BOARD = os.environ.get("RK3568_BOARD", "10.176.240.215")
DEFAULT_USER = "linaro"
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519_rk3568")

# 让远端逐行读路径再逐个算哈希。
# 路径走 stdin 而不是拼进命令行 —— 省掉一整套引号转义的地狱。
REMOTE_SCRIPT = r'''while IFS= read -r p; do
  if [ -f "$p" ]; then
    h=$(sha256sum "$p")
    printf 'OK %s %s\n' "${h%% *}" "$p"
  else
    printf 'MISS %s\n' "$p"
  fi
done'''


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def has_crlf(path):
    with open(path, "rb") as fh:
        return b"\r\n" in fh.read()


def load_manifest():
    with io.open(MANIFEST, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("entries", []), data.get("unverified", [])


def local_root_path(root, remote):
    """把板端绝对路径映射到 --local-root 下的相对路径。"""
    return os.path.join(root, remote.lstrip("/").replace("/", os.sep))


def _hash_map(pairs):
    """pairs 是 [(key, 文件路径)]，返回 {key: (status, sha256)}。"""
    result = {}
    for key, path in pairs:
        if not os.path.isfile(path):
            result[key] = ("missing", None)
            continue
        result[key] = ("ok", sha256_of(path))
    return result


def probe_repo(entries):
    """读仓库里的本地副本。**永远从仓库读**，跟 --local-root 无关。"""
    return _hash_map([
        (e["remote"], os.path.join(REPO, e["local"].replace("/", os.sep)))
        for e in entries
    ])


def probe_root(entries, root):
    """把 root 当成板端根目录，按映射表里的板端绝对路径去读。"""
    return _hash_map([
        (e["remote"], local_root_path(root, e["remote"]))
        for e in entries
    ])


def probe_board(entries, board, user, key, timeout):
    """一次 ssh 批量取回所有远端文件的哈希。"""
    paths = [e["remote"] for e in entries]
    cmd = [
        "ssh",
        "-i", key,
        "-o", "IPQoS=none",
        "-o", "ConnectTimeout=%d" % timeout,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "%s@%s" % (user, board),
        REMOTE_SCRIPT,
    ]
    proc = subprocess.run(
        cmd,
        input="\n".join(paths).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("ssh 失败（退出码 %d）：%s" % (proc.returncode, err or "无 stderr"))

    result = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "OK":
            result[parts[2]] = ("ok", parts[1])
        elif len(parts) == 2 and parts[0] == "MISS":
            result[parts[1]] = ("missing", None)
    return result


def main():
    ap = argparse.ArgumentParser(description="核对本地工程与板端部署副本是否一致")
    ap.add_argument("--board", default=DEFAULT_BOARD, help="板端 IP")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--key", default=DEFAULT_KEY, help="SSH 私钥路径")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--manifest", action="store_true", help="只打印映射表，不连板子")
    ap.add_argument("--local-root", default=None,
                    help="拿这个本地目录假装板端根目录（离线自测用）")
    args = ap.parse_args()

    try:
        entries, unverified = load_manifest()
    except Exception as exc:
        print("读不了映射表 %s：%s" % (MANIFEST, exc))
        return 2

    if args.manifest:
        print("本地文件  ->  板端路径        [required]")
        print("-" * 78)
        for e in entries:
            print("%-52s -> %s  [%s]" % (
                e["local"], e["remote"], "必须" if e.get("required", True) else "可选"))
            if e.get("note"):
                print("      // %s" % e["note"])
        print("")
        print("未纳入核对（板端位置待确认）：")
        for u in unverified:
            print("  - %s" % u)
        return 0

    if args.local_root and not os.path.isdir(args.local_root):
        print("--local-root 目录不存在：%s" % args.local_root)
        return 2

    if not os.path.isfile(MANIFEST):
        print("找不到映射表：%s" % MANIFEST)
        return 2

    print("本地副本：%s" % (args.local_root or REPO))
    if args.local_root:
        print("模拟板端：%s" % args.local_root)
    else:
        print("板端    ：%s@%s" % (args.user, args.board))
    print("")

    # 本地永远从仓库读；板端才可能是模拟目录。
    # （一开始两个都从 --local-root 读了，等于自己跟自己比，永远"一致" —— 自测抓出来的。）
    local = probe_repo(entries)

    if args.local_root:
        board = probe_root(entries, args.local_root)
    else:
        if not os.path.isfile(args.key):
            print("找不到 SSH 私钥：%s" % args.key)
            print("用 --key 指定，或先用 --local-root 做离线核对。")
            return 2
        try:
            board = probe_board(entries, args.board, args.user, args.key, args.timeout)
        except RuntimeError as exc:
            print(str(exc))
            return 2

    drift_required = []
    drift_optional = []
    crlf = []
    lines = []

    for e in entries:
        remote = e["remote"]
        required = e.get("required", True)
        lstat, lhash = local.get(remote, ("missing", None))
        bstat, bhash = board.get(remote, ("missing", None))

        if lstat == "missing":
            # 映射表列了但本地没有 —— 要么映射表过期了，要么文件被改名/删了。
            # 这种情况必须报出来，否则清单会悄悄失效、核对变成空转。
            status, mark = "本地缺失（映射表可能过期）", "!!"
        elif bstat == "missing":
            status, mark = "板端缺失", "!!"
        elif lhash == bhash:
            status, mark = "一致", "OK"
        else:
            status, mark = "不一致", "!!"

        if mark == "!!":
            (drift_required if required else drift_optional).append((e["local"], status))

        # 本地文件带 CRLF 的话，scp 上去的行尾和 Linux 版不同，
        # 哈希必然不一致 —— 单独提示，免得当成"没部署"白查半天。
        if not args.local_root:
            path = os.path.join(REPO, e["local"].replace("/", os.sep))
            if os.path.isfile(path) and has_crlf(path):
                crlf.append(e["local"])

        lines.append((mark, e["local"], remote, status, required))

    width = max(len(x[1]) for x in lines) if lines else 20
    for mark, localrel, remote, status, required in lines:
        print("[%s] %-*s  %s%s" % (
            mark, width, localrel, status, "" if required else "（可选）"))

    print("")
    print("-" * 78)
    ok = sum(1 for x in lines if x[0] == "OK")
    print("共 %d 项：一致 %d，有问题 %d（必须 %d / 可选 %d）" % (
        len(lines), ok, len(drift_required) + len(drift_optional),
        len(drift_required), len(drift_optional)))

    if crlf:
        print("")
        print("⚠️ 这些本地文件含 CRLF 行尾，scp 到板端后和 Linux 版哈希必然不同：")
        for c in crlf:
            print("   %s" % c)
        print("   修法：git config core.autocrlf false，然后重新检出该文件。")

    if unverified:
        print("")
        print("未纳入核对（板端位置待确认，见映射表 unverified）：%d 项" % len(unverified))

    if drift_required:
        print("")
        print("❌ 必须项有 %d 处问题：" % len(drift_required))
        for localrel, status in drift_required:
            print("   [%s] %s" % (status, localrel))
        print("   「不一致」= 本地改了但没部署；「板端缺失」= 没部署过或路径变了；")
        print("   「本地缺失」= 映射表过期，先修 tools/board_sync_manifest.json。")
        print("   部署用对应模块的脚本，别手工 mv 覆盖。")
        return 1

    print("")
    print("✅ 必须项全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
