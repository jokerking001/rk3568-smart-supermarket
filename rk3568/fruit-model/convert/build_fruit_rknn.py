#!/usr/bin/env python3
"""Build a fruit-detection RKNN model for RK3568, end to end.

Run this on the RKNN-Toolkit2 conversion host (Linux x86_64), inside the
rknn-toolkit2 virtualenv.  It does everything needed between "I have a .pt" and
"I have a .rknn the board can load":

    1. (optional) re-export ONNX from the .pt with ultralytics
    2. verify the ONNX output layout matches what the board expects
    3. build INT8 and FP RKNN models, calibrating INT8 on real fruit photos
    4. write a report

Why step 1 exists
-----------------
The ONNX shipped alongside this model was exported for OAK/Luxonis and has only
three outputs named ``output1..3_yolov6r2`` (one fused tensor per detection scale).
The board's post-processing in ``yolo11_infer.py`` expects the *nine*-output RKNN
layout, i.e. per scale a ``(box_dfl, class_scores, score_sum)`` triple.  Feeding it
a 3-output model raises IndexError.  Re-exporting from the ``.pt`` with ultralytics
produces the single-output graph that RKNN then splits into the nine outputs the
board already handles, so no board-side code changes are needed.

Usage:
    python3 build_fruit_rknn.py --pt my_model.pt --calib-dir calib --out-dir out

    # if you already have a board-compatible ONNX and want to skip the re-export
    python3 build_fruit_rknn.py --onnx my_model.onnx --no-export --calib-dir calib
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")
EXPECTED_RKNN_OUTPUTS = 9


def log(message):
    print("[fruit-rknn] %s" % message, flush=True)


def export_onnx_from_pt(pt_path, out_dir, imgsz, opset, simplify=True):
    """Re-export a standard single-output YOLO ONNX from the .pt weights.

    ``simplify=True`` needs an ONNX simplifier (onnxslim / onnxsim) which is an
    extra dependency; if it is missing we retry without simplification rather
    than failing the whole build, because an unsimplified graph still converts.
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        log("ERROR: ultralytics is not installed in this environment.")
        log("       pip install ultralytics   (or pass --onnx with --no-export)")
        return None
    log("exporting ONNX from %s (imgsz=%d opset=%d simplify=%s)"
        % (pt_path, imgsz, opset, simplify))
    model = YOLO(pt_path)
    produced = None
    try:
        produced = model.export(format="onnx", imgsz=imgsz, opset=opset,
                                simplify=simplify, dynamic=False)
    except Exception as exc:  # noqa: BLE001 - we want the fallback for any cause
        if not simplify:
            log("ERROR: ONNX export failed: %s" % exc)
            return None
        log("WARNING: simplified export failed (%s)" % exc)
        log("         retrying without the ONNX simplifier")
        try:
            produced = model.export(format="onnx", imgsz=imgsz, opset=opset,
                                    simplify=False, dynamic=False)
        except Exception as exc2:  # noqa: BLE001
            log("ERROR: ONNX export failed even without simplify: %s" % exc2)
            return None
    produced = str(produced)
    if not os.path.exists(produced):
        log("ERROR: ultralytics reported %s but the file is missing" % produced)
        return None
    target = os.path.join(out_dir, os.path.basename(produced))
    if os.path.abspath(produced) != os.path.abspath(target):
        shutil.move(produced, target)
    log("exported -> %s (%d bytes)" % (target, os.path.getsize(target)))
    return target


def inspect_onnx(onnx_path):
    """Report the graph I/O and judge whether the layout is board-compatible."""
    try:
        import onnx
    except ImportError:
        log("WARNING: onnx not importable; cannot verify the layout")
        return None
    model = onnx.load(onnx_path)

    def sig(values):
        out = []
        for value in values:
            tensor = value.type.tensor_type
            dims = [d.dim_value if d.dim_value > 0 else (d.dim_param or "?")
                    for d in tensor.shape.dim]
            out.append({"name": value.name,
                        "dtype": onnx.TensorProto.DataType.Name(tensor.elem_type),
                        "shape": dims})
        return out

    info = {"inputs": sig(model.graph.input), "outputs": sig(model.graph.output),
            "opset": [(i.domain or "ai.onnx", i.version) for i in model.opset_import],
            "node_count": len(model.graph.node)}
    log("ONNX inputs : %s" % json.dumps(info["inputs"], ensure_ascii=False))
    log("ONNX outputs: %s" % json.dumps(info["outputs"], ensure_ascii=False))

    count = len(info["outputs"])
    names = [o["name"] for o in info["outputs"]]
    yolov6 = any("yolov6r2" in n for n in names)
    if yolov6 or count in (3, 4):
        info["board_compatible"] = False
        info["verdict"] = ("OAK/Luxonis yolov6r2 export (%d outputs). The board's "
                           "post-processing expects %d outputs -> re-export needed."
                           % (count, EXPECTED_RKNN_OUTPUTS))
    elif count == 1:
        info["board_compatible"] = True
        info["verdict"] = ("standard single-output YOLO graph; RKNN will split it "
                           "into %d outputs" % EXPECTED_RKNN_OUTPUTS)
    else:
        info["board_compatible"] = None
        info["verdict"] = "unexpected output count %d; verify after conversion" % count
    log("verdict: %s" % info["verdict"])
    return info


def build_calibration_list(calib_dir, target_txt, limit):
    """Write the INT8 calibration image list.

    Handoff section 10.4 requires representative real images here; for a fruit model
    that means fruit photographs, not COCO.
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
    log("config_model platform=%s mean=%s std=%s" % (platform, mean, std))
    if rknn.config(mean_values=[mean], std_values=[std], target_platform=platform) != 0:
        log("ERROR: rknn.config failed")
        return False
    log("loading ONNX")
    if rknn.load_onnx(model=onnx_path) != 0:
        log("ERROR: load_onnx failed")
        rknn.release()
        return False
    log("building (do_quantization=%s)" % quantize)
    if rknn.build(do_quantization=quantize, dataset=calib_txt if quantize else None) != 0:
        log("ERROR: build failed")
        rknn.release()
        return False
    log("exporting %s" % out_path)
    ret = rknn.export_rknn(out_path)
    rknn.release()
    if ret != 0:
        log("ERROR: export_rknn failed")
        return False
    return True


def verify_rknn_outputs(rknn_path, image_path):
    """Load the produced model and confirm it really has nine outputs."""
    try:
        from rknnlite.api import RKNNLite
    except ImportError:
        log("note: rknnlite unavailable; skipping runtime output check")
        return None
    import numpy as np
    try:
        import cv2
    except ImportError:
        log("note: cv2 unavailable; skipping runtime output check")
        return None
    if not image_path or not os.path.exists(image_path):
        log("note: no probe image; skipping runtime output check")
        return None
    image = cv2.imread(image_path)
    if image is None:
        return None
    image = cv2.resize(image, (640, 640))
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    runtime = RKNNLite()
    if runtime.load_rknn(rknn_path) != 0 or runtime.init_runtime() != 0:
        log("note: could not init runtime for %s" % rknn_path)
        runtime.release()
        return None
    outputs = runtime.inference(inputs=[np.expand_dims(image, 0)], data_format=["nhwc"])
    runtime.release()
    shapes = [tuple(o.shape) for o in outputs]
    log("runtime outputs: %d -> %s" % (len(outputs), shapes))
    return {"count": len(outputs), "shapes": [list(s) for s in shapes],
            "matches_board_expectation": len(outputs) == EXPECTED_RKNN_OUTPUTS}


def main():
    parser = argparse.ArgumentParser(description="Build fruit RKNN for RK3568")
    parser.add_argument("--pt", help="YOLO .pt weights (triggers re-export)")
    parser.add_argument("--onnx", help="existing ONNX to convert")
    parser.add_argument("--no-export", action="store_true",
                        help="use --onnx as-is, skip the ultralytics re-export")
    parser.add_argument("--calib-dir", default="calib")
    parser.add_argument("--calib-limit", type=int, default=80)
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--platform", default="rk3568")
    parser.add_argument("--dtypes", default="i8,fp")
    parser.add_argument("--name", default="fruits_yolo11s")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=12)
    parser.add_argument("--no-simplify", action="store_true",
                        help="skip the ONNX simplifier during re-export")
    parser.add_argument("--mean", default="0,0,0")
    parser.add_argument("--std", default="255,255,255")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    summary = {"platform": args.platform, "dtypes": args.dtypes, "results": {}}

    onnx_path = args.onnx
    if args.pt and not args.no_export:
        onnx_path = export_onnx_from_pt(args.pt, args.out_dir, args.imgsz, args.opset,
                                        simplify=not args.no_simplify)
        if not onnx_path:
            return 1
    if not onnx_path or not os.path.exists(onnx_path):
        log("ERROR: no usable ONNX (pass --pt or --onnx)")
        return 1
    summary["onnx"] = os.path.abspath(onnx_path)

    graph = inspect_onnx(onnx_path)
    if graph:
        summary["graph"] = graph
        if graph.get("board_compatible") is False:
            log("ERROR: this ONNX is not board-compatible and --no-export was given.")
            log("       Drop --no-export and pass --pt so it gets re-exported.")
            with open(os.path.join(args.out_dir, "report.json"), "w") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
            return 2

    mean = [float(v) for v in args.mean.split(",")]
    std = [float(v) for v in args.std.split(",")]

    calib_txt = None
    probe_image = None
    if "i8" in args.dtypes:
        calib_txt, count = build_calibration_list(
            args.calib_dir, os.path.join(args.out_dir, "calib_list.txt"), args.calib_limit)
        if not calib_txt:
            log("ERROR: no calibration images in %s" % args.calib_dir)
            log("       INT8 quantization requires representative real images (handoff 10.4).")
            return 1
        probe_image = open(calib_txt).readline().strip()
        log("calibration images: %d" % count)
        summary["calibration_images"] = count

    exit_code = 0
    for dtype in [d.strip() for d in args.dtypes.split(",") if d.strip()]:
        out_path = os.path.join(args.out_dir, "%s_%s.rknn" % (args.name, dtype))
        ok = convert(onnx_path, out_path, args.platform, calib_txt,
                     quantize=(dtype == "i8"), mean=mean, std=std)
        size = os.path.getsize(out_path) if ok and os.path.exists(out_path) else 0
        entry = {"ok": ok, "path": out_path, "bytes": size}
        if ok:
            entry["runtime_check"] = verify_rknn_outputs(out_path, probe_image)
        summary["results"][dtype] = entry
        log("%s -> %s (%d bytes)" % (dtype, "OK" if ok else "FAILED", size))
        if not ok:
            exit_code = 1

    report_path = os.path.join(args.out_dir, "report.json")
    with open(report_path, "w") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    log("report: %s" % report_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
