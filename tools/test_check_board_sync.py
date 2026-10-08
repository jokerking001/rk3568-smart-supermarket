#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_board_sync.py 的自测。

板子不在手边时，怎么证明这个核对工具真的能抓出漂移？
—— 拿一个本地目录假装板端：先把仓库里的文件按映射表铺成"一致"的样子，
再故意改一个、删一个，看它认不认。

四个离线场景 + 一个真机分支场景：
  1. 假板端与本地完全一致        -> 退出码 0，报"必须项全部一致"
  2. 改掉一个 required 文件       -> 退出码 1，报"不一致"
  3. 删掉一个 required 文件       -> 退出码 1，报"板端缺失"
  4. 删掉一个可选文件             -> 退出码 0（可选不该让核对失败）
  5. 把真正的远端脚本本地跑一遍   -> 清单每一条都要回来，包括最后一条

第 5 条是补的：1-4 全走 `--local-root`，碰不到 probe_board 的 ssh 分支，
所以"最后一条被 while read 吞掉"这个 bug 在离线自测里完全隐形。

用法：
    python tools/test_check_board_sync.py

临时目录用系统临时目录，可用 BOARD_SYNC_TEST_DIR 环境变量改到别的盘。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CHECKER = os.path.join(HERE, "check_board_sync.py")
MANIFEST = os.path.join(HERE, "board_sync_manifest.json")


def load_entries():
    with io.open(MANIFEST, "r", encoding="utf-8") as fh:
        return json.load(fh)["entries"]


def fake_path(root, remote):
    return os.path.join(root, remote.lstrip("/").replace("/", os.sep))


def build_fake_board(root, entries):
    for e in entries:
        src = os.path.join(REPO, e["local"].replace("/", os.sep))
        dst = fake_path(root, e["remote"])
        parent = os.path.dirname(dst)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        shutil.copy2(src, dst)


def run_checker(root):
    proc = subprocess.run(
        [sys.executable, CHECKER, "--local-root", root],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def expect(name, rc, out, want_rc, want_substrings, want_absent=()):
    problems = []
    if rc != want_rc:
        problems.append("退出码 %d（期望 %d）" % (rc, want_rc))
    for sub in want_substrings:
        if sub not in out:
            problems.append("缺少预期内容: %s" % sub)
    for sub in want_absent:
        if sub in out:
            problems.append("不该出现: %s" % sub)
    print("=== %s ===" % name)
    print("退出码 %d（期望 %d）" % (rc, want_rc))
    print("  -> %s" % ("通过" if not problems else "不通过: " + "；".join(problems)))
    print("")
    return not problems


def find_usable_shell():
    """找一个真能跑的 shell。

    Windows 上 `bash` 经常解析到 WSL 的 stub，而它可能被安全策略拦掉 ——
    那时 subprocess 会静默返回空输出，把测试变成**假失败**（或更糟：假通过）。
    所以先探一下再决定用不用，宁可跳过也不出假结果。
    """
    for name in ("bash", "sh"):
        path = shutil.which(name)
        if not path:
            continue
        low = path.lower()
        if "wsl" in low or "windowsapps" in low:
            continue
        try:
            probe = subprocess.run([path, "-c", "echo probe-ok"],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            continue
        if probe.returncode == 0 and b"probe-ok" in probe.stdout:
            return path
    return None


def test_last_entry_not_dropped():
    """场景 5：清单里最后一条不能被静默丢掉。

    前面四个场景走的都是 `--local-root`，那条路**完全不碰** probe_board 的
    ssh 分支。而「最后一条被 `while read` 吞掉」这个 bug 只在 ssh 分支里出现，
    所以离线自测看不见它 —— 真机上表现是「映射表最后一条永远报板端缺失」，
    而那条文件其实好好的。

    分两层验：
      a) 清单契约 —— 喂出去的字节末尾必须有换行符，且每条路径都能原样取回。
         这一层不依赖 shell，任何平台都跑。
      b) 实跑远端脚本 —— 有可用 shell 才跑，数回来的行数够不够。
    """
    import check_board_sync as cbs

    paths = [e["remote"] for e in load_entries()]
    payload = cbs.paths_payload(paths)
    text = payload.decode("utf-8")

    problems = []

    # ---- a) 清单契约 ----
    if not text.endswith("\n"):
        problems.append("清单末尾没有换行符 —— 远端 while read 会吞掉最后一条")
    lines = text.split("\n")
    if lines[-1] != "":
        problems.append("按换行切分后最后一段不是空串")
    recovered = [ln for ln in lines if ln]
    if recovered != paths:
        problems.append("清单切出来后和原路径列表对不上（%d vs %d 条）"
                        % (len(recovered), len(paths)))

    # ---- b) 实跑远端脚本 ----
    shell = find_usable_shell()
    if shell:
        proc = subprocess.run([shell, "-c", cbs.REMOTE_SCRIPT],
                              input=payload, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("utf-8", "replace")
        seen = [ln.split(" ", 2)[-1] for ln in out.splitlines()
                if ln.startswith("OK ") or ln.startswith("MISS ")]
        if len(seen) != len(paths):
            problems.append("远端脚本发出 %d 条、只回 %d 条（少了 %d 条）"
                            % (len(paths), len(seen), len(paths) - len(seen)))
        if paths[-1] not in seen:
            problems.append("最后一条被远端脚本吞掉了: %s" % paths[-1])
        note = "用 %s 实跑，回 %d 条" % (shell, len(seen))
    else:
        note = "本机没有可用 shell（WSL stub 被拦），只验清单契约"

    print("=== 5. 清单最后一条不能被吞掉 ===")
    print("清单 %d 条；%s" % (len(paths), note))
    print("  最后一条: %s" % paths[-1])
    print("  -> %s" % ("通过" if not problems else "不通过: " + "；".join(problems)))
    print("")
    return not problems


def main():
    if not os.path.isfile(CHECKER) or not os.path.isfile(MANIFEST):
        print("找不到核对工具或映射表")
        return 1

    entries = load_entries()
    required = [e for e in entries if e.get("required", True)]
    optional = [e for e in entries if not e.get("required", True)]
    if not required or not optional:
        print("映射表里 required / 可选 两类都得有，自测才有意义")
        return 1

    base = os.environ.get("BOARD_SYNC_TEST_DIR") or tempfile.mkdtemp(prefix="board-sync-")
    if not os.path.isdir(base):
        os.makedirs(base)

    failures = []
    results = []

    # ---- 1. 完全一致 ----
    root = os.path.join(base, "sync")
    if os.path.isdir(root):
        shutil.rmtree(root)
    build_fake_board(root, entries)
    rc, out = run_checker(root)
    results.append(expect("1. 假板端与本地一致", rc, out, 0,
                          ["必须项全部一致", "共 %d 项" % len(entries)]))

    # ---- 2. 改掉一个 required 文件（模拟"本地改了没部署"）----
    victim = required[0]
    p = fake_path(root, victim["remote"])
    with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n# 假装这是板端上的旧版本，和本地不一样\n")
    rc, out = run_checker(root)
    results.append(expect("2. required 文件不一致", rc, out, 1,
                          ["不一致", victim["local"]]))

    # 还原，免得影响后面
    shutil.copy2(os.path.join(REPO, victim["local"].replace("/", os.sep)), p)

    # ---- 3. 删掉一个 required 文件 ----
    missing_req = required[1]
    os.remove(fake_path(root, missing_req["remote"]))
    rc, out = run_checker(root)
    results.append(expect("3. required 文件板端缺失", rc, out, 1,
                          ["板端缺失", missing_req["local"]]))

    # 还原
    build_fake_board(root, [missing_req])

    # ---- 4. 删掉一个可选文件：不该让核对失败 ----
    missing_opt = optional[0]
    os.remove(fake_path(root, missing_opt["remote"]))
    rc, out = run_checker(root)
    results.append(expect("4. 可选文件缺失不该失败", rc, out, 0,
                          ["必须项全部一致"],
                          want_absent=["❌ 必须项有"]))

    # ---- 5. 真机分支：远端脚本不能吞掉清单最后一条 ----
    results.append(test_last_entry_not_dropped())

    print("=" * 60)
    if not all(results):
        print("❌ 有场景不符合预期")
        return 1
    print("✅ 五个场景全部符合预期 —— 核对工具能抓漂移，不被可选项误伤，"
          "也不会漏掉清单最后一条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
