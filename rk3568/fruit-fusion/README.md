# 水果视觉 + 重量融合（RK3568 侧）

原工程 ESP32 主控里 `/api/vision-fusion` 那一块，在 RK3568 上的落点。
补的是 RK 侧**当前完全缺失**的一整层。

## 为什么要做这个

`firmware/store-controller/` 的 ESP32 主控做的是「视觉 + 重量」双证据判定：

    融合分 = 视觉概率 × 0.78 + 重量匹配分 × 0.22

换成 RK3568 之后，8094 收银后端的 `observe_vision(label, confidence)` 只剩
**单帧标签 + 置信度**，重量、多帧稳定性、幂等都丢了：

| | 原工程 ESP32 主控 | RK 8094 现状 | 本模块 |
| --- | --- | --- | --- |
| 视觉输入 | 5 帧聚合概率 + spread/margin/sample_count | 单帧 `{label, confidence}` | 恢复多帧聚合 |
| 重量输入 | HX711 快照（稳定 / 新鲜 / ≥25 g） | **无** | 恢复重量融合 |
| 判定 | `v×0.78 + w×0.22` + 四重门禁 | `confidence ≥ sku_map.min_conf` | 恢复原判据 |
| 幂等 | `observation_id` | 无 | 恢复，且跨服务边界去重 |

## 文件

| 文件 | 作用 |
| --- | --- |
| `fruit_fusion.py` | 融合引擎：重量匹配分、称重缓冲、融合判定、observation_id 幂等 |
| `fruit_rules.json` | 8 类水果的典型重量与容差（前 3 条是原工程原值） |
| `vision_observer.py` | 视觉节点：多帧概率聚合 → 提交观测 |
| `fruit_fusion_service.py` | 融合服务本体（8099）：观测入口 + 称重入口 + 幂等转发 |
| `fruit_fusion_bridge.py` | 桥接：按原工程节奏轮询 8089 的检测结果喂给观测器 |
| `deploy/rk3568-fruit-fusion.service` | 本服务的 systemd unit |
| `deploy/rk3568-fruit.service` | 水果识别服务的 unit（**取代**旧版，换成 8 类模型） |
| `test_*.py` | 268 项测试，不依赖 pytest，板端直接跑 |

## 桥接层：为什么必须有，以及为什么是 1400 ms

`fruit_service.py`（8089）以 **10 Hz** 推理并输出 detections。但原工程的视觉节点是
**每 1400 ms 推一帧**给融合判定的：

```
原工程 INFERENCE_INTERVAL_MS = 1400，窗口 5 帧
→ 一个判定窗口横跨约 7 秒 = 「水果必须在秤上稳住约 7 秒」
```

如果直接把 10 Hz 的结果全喂进窗口，窗口 **0.5 秒**就填满，判定会变得极其敏感；
而且秤那边（500 ms 一次采样、要 5 个样本）根本还没稳。
**所以桥接按 1400 ms 的节奏喂 —— 这个节奏是判定行为的一部分，别随手调小。**

桥接通过 8089 **已有的** `GET /api/fruit/result` 拿结果，对已部署的服务零侵入。
`fruit_service.py` 本来就支持 `--model` / `--classes`，换 8 类模型只改 unit 参数，
**一行代码都不用动**。

源不可用时的处理很关键：

| 8089 的 `status` | 桥接动作 | 为什么 |
| --- | --- | --- |
| `running` + 有检测 | 正常喂 | — |
| `running` + 空检测 | **照常喂 0** | 这是「台面上没东西」，该判无效 |
| `model_unavailable` / `vision_unavailable` / `starting` | **跳过，不喂** | 这是「我自己坏了」，喂进去会污染窗口 |

链路全貌：

```
8089 fruit_service.py（10 Hz 推理）
     ↓ GET /api/fruit/result   每 1400 ms
fruit_fusion_bridge.FruitBridge
     ↓ observer.submit_detections()   （服务内置时走进程内直连，不走 loopback HTTP）
8099 fruit_fusion_service.py（融合判定，窗口 5 帧 ≈ 7 秒）
     ↓ POST /api/vision/observe
8094 store_service.py（加购）
```

开启方式（融合服务的内置桥接线程）：

```bash
python3 fruit_fusion_service.py --port 8099 --bridge-url auto
# auto = http://127.0.0.1:8089/api/fruit/result
# 也可单独跑桥接做联调：python3 fruit_fusion_bridge.py --steps 20
```

## 判据（与原工程逐条一致，不要改）

```
融合分 = 视觉概率 × 0.78 + 重量匹配分 × 0.22
重量匹配分 = 1 - |实测 - 典型| / 容差        （钳制到 [0, 1]）

自动确认需同时满足：
    视觉有效  = 样本数 ≥ 5 且 spread ∈ [0, 0.25] 且 margin ≥ 0.12
                且 概率和 ∈ [0.80, 1.20] 且每个概率 ∈ [0, 1]
    融合分 ≥ 0.72
    视觉概率 ≥ 0.55
    重量匹配分 ≥ 0.15
    重量 ≥ 25 g
    称重稳定（极差 ≤ 4 g）且新鲜（≤ 1500 ms）
不满足就输出「请人工确认」，**绝不自动加购**
```

## 端口

板端服务端口（来自 `rk3568-smart-supermarket-handoff-v2.md` 第 54-62 行）：

| 端口 | 服务 |
| --- | --- |
| 8088 | 视觉推理 |
| 8089 | 水果识别 |
| 8090 | 融合（**雷达 + 视觉 person，不是这个**） |
| 8091 | 雷达 |
| 8093 | 数据采集 |
| 8094 | 收银后端 |
| 8095 | 扫码枪 |
| 8096 | OCR |
| **8099** | **本模块：水果视觉 + 重量融合** |

> 8090 很容易搞混。它是「雷达背景变化 + 视觉 person」的顾客会话状态机，
> 端点是 `/api/fusion/status`，**没有** `/api/vision-fusion`。
> 8099 原来干跑水果服务用过，已关闭，现在收回来。

## 接口

```
POST /api/vision-fusion           视觉节点提交观测（原工程同款表单）
     observation_id=xxx&captured_ms=12345&sample_count=5
     &spread=0.0156&margin=0.8648&apple=0.8817&banana=...
     → 融合判定 JSON，accepted=true 时自动转发到 8094 加购

GET  /api/vision-fusion/latest    最近一次判定（原工程 /api/vision-fusion/latest）
POST /api/scale/sample            ESP32-S3 从机喂重量：grams=180.0
GET  /api/scale/status            称重缓冲状态
GET  /api/fusion/status           健康检查
GET  /                            页面
```

## 部署

```bash
# 1. 传上去
pscp -r rk3568-fruit-fusion linaro@<板端IP>:/home/linaro/ai/fruit-fusion

# 2. 板端自检（不需要摄像头，不需要板子在网络里）
cd /home/linaro/ai/fruit-fusion
python3 test_fruit_fusion.py            # 52 项
python3 test_vision_observer.py         # 73 项
python3 test_fruit_fusion_service.py    # 81 项
python3 test_fruit_fusion_bridge.py     # 62 项

# 3. 换水果识别服务的 unit（8 类模型），再装融合服务
sudo cp deploy/rk3568-fruit.service /etc/systemd/system/          # 覆盖旧版
sudo cp deploy/rk3568-fruit-fusion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart rk3568-fruit
sudo systemctl enable --now rk3568-fruit-fusion
curl -s --noproxy '*' http://127.0.0.1:8099/api/fusion/status
```

**先确认模型到位再重启 8089**，否则它会一直报 `model_loaded: false`：

```bash
ls -l /home/linaro/ai/models/fruit8_yolo11n_i8.rknn
curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/status   # 等 model_loaded: true
```

## 联调（不接摄像头，先把链路跑通）

```bash
# 起服务，关掉转发
python3 fruit_fusion_service.py --port 8099 --no-forward

# 喂重量样本（8 个 180 g）
for i in $(seq 8); do
  curl -s --noproxy '*' -d 'grams=180.0' http://127.0.0.1:8099/api/scale/sample
done

# 视觉节点空跑合成概率流（前 3 帧故意抖动，跑 9 帧以上能看到「通过」）
python3 vision_observer.py --synthetic --steps 9 --interval 0.1
```

预期：前 4 帧 `窗口 n/5 预热中`，第 5 帧起 `accepted=True label=apple
confidence≈0.91 weight=180.0g`。

## 接上真实视觉（生产做法）

`vision_observer.py` **不自己开摄像头**。板端 8088 视觉服务已经占了摄像头，
它复用 8088 的 `http://127.0.0.1:8088/raw.jpg` 出帧、自己跑 RKNN。

生产做法是在 8088 的推理循环里，把单帧结果直接喂给 observer：

```python
import vision_observer as vo

observer = vo.VisionObserver(vo.labels_from_rules())

# 推理循环里，每帧拿到 detections 之后：
response = observer.submit_detections(detections)   # 窗口不满 5 帧返回 None
```

不要另起一个服务去抢摄像头。

## 一个必须知道的语义差异

原工程用 Edge Impulse 分类器，`result.classification[i].value` 是 softmax 输出，
**天然和为 1**。RK 侧用 YOLO 检测框置信度，没有这种分布。
所以 `detections_to_probabilities()` 有两种模式：

- `top1`（默认）：每类取最高检测框置信度，**不归一化**。
  台面只有一个水果时 sum ≈ 0.85~0.95 通过；同时出现多个水果时 sum > 1.2
  会被判**视觉无效** —— 这恰好符合「一个秤盘一个商品」的假设，是个有用的
  副作用，但它不是原工程显式设计的行为。
- `normalize`：归一化到和为 1。sum 判据恒成立，判别力落在 margin / spread /
  单类概率上，更贴近原工程 softmax 语义。

两种都留着，**等拿板子在真实台面跑一批数据再定**。

## 已知边界

1. 融合服务是**独立服务**，没并进 8094。原工程是单主控所以融合和购物车在一起，
   但 8094 的 `store_service.py` 有 1156 行 + 77 个回归测试，而且本地副本与板端
   副本**是否一致还没核对过**。所以先独立跑，判定通过后复用 8094 已有的
   `/api/vision/observe` 把结果交出去，一行都不碰它。
   等核对完一致，再决定要不要并进去。
2. 跨服务边界去重是**本模块新增的职责**：`engine.observe` 对 observation_id 幂等，
   但 8094 的 `/api/vision/observe` **不幂等**，重放会重复加购。
   所以去重表 `FORWARD_DEDUPE_LIMIT = 512` 条，满了淘汰最早的。
3. 重量来自 ESP32-S3 从机（HX711 在它那边）。从机通过
   `POST /api/scale/sample` 喂样本，时间戳用**服务本地单调时钟**盖，
   不用从机的时钟 —— 避免两块板子时钟漂移。
4. `fruit_rules.json` 里前 3 类（apple / banana / grapes）是原工程原值。
   后 5 类是新增的初始值，**原工程 README 明确要求比赛用固定样品时要实测收紧**，
   不要直接采信。

## 类名必须逐字对齐

融合引擎按**名字**取概率（`probabilities.get(rule.label, 0.0)`）。
模型类别名和 `fruit_rules.json` 的 label 差一个字符，概率就恒为 0，
**不报错、不告警**，表现就是「这个水果怎么都识别不了」。

校验脚本在训练工程那边：

```bash
python3 05_check_labels.py --classes /home/linaro/ai/models/fruit8_yolo11n.classes.txt
```

（注意 `grape` vs `grapes` —— 原工程 label 是 `grapes`，训练时特意对齐了它。）
