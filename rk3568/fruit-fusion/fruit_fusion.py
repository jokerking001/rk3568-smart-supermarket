# -*- coding: utf-8 -*-
"""水果视觉 + 重量融合（RK3568 侧实现）。

从 ESP32-S3 原工程 `D:\\789\\Work7_20\\WebServer.cpp` 的 `/api/vision-fusion`
处理逻辑 1:1 搬过来，参数与判据完全一致：

    融合分 = 视觉概率 * 0.78 + 重量匹配分 * 0.22
    自动确认需同时满足：视觉有效 + 融合 >= 0.72 + 视觉 >= 0.55
                        + 重量匹配 >= 0.15 + 重量 >= 25 g + 称重稳定且新鲜
    重量匹配分 = 1 - |实测 - 典型| / 容差，钳制到 [0, 1]

与 ESP32 版的差异（都是搬移带来的必然差异，不是行为改变）：

1. 典型重量/容差不再硬编码在 C 里，改为可加载的 JSON（`fruit_rules.json`），
   便于实测后收紧而不用重新编译。
2. 称重数据由外部喂入（RK3568 上没有 HX711，重量来自 ESP32-S3 从机），
   所以 `ScaleBuffer` 只做原工程 `Scale_GetSnapshot` 的等价判据。

板端是 Python 3.7.3，本文件不使用 walrus、dict 合并、PEP 585 泛型下标。
"""

import json
import os
import threading

# ── 与 ESP32 原工程一致的常量 ────────────────────────────────────────────

VISION_WEIGHT = 0.78
WEIGHT_WEIGHT = 0.22
MIN_FUSED = 0.72
MIN_VISION = 0.55
MIN_WEIGHT_SCORE = 0.15
MIN_GRAMS = 25.0

VISION_MIN_SAMPLES = 5
VISION_MAX_SPREAD = 0.25
VISION_MIN_MARGIN = 0.12
VISION_PROB_SUM_MIN = 0.80
VISION_PROB_SUM_MAX = 1.20

SCALE_SAMPLES = 8
SCALE_STABLE_SPAN_G = 4.0
SCALE_MAX_AGE_MS = 1500

OBSERVATION_ID_MAX_LEN = 40

# 原工程 `Scale_IsProductRemoved()` 的判据：重量骤降超过 30 g 视为商品取走
PRODUCT_REMOVED_DROP_G = 30.0


class FruitRule(object):
    """一种水果的典型重量与容差。"""

    __slots__ = ("label", "name", "typical_g", "tolerance_g")

    def __init__(self, label, name, typical_g, tolerance_g):
        self.label = label
        self.name = name
        self.typical_g = float(typical_g)
        self.tolerance_g = float(tolerance_g)


# 前三项是原工程 `VISION_WEIGHT_RULES` 的原值，不要改。
# 后五项是按「扩充到更多常见水果」新增的初始值 ——
# 原工程 README 明确要求：比赛用固定样品时应实测收紧，不要直接采信。
DEFAULT_RULES = [
    FruitRule("apple", "苹果", 180.0, 90.0),
    FruitRule("banana", "香蕉", 140.0, 80.0),
    FruitRule("grapes", "葡萄", 300.0, 220.0),
    FruitRule("orange", "橙子", 200.0, 100.0),
    FruitRule("pear", "梨", 220.0, 110.0),
    FruitRule("kiwi", "猕猴桃", 100.0, 50.0),
    FruitRule("strawberry", "草莓", 250.0, 150.0),
    FruitRule("watermelon", "西瓜", 3000.0, 1500.0),
]


def load_rules(path=None):
    """从 JSON 加载规则；文件不存在或格式不对时回退到 DEFAULT_RULES。"""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fruit_rules.json")
    try:
        with open(path, "r") as fp:
            raw = json.load(fp)
        rules = []
        for item in raw:
            rules.append(FruitRule(item["label"], item["name"],
                                   item["typical_g"], item["tolerance_g"]))
        if rules:
            return rules
    except (IOError, OSError, ValueError, KeyError, TypeError):
        pass
    return list(DEFAULT_RULES)


def weight_score(rule, grams):
    """原工程 `visionWeightScore()`：1 - |实测-典型|/容差，钳制 [0,1]。"""
    if rule.tolerance_g <= 0:
        return 1.0 if abs(grams - rule.typical_g) < 1e-9 else 0.0
    score = 1.0 - abs(grams - rule.typical_g) / rule.tolerance_g
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def valid_observation_id(observation_id):
    """原工程对 observation_id 的校验：长度 1..40，只允许字母数字、'-'、'_'。"""
    if not observation_id:
        return False
    if len(observation_id) > OBSERVATION_ID_MAX_LEN:
        return False
    for ch in observation_id:
        if not (ch.isalnum() and ch.isascii()) and ch not in ("-", "_"):
            return False
    return True


class ScaleBuffer(object):
    """等价于原工程 `HX711_Scale.cpp` 的环形缓冲与 `Scale_GetSnapshot()`。

    原工程每 500 ms 采一次，保留最近 8 个样本；
    样本数 >= 5 且 (max - min) <= 4.0 g 才算稳定；
    最后一个有效样本距今 <= 1500 ms 才算新鲜。
    """

    def __init__(self, size=SCALE_SAMPLES, stable_span_g=SCALE_STABLE_SPAN_G,
                 max_age_ms=SCALE_MAX_AGE_MS):
        self._size = int(size)
        self._stable_span_g = float(stable_span_g)
        self._max_age_ms = int(max_age_ms)
        self._samples = []
        self._last_valid_time_ms = None
        self._last_stable = None
        self._lock = threading.Lock()

    def add_sample(self, grams, now_ms):
        """喂入一个重量样本。负值表示读取失败，按原工程语义忽略。"""
        if grams is None or grams < 0:
            return
        with self._lock:
            self._samples.append(float(grams))
            if len(self._samples) > self._size:
                del self._samples[0:len(self._samples) - self._size]
            self._last_valid_time_ms = now_ms

    def clear(self):
        with self._lock:
            self._samples = []
            self._last_valid_time_ms = None
            self._last_stable = None

    def snapshot(self, now_ms):
        """返回 (grams, stable, age_ms, ok)，语义与原工程逐条对应。

        不满足前置条件时返回 (-1.0, False, 0, False)，对应 C++ 里
        `*weight` / `*stable` 未被写入、调用方拿到 -1 的情形。
        """
        with self._lock:
            samples = list(self._samples)
            last_valid = self._last_valid_time_ms

        if len(samples) < 5 or last_valid is None:
            return -1.0, False, 0, False

        mean = sum(samples) / float(len(samples))
        span = max(samples) - min(samples)
        stable = span <= self._stable_span_g
        age_ms = now_ms - last_valid
        return mean, stable, age_ms, age_ms <= self._max_age_ms

    def is_product_removed(self, grams):
        """原工程 `Scale_IsProductRemoved()`：相对上次稳定值骤降 > 30 g。"""
        with self._lock:
            last = self._last_stable
            if grams is None or grams < 0:
                return False
            if last is None:
                self._last_stable = float(grams)
                return False
            if last - grams > PRODUCT_REMOVED_DROP_G:
                self._last_stable = float(grams)
                return True
            if grams > last:
                self._last_stable = float(grams)
            return False


class FusionEngine(object):
    """视觉概率 + 重量 -> 融合判定，并保持 observation_id 幂等。"""

    def __init__(self, rules=None, scale=None):
        self.rules = rules if rules is not None else load_rules()
        self.scale = scale if scale is not None else ScaleBuffer()
        self._last_observation_id = None
        self._last_response = None
        self._lock = threading.Lock()

    def labels(self):
        return [r.label for r in self.rules]

    def observe(self, observation_id, probabilities, sample_count, spread, margin, now_ms):
        """处理一次视觉观测，返回与原工程同形状的响应 dict。

        同一个 observation_id 重复提交直接返回上次结果（原工程的幂等设计），
        这样视觉节点重试不会造成重复加购。
        """
        if not valid_observation_id(observation_id):
            return {"ok": False, "msg": "invalid observation_id"}

        with self._lock:
            if observation_id == self._last_observation_id and self._last_response is not None:
                return dict(self._last_response)

        probs = []
        for rule in self.rules:
            value = probabilities.get(rule.label, 0.0)
            try:
                probs.append(float(value))
            except (TypeError, ValueError):
                probs.append(0.0)

        prob_sum = sum(probs)
        valid_vision = (sample_count >= VISION_MIN_SAMPLES
                        and 0.0 <= spread <= VISION_MAX_SPREAD
                        and margin >= VISION_MIN_MARGIN
                        and VISION_PROB_SUM_MIN <= prob_sum <= VISION_PROB_SUM_MAX)
        for p in probs:
            if p < 0.0 or p > 1.0:
                valid_vision = False

        grams, stable, age_ms, scale_ok = self.scale.snapshot(now_ms)

        best = -1
        best_vision = 0.0
        best_weight = 0.0
        best_fused = 0.0
        for index, rule in enumerate(self.rules):
            w = weight_score(rule, grams) if stable else 0.0
            fused = probs[index] * VISION_WEIGHT + w * WEIGHT_WEIGHT
            if fused > best_fused:
                best = index
                best_vision = probs[index]
                best_weight = w
                best_fused = fused

        accepted = (valid_vision and best >= 0 and grams >= MIN_GRAMS
                    and scale_ok and stable
                    and best_vision >= MIN_VISION
                    and best_weight >= MIN_WEIGHT_SCORE
                    and best_fused >= MIN_FUSED)

        label = self.rules[best].label if best >= 0 else "unknown"
        name = self.rules[best].name if accepted else "待确认"

        response = {
            "ok": True,
            "observation_id": observation_id,
            "accepted": accepted,
            "label": label,
            "name": name,
            "weight_g": round(grams, 1),
            "stable": stable,
            "vision": round(best_vision, 4),
            "vision_stable": valid_vision,
            "vision_spread": round(spread, 4),
            "vision_margin": round(margin, 4),
            "scale_age_ms": age_ms,
            "weight_score": round(best_weight, 4),
            "confidence": round(best_fused, 4),
        }

        with self._lock:
            self._last_observation_id = observation_id
            self._last_response = dict(response)
        return response

    def latest(self):
        """等价于原工程 `/api/vision-fusion/latest`。"""
        with self._lock:
            if self._last_response is None:
                return {"ok": False, "msg": "暂无视觉结果"}
            return dict(self._last_response)
