# -*- coding: utf-8 -*-
"""板端 Python 3.7 兼容性守卫。

RK3568 上是 Debian 10 + **Python 3.7.3**，但大家的开发机是 3.10+。
用新语法在电脑上跑得好好的，一上板就 SyntaxError / TypeError ——
这种错误在本地测不出来，只能靠这个脚本兜。

用法：
    python tools/check_py37.py            # 扫 rk3568/ 下的板端代码
    python tools/check_py37.py --all      # 连 tools/ 一起扫
    python tools/check_py37.py <路径...>   # 只扫指定文件/目录

退出码：0 全部通过；1 有文件不兼容；2 没扫到任何文件。

============================================================
检查分两层：
  1. 语法层 —— ast.parse(feature_version=(3,7))
     抓：海象运算符 :=、位置限定参数 /、f-string 的 = 说明符、
         match 语句、带括号的 with 多上下文……

  2. 语义层 —— 走 AST 找"语法合法、一跑就炸"的 3.8+/3.9+ 用法
     抓：list[int] 这类 PEP 585 泛型下标、
         str.removeprefix/removesuffix、math.lcm 等、
         能静态确定的 dict 合并（字面量或 dict(...) 参与）

  ⚠️ 语义层刻意走 AST 而不是正则 —— 正则会把注释和文档字符串里
     提到 "list[int]" 的地方误报成真问题（第一版就栽在这上面）。

  ⚠️ 已知漏报（宁漏不误）：形如 `a | b` 的两个变量无法静态区分
     是字典合并还是集合求并，因此**不报**。写代码时自己留意。
============================================================
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "build", "node_modules"}

# PEP 585：3.9 起内置容器可以下标当泛型用
BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}

# 3.8 / 3.9 才加的方法和函数
LATE_ATTRS = {
    "removeprefix": "str.removeprefix() 是 3.9+，改用 s[len(p):] if s.startswith(p) else s",
    "removesuffix": "str.removesuffix() 是 3.9+，改用 s[:-len(p)] if p and s.endswith(p) else s",
}

LATE_MATH = {
    "isqrt": "math.isqrt() 是 3.8+，改用 int(n ** 0.5) 或自己实现",
    "comb": "math.comb() 是 3.8+，自己用阶乘实现",
    "perm": "math.perm() 是 3.8+，自己用阶乘实现",
    "lcm": "math.lcm() 是 3.9+，用 a * b // gcd(a, b) 实现",
    "nextafter": "math.nextafter() 是 3.9+，避开或自己实现",
}

FUTURE_ANNOTATIONS_SRC = "from __future__ import annotations"


def rel_to_repo(path):
    """尽量给出仓库相对路径；路径在别的盘符时退回绝对路径。

    （Windows 上 os.path.relpath 跨盘符会直接抛 ValueError。）
    """
    try:
        return os.path.relpath(path, REPO).replace("\\", "/")
    except ValueError:
        return os.path.abspath(path).replace("\\", "/")


class Py37Visitor(ast.NodeVisitor):
    def __init__(self, has_future):
        self.has_future = has_future
        self.problems = []          # [(lineno, 说明, 建议)]

    def _add(self, node, what, how):
        self.problems.append((getattr(node, "lineno", 0), what, how))

    # ---- PEP 585 泛型下标 ----
    def visit_Subscript(self, node):
        base = node.value
        if isinstance(base, ast.Name) and base.id in BUILTIN_GENERICS:
            if not self.has_future:
                self._add(
                    node,
                    "PEP 585 泛型下标（如 %s[...]）是 3.9+，3.7 下会 TypeError" % base.id,
                    "文件开头加 from __future__ import annotations，"
                    "或改用 typing.%s" % base.id.capitalize(),
                )
        self.generic_visit(node)

    # ---- 3.8+/3.9+ 的方法与函数 ----
    def visit_Attribute(self, node):
        if node.attr in LATE_ATTRS:
            self._add(node, LATE_ATTRS[node.attr].split("，")[0], LATE_ATTRS[node.attr])
        elif node.attr in LATE_MATH:
            base = node.value
            if isinstance(base, ast.Name) and base.id == "math":
                self._add(node, LATE_MATH[node.attr].split("，")[0], LATE_MATH[node.attr])
        self.generic_visit(node)

    # ---- dict 合并运算符 | ----
    def visit_BinOp(self, node):
        if isinstance(node.op, ast.BitOr) and self._looks_like_dict_merge(node):
            self._add(
                node,
                "dict 合并运算符 | 是 3.9+，3.7 下会 TypeError",
                "改用 dict(a, **b)，或 c = a.copy(); c.update(b)",
            )
        self.generic_visit(node)

    @staticmethod
    def _looks_like_dict_merge(node):
        """只在能静态确定是字典时才判为合并。

        集合的 | 运算在 3.7 也是合法的，所以不能见 BitOr 就报。
        能确定的只有两种：一边是字典字面量，或一边是 dict(...) 调用。
        形如 `a | b`（两个变量）无法区分字典还是集合，这里**故意不报** ——
        宁可漏报也不误报。
        """
        def is_dictish(n):
            if isinstance(n, ast.Dict):
                return True
            if isinstance(n, ast.Call):
                fn = n.func
                return isinstance(fn, ast.Name) and fn.id == "dict"
            return False

        return is_dictish(node.left) or is_dictish(node.right)


def check_file(path):
    """返回 [(行号, 类别, 说明, 建议), ...]"""
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as exc:
        return [(0, "读取失败", str(exc), "")]

    problems = []

    # ---- 第 1 层：语法 ----
    try:
        tree = ast.parse(src, filename=path, feature_version=(3, 7))
    except SyntaxError as exc:
        return [(exc.lineno or 0, "语法",
                 "%s（Python 3.7 解析不了）" % (exc.msg or ""),
                 "检查海象运算符 :=、match 语句、位置限定参数 /、"
                 "f-string 的 = 说明符等 3.8+ 语法")]

    # ---- 第 2 层：语义（走 AST，注释和字符串自动被忽略）----
    has_future = FUTURE_ANNOTATIONS_SRC in src
    visitor = Py37Visitor(has_future)
    visitor.visit(tree)
    for lineno, what, how in visitor.problems:
        problems.append((lineno, "语义", what, how))

    problems.sort(key=lambda p: p[0])
    return problems


def iter_targets(argv):
    paths = [a for a in argv if not a.startswith("--")]
    if paths:
        for a in paths:
            if os.path.isdir(a):
                for dp, dns, fns in os.walk(a):
                    dns[:] = [d for d in dns if d not in SKIP_DIRS]
                    for fn in sorted(fns):
                        if fn.endswith(".py"):
                            yield os.path.join(dp, fn)
            elif a.endswith(".py"):
                yield a
        return

    roots = [os.path.join(REPO, "rk3568")]
    if "--all" in argv:
        roots.append(HERE)
    for root in roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for fn in sorted(fns):
                if fn.endswith(".py"):
                    yield os.path.join(dp, fn)


def main():
    targets = list(iter_targets(sys.argv[1:]))
    if not targets:
        print("没有扫到任何 .py 文件")
        return 2

    total = 0
    bad_files = 0
    for path in targets:
        rel = rel_to_repo(path)
        problems = check_file(path)
        if not problems:
            continue
        bad_files += 1
        total += len(problems)
        print("❌ %s" % rel)
        for lineno, kind, what, how in problems:
            print("     %s:%d  [%s] %s" % (rel, lineno, kind, what))
            if how:
                print("              → %s" % how)
        print("")

    print("扫描 %d 个文件，%d 个有问题（共 %d 处）" % (len(targets), bad_files, total))
    if total:
        print("")
        print("板子是 Python 3.7.3，这些写法上板必炸。改完再提交。")
        return 1

    print("✅ 全部兼容 Python 3.7.3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
