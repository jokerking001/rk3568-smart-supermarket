# -*- coding: utf-8 -*-
"""打印文件内容，但把凭据值打码，便于在安全策略下查看结构。"""
import re
import sys

path = sys.argv[1]
start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
end = int(sys.argv[3]) if len(sys.argv) > 3 else 10 ** 9

MASKERS = [
    (re.compile(r'(WIFI_SSID\s*[:=]\s*)"[^"]*"'), r'\1"<MASKED>"'),
    (re.compile(r'(WIFI_PASSWORD\s*[:=]\s*)"[^"]*"'), r'\1"<MASKED>"'),
    (re.compile(r'(ssid[12]?\s*=\s*)"[^"]*"'), r'\1"<MASKED>"'),
    (re.compile(r'(password[12]?\s*=\s*)"[^"]*"'), r'\1"<MASKED>"'),
    (re.compile(r'("sk-)[0-9a-zA-Z]+"'), r'\1<MASKED>"'),
    (re.compile(r'(baidu_(?:api|secret)_key\s*=\s*)"[^"]*"'), r'\1"<MASKED>"'),
    (re.compile(r'(=\s*")[0-9a-f]{28,}(\s*;)'), r'\1<MASKED>\2'),
    (re.compile(r'mimiclaw-store-\d+'), '<MASKED_KEY>'),
]

with open(path, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

for i, line in enumerate(lines, 1):
    if i < start or i > end:
        continue
    out = line.rstrip("\n")
    for rx, rep in MASKERS:
        out = rx.sub(rep, out)
    print("%4d| %s" % (i, out))
