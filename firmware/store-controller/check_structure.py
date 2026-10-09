#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从机固件的结构自检 —— **没有 Arduino 编译环境时的替代验证**。

这不是编译器，只能挡住「结构性」的错误：

  1. 花括号深度逐行追踪（能报出第几行开始不对劲，不只是总数）
  2. `#if / #endif` 配平（`SLAVE_MODE` 双角色靠条件编译，配错就编译不过）
  3. `#include` 的本工程头文件是否真实存在
  4. `SlaveLink_*` 声明与定义一一对应
  5. 从机 setup/loop 调用的外部函数是否都在头文件里声明过
  6. ⚠️ `SLAVE_MODE` 必须定义在 **.h** 里、**不能**在 .ino 里
  7. ⚠️ 主控专属符号不能在「SLAVE_MODE=1 生效区间」被引用

第 6、7 条是 **2026-10-09 第一次真实编译时踩出来的坑**，必须固化成断言：

  * 第 6 条 —— Arduino 会**单独编译 sketch 目录下的每一个 .cpp**，
    `.ino` 里的 `#define` 对它们**不可见**。所以 `SLAVE_MODE` 一旦写在
    `.ino` 里，「裁剪主控模块」就会**静默失效**：从机固件里照样塞着
    `WebServer.cpp` 的 81 个 HTTP 端点，编译还不报错。必须放在头文件里。
  * 第 7 条 —— 光有第 6 条还不够。符号即使能被看到，只要某个 `.cpp`
    在从机区间里引用了主控符号（例如 `HC_SR04.h` 的 `DETECT_DISTANCE`、
    `WebServer.cpp` 的 `global_voice_*`），链接/编译照样炸。
    这里用迷你预处理模拟 `SLAVE_MODE=1`，把这类引用提前揪出来。

**真正的编译必须在 Arduino IDE / arduino-cli 里做。** 这个脚本只是让
「改完不知道对不对」有个最底线的检查 —— 之前它确实抓到过问题。

用法：
    python firmware/store-controller/check_structure.py
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 参与自检的源文件：目录下所有 .ino / .cpp / .h（排除编译产物）
SRC_EXT = (".ino", ".cpp", ".h")
FILES = sorted(f for f in os.listdir(HERE)
               if f.lower().endswith(SRC_EXT)) if os.path.isdir(HERE) else []

# 主控形态专属模块（`#if !SLAVE_MODE` 包住的那 6 个 .cpp）
MASTER_CPP = ["WebServer.cpp", "AI_Test.cpp", "Product_Data.cpp",
              "HC_SR04.cpp", "Mode_LowPower.cpp", "Inventory_Monitor.cpp"]

# 主控专属符号（定义在上面 6 个模块里；从机区间不得引用）
MASTER_SYMBOLS = [
    "WebServer_Init", "WebServer_SyncToPi", "WebServer_SubmitScanGunCode",
    "WebServer_CreateHumanServiceRequest",
    "global_voice_q", "global_voice_a", "global_voice_show",
    "AI_Init", "AI_Ask", "AI_Analyze", "AI_ProcessQuestion", "AI_HandleLoop",
    "Product_Data_Init", "Product_Save", "Product_Data_ToText",
    "DailyStats_Init", "DailyStats_Reset", "DailyStats_AddSale",
    "DailyStats_ToJson", "Product_FindByQR", "Product_DeductStock",
    "Product_AddSold", "Product_CreateOrder", "Product_ConfirmOrder",
    "Product_SetPending", "Product_IsPending", "Product_IsPaid",
    "Product_RefundOrder", "Product_OrderHistoryToJson",
    "HC_SR04_Init", "HC_SR04_GetDistance", "HC_SR04_IsPersonNear",
    "HC_SR04_HandleLoop", "DETECT_DISTANCE",
    "LowPower_Init", "LowPower_SetMode", "LowPower_GetMode",
    "LowPower_PrintStatus", "MODE_NORMAL", "MODE_MODEM_SLEEP",
    "InventoryMonitor_Init", "InventoryMonitor_HandleLoop",
]

THIRD_PARTY = {
    "Arduino.h", "WiFi.h", "HTTPClient.h", "ArduinoJson.h", "DHT.h",
    "Adafruit_NeoPixel.h", "WiFiClientSecure.h", "ElegantOTA.h",
    "WebServer.h", "FS.h", "SPI.h", "Wire.h", "SD.h", "ESPmDNS.h",
    "Update.h", "LittleFS.h",
}

# 本仓库只提交 .example，实际部署时由使用者填成 secrets.h
OPTIONAL_INCLUDE = {"secrets.h"}


def strip_code(src):
    """去掉注释和字符串字面量，只留代码骨架。"""
    s = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r'"(\\.|[^"\\])*"', '""', s)
    s = re.sub(r"'(\\.|[^'\\])*'", "''", s)
    return s


def depth_trace(path):
    """逐行追踪 {} 深度。返回 (最终深度, 首次跌破 0 的行号)。"""
    depth = 0
    first_neg = None
    for i, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
        for ch in strip_code(line):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0 and first_neg is None:
                    first_neg = i
    return depth, first_neg


def pp_balance(path):
    """#if/#ifdef/#ifndef 与 #endif 的配平。"""
    opens = closes = 0
    for line in open(path, encoding="utf-8"):
        t = line.strip()
        if re.match(r"#\s*(if|ifdef|ifndef)\b", t):
            opens += 1
        elif re.match(r"#\s*endif\b", t):
            closes += 1
    return opens, closes


def _strip_comments_keep_lines(text):
    """去掉注释，但保留行数（用于定位行号）。"""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text,
                  flags=re.S)
    out = []
    for line in text.split("\n"):
        i = line.find("//")
        out.append(line[:i] if i >= 0 else line)
    return out


def _eval_cond(expr):
    """True/False/None；None = 与 SLAVE_MODE 无关（未知）。"""
    e = expr.strip().replace(" ", "")
    if e in ("SLAVE_MODE", "SLAVE_MODE==1", "SLAVE_MODE>0"):
        return True
    if e in ("!SLAVE_MODE", "SLAVE_MODE==0", "SLAVE_MODE<1"):
        return False
    return None


def scan_slave_active_refs(path):
    """迷你预处理：找出 SLAVE_MODE=1 时引用了主控符号、但**自身没有从机侧定义**的行。

    判定思路（避免误报）：
      * `.h` 里的纯声明永远无害（从机模式下那些头文件根本不会被 include），跳过。
      * 符号只要在从机生效区间内**有定义**（如 `global_voice_q` 在
        Voice_Interaction.cpp 的 `#if SLAVE_MODE` 分支里补了定义），就算 OK。
      * 只有「在从机区间被引用、且整个从机区间都找不到定义」的符号才是真问题 ——
        那正是 `DETECT_DISTANCE` 那次失败的特征。

    返回 [(行号, 符号, 原文)]。
    """
    if path.lower().endswith(".h"):
        return []                     # 头文件只放声明，单独看没有意义
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    lines = _strip_comments_keep_lines(src)
    raw = src.split("\n")
    stack = []
    slave_lines = []                  # [(行号, 文本)] 从机生效区间

    def active():
        return all(v is not False for v, _ in stack)

    for idx, line in enumerate(lines, 1):
        m = re.match(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$", line)
        if m:
            kind, rest = m.group(1), m.group(2)
            if kind in ("if", "ifdef", "ifndef"):
                v = _eval_cond(rest)
                if kind == "ifndef" and v is not None:
                    v = not v
                stack.append((v, False))
            elif kind == "elif" and stack:
                v, _ = stack[-1]
                if v is None:
                    stack[-1] = (_eval_cond(rest), True)
            elif kind == "else" and stack:
                v, _ = stack[-1]
                stack[-1] = ((not v, True) if v is not None else (True, True))
            elif kind == "endif" and stack:
                stack.pop()
            continue
        if active():
            # 匹配用**去注释后**的文本，展示也用同一份（否则注释里的符号名会被误报）
            slave_lines.append((idx, lines[idx - 1] if idx - 1 < len(lines) else line))

    # 第一遍：从机区间内哪些主控符号「有定义」
    defined = set()
    for _, txt in slave_lines:
        t = txt.strip()
        if not t or t.startswith("#"):
            continue
        for sym in MASTER_SYMBOLS:
            if sym not in txt:
                continue
            # 定义特征：不是 extern 声明，且后面跟 `=`（变量）或 `(`（函数）
            if re.search(r"^\s*extern\b", t):
                continue
            if re.search(r"\b%s\b\s*(=|\()" % re.escape(sym), t):
                defined.add(sym)

    # 第二遍：从机区间内引用、但没有从机侧定义的
    hits = []
    for ln, txt in slave_lines:
        t = txt.strip()
        if not t or t.startswith("#"):
            continue
        for sym in MASTER_SYMBOLS:
            if sym in t and sym not in defined:
                hits.append((ln, sym, t[:100]))
    return hits


def main():
    os.chdir(HERE)
    have = set(os.path.basename(p) for p in glob.glob("*"))
    bad = 0

    print("=== 1. 花括号深度追踪（逐行） ===")
    for f in FILES:
        d, neg = depth_trace(f)
        ok = (d == 0 and neg is None)
        if not ok:
            bad += 1
        note = "OK"
        if d != 0:
            note = "!! 收尾深度 %d（少了 %d 个 }）" % (d, d)
        elif neg is not None:
            note = "!! 第 %d 行深度跌破 0" % neg
        print("  %-20s 收尾深度=%3d  %s" % (f, d, note))

    print()
    print("=== 2. 条件编译配平（#if / #endif） ===")
    for f in FILES:
        o, c = pp_balance(f)
        ok = (o == c)
        if not ok:
            bad += 1
        print("  %-20s #if=%-3d #endif=%-3d  %s"
              % (f, o, c, "OK" if ok else "!! 不配平"))

    print()
    print("=== 3. include 的本工程头文件 ===")
    missing = 0
    for f in FILES:
        for m in re.finditer(r'#\s*include\s+["<]([^">]+)[">]',
                             open(f, encoding="utf-8").read()):
            inc = m.group(1)
            if "/" in inc or inc in THIRD_PARTY or inc in have:
                continue
            if inc in OPTIONAL_INCLUDE:
                print("  -- %s -> %s  可选（本仓库只有 .example）" % (f, inc))
                continue
            print("  !! %s -> %s  未找到" % (f, inc))
            missing += 1
    if missing == 0:
        print("  没有真正缺失的头文件")

    print()
    print("=== 4. SlaveLink_* 声明与定义对应 ===")
    hsrc = open("Slave_Link.h", encoding="utf-8").read()
    csrc = open("Slave_Link.cpp", encoding="utf-8").read()
    decl = set(re.findall(r"\b(SlaveLink_\w+)\s*\(", hsrc))
    defn = set(re.findall(r"^\s*(?:static\s+)?\w[\w\s\*]*?\b(SlaveLink_\w+)\s*\(",
                          csrc, flags=re.M))
    print("  .h 声明 :", sorted(decl))
    print("  .cpp 定义:", sorted(defn))
    only_h = decl - defn
    only_c = defn - decl
    if only_h:
        print("  !! 只有声明没有定义:", sorted(only_h))
    if only_c:
        print("  !! 只有定义没有声明:", sorted(only_c))
    if not only_h and not only_c:
        print("  一一对应")
    bad += len(only_h) + len(only_c)

    print()
    print("=== 5. 从机主循环调用的外部函数是否都在头文件里声明过 ===")
    ino = open("Work7_20.ino", encoding="utf-8").read()
    # 文件里有 **两个** `#if SLAVE_MODE` 块：include 区 和 setup/loop 区。
    # 只看含 setup/loop 的那个（用 findall 全取，最后一块就是）。
    blocks = re.findall(r"^#if SLAVE_MODE\s*\n(.*?)^#else", ino,
                        flags=re.M | re.S)
    if not blocks:
        print("  !! 没找到 `#if SLAVE_MODE` ... `#else` 块，检查未生效")
        bad += 1
        slave_part = ""
    else:
        slave_part = blocks[-1]
        print("  从机 setup/loop 块共 %d 行" % slave_part.count("\n"))
    calls = set(re.findall(
        r"\b((?:Scale|Voice|RFID|USB_BarcodeScanner|Plus|DHT22|led|key|wifi|SlaveLink)_\w+)\s*\(",
        slave_part))
    headers = " ".join(open(h, encoding="utf-8").read()
                       for h in glob.glob("*.h"))
    undecl = []
    for c in sorted(calls):
        ok = re.search(r"\b%s\b" % re.escape(c), headers)
        if not ok:
            undecl.append(c)
        print("  %-28s %s" % (c, "OK" if ok else "!! 未声明"))
    bad += len(undecl)

    print()
    print("=== 6. SLAVE_MODE 的定义位置（必须在 .h，不能在 .ino） ===")
    ino_defs = []
    hdr_defs = []
    for f in FILES:
        src = open(f, encoding="utf-8").read()
        if re.search(r"^\s*#\s*define\s+SLAVE_MODE\b", src, flags=re.M):
            if f.lower().endswith(".ino"):
                ino_defs.append(f)
            elif f.lower().endswith(".h"):
                hdr_defs.append(f)
            else:
                ino_defs.append(f)   # .cpp 里定义同样不行（可见性只对后续 TU 生效）
    if ino_defs:
        print("  !! SLAVE_MODE 被定义在 %s —— 其它 .cpp 看不到它，"
              "裁剪会静默失效" % ", ".join(ino_defs))
        bad += 1
    else:
        print("  OK：没有 .ino/.cpp 定义 SLAVE_MODE")
    if hdr_defs:
        print("  OK：头文件里的定义 -> %s" % ", ".join(hdr_defs))
    else:
        print("  !! 没有任何头文件定义 SLAVE_MODE")
        bad += 1
    # .ino 必须 include 那个头文件，否则自己也拿不到开关
    ino_src = open("Work7_20.ino", encoding="utf-8").read()
    if re.search(r'#\s*include\s+"Slave_Config\.h"', ino_src):
        print("  OK：Work7_20.ino 已 include \"Slave_Config.h\"")
    else:
        print("  !! Work7_20.ino 没有 include \"Slave_Config.h\"")
        bad += 1
    # 6 个主控 .cpp 必须逐个加整文件保护
    missing_guard = []
    for f in MASTER_CPP:
        if not os.path.isfile(f):
            continue
        src = open(f, encoding="utf-8").read()
        if not re.search(r"^\s*#\s*if\s+!\s*SLAVE_MODE\b", src, flags=re.M):
            missing_guard.append(f)
    if missing_guard:
        print("  !! 缺 `#if !SLAVE_MODE` 整文件保护：%s"
              % ", ".join(missing_guard))
        bad += len(missing_guard)
    else:
        print("  OK：6 个主控 .cpp 都有整文件保护")

    print()
    print("=== 7. 主控专属符号在 SLAVE_MODE=1 生效区间是否被引用 ===")
    hits = []
    for f in FILES:
        for ln, sym, txt in scan_slave_active_refs(f):
            hits.append((f, ln, sym, txt))
    if hits:
        for f, ln, sym, txt in hits:
            print("  !! %s L%d [%s] %s" % (f, ln, sym, txt))
        bad += len(hits)
    else:
        print("  OK：没有主控专属符号泄漏进从机固件")

    print()
    print("结论：%s"
          % ("结构自检通过（仍未经真实编译）" if bad == 0
             else "有 %d 处问题，见上面 !!" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
