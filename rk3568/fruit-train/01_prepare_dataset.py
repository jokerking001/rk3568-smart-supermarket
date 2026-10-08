# -*- coding: utf-8 -*-
"""准备水果检测数据集：下载 -> 统计 -> 子集化到目标类别。

数据源：HuggingFace `henningheyen/LVIS_Fruits_And_Vegetables`
        （8401 张，63 类，YOLO bbox，自带 data.yaml）
国内走 `hf-mirror.com`（`huggingface.co` 不可达）。

用法：

    python 01_prepare_dataset.py --stats           # 只看类别分布，不下载
    python 01_prepare_dataset.py --download        # 标准下载（huggingface_hub）
    python 01_prepare_dataset.py --download-fast   # 快速下载（见下，本网络下推荐）
    python 01_prepare_dataset.py --subset          # 子集化到 8 类，产出 dataset/fruit8

    python 01_prepare_dataset.py --download --subset        # 一条龙
    python 01_prepare_dataset.py --download-fast --subset   # 一条龙（快）

两条下载路径产出的目录结构**完全一样**，--subset 不关心你用了哪条。

为什么还要一条 --download-fast：
    `snapshot_download` 是标准做法，但在某些网络下会栽：
      - `max_workers=8` 的并发 TLS 握手会被中间设备掐断，报
        `SSL: UNEXPECTED_EOF_WHILE_READING` —— 而单独 urllib 请求同一个 URL 却正常，
        所以这个错看着像"网络不通"，其实是并发握手的问题
      - 退回逐文件 `/resolve/` 更慢：每个文件新建一次 TLS + 一次 307 跳转，
        实测 0.7 个/秒，8401 个标签要 3.3 小时

    --download-fast 把实测跑得通的组合固化成三条：
      1. 图片走 **parquet 分片** —— 5 个请求拿 1.34 GB，比逐文件快几个数量级
         （仓库里 `*.jpg` 在 .gitattributes 里声明了 LFS，`git show` 出来只是指针，
          `git lfs pull` 实测挂死，所以真图只能从 parquet 里取）
      2. 标签走 **git 浅克隆 + sparse-checkout** —— 一次 pack 拿全 8401 个 txt，
         秒级（该仓库 pack 只有约 5 MiB，因为图片都是 LFS 指针）
      3. data.yaml 从克隆里复制

    注意：若本机被注入了指向失效端口的 HTTP 代理，git 会假失败。
    清掉即可：`git config --global --unset http.proxy`，
    或临时 `env -u HTTPS_PROXY -u HTTP_PROXY git ...`。
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "dataset", "raw")
OUT_DIR = os.path.join(HERE, "dataset", "fruit8")
HF_ENDPOINT = "https://hf-mirror.com"
REPO_ID = "henningheyen/LVIS_Fruits_And_Vegetables"
GIT_MIRROR = "%s/datasets/%s" % (HF_ENDPOINT, REPO_ID)
CLONE_DIR = os.path.join(HERE, "dataset", "_clone")

# --download-fast 用的 parquet 分片清单。顺序即 (分片 -> split) 的映射，
# 图片从分片里解出来落到 dataset/raw/images/<split>/。
# 分片名里的 train/validation/test 和 raw 目录的 train/val/test **不是一一对应**，
# 所以这里显式写映射，别靠字符串猜。
PARQUET_SHARDS = [
    ("train-00000-of-00003.parquet", "train"),
    ("train-00001-of-00003.parquet", "train"),
    ("train-00002-of-00003.parquet", "train"),
    ("validation-00000-of-00001.parquet", "val"),
    ("test-00000-of-00001.parquet", "test"),
]
PARQUET_DIR = os.path.join(HERE, "dataset", "parquet")

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


# ── --download-fast ─────────────────────────────────────────────────────────
# 图片走 parquet 分片，标签走 git 浅克隆。理由见文件头注释。


def _run_git(args, cwd=None):
    cmd = ["git"] + list(args)
    print("     $ %s" % " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError("git 退出码 %d" % proc.returncode)


def _http_get(url, dest, tries=4):
    """urllib + 断点续传 + 重试。

    刻意**不用 requests.Session**：实测它的长连接池跑一会儿会整段僵死
    （8000 个文件下到 307 个就再也没动静，另起进程请求同一 URL 却秒回）。
    这里每个文件独立请求，慢一点但不会卡住。
    """
    import time
    import urllib.request

    tmp = dest + ".part"
    for attempt in range(1, tries + 1):
        have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "fruit8-prep/1.0"})
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                # 只有服务端真的支持 Range（206）才续写，否则从头写，
                # 不然会把两段内容拼成坏文件。
                mode = "ab" if (have and resp.status == 206) else "wb"
                with open(tmp, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
            os.replace(tmp, dest)
            return True
        except Exception as exc:
            print("     第 %d/%d 次失败：%s" % (attempt, tries, exc))
            time.sleep(min(2.0 * attempt, 10))
    return False


def download_parquet():
    os.makedirs(PARQUET_DIR, exist_ok=True)
    all_ok = True
    for shard, _ in PARQUET_SHARDS:
        dest = os.path.join(PARQUET_DIR, shard)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print("  已有 %s（%s）" % (shard, _human(os.path.getsize(dest))))
            continue
        url = "%s/datasets/%s/resolve/main/%s" % (HF_ENDPOINT, REPO_ID, shard)
        print("  下载 %s ..." % shard)
        if _http_get(url, dest):
            print("    -> %s" % _human(os.path.getsize(dest)))
        else:
            print("    !! 失败，重跑会断点续传")
            all_ok = False
    return all_ok


def extract_images():
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("缺 pyarrow：pip install pyarrow -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return False
    total = 0
    for shard, split in PARQUET_SHARDS:
        path = os.path.join(PARQUET_DIR, shard)
        if not os.path.isfile(path):
            print("  跳过 %s（分片没下到）" % shard)
            continue
        out_dir = os.path.join(RAW_DIR, "images", split)
        os.makedirs(out_dir, exist_ok=True)
        pf = pq.ParquetFile(path)
        written = 0
        for rg in range(pf.metadata.num_row_groups):
            table = pf.read_row_group(rg, columns=["image"])
            col = table.column("image")
            for i in range(table.num_rows):
                cell = col[i].as_py()
                name = os.path.basename(cell.get("path") or "")
                data = cell.get("bytes")
                if not name or not data or not name.lower().endswith(
                        (".jpg", ".jpeg", ".png")):
                    continue
                dest = os.path.join(out_dir, name)
                if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                    continue
                with open(dest, "wb") as fh:
                    fh.write(data)
                written += 1
        total += written
        print("  images/%-6s 写出 %5d 张" % (split, written))
    print("  图片合计 %d 张" % total)
    return True


def download_labels_via_git():
    if shutil.which("git") is None:
        print("找不到 git，标签取不到。装一个 git 再重跑。")
        return False
    try:
        if not os.path.isdir(os.path.join(CLONE_DIR, ".git")):
            if os.path.isdir(CLONE_DIR):
                shutil.rmtree(CLONE_DIR)
            print("  浅克隆 %s" % GIT_MIRROR)
            _run_git(["clone", "--depth", "1", "--sparse", GIT_MIRROR, CLONE_DIR])
        else:
            print("  复用已有克隆 %s" % CLONE_DIR)
        # 只检出 labels/：图片在仓库里是 LFS 指针，检出也没用（真图走 parquet）
        _run_git(["sparse-checkout", "set", "labels"], cwd=CLONE_DIR)
        _run_git(["checkout"], cwd=CLONE_DIR)
    except RuntimeError as exc:
        print("  git 失败：%s" % exc)
        print("  如果本机有失效的 HTTP 代理，先 `git config --global --unset http.proxy`。")
        return False

    src = os.path.join(CLONE_DIR, "labels")
    if not os.path.isdir(src):
        print("  克隆里没有 labels/，仓库结构可能变了。")
        return False
    for split in sorted(os.listdir(src)):
        sdir = os.path.join(src, split)
        if not os.path.isdir(sdir):
            continue
        ddir = os.path.join(RAW_DIR, "labels", split)
        os.makedirs(ddir, exist_ok=True)
        n = 0
        for fname in os.listdir(sdir):
            if fname.endswith(".txt"):
                shutil.copyfile(os.path.join(sdir, fname), os.path.join(ddir, fname))
                n += 1
        print("  labels/%-6s %5d 个" % (split, n))

    yaml_src = os.path.join(CLONE_DIR, "data.yaml")
    if os.path.isfile(yaml_src):
        shutil.copyfile(yaml_src, os.path.join(RAW_DIR, "data.yaml"))
        print("  data.yaml 已复制")
    else:
        print("  !! 克隆里没有 data.yaml，--subset 会失败")
    return True


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def cmd_download_fast():
    print("快速下载：图片走 parquet 分片，标签走 git 浅克隆")
    os.makedirs(RAW_DIR, exist_ok=True)

    print("\n[1/3] parquet 分片（约 1.34 GB）")
    if not download_parquet():
        print("\n分片没下全，重跑会断点续传。")
        return 1

    print("\n[2/3] 从 parquet 提取图片")
    if not extract_images():
        return 1

    print("\n[3/3] 用 git 取标签 + data.yaml")
    if not download_labels_via_git():
        return 1

    print("\n原始数据就绪：%s" % RAW_DIR)
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
    parser.add_argument("--download-fast", dest="download_fast", action="store_true",
                        help="快速下载（parquet 取图 + git 取标签），本网络下推荐")
    parser.add_argument("--subset", action="store_true", help="子集化到目标类别")
    parser.add_argument("--stats", action="store_true", help="只看类别分布")
    args = parser.parse_args()

    names = {}
    yaml_path = os.path.join(RAW_DIR, "data.yaml")
    if os.path.isfile(yaml_path):
        names = read_data_yaml_names(yaml_path)
        print("data.yaml 读到 %d 个类别" % len(names))
    elif args.stats or args.subset:
        print("没找到 %s，先跑 --download 或 --download-fast" % yaml_path)
        return 1

    if args.download_fast:
        code = cmd_download_fast()
        if code:
            return code
        if os.path.isfile(yaml_path):
            names = read_data_yaml_names(yaml_path)

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

    if not (args.download or args.download_fast or args.subset or args.stats):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
