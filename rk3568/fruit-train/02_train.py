# -*- coding: utf-8 -*-
"""训练水果检测模型（YOLO11）。

用法：

    python 02_train.py                          # 默认 yolo11n，100 epochs
    python 02_train.py --model yolo11s.pt       # 换更大模型
    python 02_train.py --weights 本地.pt        # 用本地权重，免下载
    python 02_train.py --epochs 150 --batch 8   # 显存不够时减小 batch

产出：runs/fruit8/<时间戳>/weights/best.pt

显存/内存提示（RTX 4060 Laptop 8 GB + 16 GB 内存）：
    yolo11n + imgsz 640 + batch 16 约用 5 GB 显存，安全。
    **内存才是瓶颈**，而且崩的时候报错完全指不出原因 —— 实测 16 GB 机器在
    640/batch16/workers4 下跑到第 11 个 epoch 报：

        numpy._core._exceptions._ArrayMemoryError: Unable to allocate 1.17 MiB

    连 1 MiB 都分不出来。所以脚本开头会先打印可用内存，不足 3 GB 会明确告警。
    实测稳的配置：`--workers 2 --batch 8`（再紧张就 `--workers 1`）。
    训练前关掉游戏客户端、浏览器、聊天软件。
"""

import argparse
import os
import sys

# ！！必须在 import ultralytics 之前设置 ！！
#
# polars（ultralytics 用它读 results.csv）在 import 时会做一次 CPU 特性自检：
# 它拿二进制编译时声明的特性列表去比对自检表，而自检表里**没有 `sse3` 这个名字**
# （只有 sse4.1/sse4.2/avx/avx2/bmi1/bmi2/...），于是直接抛
#     RuntimeError: unknown feature flag: 'sse3'
# 这是 polars 自己的 build/check 不一致，AMD（实测 Ryzen 7 7840H）上必现。
#
# 为什么这个坑特别阴：ultralytics 只在**保存权重**时才 import polars
# （trainer.read_results_csv），所以前面训练一切正常，跑到最后一个 epoch
# 才崩 —— 白烧几小时。polars 官方留了开关，这里提前关掉。
# 关掉是安全的：该自检对「不认识的 flag」才抛错，对「认识的但 CPU 不支持」只告警；
# Zen4 支持列表里的全部特性。
os.environ.setdefault("POLARS_SKIP_CPU_CHECK", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_YAML = os.path.join(HERE, "dataset", "fruit8", "data.yaml")


def mem_snapshot():
    """返回 (可用物理内存 GB, 可用提交量 GB 或 None)；取不到就 (None, None)。

    为什么要看"提交量"：Windows 上提交上限 = 物理内存 + 页面文件。
    内存不够时 numpy 抛的是 `_ArrayMemoryError: Unable to allocate 1.17 MiB`
    —— 连 1 MiB 都分不出来，看着完全不像"内存不够"，很容易误判成
    opencv/numpy 版本问题（`SystemError: <built-in function merge> returned
    a result with an exception set` 就是被它连累的假象）。
    """
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return (st.ullAvailPhys / 1024.0 ** 3,
                    st.ullAvailPageFile / 1024.0 ** 3)
        info = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                info[key.strip()] = int(value.split()[0])  # kB
        return info.get("MemAvailable", 0) / 1024.0 ** 2, None
    except Exception:
        return None, None


def warn_low_memory():
    avail, commit = mem_snapshot()
    if avail is None:
        return
    tail = "" if commit is None else "，可用提交量 %.1f GB" % commit
    print("可用物理内存 %.1f GB%s" % (avail, tail))
    if avail >= 3.0:
        return
    print("")
    print("!! 可用内存不足 3 GB —— 实测会在训练中途崩，而且报错完全指不出原因：")
    print("!!   numpy._core._exceptions._ArrayMemoryError: Unable to allocate 1.17 MiB")
    print("!! 连 1 MiB 都分不出来，看着像 opencv/numpy 版本问题，其实是内存见底。")
    print("!! 16 GB 机器在 640/batch16/workers4 下实测第 11 个 epoch 崩过。")
    print("!! 建议：先关掉浏览器 / 游戏客户端 / 聊天软件，再")
    print("!!   --workers 1（或 2） --batch 8")
    print("!! 还是紧张就 --imgsz 512 先跑通。")
    print("")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="yolo11n.pt",
                        help="ultralytics 模型名，会自动下载")
    parser.add_argument("--weights", default=None,
                        help="本地 .pt 权重路径（给了就忽略 --model）")
    parser.add_argument("--data", default=DATA_YAML)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--device", default="0", help="'0' 用 GPU，'cpu' 用 CPU")
    parser.add_argument("--name", default="fruit8")
    args = parser.parse_args()

    if not os.path.isfile(args.data):
        print("找不到数据集配置：%s" % args.data)
        print("先跑：python 01_prepare_dataset.py --download --subset")
        return 1

    warn_low_memory()

    try:
        from ultralytics import YOLO
    except ImportError:
        print("缺 ultralytics。先装：")
        print("  pip install -U ultralytics -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    source = args.weights if args.weights else args.model
    print("起点权重：%s" % source)
    if not os.path.isfile(source):
        print("（不是本地文件，ultralytics 会去下载；国内慢的话先开代理）")

    model = YOLO(source)
    model.train(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        project=os.path.join(HERE, "runs"),
        name=args.name,
        exist_ok=True,
        pretrained=True,
        plots=True,
        val=True,
        patience=30,
    )

    best = os.path.join(HERE, "runs", args.name, "weights", "best.pt")
    print("")
    if os.path.isfile(best):
        print("训练完成，最佳权重：%s" % best)
        print("下一步：python 03_export_onnx.py --weights \"%s\"" % best)
    else:
        print("没找到 best.pt，检查训练日志。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
