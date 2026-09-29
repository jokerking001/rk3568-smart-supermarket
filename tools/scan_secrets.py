# -*- coding: utf-8 -*-
"""扫描仓库里残留的明文凭据，只输出 文件:行号:类型，不输出凭据本身。"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# (标签, 正则)  —— 只报告位置，不回显明文
PATTERNS = [
    ("dashscope_sk", re.compile(r"sk-[0-9a-f]{20,}")),
    ("mimiclaw_key", re.compile(r"mimiclaw-store-\d+")),
    ("wifi_ssid_var", re.compile(r"\bssid[12]?\s*=\s*\"")),
    ("wifi_pass_var", re.compile(r"\bpassword[12]?\s*=\s*\"")),
    ("baidu_key", re.compile(r"baidu_(api|secret)_key")),
    ("juhe_key", re.compile(r"apis\.juhe\.cn|juhe.*key", re.I)),
    ("bearer", re.compile(r"Bearer\s*\+?\s*\"?[A-Za-z0-9_\-]{16,}")),
    ("fusion_url", re.compile(r"FUSION_URL\s*=")),
    ("hard_ip", re.compile(r"http://(?:192\.168|10\.|172\.(?:1[6-9]|2\d|3[01]))\.[0-9.]+")),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules"}
SKIP_EXT = {".pyc", ".png", ".jpg", ".jpeg", ".bin", ".a", ".o", ".elf", ".rknn", ".onnx", ".pt", ".tflite"}


def main():
    hits = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXT:
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except Exception:
                continue
            scanned += 1
            for i, line in enumerate(lines, 1):
                for label, rx in PATTERNS:
                    if rx.search(line):
                        hits.setdefault(label, []).append("%s:%d" % (rel, i))

    print("scanned_files=%d" % scanned)
    print("")
    if not hits:
        print("== 没有发现残留明文凭据 ==")
        return 0
    total = 0
    for label in sorted(hits):
        locs = hits[label]
        total += len(locs)
        print("== %s (%d 处) ==" % (label, len(locs)))
        for loc in locs[:40]:
            print("   %s" % loc)
        if len(locs) > 40:
            print("   ... 还有 %d 处" % (len(locs) - 40))
        print("")
    print("TOTAL=%d" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
