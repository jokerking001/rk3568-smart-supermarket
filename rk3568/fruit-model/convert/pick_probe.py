import glob
import os

# 数据集根目录：可用环境变量 DATASET 覆盖，默认取 fruit-model/dataset
ROOT = os.environ.get(
    "DATASET",
    os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dataset")),
)
CLASSES = ["apple", "carrot", "orange"]
labels = sorted(glob.glob(os.path.join(ROOT, "labels", "*.txt")))

best = {c: [] for c in range(len(CLASSES))}
for lab in labels:
    with open(lab) as fh:
        rows = [r.split() for r in fh.read().strip().splitlines() if r.strip()]
    if not rows:
        continue
    for r in rows:
        try:
            cid = int(float(r[0]))
            w = float(r[3])
            h = float(r[4])
        except (ValueError, IndexError):
            continue
        if 0 <= cid < len(CLASSES):
            # single dominant object: largest area, and only one class in the image
            if len(rows) == 1:
                best[cid].append((w * h, os.path.basename(lab)))

for cid, name in enumerate(CLASSES):
    items = sorted(best[cid], reverse=True)[:3]
    print("%-7s 单目标图片中最大的 3 张:" % name)
    for area, lab in items:
        img = os.path.join(ROOT, "images", lab.replace(".txt", ".jpg"))
        print("   area=%.3f  %s  exists=%s" % (area, os.path.basename(img), os.path.exists(img)))
    if not items:
        print("   (没有单目标样本)")
