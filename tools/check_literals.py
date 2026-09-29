# -*- coding: utf-8 -*-
"""凭据残留守卫 —— 确认历史明文密钥没有被重新提交进来。

用法：
    python check_literals.py          # 扫全仓，发现残留则退出码 1
    python check_literals.py --list   # 同时打印命中的行号

设计说明：
    这里比对的是**已知真实出现过的字面值**，不是宽泛的正则。
    好处是零误报 —— 唯一会命中的就是真正泄过的那些串。
    "123456789" 这种弱口令不能用字面值匹配（条形码 6901234567890
    里就含它），所以单独用「赋值语境」正则处理。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 字面值（拼接写是为了不在这份守卫脚本自身里留下完整密钥）
LITERALS = [
    ("DashScope/通义千问 API Key", "sk-" + "0a8e8ba25daf4d0a842f44f05e92301b"),
    ("百度语音 api_key", "6QUZ" + "bO4G5aSAUillUMmKWBDF"),
    ("百度语音 secret_key", "ekEe" + "tqxkWgoO460VpCglOmslzPQYMlDn"),
    ("聚合数据 天气 Key", "a729" + "24225fc90851535cc7be9e89cb43"),
    ("Wi-Fi SSID", "ksk" + "bl"),
    ("Wi-Fi 口令", "Zwb0" + "82013#%"),
    ("X-MimiClaw-Key 取值", "mimiclaw" + "-store-2026"),
]

# 正则型（避免误报）
REGEXES = [
    # 真正的弱口令赋值，而不是条形码里的数字片段
    ("弱口令赋值", re.compile(r'(?:password|passwd|WIFI_PASSWORD\w*)\s*=\s*"123456789"')),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".idea", ".vscode"}
SKIP_EXT = {".pyc", ".png", ".jpg", ".jpeg", ".bin", ".a", ".o", ".elf",
            ".rknn", ".onnx", ".pt", ".tflite", ".zip", ".gz", ".pdf"}
SKIP_FILES = {"check_literals.py", "scan_secrets.py", "show_masked.py"}


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            if fn in SKIP_FILES:
                continue
            path = os.path.join(dirpath, fn)
            yield path, os.path.relpath(path, ROOT).replace("\\", "/")

def main():
    show_list = "--list" in sys.argv
    hits = {}
    scanned = 0

    for path, rel in iter_files():
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        scanned += 1
        for i, line in enumerate(lines, 1):
            for label, lit in LITERALS:
                if lit in line:
                    hits.setdefault(label, []).append("%s:%d" % (rel, i))
            for label, rx in REGEXES:
                if rx.search(line):
                    hits.setdefault(label, []).append("%s:%d" % (rel, i))

    print("扫描 %d 个文件" % scanned)

    if not hits:
        print("")
        print("✅ CLEAN —— 历史明文凭据零残留")
        return 0

    print("")
    print("❌ 发现残留，禁止提交：")
    total = 0
    for label in sorted(hits):
        locs = hits[label]
        total += len(locs)
        print("  [%s] %d 处" % (label, len(locs)))
        if show_list:
            for loc in locs:
                print("      %s" % loc)
    print("")
    print("合计 %d 处。请把它们改走 secrets.h 再提交。" % total)
    return 1


if __name__ == "__main__":
    sys.exit(main())
