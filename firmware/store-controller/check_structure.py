#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从机固件的结构自检 —— **没有 Arduino 编译环境时的替代验证**。

这不是编译器，只能挡住「结构性」的错误：

  1. 花括号深度逐行追踪（能报出第几行开始不对劲，不只是总数）
  2. `#if / #endif` 配平（`SLAVE_MODE` 双角色靠条件编译，配错就编译不过）
  3. `#include` 的本工程头文件是否真实存在
  4. `SlaveLink_*` 声明与定义一一对应
  5. 从机 setup/loop 调用的外部函数是否都在头文件里声明过

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

FILES = ["Slave_Config.h", "Slave_Link.h", "Slave_Link.cpp",
         "Work7_20.ino", "WIFI_Test.cpp", "WIFI_Test.h",
         "secrets.h.example"]

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
    print("结论：%s"
          % ("结构自检通过（仍未经真实编译）" if bad == 0
             else "有 %d 处问题，见上面 !!" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
