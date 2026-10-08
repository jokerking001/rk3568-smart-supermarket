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
import subprocess
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
# 二进制格式一律跳过：里面的字节序列是随机的，拿字面值去匹配必然假阳性
# （踩过：parquet 里偶然出现 "kskbl"，把一次正常提交拦了）。
SKIP_EXT = {".pyc", ".png", ".jpg", ".jpeg", ".bin", ".a", ".o", ".elf",
            ".rknn", ".onnx", ".pt", ".tflite", ".zip", ".gz", ".pdf",
            ".parquet", ".arrow", ".feather", ".npz", ".npy", ".so", ".dll",
            ".exe", ".whl", ".tar", ".7z", ".rar", ".db", ".sqlite"}
SKIP_FILES = {"check_literals.py", "scan_secrets.py", "show_masked.py"}


def git_ignored(paths):
    """批量问出哪些路径被 .gitignore 忽略。

    忽略的文件**永远不会被提交**，扫它们只会制造假阳性 ——
    数据集、模型权重、构建产物都在忽略名单里，而它们恰恰都是二进制。
    用 `git check-ignore --stdin -z` 一次问完，比逐个调用快几个数量级。

    git 不可用 / 不在仓库里时返回空集（退回"全扫"），不能让守卫自己挂掉。
    """
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            cwd=ROOT,
            input="\0".join(paths).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180,
        )
    except Exception:
        return set()
    if proc.returncode not in (0, 1):     # 1 = 没有任何路径被忽略，正常
        return set()
    out = proc.stdout.decode("utf-8", "replace")
    return set(p for p in out.split("\0") if p)


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
    skipped_ignored = 0

    # 先把被 gitignore 的路径剔掉 —— 它们永远不会被提交，扫了只会假阳性
    candidates = list(iter_files())
    ignored = git_ignored([rel for _, rel in candidates])

    for path, rel in candidates:
        if rel in ignored:
            skipped_ignored += 1
            continue
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

    print("扫描 %d 个文件（跳过 %d 个被 gitignore 的）"
          % (scanned, skipped_ignored))

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
