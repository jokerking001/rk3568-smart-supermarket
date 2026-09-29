# -*- coding: utf-8 -*-
"""从原工程 WebServer.cpp 里把内嵌的 HTML 页面逐字抽出来。

原工程把页面写死在 C++ 源文件里（`R"rawliteral(...)"` 之类）。迁移到 RK3568 时
如果重写 UI，就是白白丢掉一份已经调好的界面；所以这里**逐字搬运**，不改一个字符。
搬完由 `store_ext_routes.py` 从磁盘读出来发。

## 为什么单独写个脚本而不是手抄

  1. 页面合计约 2400 行，手抄必错
  2. 原工程以后要是改了页面，重跑一次就能同步
  3. 抽取结果带 sha256，能证明「搬过来的和原来的一模一样」

## 两种需要拼回去的情况

  1. **一个页面被拆成多段原始字符串**，中间夹着 C++ 表达式做服务端插值：

         String html = R"payraw( <div>¥)payraw" + String(total, 2) + R"payraw(</div> )payraw";

     这里拼成 `¥{{total}}`，由路由层填值。不认识的表达式会写成 `{{?原样}}`
     并在末尾报警 —— 宁可显眼地报错，也不要静默发出一段坏 HTML。

  2. **普通 C 字符串字面量**（`/manifest.webmanifest`、`/icon.svg`、`/sw.js`、
     `/customer/qr`）。这些也一并抽出来，同样不留手抄。

## 归属判定

每个块往前找最近的 `server.on("<path>", HTTP_<METHOD>`。
`/admin` 有两个块（未登录显示登录页、已登录显示管理页），按出现顺序
分别命名为 `admin-login` 和 `admin`。

用法：
    python tools/extract_legacy_web.py            # 抽到默认目录
    python tools/extract_legacy_web.py --check    # 只校验已抽出的与源文件是否一致
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(REPO, "firmware", "store-controller", "WebServer.cpp")
DEFAULT_OUT = os.path.join(REPO, "rk3568", "store-backend", "web")

BLOCK_START = re.compile(r'R"([A-Za-z_]*)\(', re.M)
ROUTE = re.compile(r'server\.on\(\s*"([^"]+)"\s*,\s*HTTP_([A-Z]+)')
# 块结束后、下一个块开始前，若形如 ` + expr + ` 则说明两块之间插了一个表达式
JOIN = re.compile(r'\A\s*\+\s*(?P<expr>.+?)\s*\+\s*\Z', re.S)

# 一个路由对应多个块时，按出现顺序取这里登记的名字
NAMES = {
    "/": ["index"],
    "/admin": ["admin-login", "admin"],
    "/admin/qr-confirm": ["admin-qr-confirm"],
    "/pay": ["pay"],
    "/bigscreen": ["bigscreen"],
}

# C++ 表达式 -> 模板占位符。路由层负责填这些值。
PLACEHOLDERS = {
    "String(total, 2)": "total",
    "String(total)": "total",
    "String(orderId + 1)": "order_no",
    "String(orderId)": "order_id",
}

# 单行 C 字符串字面量也要抽出来的路由。
# (路由, 文件名, 提取正则, 拼接符)。拼接符为 None 表示只有一段字面量。
LITERALS = [
    ("/manifest.webmanifest", "manifest.webmanifest",
     re.compile(r'request->send\(\s*200\s*,\s*"application/manifest\+json"\s*,'
                r'\s*"((?:[^"\\]|\\.)*)"\s*\)'), None),
    ("/icon.svg", "icon.svg",
     re.compile(r'static const char icon\[\]\s*=\s*"((?:[^"\\]|\\.)*)"'), None),
    ("/sw.js", "sw.js",
     re.compile(r'request->send\(\s*200\s*,\s*"application/javascript"\s*,'
                r'\s*"((?:[^"\\]|\\.)*)"\s*\)'), None),
    # 这条被 `url` 插了两次，所以先把整条语句抓下来，再按字面量切开拼回去
    ("/customer/qr", "customer-qr.html",
     re.compile(r'String html = ("(?:[^"\\]|\\.)*"'
                r'(?:\s*\+\s*url\s*\+\s*"(?:[^"\\]|\\.)*")+)\s*;'), "{{url}}"),
]

LITERAL_PART = re.compile(r'"((?:[^"\\]|\\.)*)"')

# 扩展名 -> Content-Type。路由层按这个发，别让浏览器猜。
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".js": "application/javascript",
}


def content_type(filename):
    return CONTENT_TYPES.get(os.path.splitext(filename)[1], "application/octet-stream")


C_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "'": "'", "\\": "\\"}


def unescape_c(text):
    """把 C 字符串字面量里的转义还原成真实字符。"""
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append(C_ESCAPES.get(nxt, "\\" + nxt))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def owner_route(text, offset):
    """往前找最近的 server.on(...)，返回 (path, method, 行号)。"""
    best = None
    for match in ROUTE.finditer(text, 0, offset):
        best = match
    if not best:
        return None, None, None
    return best.group(1), best.group(2), line_of(text, best.start())


def strip_indent(body):
    """去掉共同缩进（原工程为了嵌在 C++ 里整体缩进了几格）。

    只动行首空白，不动内容 —— 少动一个字节就少一份走样风险。
    """
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    if not indents:
        return "\n".join(lines)
    cut = min(indents)
    if cut == 0:
        return "\n".join(lines)
    return "\n".join(line[cut:] if line.strip() else "" for line in lines)


def raw_blocks(text):
    """产出 (start, end, delimiter, body)，body 是原始字符串内的内容。"""
    pos = 0
    while True:
        match = BLOCK_START.search(text, pos)
        if not match:
            return
        delim = match.group(1)
        opener_end = match.end()
        closer = ')%s"' % delim
        end = text.find(closer, opener_end)
        if end < 0:
            raise ValueError("块没有闭合：delimiter=%r 起始偏移 %d" % (delim, match.start()))
        yield match.start(), end + len(closer), delim, text[opener_end:end]
        pos = end + len(closer)


def merge_fragments(text, raws):
    """把被 `+ expr +` 拆开的相邻块拼回一个页面。

    返回 [(start, end, delimiter, html, unknowns), ...]，unknowns 是不认识的表达式。
    """
    merged = []
    index = 0
    while index < len(raws):
        start, end, delim, body = raws[index]
        html = body
        unknowns = []
        while index + 1 < len(raws):
            nstart, nend, ndelim, nbody = raws[index + 1]
            if ndelim != delim:
                break
            gap = JOIN.match(text[end:nstart])
            if not gap:
                break
            expr = " ".join(gap.group("expr").split())
            name = PLACEHOLDERS.get(expr)
            if name is None:
                unknowns.append(expr)
                html += "{{?%s}}" % expr
            else:
                html += "{{%s}}" % name
            html += nbody
            end = nend
            index += 1
        merged.append((start, end, delim, html, unknowns))
        index += 1
    return merged


def collect(src_path):
    with io.open(src_path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()

    found = []
    used = {}
    unknowns = []

    for start, end, delim, html, unknown in merge_fragments(text, list(raw_blocks(text))):
        path, method, route_line = owner_route(text, start)
        if path is None:
            print("  ! 跳过：找不到归属路由（第 %d 行，定界符 %r）"
                  % (line_of(text, start), delim), file=sys.stderr)
            continue
        names = NAMES.get(path)
        if not names:
            print("  ! 跳过：路由 %s 没有登记文件名" % path, file=sys.stderr)
            continue
        position = used.get(path, 0)
        if position >= len(names):
            print("  ! 跳过：路由 %s 的第 %d 个块没有对应文件名"
                  % (path, position + 1), file=sys.stderr)
            continue
        used[path] = position + 1
        for expr in unknown:
            unknowns.append("%s -> %s" % (names[position], expr))

        page = strip_indent(html)
        found.append({
            "path": path,
            "method": method,
            "route_line": route_line,
            "name": names[position],
            "filename": names[position] + ".html",
            "kind": "template" if "{{" in page else "static",
            "start_line": line_of(text, start),
            "end_line": line_of(text, end),
            "html": page,
        })

    for path, name, pattern, joiner in LITERALS:
        match = pattern.search(text)
        if not match:
            print("  ! 找不到字面量路由 %s —— 源文件结构变了？" % path, file=sys.stderr)
            continue
        if joiner is None:
            page = unescape_c(match.group(1))
        else:
            parts = [unescape_c(part) for part in LITERAL_PART.findall(match.group(1))]
            page = joiner.join(parts)
        found.append({
            "path": path,
            "method": "GET",
            "route_line": line_of(text, match.start()),
            "name": name,
            "filename": name,
            "kind": "template" if "{{" in page else "static",
            "start_line": line_of(text, match.start()),
            "end_line": line_of(text, match.end()),
            "html": page,
        })

    for item in found:
        item["content_type"] = content_type(item["filename"])
        item["sha256"] = hashlib.sha256(item["html"].encode("utf-8")).hexdigest()
        item["bytes"] = len(item["html"].encode("utf-8"))
        item["lines"] = item["html"].count("\n") + 1

    return found, unknowns


def main():
    parser = argparse.ArgumentParser(description="抽取原工程内嵌 HTML 页面")
    parser.add_argument("--src", default=DEFAULT_SRC)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true",
                        help="只校验已抽出的文件与源文件一致，不写盘")
    args = parser.parse_args()

    if not os.path.isfile(args.src):
        print("找不到源文件：%s" % args.src, file=sys.stderr)
        return 2

    found, unknowns = collect(args.src)
    if not found:
        print("一个页面都没抽到 —— 源文件结构变了？", file=sys.stderr)
        return 2

    print("%-22s %-6s %-9s %-7s %-9s %s"
          % ("路由", "方法", "类型", "行数", "字节", "输出"))
    print("-" * 92)
    manifest = []
    drifted = []
    for item in found:
        target = os.path.join(args.out, item["filename"])
        if args.check:
            existing = None
            if os.path.isfile(target):
                with io.open(target, "r", encoding="utf-8", newline="") as handle:
                    existing = handle.read()
            if existing == item["html"]:
                mark = "一致"
            else:
                mark = "**不一致**"
                drifted.append(item["filename"])
        else:
            if not os.path.isdir(args.out):
                os.makedirs(args.out)
            # newline="" 且内容里只有 \n：板端是 Linux，也避开 core.autocrlf 的 CRLF
            with io.open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(item["html"])
            mark = "已写出"
        print("%-22s %-6s %-9s %-7d %-9d %s (%s)"
              % (item["path"], item["method"], item["kind"], item["lines"],
                 item["bytes"], item["filename"], mark))
        manifest.append({
            "path": item["path"],
            "method": item["method"],
            "name": item["name"],
            "file": item["filename"],
            "content_type": item["content_type"],
            "kind": item["kind"],
            "source_lines": [item["start_line"], item["end_line"]],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
            "lines": item["lines"],
        })

    print("-" * 92)
    if unknowns:
        print("⚠️  有 %d 个表达式没登记占位符，已写成 {{?原样}}：" % len(unknowns))
        for entry in unknowns:
            print("     %s" % entry)
        print("    要接的话，在 PLACEHOLDERS 里登记，并在 store_ext_routes.py 里填值。")

    if args.check:
        if drifted:
            print("有 %d 个页面和源文件不一致：%s" % (len(drifted), ", ".join(drifted)))
            return 1
        print("全部 %d 个页面与源文件一致" % len(manifest))
        return 0

    manifest_path = os.path.join(args.out, "manifest.json")
    with io.open(manifest_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps({
            "source": os.path.relpath(args.src, REPO).replace("\\", "/"),
            "pages": manifest,
        }, ensure_ascii=False, indent=2) + "\n")
    total_lines = sum(m["lines"] for m in manifest)
    total_bytes = sum(m["bytes"] for m in manifest)
    print("%d 个页面，共 %d 行 / %d 字节 -> %s"
          % (len(manifest), total_lines, total_bytes,
             os.path.relpath(args.out, REPO).replace("\\", "/")))
    print("清单：%s" % os.path.relpath(manifest_path, REPO).replace("\\", "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
