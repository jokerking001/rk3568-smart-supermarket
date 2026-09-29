# -*- coding: utf-8 -*-
"""纯 Python 二维码生成 —— 只为 `/qr-svg?text=` 服务。

## 为什么自己写

板端是 Debian 10 + Python 3.7.3，装第三方包要联网、要编译，运维上是负担。
二维码算法是完全确定的（ISO/IEC 18004），自己实现一遍就没有外部依赖。
原工程的 `/qr-svg` 也是自己画的。

## 范围

只做**字节模式 + 纠错等级 M + 版本 1..10**。

  * 字节模式：URL、中文都能编（内部按 UTF-8 取字节）
  * 等级 M：约 15% 纠错，屏幕/纸张扫码的常用档
  * 版本 1..10：v10-M 能装 213 字节，覆盖 `/pay?order=..&t=..` 这类 URL 绰绰有余

超出范围会抛 `QrError` 并说明原因，**不会**悄悄截断或降级。

## 正确性怎么保证

`tools/test_qr_svg.py` 用两套独立手段验证：

  1. 与参考实现 `segno` 生成的矩阵逐模块比对（金标准哈希写死在测试里，
     所以测试本身不需要装 segno）
  2. 结构性自检：定位图形、时序图形、格式信息、掩码惩罚分

## 兼容性

Python 3.7.3：不用海象运算符、不用字典合并、不用 `list[int]`。
"""

# 纠错等级 M 的格式信息位（ISO 18004 表 12）
FORMAT_ECC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
FORMAT_MASK = 0x5412

# 每个版本的结构：(每块纠错码字数, [(块数, 每块数据码字数), ...])
# 只列等级 M。数值取自 ISO 18004 表 9，并用「总码字数」校验（见 _self_check_tables）。
VERSION_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}

# 每个版本的总码字数（数据 + 纠错）。用来校验上面的表没抄错。
TOTAL_CODEWORDS = {
    1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
    6: 172, 7: 196, 8: 242, 9: 292, 10: 346,
}

# 校正图形中心坐标（ISO 18004 附录 E）。版本 1 没有校正图形。
ALIGNMENT = {
    1: [],
    2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

MIN_VERSION = 1
MAX_VERSION = 10
ECC_LEVEL = "M"

# 填充字节（ISO 18004 8.4.9）
PAD_BYTES = (0xEC, 0x11)


class QrError(Exception):
    """编不出来。消息里一定说清是哪一步、为什么。"""


# ================================================================ GF(256)
def _build_gf():
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for index in range(255):
        exp[index] = value
        log[value] = index
        value <<= 1
        if value & 0x100:
            value ^= 0x11D          # 本原多项式 x^8+x^4+x^3+x^2+1
    for index in range(255, 512):
        exp[index] = exp[index - 255]
    return exp, log


GF_EXP, GF_LOG = _build_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def rs_generator(degree):
    """生成多项式 g(x) = (x-α^0)(x-α^1)...(x-α^(degree-1))。"""
    poly = [1]
    for index in range(degree):
        factor = GF_EXP[index]
        out = [0] * (len(poly) + 1)
        for position, coefficient in enumerate(poly):
            out[position] ^= coefficient
            out[position + 1] ^= _gf_mul(coefficient, factor)
        poly = out
    return poly


def rs_encode(data, ec_count):
    """对一块数据算 ec_count 个纠错码字。"""
    generator = rs_generator(ec_count)
    remainder = list(data) + [0] * ec_count
    for index in range(len(data)):
        factor = remainder[index]
        if factor == 0:
            continue
        for offset, coefficient in enumerate(generator):
            remainder[index + offset] ^= _gf_mul(coefficient, factor)
    return remainder[len(data):]


# ================================================================ 位流
class BitBuffer(object):
    def __init__(self):
        self.bits = []

    def put(self, value, length):
        for shift in range(length - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def __len__(self):
        return len(self.bits)

    def to_bytes(self):
        if len(self.bits) % 8:
            raise QrError("位流长度不是 8 的倍数（%d），不应该发生" % len(self.bits))
        out = []
        for start in range(0, len(self.bits), 8):
            value = 0
            for bit in self.bits[start:start + 8]:
                value = (value << 1) | bit
            out.append(value)
        return out


def data_codewords(version):
    ec_per_block, groups = VERSION_M[version]
    return sum(count * size for count, size in groups)


def char_count_bits(version):
    return 8 if version <= 9 else 16


def capacity_bytes(version):
    """这个版本在字节模式下最多能放多少字节。"""
    available = data_codewords(version) * 8 - 4 - char_count_bits(version)
    return available // 8


def pick_version(byte_length):
    for version in range(MIN_VERSION, MAX_VERSION + 1):
        if capacity_bytes(version) >= byte_length:
            return version
    raise QrError("内容太长：%d 字节，版本 %d（等级 M）最多 %d 字节"
                  % (byte_length, MAX_VERSION, capacity_bytes(MAX_VERSION)))


def build_codewords(payload, version):
    """字节模式编码 + 纠错 + 交织，返回最终码字序列。"""
    ec_per_block, groups = VERSION_M[version]
    total_data = data_codewords(version)

    buffer = BitBuffer()
    buffer.put(0b0100, 4)                                  # 字节模式
    buffer.put(len(payload), char_count_bits(version))
    for byte in payload:
        buffer.put(byte, 8)
    # 终止符最多 4 位，但不超过剩余容量
    remaining = total_data * 8 - len(buffer)
    if remaining < 0:
        raise QrError("内容超出该版本容量（差 %d 位）" % (-remaining))
    buffer.put(0, min(4, remaining))
    # 补齐到字节边界
    if len(buffer) % 8:
        buffer.put(0, 8 - len(buffer) % 8)

    codewords = buffer.to_bytes()
    if len(codewords) > total_data:
        raise QrError("编码后 %d 码字，超过版本 %d 的 %d"
                      % (len(codewords), version, total_data))
    index = 0
    while len(codewords) < total_data:
        codewords.append(PAD_BYTES[index % 2])
        index += 1

    # 切块 + 各自算纠错
    blocks = []
    ec_blocks = []
    cursor = 0
    for count, size in groups:
        for _ in range(count):
            chunk = codewords[cursor:cursor + size]
            cursor += size
            blocks.append(chunk)
            ec_blocks.append(rs_encode(chunk, ec_per_block))

    # 交织：先按列取数据码字，再按列取纠错码字
    interleaved = []
    max_data = max(len(block) for block in blocks)
    for position in range(max_data):
        for block in blocks:
            if position < len(block):
                interleaved.append(block[position])
    for position in range(ec_per_block):
        for block in ec_blocks:
            interleaved.append(block[position])
    if len(interleaved) != TOTAL_CODEWORDS[version]:
        raise QrError("交织后 %d 个码字，版本 %d 应该是 %d"
                      % (len(interleaved), version, TOTAL_CODEWORDS[version]))
    return interleaved


# ================================================================ 矩阵
def bch_remainder(value, generator, generator_bits):
    remainder = value << (generator_bits - 1)
    for shift in range(remainder.bit_length() - generator_bits, -1, -1):
        if remainder & (1 << (shift + generator_bits - 1)):
            remainder ^= generator << shift
    return remainder


def format_bits(mask, ecc_level=ECC_LEVEL):
    """15 位格式信息：5 位数据 + 10 位 BCH，最后异或 0x5412。"""
    data = (FORMAT_ECC_BITS[ecc_level] << 3) | mask
    return ((data << 10) | bch_remainder(data, 0x537, 11)) ^ FORMAT_MASK


def version_bits(version):
    """18 位版本信息（版本 >= 7）。"""
    return (version << 12) | bch_remainder(version, 0x1F25, 13)


class Matrix(object):
    def __init__(self, size):
        self.size = size
        self.modules = [[0] * size for _ in range(size)]
        self.reserved = [[False] * size for _ in range(size)]

    def set(self, row, col, value, reserve=True):
        self.modules[row][col] = 1 if value else 0
        if reserve:
            self.reserved[row][col] = True

    def is_free(self, row, col):
        return not self.reserved[row][col]


def place_finder(matrix, row, col):
    """7x7 定位图形 + 一圈分隔符。"""
    size = matrix.size
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            r, c = row + dr, col + dc
            if not (0 <= r < size and 0 <= c < size):
                continue
            if 0 <= dr <= 6 and 0 <= dc <= 6:
                edge = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                matrix.set(r, c, 1 if (edge or core) else 0)
            else:
                matrix.set(r, c, 0)


def place_alignment(matrix, version):
    centers = ALIGNMENT[version]
    if not centers:
        return
    size = matrix.size
    for row in centers:
        for col in centers:
            # 跳过和定位图形重叠的三个角
            if (row == 6 and col == 6) or \
               (row == 6 and col == size - 7) or \
               (row == size - 7 and col == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    edge = abs(dr) == 2 or abs(dc) == 2
                    core = dr == 0 and dc == 0
                    matrix.set(row + dr, col + dc, 1 if (edge or core) else 0)


def place_timing(matrix):
    size = matrix.size
    for index in range(8, size - 8):
        bit = 1 if index % 2 == 0 else 0
        if matrix.is_free(6, index):
            matrix.set(6, index, bit)
        if matrix.is_free(index, 6):
            matrix.set(index, 6, bit)


def reserve_format(matrix, version):
    size = matrix.size
    # 左上
    for index in range(9):
        if index != 6:
            matrix.reserved[8][index] = True
            matrix.reserved[index][8] = True
    # 右上 / 左下
    for index in range(8):
        matrix.reserved[8][size - 1 - index] = True
        matrix.reserved[size - 1 - index][8] = True
    matrix.set(size - 8, 8, 1)        # 固定的暗模块
    # 版本信息（版本 >= 7）
    if version >= 7:
        for row in range(6):
            for col in range(3):
                matrix.reserved[row][size - 11 + col] = True
                matrix.reserved[size - 11 + col][row] = True


def place_format(matrix, mask, version, ecc_level=ECC_LEVEL):
    """15 位格式信息，放两份（左上角 + 右上/左下），另外放 18 位版本信息。

    bit 0 是最低位。注意下面的落点坐标是 **(行, 列)**：
    副本 1 的主体在**第 8 列**（行 0..5、7、8），然后转到**第 8 行**（列 7、5..0）。
    这里最容易把行列写反 —— 写反了矩阵看着像二维码、就是扫不出来。
    """
    size = matrix.size
    bits = format_bits(mask, ecc_level)
    for index in range(15):
        bit = (bits >> index) & 1
        # 副本 1：先沿第 8 列自下而上到上边，再沿第 8 行向左
        if index < 6:
            matrix.set(index, 8, bit, reserve=False)
        elif index == 6:
            matrix.set(7, 8, bit, reserve=False)
        elif index == 7:
            matrix.set(8, 8, bit, reserve=False)
        elif index == 8:
            matrix.set(8, 7, bit, reserve=False)
        else:
            matrix.set(8, 14 - index, bit, reserve=False)
        # 副本 2：第 8 行右侧 8 位，第 8 列下方 7 位
        if index < 8:
            matrix.set(8, size - 1 - index, bit, reserve=False)
        else:
            matrix.set(size - 15 + index, 8, bit, reserve=False)
    if version >= 7:
        vbits = version_bits(version)
        for index in range(18):
            bit = (vbits >> index) & 1
            matrix.set(index // 3, size - 11 + index % 3, bit, reserve=False)
            matrix.set(size - 11 + index % 3, index // 3, bit, reserve=False)


def place_data(matrix, codewords):
    """之字形放置。从右下角起，两列一组，自下而上/自上而下交替。"""
    size = matrix.size
    bits = []
    for codeword in codewords:
        for shift in range(7, -1, -1):
            bits.append((codeword >> shift) & 1)

    index = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:                      # 跳过垂直时序图形
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for offset in (0, -1):
                target = col + offset
                if matrix.is_free(row, target):
                    if index < len(bits):
                        matrix.set(row, target, bits[index], reserve=False)
                        index += 1
                    else:
                        matrix.set(row, target, 0, reserve=False)
        col -= 2
        upward = not upward
    if index < len(bits):
        raise QrError("码字没放完：还剩 %d 位（矩阵 %dx%d 放不下）"
                      % (len(bits) - index, size, size))


MASK_FUNCS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def apply_mask(matrix, mask):
    """只掩数据区。返回新矩阵，不改原图。"""
    size = matrix.size
    out = [[0] * size for _ in range(size)]
    func = MASK_FUNCS[mask]
    for row in range(size):
        for col in range(size):
            value = matrix.modules[row][col]
            if not matrix.reserved[row][col] and func(row, col):
                value ^= 1
            out[row][col] = value
    return out


def penalty(modules):
    """四条惩罚规则，分数越低越好。"""
    size = len(modules)
    score = 0

    # 规则 1：同色连续 >= 5
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        run = 1
        for index in range(1, size):
            if line[index] == line[index - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # 规则 2：2x2 同色块
    for row in range(size - 1):
        for col in range(size - 1):
            value = modules[row][col]
            if modules[row][col + 1] == value and \
               modules[row + 1][col] == value and \
               modules[row + 1][col + 1] == value:
                score += 3

    # 规则 3：1:1:3:1:1 加四格空白
    pattern_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pattern_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        for start in range(size - 10):
            window = line[start:start + 11]
            if window == pattern_a or window == pattern_b:
                score += 40

    # 规则 4：黑白比例偏离 50%
    dark = sum(sum(row) for row in modules)
    total = size * size
    percent = dark * 100.0 / total
    score += int(abs(percent - 50) / 5) * 10
    return score


def build_matrix(payload, version, mask=None):
    """返回 (size, modules)。mask 为 None 时按惩罚分自动挑。"""
    size = version * 4 + 17
    base = Matrix(size)
    place_finder(base, 0, 0)
    place_finder(base, 0, size - 7)
    place_finder(base, size - 7, 0)
    place_alignment(base, version)
    place_timing(base)
    reserve_format(base, version)

    codewords = build_codewords(payload, version)
    place_data(base, codewords)

    if mask is not None:
        if not 0 <= mask <= 7:
            raise QrError("掩码只能是 0..7，收到 %r" % (mask,))
        place_format(base, mask, version)
        return size, apply_mask(base, mask)

    best = None
    for candidate in range(8):
        trial = Matrix(size)
        trial.modules = [list(row) for row in base.modules]
        trial.reserved = [list(row) for row in base.reserved]
        place_format(trial, candidate, version)
        modules = apply_mask(trial, candidate)
        score = penalty(modules)
        if best is None or score < best[0]:
            best = (score, candidate, modules)
    return size, best[2]


def encode(text, ecc_level=ECC_LEVEL):
    """文本 -> (version, size, modules)。"""
    if ecc_level != ECC_LEVEL:
        raise QrError("只实现了纠错等级 %s，收到 %r" % (ECC_LEVEL, ecc_level))
    if text is None:
        raise QrError("内容为空")
    payload = text.encode("utf-8")
    if not payload:
        raise QrError("内容为空")
    version = pick_version(len(payload))
    size, modules = build_matrix(payload, version)
    return version, size, modules


# ================================================================ 输出
def to_svg(text, scale=4, border=4, dark="#111111", light="#ffffff"):
    """生成 SVG 字符串。扫码用，所以用 crispEdges 保证像素不糊。"""
    if scale < 1:
        raise QrError("scale 至少是 1")
    if border < 0:
        raise QrError("border 不能是负数")
    _, size, modules = encode(text)
    span = size + border * 2
    pixels = span * scale

    parts = []
    for row in range(size):
        for col in range(size):
            if not modules[row][col]:
                continue
            x = (col + border) * scale
            y = (row + border) * scale
            parts.append("M%d %dh%dv%dh-%dz" % (x, y, scale, scale, scale))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" shape-rendering="crispEdges" '
        'role="img" aria-label="QR">'
        '<rect width="%d" height="%d" fill="%s"/>'
        '<path d="%s" fill="%s"/></svg>'
    ) % (pixels, pixels, pixels, pixels, pixels, pixels, light, "".join(parts), dark)


def matrix_hash(modules):
    """给测试用的稳定指纹：每行一个 0/1 字符串再取 sha256。"""
    import hashlib
    text = "\n".join("".join("1" if bit else "0" for bit in row) for row in modules)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _self_check_tables():
    """导入时就校验版本表内部自洽，抄错了立刻炸。"""
    for version, (ec_per_block, groups) in VERSION_M.items():
        total = sum(count * (size + ec_per_block) for count, size in groups)
        if total != TOTAL_CODEWORDS[version]:
            raise AssertionError(
                "版本 %d 的码字数对不上：表里 %d，按分块算 %d"
                % (version, TOTAL_CODEWORDS[version], total))


_self_check_tables()
