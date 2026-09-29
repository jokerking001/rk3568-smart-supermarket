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
    内存才是瓶颈 —— 训练前关掉游戏客户端、ChatGPT、Edge。
    报 OOM 就把 --batch 降到 8，再不行降到 4。
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_YAML = os.path.join(HERE, "dataset", "fruit8", "data.yaml")


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
