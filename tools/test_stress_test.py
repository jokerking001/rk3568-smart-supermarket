# -*- coding: utf-8 -*-
"""stress_test_30min 的自测 —— 不需要板子。

重点验证三件事：
  1. 没测到数据时**绝不能算通过**（假绿比不跑更危险）
  2. 阈值判定在边界上是闭区间（等于阈值算通过）
  3. 报告渲染不会因为缺字段炸掉

用法：
    python tools/test_stress_test.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import stress_test_30min as st  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        FAILURES.append(label)
        print("  FAIL %s\n         实得 %r\n         期望 %r" % (label, got, want))


def make_sampler():
    return st.Sampler("127.0.0.1", 5.0, 5.0, 60.0, False, "linaro", "")


def test_percentile():
    print("\n[1] percentile —— 空输入返回 None，不能返回 0")
    check("空列表", st.percentile([], 95), None)
    check("单个元素", st.percentile([10.0], 95), 10.0)
    check("中位数", st.percentile([1.0, 2.0, 3.0], 50), 2.0)
    # nearest-rank：index = round(0.95 * (n-1))，n=100 -> index 94 -> 值 95
    check("p95 是 nearest-rank（1..100 -> 95）",
          st.percentile(list(range(1, 101)), 95), 95.0)
    check("p100 就是最大", st.percentile([5.0, 9.0, 7.0], 100), 9.0)
    check("p0 就是最小", st.percentile([5.0, 9.0, 7.0], 0), 5.0)


def test_extract_fps():
    print("\n[2] extract_fps —— 各家字段名不一样，都得认")
    check("fps", st.extract_fps('{"fps": 4.69}'), 4.69)
    check("loop_fps", st.extract_fps('{"loop_fps": 12}'), 12.0)
    check("rate", st.extract_fps('{"rate": 3.5}'), 3.5)
    check("没有字段", st.extract_fps('{"ok": true}'), None)
    check("非 JSON", st.extract_fps("garbage"), None)
    check("空串", st.extract_fps(""), None)
    check("值是字符串不算", st.extract_fps('{"fps": "fast"}'), None)


def test_no_data_is_not_pass():
    print("\n[3] 没测到数据 = 不能判通过（最关键的一条）")
    sampler = make_sampler()
    # 什么都没采到
    report = st.build_report(sampler, 10.0, {
        "success_rate": 99.0, "vision_p95_ms": 500.0,
        "temp_max_c": 85.0, "radar_drop_pct": 5.0,
    })
    check("整体不通过", report["passed"], False)
    statuses = dict((v["item"], v["status"]) for v in report["verdicts"])
    check("视觉 p95 -> unknown", statuses["视觉 p95 延迟"], "unknown")
    check("温度 -> unknown", statuses["温度峰值"], "unknown")
    # 一次请求都没发出去 -> 成功率也属于「没测到」，不是「0% 不达标」
    check("成功率 -> unknown", statuses["请求成功率"], "unknown")
    check("雷达 -> unknown", statuses["雷达掉线率"], "unknown")
    check("四项全是 unknown", sorted(report["unknown_items"]),
          sorted(["请求成功率", "视觉 p95 延迟", "温度峰值", "雷达掉线率"]))
    check("unknown 的 pass 是 False", all(not v["pass"] for v in report["verdicts"]
                                          if v["status"] == "unknown"), True)
    text = st.render_text(report)
    check("报告里写明「没测到」", "没测到不等于通过" in text, True)


def test_threshold_boundary():
    print("\n[4] 阈值边界 —— 等于阈值算通过（闭区间）")
    sampler = make_sampler()
    sampler.attempts = {"vision": 100}
    sampler.failures = {"vision": 1}          # 成功率 99.0%
    sampler.latencies = {"vision": [500.0]}   # p95 = 500.0
    sampler.temps = [85.0]                    # 峰值 = 85.0
    sampler.radar_total = 100
    sampler.radar_fail = 5                    # 掉线率 5.0%
    report = st.build_report(sampler, 60.0, {
        "success_rate": 99.0, "vision_p95_ms": 500.0,
        "temp_max_c": 85.0, "radar_drop_pct": 5.0,
    })
    statuses = dict((v["item"], v["status"]) for v in report["verdicts"])
    check("成功率 99.0 恰好达标", statuses["请求成功率"], "pass")
    check("视觉 p95 500ms 恰好达标", statuses["视觉 p95 延迟"], "pass")
    check("温度 85C 恰好达标", statuses["温度峰值"], "pass")
    check("掉线率 5% 恰好达标", statuses["雷达掉线率"], "pass")
    check("整体通过", report["passed"], True)


def test_over_threshold():
    print("\n[5] 越过阈值必须判 fail（不是 unknown）")
    sampler = make_sampler()
    sampler.attempts = {"vision": 100}
    sampler.failures = {"vision": 10}          # 90%
    sampler.latencies = {"vision": [900.0]}    # 超过 500
    sampler.temps = [92.0]                     # 超过 85
    sampler.radar_total = 100
    sampler.radar_fail = 30                    # 30%
    report = st.build_report(sampler, 60.0, {
        "success_rate": 99.0, "vision_p95_ms": 500.0,
        "temp_max_c": 85.0, "radar_drop_pct": 5.0,
    })
    statuses = dict((v["item"], v["status"]) for v in report["verdicts"])
    check("成功率 fail", statuses["请求成功率"], "fail")
    check("视觉 fail", statuses["视觉 p95 延迟"], "fail")
    check("温度 fail", statuses["温度峰值"], "fail")
    check("雷达 fail", statuses["雷达掉线率"], "fail")
    check("unknown 列表为空", report["unknown_items"], [])
    check("整体不通过", report["passed"], False)


def test_render_robust():
    print("\n[6] 报告渲染 —— 缺字段也不能炸")
    sampler = make_sampler()
    sampler.attempts = {"store": 3}
    sampler.failures = {}
    sampler.latencies = {"store": [12.0, 30.0, 80.0]}
    report = st.build_report(sampler, 12.0, {
        "success_rate": 99.0, "vision_p95_ms": 500.0,
        "temp_max_c": 85.0, "radar_drop_pct": 5.0,
    })
    text = st.render_text(report)
    check("含标题", "压测报告" in text, True)
    check("含 store 延迟行", "store" in text, True)
    check("FPS 无数据时写 None 不报错", "样本 0" in text, True)


def main():
    test_percentile()
    test_extract_fps()
    test_no_data_is_not_pass()
    test_threshold_boundary()
    test_over_threshold()
    test_render_robust()

    print("\n" + "=" * 60)
    if FAIL:
        print("✗ %d 通过 / %d 失败" % (PASS, FAIL))
        for label in FAILURES:
            print("    - %s" % label)
        return 1
    print("✅ %d 项全部通过 —— 压测脚本本身可信" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
