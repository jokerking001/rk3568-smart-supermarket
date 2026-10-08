# -*- coding: utf-8 -*-
"""准备水果检测数据集：下载 -> 统计 -> 子集化到目标类别。

数据源：HuggingFace `henningheyen/LVIS_Fruits_And_Vegetables`
        （8401 张，63 类，YOLO bbox，自带 data.yaml）
国内走 `hf-mirror.com`（`huggingface.co` 不可达）。

用法：

    python 01_prepare_dataset.py --stats        # 只看类别分布，不下载
    python 01_prepare_dataset.py --download     # 下载原始数据到 dataset/raw
    python 01_prepare_dataset.py --subset       # 子集化到 8 类，产出 dataset/fruit8

    python 01_prepare_dataset.py --download --subset   # 一条龙

注意：--download 约 1.43 GB。开代理前实测直连只有约 295 KB/s，会很久。
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "dataset", "raw")
OUT_DIR = os.path.join(HERE, "dataset", "fruit8")
HF_ENDPOINT = "https://hf-mirror.com"
REPO_ID = "henningheyen/LVIS_Fruits_And_Vegetables"

# LVIS 的类别名很长（如 "orange/orange fruit"），这里映射成训练用的短名。
# 顺序即最终 data.yaml 里的类别顺序。
#
# ！！短名必须与 fruit_rules.json 里的 label 逐字一致 ！！
# 融合引擎是按名字查概率的（`probabilities.get(rule.label, 0.0)`），
# 名字对不上就等于该类概率恒为 0，永远判不出来，而且不会报错。
# 所以 grape 这里刻意写成 "grapes" —— 原工程 VISION_WEIGHT_RULES 里的
# label 就是 "grapes"，继承下来的名字不要动。
# 改完用 `python 05_check_labels.py` 复查。
TARGET_CLASSES = [
    ("apple", "apple"),
    ("banana", "banana"),
    ("orange/orange fruit", "orange"),
    ("grape", "grapes"),
    ("pear", "pear"),
    ("strawberry", "strawberry"),
    ("kiwi fruit", "kiwi"),
    ("watermelon", "watermelon"),
]


def read_data_yaml_names(path):
    """只解析 data.yaml 里的 names 段，不引入 pyyaml 依赖。"""
    names = {}
    in_names = False
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            stripped = line.strip()
            if stripped.startswith("names:"):
                in_names = True
                continue
            if in_names:
                if not stripped or (":" not in stripped and not stripped.startswith("-")):
                    break
                if ":" in stripped:
                    key, _, value = stripped.partition(":")
                    key = key.strip()
                    if key.isdigit():
                        names[int(key)] = value.strip().strip("'\"")
    return names


def label_dirs(raw_dir):
    """返回实际存在的 (split, labels 目录, images 目录) 列表。

    注意：该仓库 data.yaml 里写的 val 指向 images/test，
    但目录里同时存在 val 与 test，所以这里以实际目录为准。
    """
    found = []
    labels_root = os.path.join(raw_dir, "labels")
    images_root = os.path.join(raw_dir, "images")
    if not os.path.isdir(labels_root):
        return found
    for split in sorted(os.listdir(labels_root)):
        ldir = os.path.join(labels_root, split)
        idir = os.path.join(images_root, split)
        if os.path.isdir(ldir) and os.path.isdir(idir):
            found.append((split, ldir, idir))
    return found


def scan_stats(raw_dir, names):
    counts = {}
    files = 0
    boxes = 0
    for split, ldir, _ in label_dirs(raw_dir):
        for fname in os.listdir(ldir):
            if not fname.endswith(".txt"):
                continue
            files += 1
            with open(os.path.join(ldir, fname), "r", encoding="utf-8",
                      errors="replace") as fp:
                for line in fp:
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    try:
                        cid = int(parts[0])
                    except ValueError:
                        continue
                    boxes += 1
                    counts[cid] = counts.get(cid, 0) + 1
    return files, boxes, counts


def cmd_stats(names):
    if not os.path.isdir(os.path.join(RAW_DIR, "labels")):
        print("原始数据还没下载。先跑：python 01_prepare_dataset.py --download")
        return 1
    files, boxes, counts = scan_stats(RAW_DIR, names)
    print("标签文件 %d 个，标注框 %d 个" % (files, boxes))
    print("")
    print("%-6s %-34s %s" % ("class", "name", "instances"))
    for cid in sorted(counts, key=lambda k: -counts[k]):
        print("%-6d %-34s %d" % (cid, names.get(cid, "?")[:34], counts[cid]))
    print("")
    print("目标类别命中情况：")
    for lvis_name, short in TARGET_CLASSES:
        hit = None
        for cid, nm in names.items():
            if nm == lvis_name:
                hit = counts.get(cid, 0)
                break
        print("  %-12s <- %-26s %s" % (short, lvis_name, hit if hit is not None else "未找到"))
    return 0


def cmd_download():
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("缺 huggingface_hub。先装：")
        print("  pip install -U huggingface_hub -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1
    os.makedirs(RAW_DIR, exist_ok=True)
    print("从 %s 下载 %s 到 %s" % (HF_ENDPOINT, REPO_ID, RAW_DIR))
    print("约 1.43 GB，慢的话先开代理。中断后重跑会自动续传。")
    snapshot_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        endpoint=HF_ENDPOINT,
        local_dir=RAW_DIR,
        allow_patterns=["images/**", "labels/**", "data.yaml"],
        max_workers=8,
    )
    print("下载完成：%s" % RAW_DIR)
    return 0


def cmd_subset(names):
    if not os.path.isdir(os.path.join(RAW_DIR, "labels")):
        print("原始数据还没下载。先跑：python 01_prepare_dataset.py --download")
        return 1

    wanted = {}
    for lvis_name, short in TARGET_CLASSES:
        for cid, nm in names.items():
            if nm == lvis_name:
                wanted[cid] = short
                break
    missing = [s for _, s in TARGET_CLASSES if s not in wanted.values()]
    if missing:
        print("警告：以下类别在 data.yaml 里没找到，将被跳过：%s" % ", ".join(missing))
    if not wanted:
        print("没有任何目标类别匹配上，中止。")
        return 1

    new_ids = {}
    for index, (_, short) in enumerate(TARGET_CLASSES):
        if short in wanted.values():
            new_ids[short] = len(new_ids)

    if os.path.isdir(OUT_DIR):
        print("清空旧输出：%s" % OUT_DIR)
        shutil.rmtree(OUT_DIR)

    summary = {}
    for split, ldir, idir in label_dirs(RAW_DIR):
        # test 不并入训练，避免和 val 重复
        if split == "test":
            continue
        out_images = os.path.join(OUT_DIR, "images", split)
        out_labels = os.path.join(OUT_DIR, "labels", split)
        os.makedirs(out_images, exist_ok=True)
        os.makedirs(out_labels, exist_ok=True)
        kept = 0
        for fname in sorted(os.listdir(ldir)):
            if not fname.endswith(".txt"):
                continue
            src_label = os.path.join(ldir, fname)
            new_lines = []
            with open(src_label, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    try:
                        cid = int(parts[0])
                    except ValueError:
                        continue
                    short = wanted.get(cid)
                    if short is None:
                        continue
                    new_lines.append("%d %s" % (new_ids[short], " ".join(parts[1:5])))
            if not new_lines:
                continue
            stem = os.path.splitext(fname)[0]
            src_image = None
            for ext in (".jpg", ".jpeg", ".png"):
                candidate = os.path.join(idir, stem + ext)
                if os.path.isfile(candidate):
                    src_image = candidate
                    break
            if src_image is None:
                continue
            shutil.copyfile(src_image, os.path.join(out_images, os.path.basename(src_image)))
            with open(os.path.join(out_labels, fname), "w", encoding="utf-8") as fp:
                fp.write("\n".join(new_lines) + "\n")
            kept += 1
        summary[split] = kept

    class_names = [None] * len(new_ids)
    for short, index in new_ids.items():
        class_names[index] = short

    with open(os.path.join(OUT_DIR, "data.yaml"), "w", encoding="utf-8") as fp:
        fp.write("path: %s\n" % OUT_DIR.replace("\\", "/"))
        fp.write("train: images/train\n")
        fp.write("val: images/val\n")
        fp.write("nc: %d\n" % len(class_names))
        fp.write("names:\n")
        for index, name in enumerate(class_names):
            fp.write("  %d: %s\n" % (index, name))

    print("")
    for split in sorted(summary):
        print("%-6s 保留 %d 张" % (split, summary[split]))
    print("")
    print("类别顺序：%s" % ", ".join(class_names))
    print("输出目录：%s" % OUT_DIR)
    print("data.yaml 已生成。")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="下载原始数据集")
    parser.add_argument("--subset", action="store_true", help="子集化到目标类别")
    parser.add_argument("--stats", action="store_true", help="只看类别分布")
    args = parser.parse_args()

    names = {}
    yaml_path = os.path.join(RAW_DIR, "data.yaml")
    if os.path.isfile(yaml_path):
        names = read_data_yaml_names(yaml_path)
        print("data.yaml 读到 %d 个类别" % len(names))
    elif args.stats or args.subset:
        print("没找到 %s，先跑 --download" % yaml_path)
        return 1

    if args.download:
        code = cmd_download()
        if code:
            return code
        if os.path.isfile(yaml_path):
            names = read_data_yaml_names(yaml_path)

    if args.stats:
        return cmd_stats(names)
    if args.subset:
        return cmd_subset(names)

    if not (args.download or args.subset or args.stats):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
