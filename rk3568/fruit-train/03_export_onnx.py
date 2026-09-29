# -*- coding: utf-8 -*-
"""把训练好的 .pt 导出成 RKNN 转换用的 ONNX，并做输出层数校验。

    python 03_export_onnx.py --weights runs/fruit8/weights/best.pt

为什么必须从 .pt 重导，不能直接用别人发布的 ONNX：
    板端 `yolo11_infer.py` 的后处理按 **9 输出** 写死
    （`pair = len(outputs) // 3`，每尺度 box_dfl / class_scores / score_sum）。
    OAK / Luxonis 导出的 ONNX 只有 **3 输出**（output1..3_yolov6r2），
    喂进板端会直接 IndexError。
    标准 ultralytics 单输出 ONNX 交给 RKNN 自己切，才会得到 9 输出。

本脚本导出后会打开 ONNX 数一遍输出，数量不对就明确报错，不会静默产出坏模型。
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "artifacts")

# 板端 rknn_model_zoo 的 yolo11 示例会把单输出切成 9 个
EXPECTED_ONNX_OUTPUTS = 1


def inspect_onnx(path):
    try:
        import onnx
    except ImportError:
        return None, "未安装 onnx，跳过校验（建议装上：pip install onnx）"
    model = onnx.load(path)
    names = [o.name for o in model.graph.output]
    return names, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", required=True, help="训练产出的 best.pt")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=12)
    parser.add_argument("--no-simplify", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.weights):
        print("找不到权重：%s" % args.weights)
        return 1

    try:
        from ultralytics import YOLO
    except ImportError:
        print("缺 ultralytics：pip install -U ultralytics -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)

    model = YOLO(args.weights)
    print("从 %s 导出 ONNX（imgsz=%d, opset=%d）" % (args.weights, args.imgsz, args.opset))
    exported = model.export(
        format="onnx",
        imgsz=args.imgsz,
        opset=args.opset,
        simplify=not args.no_simplify,
        dynamic=False,
    )

    if not exported or not os.path.isfile(exported):
        print("导出失败，没拿到 ONNX 文件。")
        return 1

    target = os.path.join(OUT_DIR, os.path.basename(exported))
    if os.path.abspath(exported) != os.path.abspath(target):
        shutil.copyfile(exported, target)
    print("ONNX 输出：%s" % target)

    names, note = inspect_onnx(target)
    if note:
        print("校验提示：%s" % note)
    else:
        print("ONNX 输出层：%s（共 %d 个）" % (", ".join(names), len(names)))
        if len(names) == 3:
            print("")
            print("!! 这是 OAK / Luxonis 那种 3 输出格式，板端后处理会 IndexError。")
            print("!! 不要用这个文件。确认 --weights 是 ultralytics 直接训出来的 .pt。")
            return 3
        if len(names) != EXPECTED_ONNX_OUTPUTS:
            print("")
            print("!! 输出层数是 %d，预期 %d。转换前先确认板端 yolo11_infer.py 能对上。"
                  % (len(names), EXPECTED_ONNX_OUTPUTS))
            return 3
        print("输出层数正确：单输出，RKNN 会自行切成 9 输出，板端后处理无需改动。")

    print("")
    print("下一步：把 ONNX 交给转换机（RKNN-Toolkit2 只能在 Linux x86_64 上跑）")
    print("  bash 04_pack_rknn_bundle.sh --onnx \"%s\"" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
