#!/usr/bin/env python3
"""Convert the fruit YOLO11s ONNX model to RKNN for RK3568.

Runs on the Linux x86_64 conversion machine (the RKNN-Toolkit2 host), NOT on the
board: rknn-toolkit2 has no aarch64-friendly footprint here (it hard-depends on
torch) and the board only has ~1.4 GB of disk left.

Usage (inside the rknn-toolkit2 venv):
    python3 convert_fruit_rknn.py \
        --onnx fruits_yolo11s.onnx \
        --calib-dir calib \
        --out-dir out \
        --platform rk3568 \
        --dtypes i8,fp

Outputs:
    out/fruits_yolo11s_i8.rknn
    out/fruits_yolo11s_fp.rknn
    out/report.json      (structure + conversion summary)

Why the structure report matters
--------------------------------
The board's existing post-processing (yolo11_infer.py) expects the *standard*
RKNN YOLO11 layout: nine outputs, i.e. per detection scale a
(box_dfl, class_scores, score_sum) triple, with class scores still un-argmaxed.
The ONNX in this bundle was originally exported for OAK/Luxonis, so the script
checks the graph outputs first and says clearly whether the layout matches. If it
does not, re-export from the .pt with ultralytics and convert that instead.
"""

import argparse
import glob
import json
import os
import sys

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")


def report_onnx(onnx_path):
    """Describe the ONNX graph.  Returns None when onnx is unavailable."""
    try:
        import onnx
    except ImportError:
        print("[warn] onnx not importable here; skipping graph report")
        return None
    try:
        model = onnx.load(onnx_path)
    except Exception as exc:
        print("[warn] could not load ONNX for reporting: %s" % exc)
        return None

    def signature(values):
        out = []
        for value in values:
            tensor = value.type.tensor_type
            dims = []
            for dim in tensor.shape.dim:
                dims.append(dim.dim_value if dim.dim_value > 0 else (dim.dim_param or "?"))
            out.append({"name": value.name,
                        "dtype": onnx.TensorProto.DataType.Name(tensor.elem_type),
                        "shape": dims})
        return out

    report = {
        "ir_version": model.ir_version,
        "opset": [(imp.domain or "ai.onnx", imp.version) for imp in model.opset_import],
        "producer": "%s %s" % (model.producer_name, model.producer_version),
        "inputs": signature(model.graph.input),
        "outputs": signature(model.graph.output),
        "node_count": len(model.graph.node),
    }
    print("--> ONNX graph")
    print("    inputs : %s" % json.dumps(report["inputs"], ensure_ascii=False))
    print("    outputs: %s" % json.dumps(report["outputs"], ensure_ascii=False))

    count = len(report["outputs"])
    # The RKNN YOLO11 post-process on the board needs a multiple-of-three output set
    # whose entries alternate (box_dfl, class_scores, score_sum) per scale.
    if count in (6, 9):
        report["layout_ok"] = True
        print("    layout : OK (%d outputs -> %d scales)" % (count, count // 3))
    elif count == 1:
        report["layout_ok"] = False
        print("    layout : SINGLE OUTPUT (already decoded / NMS fused).")
        print("             The board's yolo11_infer.py post-processing expects 9 raw")
        print("             outputs. Re-export from the .pt with:")
        print("               yolo export model=my_model.pt format=onnx imgsz=640 opset=12")
    else:
        report["layout_ok"] = False
        print("    layout : UNEXPECTED (%d outputs)" % count)
    return report


def build_calibration_list(calib_dir, target_txt, limit):
    """Write the image list RKNN uses for INT8 calibration.

    Handoff section 10.4 is explicit that calibration must use representative real
    images.  For a fruit model that means fruit photographs, so this defaults to the
    fruit dataset rather than COCO.
    """
    images = []
    for suffix in IMAGE_SUFFIXES:
        images.extend(glob.glob(os.path.join(calib_dir, "**", "*" + suffix), recursive=True))
    images = sorted(set(images))
    if not images:
        return None, 0
    if limit and len(images) > limit:
        step = len(images) / float(limit)
        images = [images[int(i * step)] for i in range(limit)]
    with open(target_txt, "w") as handle:
        for path in images:
            handle.write(os.path.abspath(path) + "\n")
    return target_txt, len(images)


def convert(onnx_path, out_path, platform, calib_txt, quantize, mean, std):
    from rknn.api import RKNN

    rknn = RKNN(verbose=True)
    print("--> config_model (mean=%s std=%s platform=%s)" % (mean, std, platform))
    ret = rknn.config(mean_values=[mean], std_values=[std], target_platform=platform)
    if ret != 0:
        print("[error] rknn.config failed: %s" % ret)
        return False

    print("--> loading ONNX")
    ret = rknn.load_onnx(model=onnx_path)
    if ret != 0:
        print("[error] load_onnx failed: %s" % ret)
        rknn.release()
        return False

    print("--> building (do_quantization=%s)" % quantize)
    ret = rknn.build(do_quantization=quantize, dataset=calib_txt if quantize else None)
    if ret != 0:
        print("[error] build failed: %s" % ret)
        rknn.release()
        return False

    print("--> exporting %s" % out_path)
    ret = rknn.export_rknn(out_path)
    rknn.release()
    if ret != 0:
        print("[error] export_rknn failed: %s" % ret)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Fruit YOLO11 ONNX -> RKNN")
    parser.add_argument("--onnx", required=True)
    parser.add_argument("--calib-dir", default="calib",
                        help="directory of representative fruit images")
    parser.add_argument("--calib-limit", type=int, default=100)
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--platform", default="rk3568")
    parser.add_argument("--dtypes", default="i8,fp")
    parser.add_argument("--name", default="fruits_yolo11s")
    parser.add_argument("--mean", default="0,0,0")
    parser.add_argument("--std", default="255,255,255")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    summary = {"onnx": os.path.abspath(args.onnx), "platform": args.platform,
               "dtypes": args.dtypes, "results": {}}

    graph = report_onnx(args.onnx)
    if graph:
        summary["graph"] = graph

    mean = [float(v) for v in args.mean.split(",")]
    std = [float(v) for v in args.std.split(",")]

    calib_txt = None
    if "i8" in args.dtypes:
        calib_txt, count = build_calibration_list(
            args.calib_dir, os.path.join(args.out_dir, "calib_list.txt"), args.calib_limit)
        if not calib_txt:
            print("[error] no calibration images found in %s" % args.calib_dir)
            print("        INT8 quantization needs representative images (handoff 10.4).")
            return 1
        print("--> calibration images: %d (from %s)" % (count, args.calib_dir))
        summary["calibration_images"] = count

    exit_code = 0
    for dtype in [d.strip() for d in args.dtypes.split(",") if d.strip()]:
        out_path = os.path.join(args.out_dir, "%s_%s.rknn" % (args.name, dtype))
        ok = convert(args.onnx, out_path, args.platform, calib_txt,
                     quantize=(dtype == "i8"), mean=mean, std=std)
        size = os.path.getsize(out_path) if ok and os.path.exists(out_path) else 0
        summary["results"][dtype] = {"ok": ok, "path": out_path, "bytes": size}
        print("--> %s: %s (%d bytes)" % (dtype, "OK" if ok else "FAILED", size))
        if not ok:
            exit_code = 1

    with open(os.path.join(args.out_dir, "report.json"), "w") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print("--> report: %s" % os.path.join(args.out_dir, "report.json"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
