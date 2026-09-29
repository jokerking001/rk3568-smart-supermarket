# -*- coding: utf-8 -*-
"""check_py37.py 的自测。

一个只会说"没问题"的守卫比没有守卫更危险 —— 它让人以为安全。
所以这个自测要同时证明两件事：

  1. 该抓的能抓到（正例）
  2. 不该报的不会报（反例）—— 尤其是注释/文档字符串里提到危险写法时

用法：
    python tools/test_check_py37.py

退出码 0 全过，1 有样本不符合预期。
临时样本写到系统临时目录，可用 PY37_TEST_DIR 环境变量改到别的盘。
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "check_py37.py")

SYNTAX_BAD = '''# -*- coding: utf-8 -*-
def f(xs):
    if (n := len(xs)) > 3:      # 海象运算符 3.8+
        return n
    return 0
'''

SEMANTIC_BAD = '''# -*- coding: utf-8 -*-
import math


def merge(extra):
    return {"a": 1} | extra             # dict 合并 3.9+（字面量参与，可静态识别）


def trim(s):
    return s.removeprefix("ab")         # 3.9+


def lcm2():
    return math.lcm(4, 6)               # 3.9+


def ann(x: list) -> dict:               # 不带下标，合法
    return {}


def real_generic():
    z: list = []                        # 合法
    return z


def set_union(a, b):
    return a | b                        # 集合求并，3.7 合法 —— 不该报
'''

CLEAN = '''# -*- coding: utf-8 -*-
"""这个文件的注释和文档字符串里会提到一些危险写法：
   list[int]、dict[str, int]、s.removeprefix()、math.lcm()、a | b
但都不是真实代码，守卫不应该报。
"""

NOTE = "文档里也提一句 list[int] 和 removeprefix"


def ok(a, b):
    c = dict(a, **b)        # 3.7 合法的字典合并
    return c
'''

CASES = [
    # (文件名, 源码, 期望退出码, 必须出现的关键词)
    ("syntax_bad.py", SYNTAX_BAD, 1, ["语法", "Assignment expressions"]),
    ("semantic_bad.py", SEMANTIC_BAD, 1, ["dict 合并", "removeprefix", "math.lcm"]),
    ("clean.py", CLEAN, 0, []),
]

# 反例：这些出现在输出里（且带 [语义] 标记）就算误报
NOT_EXPECTED = {
    "semantic_bad.py": ["set_union", "集合"],
    "clean.py": ["list[int]", "removeprefix", "math.lcm", "dict 合并"],
}

# semantic_bad.py 必须恰好报 3 处 —— 少一处是漏报，多一处是误报
EXACT_COUNT = {"semantic_bad.py": 3, "clean.py": 0}


def run_case(tmpdir, name, src, want_rc, want_substrings):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src)

    proc = subprocess.run(
        [sys.executable, GUARD, path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")

    print("=== %s ===" % name)
    print(out.rstrip())
    print("退出码 %d（期望 %d）" % (proc.returncode, want_rc))

    problems = []
    if proc.returncode != want_rc:
        problems.append("退出码不对")

    for sub in want_substrings:
        if sub not in out:
            problems.append("缺少预期内容: %s" % sub)

    # 反例：逐行扫，只在带 [语义] 标记的问题行上判误报，
    # 避免命中文件名或"已知漏报"说明文字。
    for bad in NOT_EXPECTED.get(name, []):
        for line in out.splitlines():
            if bad in line and "[语义]" in line:
                problems.append("误报: %s" % line.strip())

    # 数量：从汇总行 "扫描 N 个文件，M 个有问题（共 K 处）" 里抠 K
    if name in EXACT_COUNT:
        want_k = EXACT_COUNT[name]
        summary = [l for l in out.splitlines() if "个有问题（共 " in l]
        if not summary:
            problems.append("没找到汇总行")
        elif ("共 %d 处" % want_k) not in summary[0]:
            problems.append("处数不对，期望 %d 处 —— %s" % (want_k, summary[0].strip()))

    print("  -> %s" % ("通过" if not problems else "不通过: " + "；".join(problems)))
    print("")
    return not problems


def main():
    if not os.path.isfile(GUARD):
        print("找不到守卫脚本: %s" % GUARD)
        return 1

    tmpdir = os.environ.get("PY37_TEST_DIR") or tempfile.mkdtemp(prefix="py37-guard-")
    if not os.path.isdir(tmpdir):
        os.makedirs(tmpdir)

    failed = []
    for name, src, want_rc, want_subs in CASES:
        if not run_case(tmpdir, name, src, want_rc, want_subs):
            failed.append(name)

    print("=" * 50)
    if failed:
        print("❌ 失败样本: %s" % ", ".join(failed))
        return 1
    print("✅ 三个样本全部符合预期 —— 守卫有效且不误报")
    return 0


if __name__ == "__main__":
    sys.exit(main())
