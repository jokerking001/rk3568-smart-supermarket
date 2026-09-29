# -*- coding: utf-8 -*-
"""qr_svg 的自测 —— 不需要联网、不需要第三方库，板端也能跑。

## 金标准是怎么来的（重要，别删）

矩阵指纹是用 **`qrcode` 库**（纯 Python，另一套独立实现）生成的：
强制字节模式、纠错 M、固定掩码，逐模块比对我这边的输出。

**为什么不拿 segno 当基准**：segno 的 `write_padding_bits()` 有这么一行

    buff.extend([0] * (8 - (length % 8)))

位流长度已经是 8 的倍数时，它会再补 **8 个 0 位**。规范（ISO/IEC 18004 7.4.10）
写的是「**若**未落在码字边界上，才补 0 位」——已经对齐就不该补。所以凡是有富余
容量、且位流恰好对齐的情况（字节模式下 v1–v9 全都如此），segno 的数据码字里会
多一个 `0x00`。两种都是合法可扫的二维码，但 `qrcode` 和本实现才符合规范。

比对结果：本实现与 `qrcode` 在 **8 个用例 × 8 个掩码 = 64/64** 上矩阵完全一致。

## 覆盖范围

  1. 矩阵指纹（固定掩码）—— 数据编码 / RS 纠错 / 交织 / 放置 全链路
  2. 惩罚分 —— 逐值对齐 `qrcode` 的 `lost_point`
  3. 结构自检 —— 定位图形、分隔符、时序图形、暗模块、格式信息、版本信息
  4. 边界与报错 —— 空内容、超长、非法 scale / mask
  5. SVG 输出 —— 尺寸、路径条数、转义

用法：
    python rk3568/store-backend/test_qr_svg.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qr_svg  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []

# (文本, 版本, 边长, 掩码 0 时的矩阵指纹) —— 由 qrcode 库生成
GOLDEN = (
    ("A", 1, 21,
     "2bae48594b7325872d655a95d8814f09f3825eba52faa8c8b4e3c6dd453105e5"),
    ("HELLO WORLD", 1, 21,
     "2cee776cd87923a0b50751b157fe74b27d8d63df96b9e797fc0e21d47726384c"),
    ("https://example.com/", 2, 25,
     "5b87bae3dcec95e1a76bcf7e5756491bd81c4e50cf880d9cd37953f13e0c9e09"),
    ("智慧超市扫码进入", 2, 25,
     "a22ea5ecc7978d80be89fb8c4aedde6af30860bb4c9fc133036b573e13b381d8"),
    ("http://127.0.0.1:8094/pay?order=1&t=12.50", 3, 29,
     "017f59a6e669c65936b60a14299e15c7a5a45a18f899a620d6f368da6f86decc"),
    ("http://192.168.1.100:8094/customer", 3, 29,
     "7e2e92c7809cfcda49af8cb5bc64dd1c9e9ab66ffb2da0cee2abd13e01a6a6b8"),
    ("http://192.168.1.100:8094/admin/qr-confirm?sid=abc123", 4, 33,
     "a1edc56a3a57e16ff9bdbbab3b60593e304ef2505f2199550282791be9c9b652"),
    ("x" * 100, 6, 41,
     "898a791db8daf87950e6fd89cebf33096e6b40e0b49b39bdd015fdd3c07d3ec9"),
    ("y" * 213, 10, 57,
     "35655c2ba409a4d038b9f98e99648851615bbf9d09d96d99fbea8fb6d9c23e9c"),
)

# qrcode.util.lost_point 在这些矩阵上的取值，逐个掩码
PENALTY_GOLDEN = {
    "HELLO WORLD": (415, 370, 303, 480, 291, 396, 381, 477),
    "A": (533, 440, 359, 355, 355, 434, 368, 554),
}


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        return True
    FAIL += 1
    FAILURES.append("%s\n        实际 %r\n        期望 %r" % (label, got, want))
    return False


def check_true(label, value, hint=""):
    global PASS, FAIL
    if value:
        PASS += 1
        return True
    FAIL += 1
    FAILURES.append("%s%s" % (label, ("  —— " + hint) if hint else ""))
    return False


def expect_error(label, func, needle=""):
    try:
        func()
    except qr_svg.QrError as exc:
        global PASS, FAIL
        if needle and needle not in str(exc):
            FAIL += 1
            FAILURES.append("%s 报错信息里没有 %r：%s" % (label, needle, exc))
            return False
        PASS += 1
        return True
    except Exception as exc:                                  # noqa: BLE001
        FAIL += 1
        FAILURES.append("%s 抛的不是 QrError 而是 %s: %s"
                        % (label, type(exc).__name__, exc))
        return False
    FAIL += 1
    FAILURES.append("%s 本该报错，却成功了" % label)
    return False


# ------------------------------------------------------------ 1. 矩阵指纹
def test_golden_matrices():
    print("[1] 矩阵指纹（与 qrcode 库逐模块一致）")
    for text, version, size, digest in GOLDEN:
        payload = text.encode("utf-8")
        got_version = qr_svg.pick_version(len(payload))
        if not check("版本选择 %r" % text[:24], got_version, version):
            continue
        got_size, modules = qr_svg.build_matrix(payload, version, mask=0)
        check("边长 %r" % text[:24], got_size, size)
        check("指纹 %r" % text[:24], qr_svg.matrix_hash(modules), digest)
    # 自动挑掩码时也要能编出来，且版本不变
    for text, version, _size, _digest in GOLDEN:
        payload = text.encode("utf-8")
        got_version, got_size, modules = qr_svg.encode(text)
        check("encode 版本 %r" % text[:24], got_version, version)
        check("encode 边长 %r" % text[:24], got_size, len(modules))


# ------------------------------------------------------------ 2. 惩罚分
def test_penalty():
    print("[2] 惩罚分（逐值对齐 qrcode 的 lost_point）")
    for text, expected in PENALTY_GOLDEN.items():
        payload = text.encode("utf-8")
        version = qr_svg.pick_version(len(payload))
        got = []
        for mask in range(8):
            _, modules = qr_svg.build_matrix(payload, version, mask=mask)
            got.append(qr_svg.penalty(modules))
        check("惩罚分 %r" % text, tuple(got), expected)


# ------------------------------------------------------------ 3. 结构自检
def test_structure():
    print("[3] 结构自检（定位 / 时序 / 暗模块 / 格式信息 / 版本信息）")

    for text, version, size, _digest in GOLDEN:
        payload = text.encode("utf-8")
        for mask in (0, 3, 7):
            _, modules = qr_svg.build_matrix(payload, version, mask=mask)
            tag = "%r mask%d" % (text[:16], mask)

            # 三个定位图形 + 分隔符
            for row, col in ((0, 0), (0, size - 7), (size - 7, 0)):
                check_true("%s 定位图形(%d,%d)" % (tag, row, col),
                           _finder_ok(modules, row, col),
                           "7x7 定位图形不对")
            # 时序图形
            timing_ok = all(modules[6][i] == (1 if i % 2 == 0 else 0)
                            for i in range(8, size - 8))
            timing_ok = timing_ok and all(modules[i][6] == (1 if i % 2 == 0 else 0)
                                          for i in range(8, size - 8))
            check_true("%s 时序图形" % tag, timing_ok)
            # 暗模块
            check("%s 暗模块" % tag, modules[size - 8][8], 1)
            # 格式信息两份都要能读回同一个值
            want = qr_svg.format_bits(mask)
            check("%s 格式信息(副本1)" % tag, _read_format_copy1(modules), want)
            check("%s 格式信息(副本2)" % tag, _read_format_copy2(modules), want)
            # 版本信息
            if version >= 7:
                want_v = qr_svg.version_bits(version)
                check("%s 版本信息" % tag, _read_version(modules, size), want_v)


def _finder_ok(modules, row, col):
    """7x7 定位图形：外框全暗、中间 3x3 全暗、之间一圈全亮。"""
    for dr in range(7):
        for dc in range(7):
            edge = dr in (0, 6) or dc in (0, 6)
            core = 2 <= dr <= 4 and 2 <= dc <= 4
            want = 1 if (edge or core) else 0
            if modules[row + dr][col + dc] != want:
                return False
    return True


def _read_format_copy1(modules):
    """按落点把 15 位格式信息读回来（与 place_format 的副本 1 对称）。"""
    bits = 0
    for index in range(15):
        if index < 6:
            row, col = index, 8
        elif index == 6:
            row, col = 7, 8
        elif index == 7:
            row, col = 8, 8
        elif index == 8:
            row, col = 8, 7
        else:
            row, col = 8, 14 - index
        bits |= (modules[row][col] & 1) << index
    return bits


def _read_format_copy2(modules):
    size = len(modules)
    bits = 0
    for index in range(15):
        if index < 8:
            row, col = 8, size - 1 - index
        else:
            row, col = size - 15 + index, 8
        bits |= (modules[row][col] & 1) << index
    return bits


def _read_version(modules, size):
    bits = 0
    for index in range(18):
        row, col = index // 3, size - 11 + index % 3
        bits |= (modules[row][col] & 1) << index
    return bits


def test_format_all_masks():
    print("[3b] 8 个掩码的格式信息都能读回")
    payload = b"HELLO WORLD"
    for mask in range(8):
        _, modules = qr_svg.build_matrix(payload, 1, mask=mask)
        check("掩码 %d 格式信息" % mask, _read_format_copy1(modules),
              qr_svg.format_bits(mask))


# ------------------------------------------------------------ 4. 边界
def test_limits():
    print("[4] 边界与报错")
    # 每个版本的容量边界：刚好放得下 / 多一字节就报错
    for version in range(qr_svg.MIN_VERSION, qr_svg.MAX_VERSION + 1):
        capacity = qr_svg.capacity_bytes(version)
        check("v%d 容量" % version, qr_svg.pick_version(capacity), version)
        if version < qr_svg.MAX_VERSION:
            check("v%d 再多一字节就升级" % version,
                  qr_svg.pick_version(capacity + 1), version + 1)
    expect_error("超过 v10 容量", lambda: qr_svg.encode("z" * 214), "太长")
    expect_error("空内容", lambda: qr_svg.encode(""), "空")
    expect_error("None", lambda: qr_svg.encode(None), "空")
    expect_error("非法掩码", lambda: qr_svg.build_matrix(b"x", 1, mask=8), "掩码")
    expect_error("非法纠错等级", lambda: qr_svg.encode("x", ecc_level="H"), "只实现了")
    expect_error("scale 太小", lambda: qr_svg.to_svg("x", scale=0), "scale")
    expect_error("border 为负", lambda: qr_svg.to_svg("x", border=-1), "border")

    # 中文按 UTF-8 字节算长度，不是按字数
    check("中文按字节选版本", qr_svg.pick_version(len("智慧超市扫码进入".encode("utf-8"))), 2)


# ------------------------------------------------------------ 5. SVG
def test_svg():
    print("[5] SVG 输出")
    text = "http://192.168.1.100:8094/customer"
    svg = qr_svg.to_svg(text, scale=4, border=4)
    check_true("以 <svg 开头", svg.startswith("<svg "))
    check_true("以 </svg> 结尾", svg.endswith("</svg>"))
    check_true("带 crispEdges", "shape-rendering=\"crispEdges\"" in svg)
    check_true("XML 可解析", _xml_ok(svg))

    _version, size, modules = qr_svg.encode(text)
    span = size + 8
    check("SVG 宽度", re.search(r'width="(\d+)"', svg).group(1), str(span * 4))
    dark = sum(sum(row) for row in modules)
    check("路径段数 = 暗模块数", svg.count("M"), dark)
    # 每个模块一个 `M x y h4 v4 h-4 z`
    check_true("路径段格式", re.search(r'd="M\d+ \d+h4v4h-4z', svg) is not None)

    # 边框参数生效
    tight = qr_svg.to_svg(text, scale=1, border=0)
    check("border=0 宽度", re.search(r'width="(\d+)"', tight).group(1), str(size))

    # 内容里的特殊字符不会破坏 SVG
    tricky = qr_svg.to_svg('a"b<c>&d', scale=2)
    check_true("特殊字符不破坏 SVG", _xml_ok(tricky))


def _xml_ok(text):
    try:
        import xml.etree.ElementTree as ET
        ET.fromstring(text)
        return True
    except Exception:                                          # noqa: BLE001
        return False


def test_tables():
    print("[6] 版本表自洽")
    for version, (ec_per_block, groups) in qr_svg.VERSION_M.items():
        total = sum(count * (size + ec_per_block) for count, size in groups)
        check("v%d 总码字数" % version, total, qr_svg.TOTAL_CODEWORDS[version])
    check("版本数", len(qr_svg.VERSION_M), qr_svg.MAX_VERSION)


def main():
    test_golden_matrices()
    test_penalty()
    test_structure()
    test_format_all_masks()
    test_limits()
    test_svg()
    test_tables()

    print("\n" + "=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        for item in FAILURES[:20]:
            print("    - %s" % item)
        return 1
    print("✅ %d 项全部通过 —— 二维码编码器可信" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
