# -*- coding: utf-8 -*-
"""校验「模型类别名」和「融合规则 label」是否逐字一致。

为什么需要这个脚本：
    融合引擎 `fruit_fusion.py` 是按**名字**取概率的：

        value = probabilities.get(rule.label, 0.0)

    名字对不上时它拿到 0.0，然后安静地判不出来 —— 不报错、不告警，
    表现就是「这个水果怎么都识别不了」。这是最难查的一类 bug，
    所以每次改数据集类别、改模型、改规则表之后都跑一遍。

用法：

    python 05_check_labels.py
    python 05_check_labels.py --rules ../rk3568-fruit-fusion/fruit_rules.json
    python 05_check_labels.py --classes artifacts/classes.txt

退出码：0 = 一致；1 = 不一致；2 = 输入缺失（还没生成，跑不了）。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_YAML = os.path.join(HERE, "dataset", "fruit8", "data.yaml")
DEFAULT_RULES = os.path.normpath(
    os.path.join(HERE, "..", "rk3568-fruit-fusion", "fruit_rules.json"))


def read_yaml_names(path):
    """解析 data.yaml 的 names 段，返回 {id: name}。不依赖 pyyaml。"""
    names = {}
    in_names = False
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            stripped = line.strip()
            if stripped.startswith("names:"):
                in_names = True
                continue
            if not in_names:
                continue
            if not stripped:
                break
            if ":" not in stripped and not stripped.startswith("-"):
                break
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                if key.isdigit():
                    names[int(key)] = value.strip().strip("'\"")
    return names


def read_classes_txt(path):
    """读一行一个类名的 classes.txt（打包脚本会生成一份）。"""
    names = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        for index, line in enumerate(fp):
            name = line.strip()
            if name:
                names[index] = name
    return names


def read_rules(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        raw = json.load(fp)
    return [(item["label"], item.get("name", "")) for item in raw]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yaml", default=DEFAULT_YAML,
                        help="数据集 data.yaml（读里面的 names）")
    parser.add_argument("--classes", default=None,
                        help="可选的 classes.txt，给了就一并校验")
    parser.add_argument("--rules", default=DEFAULT_RULES,
                        help="fruit_rules.json")
    parser.add_argument("--write-classes", default=None,
                        help="把模型类别按下标顺序写到这个文件（打包用）")
    args = parser.parse_args()

    if not os.path.isfile(args.rules):
        print("找不到规则表：%s" % args.rules)
        return 2

    rules = read_rules(args.rules)
    rule_labels = [label for label, _ in rules]

    model_names = None
    source = None
    if args.classes:
        if not os.path.isfile(args.classes):
            print("找不到 classes 文件：%s" % args.classes)
            return 2
        model_names = read_classes_txt(args.classes)
        source = args.classes
    elif os.path.isfile(args.yaml):
        model_names = read_yaml_names(args.yaml)
        source = args.yaml
    else:
        print("既没有 --classes，也找不到 %s" % args.yaml)
        print("先跑：python 01_prepare_dataset.py --download --subset")
        return 2

    model_labels = [model_names[i] for i in sorted(model_names)]

    if args.write_classes:
        target = os.path.abspath(args.write_classes)
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, "w", encoding="utf-8") as fp:
            for index in sorted(model_names):
                fp.write("%s\n" % model_names[index])
        print("已写出类别文件：%s" % target)

    print("模型类别来源：%s" % source)
    print("融合规则来源：%s" % args.rules)
    print("")
    print("模型类别（%d 个，顺序即模型输出下标）：" % len(model_labels))
    for index in sorted(model_names):
        print("  [%d] %s" % (index, model_names[index]))
    print("")
    print("融合规则（%d 个）：" % len(rule_labels))
    for index, (label, name) in enumerate(rules):
        print("  [%d] %-12s %s" % (index, label, name))
    print("")

    model_set = set(model_labels)
    rule_set = set(rule_labels)

    missing = [label for label in rule_labels if label not in model_set]
    extra = [label for label in model_labels if label not in rule_set]

    problems = 0
    if missing:
        problems += 1
        print("!! 规则表里有、模型里没有的类别：%s" % ", ".join(missing))
        print("   这些水果的概率会恒为 0，永远判不出来。")
    if extra:
        problems += 1
        print("!! 模型里有、规则表里没有的类别：%s" % ", ".join(extra))
        print("   这些类别识别到了也进不了融合判定，等于白训。")

    if len(rule_labels) != len(set(rule_labels)):
        problems += 1
        print("!! 规则表里有重复 label，先去掉。")

    if problems:
        print("")
        print("结论：不一致，先改到逐字相同再训练/转换。")
        return 1

    print("结论：一致（%d 类逐字对齐）。" % len(rule_labels))
    print("")
    print("提示：这里只校验名字。模型的输出下标顺序与规则表顺序不同也没关系，")
    print("      融合引擎是按名字查的；但视觉节点那层必须按名字建 dict，")
    print("      不能拿模型下标去索引规则表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
