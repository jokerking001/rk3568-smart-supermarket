# -*- coding: utf-8 -*-
"""fruit_fusion 的行为测试。

不依赖 pytest，可直接在板端 Python 3.7 上跑：

    python3 test_fruit_fusion.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fruit_fusion as ff

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
    else:
        FAILED.append("%s  %s" % (name, detail))


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ── 重量匹配分 ───────────────────────────────────────────────────────────

def test_weight_score():
    rule = ff.FruitRule("apple", "苹果", 180.0, 90.0)
    check("ws 恰好等于典型值 -> 1.0", approx(ff.weight_score(rule, 180.0), 1.0))
    check("ws 差一个容差 -> 0.0", approx(ff.weight_score(rule, 270.0), 0.0))
    check("ws 差半个容差 -> 0.5", approx(ff.weight_score(rule, 225.0), 0.5))
    check("ws 超出容差 -> 钳制到 0", approx(ff.weight_score(rule, 400.0), 0.0))
    check("ws 负数方向同样钳制", approx(ff.weight_score(rule, 0.0), 0.0))
    zero = ff.FruitRule("x", "x", 100.0, 0.0)
    check("ws 容差为 0 不除零", ff.weight_score(zero, 100.0) == 1.0
          and ff.weight_score(zero, 101.0) == 0.0)


# ── observation_id 校验 ─────────────────────────────────────────────────

def test_observation_id():
    check("id 正常通过", ff.valid_observation_id("obs-123_ABC"))
    check("id 空串拒绝", not ff.valid_observation_id(""))
    check("id 超 40 字符拒绝", not ff.valid_observation_id("a" * 41))
    check("id 恰好 40 字符通过", ff.valid_observation_id("a" * 40))
    check("id 含空格拒绝", not ff.valid_observation_id("obs 1"))
    check("id 含中文拒绝", not ff.valid_observation_id("观测1"))
    check("id 含斜杠拒绝", not ff.valid_observation_id("obs/1"))


# ── 称重环形缓冲 ────────────────────────────────────────────────────────

def test_scale_buffer():
    buf = ff.ScaleBuffer()
    for i in range(4):
        buf.add_sample(180.0, 1000 + i * 500)
    grams, stable, age, ok = buf.snapshot(3000)
    check("样本 <5 时不可用", not ok and grams < 0 and not stable)

    buf.add_sample(180.0, 3000)
    grams, stable, age, ok = buf.snapshot(3000)
    check("样本满 5 个后可用", ok and approx(grams, 180.0) and stable)

    buf2 = ff.ScaleBuffer()
    for v in (180.0, 181.0, 183.0, 179.0, 184.0):
        buf2.add_sample(v, 5000)
    grams, stable, age, ok = buf2.snapshot(5000)
    check("波动 5 g > 4 g -> 不稳定", not stable)

    buf3 = ff.ScaleBuffer()
    for v in (180.0, 181.0, 182.0, 179.0, 183.0):
        buf3.add_sample(v, 5000)
    grams, stable, age, ok = buf3.snapshot(5000)
    check("波动 4 g <= 4 g -> 稳定", stable)

    grams, stable, age, ok = buf3.snapshot(5000 + 1501)
    check("样本超过 1500 ms -> 不新鲜", not ok and age == 1501)

    grams, stable, age, ok = buf3.snapshot(5000 + 1500)
    check("样本恰好 1500 ms -> 仍有效", ok)

    buf4 = ff.ScaleBuffer(size=8)
    for i in range(12):
        buf4.add_sample(float(100 + i), 10000)
    grams, _, _, _ = buf4.snapshot(10000)
    check("环形缓冲只保留最后 8 个", approx(grams, sum(range(104, 112)) / 8.0))


def test_product_removed():
    buf = ff.ScaleBuffer()
    check("首次读数不算取走", not buf.is_product_removed(200.0))
    check("重量上升不算取走", not buf.is_product_removed(210.0))
    check("骤降 31 g 判定取走", buf.is_product_removed(179.0))
    check("取走后基准已更新", not buf.is_product_removed(178.0))


# ── 融合判定 ────────────────────────────────────────────────────────────

def _engine_with_weight(grams, count=8, start_ms=1000, step_ms=500):
    buf = ff.ScaleBuffer()
    now = start_ms
    for i in range(count):
        now = start_ms + i * step_ms
        buf.add_sample(grams, now)
    engine = ff.FusionEngine(scale=buf)
    return engine, now


def test_accept_apple():
    engine, now = _engine_with_weight(180.0)
    r = engine.observe("o1", {"apple": 0.90, "banana": 0.05, "grapes": 0.05},
                       8, 0.05, 0.80, now)
    # 0.90*0.78 + 1.0*0.22 = 0.922
    check("苹果正常场景被接受", r["accepted"] and r["label"] == "apple"
          and r["name"] == "苹果", str(r))
    check("苹果融合分正确", approx(r["confidence"], 0.922, 1e-4), str(r["confidence"]))
    check("苹果重量分正确", approx(r["weight_score"], 1.0, 1e-6))
    check("响应含 ok 与 observation_id", r["ok"] and r["observation_id"] == "o1")


def test_reject_low_vision():
    engine, now = _engine_with_weight(180.0)
    r = engine.observe("o2", {"apple": 0.60, "banana": 0.2, "grapes": 0.2},
                       8, 0.05, 0.4, now)
    # 0.60*0.78 + 0.22 = 0.688 < 0.72
    check("融合分不足被拒", not r["accepted"] and r["label"] == "apple", str(r))
    check("被拒时名称为待确认", r["name"] == "待确认")


def test_reject_wrong_weight():
    engine, now = _engine_with_weight(3000.0)
    r = engine.observe("o3", {"apple": 0.95, "banana": 0.02, "grapes": 0.02},
                       8, 0.05, 0.9, now)
    # 苹果重量分为 0 -> 低于 0.15 阈值
    check("重量不匹配被拒", not r["accepted"] and r["weight_score"] == 0.0, str(r))


def test_reject_light_weight():
    engine, now = _engine_with_weight(10.0)
    r = engine.observe("o4", {"apple": 0.95, "banana": 0.02, "grapes": 0.02},
                       8, 0.05, 0.9, now)
    check("重量低于 25 g 被拒", not r["accepted"], str(r))


def test_reject_unstable_scale():
    buf = ff.ScaleBuffer()
    for i, v in enumerate((180.0, 190.0, 175.0, 200.0, 170.0)):
        buf.add_sample(v, 1000 + i * 500)
    engine = ff.FusionEngine(scale=buf)
    r = engine.observe("o5", {"apple": 0.95, "banana": 0.02, "grapes": 0.02},
                       8, 0.05, 0.9, 3000)
    check("称重不稳被拒", not r["accepted"] and not r["stable"], str(r))


def test_reject_stale_scale():
    engine, now = _engine_with_weight(180.0)
    r = engine.observe("o6", {"apple": 0.95, "banana": 0.02, "grapes": 0.02},
                       8, 0.05, 0.9, now + 5000)
    check("称重数据过期被拒", not r["accepted"] and r["scale_age_ms"] == 5000, str(r))


def test_reject_invalid_vision_fields():
    engine, now = _engine_with_weight(180.0)
    base = {"apple": 0.90, "banana": 0.05, "grapes": 0.05}
    r = engine.observe("v1", base, 4, 0.05, 0.8, now)
    check("采样数 <5 判为视觉无效", not r["vision_stable"] and not r["accepted"])
    r = engine.observe("v2", base, 8, 0.30, 0.8, now)
    check("spread >0.25 判为视觉无效", not r["vision_stable"])
    r = engine.observe("v3", base, 8, 0.05, 0.10, now)
    check("margin <0.12 判为视觉无效", not r["vision_stable"])
    r = engine.observe("v4", {"apple": 0.3, "banana": 0.2, "grapes": 0.1},
                       8, 0.05, 0.8, now)
    check("概率和 <0.80 判为视觉无效", not r["vision_stable"])
    r = engine.observe("v5", {"apple": 1.0, "banana": 1.0, "grapes": 1.0},
                       8, 0.05, 0.8, now)
    check("概率和 >1.20 判为视觉无效", not r["vision_stable"])
    r = engine.observe("v6", {"apple": 1.5, "banana": 0.05, "grapes": 0.05},
                       8, 0.05, 0.8, now)
    check("单类概率 >1 判为视觉无效", not r["vision_stable"])


def test_winner_by_fused_not_vision():
    """视觉看好 A、重量看好 B 时，应由融合分决定，而不是视觉。"""
    rules = [ff.FruitRule("A", "甲", 100.0, 20.0),
             ff.FruitRule("B", "乙", 300.0, 20.0)]
    buf = ff.ScaleBuffer()
    for i in range(8):
        buf.add_sample(300.0, 1000 + i * 500)
    engine = ff.FusionEngine(rules=rules, scale=buf)
    r = engine.observe("f1", {"A": 0.70, "B": 0.50}, 8, 0.05, 0.5, 4500)
    # A: 0.70*0.78 + 0*0.22   = 0.546
    # B: 0.50*0.78 + 1.0*0.22 = 0.610  -> B 胜
    check("融合分让重量翻盘", r["label"] == "B", str(r))
    check("翻盘后融合分正确", approx(r["confidence"], 0.61, 1e-4))


def test_idempotent_observation():
    engine, now = _engine_with_weight(180.0)
    first = engine.observe("same", {"apple": 0.90, "banana": 0.05, "grapes": 0.05},
                           8, 0.05, 0.8, now)
    second = engine.observe("same", {"apple": 0.10, "banana": 0.05, "grapes": 0.05},
                            8, 0.05, 0.8, now)
    check("同 observation_id 幂等返回缓存", first == second, str(second))
    third = engine.observe("other", {"apple": 0.10, "banana": 0.05, "grapes": 0.05},
                           8, 0.05, 0.8, now)
    check("换 observation_id 会重新判定", third["vision"] != first["vision"])


def test_bad_observation_id():
    engine, now = _engine_with_weight(180.0)
    r = engine.observe("", {"apple": 0.9}, 8, 0.05, 0.8, now)
    check("非法 observation_id 返回 ok=false", r["ok"] is False and "msg" in r)
    r = engine.observe("x" * 50, {"apple": 0.9}, 8, 0.05, 0.8, now)
    check("超长 observation_id 返回 ok=false", r["ok"] is False)


def test_latest_endpoint():
    engine, now = _engine_with_weight(180.0)
    check("未观测时 latest 提示暂无", engine.latest()["ok"] is False)
    engine.observe("L1", {"apple": 0.9, "banana": 0.05, "grapes": 0.05}, 8, 0.05, 0.8, now)
    check("观测后 latest 可查", engine.latest()["observation_id"] == "L1")


def test_rules_file_and_fallback():
    rules = ff.load_rules()
    check("默认规则含 8 类", len(rules) == 8, str(len(rules)))
    check("前三类沿用原工程原值",
          [r.label for r in rules[:3]] == ["apple", "banana", "grapes"]
          and rules[0].typical_g == 180.0 and rules[1].typical_g == 140.0
          and rules[2].typical_g == 300.0)
    missing = ff.load_rules(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "__no_such_file__.json"))
    check("规则文件缺失时回退默认", len(missing) == 8)


def test_probability_defaults():
    engine, now = _engine_with_weight(180.0)
    r = engine.observe("d1", {"apple": 0.9}, 8, 0.05, 0.8, now)
    check("缺失类别概率按 0 处理", r["ok"] and r["label"] == "apple", str(r))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILED.append("%s 抛异常: %r" % (fn.__name__, exc))
    total = len(PASSED) + len(FAILED)
    for name in FAILED:
        print("FAIL  " + name)
    print("")
    print("%d passed, %d failed (共 %d 项)" % (len(PASSED), len(FAILED), total))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
