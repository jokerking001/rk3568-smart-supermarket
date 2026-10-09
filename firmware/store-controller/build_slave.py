#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译（可选上传）store-controller 从机固件。

**为什么需要这个脚本**：Arduino 硬性要求 sketch 目录名与主 `.ino` 同名，
而这个工程目录叫 `store-controller/`、主文件叫 `Work7_20.ino` ——
直接 `arduino-cli compile firmware/store-controller` 会报
`main file missing from sketch`。

所以本脚本先把整个 sketch **复制**到一个同名的临时目录
（`<build>/sketch/Work7_20/`），再编译。仓库结构不用动。

依赖：
  * arduino-cli（本机在 D:\\789\\.tools\\arduino-cli\\arduino-cli.exe）
  * esp32 core 3.3.8（装在 D:\\Arduino15）
  * 同目录下有 secrets.h（从 secrets.h.example 复制，见文件头说明）

用法：
    python build_slave.py                 # 只编译
    python build_slave.py --upload COM7   # 编译并烧录
    python build_slave.py --clean         # 先清空构建目录
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKETCH_NAME = "Work7_20"          # 必须与主 .ino 同名

# 本机默认路径（按 SOFTWARE_CAPABILITIES.md）
CLI = r"D:\789\.tools\arduino-cli\arduino-cli.exe"
CONFIG = r"E:\tmp\fruit8_deploy\arduino-cli-supermarket.yaml"
BUILD = r"D:\789\.arduino-build"
# 板子：ESP32-S3 N16R8（16MB Flash + 8MB OPI PSRAM）
FQBN = ("esp32:esp32:esp32s3:"
        "FlashSize=16M,PSRAM=opi,PartitionScheme=custom")

# sketch 里不需要参与编译的
SKIP_EXT = {".py", ".md", ".txt", ".zip", ".pdf", ".png", ".jpg", ".log"}
SKIP_NAME = {"secrets.h.example"}


def stage_sketch(dest):
    """把本目录复制成 Arduino 认得的 sketch 结构。

    用**增量复制**，不先删后建：沙箱对批量删除有保护（一次 >50 个文件会被拦），
    而 sketch 有 30+ 个文件、加上历史残留会越过阈值。增量复制既不触发保护，
    也顺带省掉每轮重写全部文件的开销。

    （需要彻底重建时用 `--clean`，那是显式操作。）
    """
    os.makedirs(dest, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(HERE)):
        src = os.path.join(HERE, name)
        if not os.path.isfile(src):
            continue
        if name in SKIP_NAME:
            continue
        if os.path.splitext(name)[1].lower() in SKIP_EXT:
            continue
        target = os.path.join(dest, name)
        if (os.path.isfile(target)
                and os.path.getsize(target) == os.path.getsize(src)
                and os.path.getmtime(target) >= os.path.getmtime(src)):
            continue                      # 源文件没更新过，跳过
        shutil.copy2(src, target)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", default=None,
                    help="编译后烧录到该串口，如 COM7")
    ap.add_argument("--clean", action="store_true", help="先清空构建目录")
    ap.add_argument("--cli", default=CLI)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--fqbn", default=FQBN)
    ap.add_argument("--build", default=BUILD)
    a = ap.parse_args()

    if not os.path.isfile(a.cli):
        print("找不到 arduino-cli：%s" % a.cli)
        return 2
    if not os.path.isfile(os.path.join(HERE, "secrets.h")):
        print("!! 缺 secrets.h —— 先执行：")
        print("     cp secrets.h.example secrets.h")
        print("   然后按需填入真实 WiFi / 密钥（占位值也能编译，只是跑不起来）")
        return 2

    sketch = os.path.join(a.build, "sketch", SKETCH_NAME)
    out = os.path.join(a.build, "output", SKETCH_NAME)
    if a.clean and os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)

    n = stage_sketch(sketch)
    print("已准备 sketch：%s（%d 个文件）" % (sketch, n))
    print("板型：%s" % a.fqbn)
    print()

    cmd = [a.cli, "--config-file", a.config, "compile",
           "--fqbn", a.fqbn, "--output-dir", out,
           # 固定 build-path：否则 arduino-cli 每轮都用临时目录，
           # 于是每轮都要重编整个 core（实测 ~4 分钟）。固定之后只重编改动的文件。
           "--build-path", os.path.join(a.build, "cache", SKETCH_NAME),
           "--warnings", "default", sketch]
    print("$", " ".join('"%s"' % c if " " in c else c for c in cmd))
    t0 = time.time()
    p = subprocess.run(cmd)
    dt = time.time() - t0
    print()
    if p.returncode != 0:
        print("✗ 编译失败（%.0fs），退出码 %d" % (dt, p.returncode))
        return p.returncode

    print("✅ 编译成功（%.0fs）" % dt)
    print()
    print("产物目录：%s" % out)
    if os.path.isdir(out):
        for f in sorted(os.listdir(out)):
            fp = os.path.join(out, f)
            if os.path.isfile(fp):
                print("   %-44s %9d B" % (f, os.path.getsize(fp)))

    if a.upload:
        print()
        print("--- 烧录到 %s ---" % a.upload)
        # --input-dir 必带：产物被 --output-dir 定向到了 out，arduino-cli 3.x
        # 的 upload 不会自动去那里找，而是回到 sketch 目录 / 默认缓存
        # （AppData\Local\arduino\sketches\...）找 .partitions.bin，
        # 于是报 `Errno 2: No such file or directory: ...Work7_20.ino.partitions.bin`。
        ucmd = [a.cli, "--config-file", a.config, "upload",
                "-p", a.upload, "--fqbn", a.fqbn,
                "--input-dir", out, sketch]
        print("$", " ".join(ucmd))
        return subprocess.run(ucmd).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
