# -*- coding: utf-8 -*-
"""INT8 校准工具 —— 采集真实相机图、打自包含校准包、对比 FP / INT8 精度。

交接文档 v2 §10.3 把它列为待办，但一直没有脚本。上板后：

    # 1) 从板端摄像头抓 200 张真实图做校准集（必须在真机位、真光照下抓）
    python tools/calibrate_int8.py collect --board 192.168.43.44 --count 200

    # 2) 打一个自包含包，拷到转换机
    python tools/calibrate_int8.py pack --onnx rk3568/fruit-train/runs/fruit8/weights/best.onnx \
        --calib calib --out dist/sku_calib_bundle

    # 3) 在转换机上跑（RKNN-Toolkit2 2.3.2）
    bash vm_calibrate_int8.sh

    # 4) 把转换机产出的 result.json 拿回来对比
    python tools/calibrate_int8.py compare --result result.json

## 一个容易搞混的点：校准集和验证集的要求不一样

* **校准集**要的是「覆盖真实推理时的输入分布」。所以**必须在真机位、真光照下抓**，
  而且**允许相邻帧近似重复** —— 校准只需要激活值的范围，重复帧不影响这个目的。
* **验证集**要的是「无泄漏地估计泛化」。所以**必须按采集会话切分，
  绝不能随机拆相邻视频帧** —— 相邻帧几乎一样，随机拆会让 val 精度虚高。
  这件事由 `rk3568/fruit-train/01_prepare_dataset.py` 负责，不在本脚本里。

把两者混为一谈是这个项目上过的一个坑，所以单独写清楚。

设计要点：
  * 板端 `/raw.jpg` 拿的是**未画框的原始帧**，正是校准该用的东西。
    不要用 `/fruit.jpg` 或带检测框的图 —— 框是推理产物，会把分布带偏。
  * 抓图**禁用代理**，否则 192.168.x.x 会被 http_proxy 绕出去。
  * 抓图按固定间隔（默认 1.0s），不要连拍 —— 连拍拿到的几十张图几乎同一场景，
    覆盖不了真实光照/摆位变化。
  * Python 3.7 兼容（转换机上的 RKNN-Toolkit2 2.3.2 只支持到 3.8）。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_BOARD = "10.181.229.215"
DEFAULT_CALIB_DIR = os.path.join(REPO, "calib")
RAW_PATH = "/raw.jpg"          # 8088 的原始帧，不带检测框

# 转换机上跑的量化脚本模板。用 %(name)s 占位，避免和 shell 的 $ 冲突。
VM_SCRIPT = r'''#!/usr/bin/env bash
# 由 tools/calibrate_int8.py pack 生成 —— 在装了 RKNN-Toolkit2 的转换机上跑。
# 全程离线，不需要网络。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

ONNX="%(onnx)s"
CALIB="calib.txt"
OUT="%(out_rknn)s"
VAL="%(val_dir)s"
TARGET="%(target)s"
MEAN="%(mean)s"
STD="%(std)s"

echo "== 1/3 量化 (do_quantization=True) =="
python3 - "$ONNX" "$CALIB" "$OUT" "$TARGET" "$MEAN" "$STD" <<'PY'
import json, sys
from rknn.api import RKNN

onnx, calib, out, target, mean, std = sys.argv[1:7]
rknn = RKNN(verbose=False)
rknn.config(
    mean_values=[[float(x) for x in mean.split(",")]],
    std_values=[[float(x) for x in std.split(",")]],
    target_platform=target,
    quantized_dtype="asymmetric_quantized-8",
)
print("  load_onnx:", onnx)
if rknn.load_onnx(model=onnx) != 0:
    sys.exit("load_onnx 失败")
print("  build(do_quantization=True)")
if rknn.build(do_quantization=True, dataset=calib) != 0:
    sys.exit("build 失败 —— 多半是校准图数量不足或路径不对")
if rknn.export_rknn(out) != 0:
    sys.exit("export_rknn 失败")
rknn.release()
print("  已导出", out)
PY

echo "== 2/3 精度对比 =="
RESULT="result.json"
if [ -n "$VAL" ] && [ -d "$VAL" ]; then
  python3 - "$ONNX" "$OUT" "$VAL" "$TARGET" "$MEAN" "$STD" "$RESULT" <<'PY'
import json, os, sys
from rknn.api import RKNN

onnx, rknn_path, val_dir, target, mean, std, result_path = sys.argv[1:8]
images = sorted(f for f in os.listdir(val_dir)
                if f.lower().endswith((".jpg", ".jpeg", ".png")))
print("  验证集图片数:", len(images))
out = {"val_images": len(images), "fp": None, "int8": None}

# 这里只做「同一批图，FP 与 INT8 输出差异」的量化对比。
# 真正要报 mAP 的话，需要标注，交给 rk3568/fruit-train 的评估脚本。
def run(quantize, label):
    rknn = RKNN(verbose=False)
    rknn.config(mean_values=[[float(x) for x in mean.split(",")]],
                std_values=[[float(x) for x in std.split(",")]],
                target_platform=target)
    rknn.load_onnx(model=onnx)
    rknn.build(do_quantization=quantize, dataset="calib.txt")
    rknn.init_runtime()
    diffs = []
    for name in images[:50]:
        import numpy as np
        try:
            from PIL import Image
        except ImportError:
            print("  缺 PIL，跳过逐图对比")
            break
        img = np.array(Image.open(os.path.join(val_dir, name)).convert("RGB"))
        outputs = rknn.inference(inputs=[img])
        flat = np.concatenate([o.ravel() for o in outputs])
        diffs.append((name, float(np.abs(flat).mean())))
    rknn.release()
    return diffs

fp = run(False, "fp")
i8 = run(True, "int8")
out["fp"] = [{"image": n, "mean_abs": v} for n, v in fp]
out["int8"] = [{"image": n, "mean_abs": v} for n, v in i8]
with open(result_path, "w") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
print("  已写出", result_path)
PY
else
  echo "  没给验证集目录，跳过精度对比（只产出了 rknn）"
fi

echo "== 3/3 完成 =="
ls -la "$OUT"
echo
echo "把 result.json 拷回本机，跑："
echo "  python tools/calibrate_int8.py compare --result result.json"
'''


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def fetch_raw_frame(board, port, timeout=10):
    """取一帧原始图。返回 (bytes, error)。"""
    url = "http://%s:%d%s" % (board, port, RAW_PATH)
    req = urllib.request.Request(url, headers={"User-Agent": "calibrate/1.0"})
    try:
        with opener().open(req, timeout=timeout) as resp:
            data = resp.read()
        if not data:
            return None, "空响应"
        if not data[:2] == b"\xff\xd8":
            return None, "不是 JPEG（前两字节 %r）—— 8088 的 /raw.jpg 没起来？" % data[:2]
        return data, ""
    except urllib.error.HTTPError as exc:
        return None, "HTTP %s" % exc.code
    except Exception as exc:
        return None, str(exc)


def cmd_collect(args):
    out_dir = args.calib
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    print("从 %s:%d%s 抓 %d 张原始帧，间隔 %.1fs"
          % (args.board, args.port, RAW_PATH, args.count, args.interval))
    print("（必须在真机位、真光照下抓；连拍拿到的图覆盖不了真实变化）\n")

    seen = {}
    saved = 0
    duplicates = 0
    attempts = 0
    max_attempts = args.count * 4

    while saved < args.count and attempts < max_attempts:
        attempts += 1
        data, error = fetch_raw_frame(args.board, args.port, timeout=args.timeout)
        if error:
            print("  ✗ %s" % error)
            if attempts >= 3 and saved == 0:
                print("\n连续取不到图 —— 先确认板子在线、8088 在跑：")
                print("  python tools/board_acceptance.py --board %s" % args.board)
                return 1
            time.sleep(args.interval)
            continue

        digest = sha256_of(data)
        if digest in seen and not args.keep_duplicates:
            duplicates += 1
            time.sleep(args.interval)
            continue

        seen[digest] = True
        name = "calib_%04d.jpg" % (saved + 1)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        saved += 1
        print("  %-18s %7d bytes%s" % (name, len(data),
                                       "  (重复)" if digest in seen and saved > 1 and
                                       args.keep_duplicates else ""))
        if saved < args.count:
            time.sleep(args.interval)

    if saved == 0:
        print("\n一张都没抓到。")
        return 1

    # 生成 RKNN 校准用的图片列表（相对路径，包拷到哪都能用）
    list_path = os.path.join(out_dir, "calib.txt")
    with open(list_path, "w") as fh:
        for index in range(1, saved + 1):
            fh.write("calib/calib_%04d.jpg\n" % index)

    print("\n抓到 %d 张（跳过重复 %d 张，尝试 %d 次）" % (saved, duplicates, attempts))
    print("图片列表已写出：%s" % list_path)
    if saved < args.count:
        print("⚠️  只拿到 %d 张（目标 %d）—— RKNN 量化建议至少 100 张，"
              "否则激活值范围估不准。" % (saved, args.count))
    print("\n下一步：python tools/calibrate_int8.py pack --onnx <你的.onnx> --calib %s"
          % args.calib)
    return 0


def cmd_pack(args):
    if not os.path.isfile(args.onnx):
        print("✗ 找不到 onnx：%s" % args.onnx)
        return 1
    images = [f for f in os.listdir(args.calib)
              if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not images:
        print("✗ %s 里没有图片 —— 先跑 collect" % args.calib)
        return 1
    if len(images) < 50:
        print("⚠️  只有 %d 张校准图，RKNN 量化建议 >= 100 张。" % len(images))

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(os.path.join(args.out, "calib"))

    for name in images:
        shutil.copy2(os.path.join(args.calib, name),
                     os.path.join(args.out, "calib", name))
    # 校准列表必须相对包根，这样包拷到转换机任意位置都能用
    with open(os.path.join(args.out, "calib.txt"), "w") as fh:
        for index, name in enumerate(sorted(images), 1):
            fh.write("calib/%s\n" % name)

    shutil.copy2(args.onnx, os.path.join(args.out, os.path.basename(args.onnx)))

    # 验证集也要一起打进包，否则转换机上跑不到（脚本里用的是相对路径）
    val_in_bundle = ""
    val_names = []
    if args.val:
        if not os.path.isdir(args.val):
            print("✗ 验证集目录不存在：%s" % args.val)
            return 1
        val_names = sorted(f for f in os.listdir(args.val)
                           if f.lower().endswith((".jpg", ".jpeg", ".png")))
        if not val_names:
            print("✗ 验证集目录里没有图片：%s" % args.val)
            return 1
        val_in_bundle = "val"
        os.makedirs(os.path.join(args.out, val_in_bundle))
        for name in val_names:
            shutil.copy2(os.path.join(args.val, name),
                         os.path.join(args.out, val_in_bundle, name))

    meta = {
        "onnx": os.path.basename(args.onnx),
        "calib_images": len(images),
        "val_images": len(val_names) if val_in_bundle else 0,
        "target": args.target,
        "mean": args.mean,
        "std": args.std,
        "val_dir": val_in_bundle,
        "out_rknn": args.out_rknn,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "校准图必须是板端 /raw.jpg 抓的原始帧，不要用带检测框的图",
    }
    with open(os.path.join(args.out, "meta.json"), "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    script = VM_SCRIPT % {
        "onnx": os.path.basename(args.onnx),
        "out_rknn": args.out_rknn,
        "target": args.target,
        "mean": args.mean,
        "std": args.std,
        "val_dir": val_in_bundle,
    }
    script_path = os.path.join(args.out, "vm_calibrate_int8.sh")
    with open(script_path, "w", newline="\n") as fh:
        fh.write(script)
    os.chmod(script_path, 0o755)

    # 校验包内没有 CRLF —— 转换机是 Linux，CRLF 会出 /bin/bash^M
    with open(script_path, "rb") as fh:
        if b"\r\n" in fh.read():
            print("✗ 生成的 shell 脚本含 CRLF，转换机会报 /bin/bash^M")
            return 1

    total = sum(os.path.getsize(os.path.join(args.out, "calib", f)) for f in images)
    print("校准包已生成：%s" % args.out)
    print("  校准图    : %d 张（%.1f MB）" % (len(images), total / 1048576.0))
    print("  onnx      : %s" % meta["onnx"])
    print("  目标平台  : %s" % args.target)
    print("  转换脚本  : vm_calibrate_int8.sh（LF 已校验）")
    if val_in_bundle:
        print("  验证集    : %d 张（已打进包）" % meta["val_images"])
    else:
        print("  验证集    : 未提供（只产出 rknn，不做精度对比）")
    print("\n拷到转换机后：")
    print("  tar czf calib_bundle.tar.gz -C %s ." % args.out)
    print("  # 转换机上解包，然后： bash vm_calibrate_int8.sh")
    if os.name != "posix":
        print("\n注意：本机是 Windows，chmod 设不了 POSIX 执行位，")
        print("      tar 出来的包到 Linux 上可能没有 x 权限 ——")
        print("      所以一律用 `bash vm_calibrate_int8.sh` 调用，别用 ./vm_calibrate_int8.sh。")
    return 0


def cmd_compare(args):
    if not os.path.isfile(args.result):
        print("✗ 找不到 result.json：%s" % args.result)
        return 1
    with open(args.result) as fh:
        data = json.load(fh)

    fp = data.get("fp") or []
    int8 = data.get("int8") or []
    print("=" * 62)
    print("FP vs INT8 对比")
    print("=" * 62)
    print("验证集图片数: %s" % data.get("val_images"))

    if not fp or not int8:
        print("\n?  result.json 里没有逐图对比数据。")
        print("   多半是转换机上缺 PIL，或没提供验证集目录。")
        print("   **没数据不等于通过** —— 需要重新在转换机上跑一次。")
        return 1

    fp_map = dict((item["image"], item["mean_abs"]) for item in fp)
    rows = []
    for item in int8:
        name = item["image"]
        if name not in fp_map:
            continue
        base = fp_map[name]
        delta = item["mean_abs"] - base
        pct = (delta / base * 100.0) if base else 0.0
        rows.append((name, base, item["mean_abs"], delta, pct))

    if not rows:
        print("✗ FP 和 INT8 的图片对不上，无法比较。")
        return 1

    avg_delta = sum(r[3] for r in rows) / len(rows)
    avg_pct = sum(r[4] for r in rows) / len(rows)
    worst = max(rows, key=lambda r: abs(r[4]))

    print("\n%-22s %-12s %-12s %-10s %s" % ("图片", "FP", "INT8", "差值", "变化"))
    for name, base, quant, delta, pct in rows[:20]:
        print("%-22s %-12.4f %-12.4f %+-10.4f %+.2f%%" % (name, base, quant, delta, pct))
    if len(rows) > 20:
        print("... 其余 %d 张略" % (len(rows) - 20))

    print("\n平均差值   : %+.4f (%+.2f%%)" % (avg_delta, avg_pct))
    print("最差单张   : %s %+.2f%%" % (worst[0], worst[4]))

    threshold = args.max_delta_pct
    print("\n判定（阈值：平均变化 |%.1f%%| 以内）" % threshold)
    if abs(avg_pct) <= threshold:
        print("✅ INT8 量化带来的平均偏移在可接受范围")
    else:
        print("✗ 平均偏移 %.2f%% 超过阈值 %.1f%%" % (avg_pct, threshold))
        print("  常见原因：校准图太少 / 不是真机位抓的 / 光照单一。")
        print("  先回去补 collect，别急着上板。")
        return 1
    print("\n注意：这只是「量化前后输出偏移」，不等于精度。")
    print("真精度要拿标注验证集跑 mAP，见 rk3568/fruit-train/ 的评估流程。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="INT8 校准：采集 / 打包 / 对比")
    sub = parser.add_subparsers(dest="command")

    collect = sub.add_parser("collect", help="从板端 8088 抓真实相机图做校准集")
    collect.add_argument("--board", default=DEFAULT_BOARD)
    collect.add_argument("--port", type=int, default=8088)
    collect.add_argument("--count", type=int, default=200)
    collect.add_argument("--interval", type=float, default=1.0, help="抓图间隔秒")
    collect.add_argument("--timeout", type=float, default=10.0)
    collect.add_argument("--calib", default=DEFAULT_CALIB_DIR)
    collect.add_argument("--keep-duplicates", action="store_true",
                         help="连完全相同的帧也保留（一般不需要）")

    pack = sub.add_parser("pack", help="打自包含校准包给转换机")
    pack.add_argument("--onnx", required=True)
    pack.add_argument("--calib", default=DEFAULT_CALIB_DIR)
    pack.add_argument("--out", default=os.path.join(REPO, "dist", "calib_bundle"))
    pack.add_argument("--target", default="rk3568")
    pack.add_argument("--mean", default="0,0,0")
    pack.add_argument("--std", default="255,255,255")
    pack.add_argument("--val", default=None, help="验证集目录（可选）")
    pack.add_argument("--out-rknn", default="model_int8.rknn")

    compare = sub.add_parser("compare", help="对比 FP / INT8 输出偏移")
    compare.add_argument("--result", required=True)
    compare.add_argument("--max-delta-pct", type=float, default=5.0)

    args = parser.parse_args()
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "pack":
        return cmd_pack(args)
    if args.command == "compare":
        return cmd_compare(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
