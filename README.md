# RK3568 智慧超市

把原来的 **ESP32-S3 智慧超市** 主控迁到 **正点原子 ATK-DLRK3568**，原 ESP32 侧只保留那些"在 MCU 上做得更好、或搬到 Linux 上不好做"的实时外设任务。

> 一句话分工：**RK3568 管逻辑、管服务、管网络；ESP32-S3 管实时硬件。**

---

## 1. 谁跑在哪

| 角色 | 硬件 | 代码位置 | 职责 |
|---|---|---|---|
| 主控 / 服务端 | ATK-DLRK3568（Debian 10 arm64） | `rk3568/` | 商品库、购物车、订单、视觉推理、雷达、VLM、扫码、OCR、融合判定 |
| 门店主控固件 | ESP32-S3（Arduino） | `firmware/store-controller/` | 人机界面：按键、语音、屏幕、HX711 称重、RFID、USB 扫码枪、OTA |
| 视觉节点 | XIAO ESP32-S3 Sense（OV3660） | `firmware/vision-node/` | 摄像头取图 + Edge Impulse 水果分类，只上报概率，不做判定 |
| 热敏打印机 | ESP32-S3（ESP-IDF） | `firmware/thermal-printer/` | 轮询打印任务，打印 384 点单色光栅小票 |

**为什么保留 ESP32-S3？** HX711 是位翻转（bit-bang）时序、I2S 麦克风/喇叭需要硬实时 DMA 和 GPIO 中断——这些在 Linux 上要么做不稳，要么要额外写内核驱动。放到 RK3568 上收益极低、风险极高。所以"换主控"换的是**决策与业务逻辑**，不是把硬件时序也搬过去。

---

## 2. 仓库结构

```
.
├── firmware/                       ESP32 侧固件
│   ├── store-controller/           ESP32-S3 Arduino 主控（原 Work7_20）
│   ├── vision-node/                XIAO ESP32-S3 Sense 视觉节点
│   └── thermal-printer/            ESP32-S3 ESP-IDF 热敏打印机（原 re_min）
│
├── rk3568/                         RK3568 板端
│   ├── store-backend/              8094 商品/购物车/订单 + 8095 扫码 + 8096 OCR
│   ├── fruit-model/                8089 水果识别（YOLO11 + RKNN）
│   ├── fruit-fusion/               8099 视觉 × 重量融合判定  ← 本次新增
│   ├── fruit-train/                8 类水果模型训练 + RKNN 转换流水线
│   ├── services/                   8088 视觉 / 8090 融合 / 8091 雷达 / 8093 数据集采集
│   └── vlm/                        8092 视觉大模型代理（mock 可跑）
│
├── docs/                           迁移总览、交接文档、开工说明
├── tools/                          凭据残留守卫等小工具
└── .gitignore
```

---

## 3. 三步上手

### 3.1 克隆

```bash
git clone <仓库地址>
cd rk3568-smart-supermarket
```

### 3.2 配 `secrets.h`（必做，否则编不过）

所有密钥、WiFi、内网地址都从源码里抽出来了，只在本地存在：

```bash
cd firmware/store-controller   && cp secrets.h.example secrets.h
cd ../vision-node              && cp secrets.h.example secrets.h
cd ../thermal-printer/main     && cp secrets.h.example secrets.h
```

然后打开这三个 `secrets.h`，把占位值换成真实值。**`secrets.h` 已被 `.gitignore` 忽略，不会进版本库。**

需要填的东西：

| 文件 | 宏 | 说明 |
|---|---|---|
| store-controller | `WIFI_STA_SSID` / `WIFI_STA_PASSWORD` | 门店路由器 |
| store-controller | `DASHSCOPE_API_KEY` | 阿里云百炼，语音问答用 |
| store-controller | `BAIDU_API_KEY` / `BAIDU_SECRET_KEY` | 百度语音识别/合成 |
| store-controller | `JUHE_WEATHER_KEY` | 聚合数据天气 |
| store-controller | `MIMICLAW_STORE_KEY` | 管理接口鉴权头，**板端要填一样的值** |
| vision-node | `WIFI_SSID_CFG` / `WIFI_PASSWORD_CFG` / `FUSION_URL_CFG` | RK3568 的融合服务地址 |
| thermal-printer | `WIFI_SSID` / `WIFI_PASSWORD` / `CONTROLLER_BASE_URL` | 主控地址 |

> ⚠️ vision-node 的宏名带 `_CFG` 后缀是**故意的**：`.ino` 里已有同名的 `static` 变量，宏名撞车会被预处理器展开成语法错误。

### 3.3 按需构建

**store-controller（Arduino）**

用 Arduino IDE 打开 `firmware/store-controller/Work7_20.ino`：

- 开发板：ESP32-S3
- 分区表：`partitions.csv`（草图目录里已有，会自动采用）
- 编译选项：`build_opt.h`（同上，会自动采用）
- 依赖库：`ESPAsyncWebServer`、`ArduinoJson`、`ElegantOTA`、`qrcode` 等

**vision-node（Arduino）**

```bash
arduino-cli compile \
  --fqbn esp32:esp32:XIAO_ESP32S3 \
  firmware/vision-node
```

需要先在 Arduino IDE 里用「项目 → 加载库 → 添加 ZIP 库」装好 Edge Impulse 导出的分类库。板子设置：启用 `OPI PSRAM`、USB CDC 启用。ESP32 Arduino Core 3.0.6。

**thermal-printer（ESP-IDF）**

```bash
cd firmware/thermal-printer
idf.py set-target esp32s3
idf.py build
idf.py -p <PORT> flash monitor
```

**RK3568 板端**

```bash
# 服务代码
scp rk3568/store-backend/*.py linaro@<板子IP>:/home/linaro/ai/store/
scp rk3568/fruit-fusion/*.py  linaro@<板子IP>:/home/linaro/ai/fruit-fusion/

# systemd 单元
sudo cp rk3568/*/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rk3568-store.service rk3568-fruit.service rk3568-fruit-fusion.service
```

板子环境：Debian 10 arm64、Linux 4.19.232、**Python 3.7.3**、用户 `linaro`。
`rknnlite` 在 `~/.local/lib/python3.7/site-packages`，跑推理要 `sudo -u linaro -H python3`。

---

## 4. 板端端口表

| 端口 | 服务 | 说明 |
|---|---|---|
| 8088 | 视觉 | 摄像头取流 + `/raw.jpg` |
| 8089 | 水果识别 | YOLO11 + RKNN，10 Hz 出结果 |
| 8090 | 雷达 × 视觉融合 | **顾客会话状态机**（不是水果融合！） |
| 8091 | 雷达 | 激光雷达点云 |
| 8092 | VLM | 视觉大模型代理 |
| 8093 | 数据集采集 | 抓图打标 |
| 8094 | 门店后端 | 商品 / 购物车 / 订单 |
| 8095 | 扫码枪 | 条码解析 |
| 8096 | OCR | 票据识别 |
| **8099** | **水果 视觉 × 重量 融合** | **本次新增** |

> 8090 和 8099 都叫"融合"，但完全无关。8090 是雷达背景变化 + 视觉人形判断"有没有顾客在"，8099 才是"这个水果是什么、多重、收不收"。

---

## 5. 水果识别流水线

```
XIAO 摄像头 ──1400ms──▶ 5 帧滑窗 ──均值/离散度/边际──▶ POST 8099
                                                          │
RK3568 8089 ──10Hz──▶ 桥接按 1400ms 取一帧 ──────────────┤
                                                          ▼
                                              fused = 视觉×0.78 + 重量分×0.22
                                                          │
                                     ┌────────────────────┴────────────────────┐
                                     ▼                                         ▼
                              达标 → 加入购物车                        不达标 → 请人工确认
```

**判定门槛**（改动前先看 `rk3568/fruit-fusion/fruit_fusion.py`）：

- 融合分 ≥ 0.72，视觉 ≥ 0.55，重量分 ≥ 0.15，重量 ≥ 25g
- 视觉有效：窗口满 5 帧、类内极差 ∈ [0, 0.25]、top1−top2 ≥ 0.12、概率和 ∈ [0.80, 1.20]
- 称重有效：8 个采样、极差 ≤ 4g、新鲜度 ≤ 1500ms

**类别顺序（8 类，必须逐字一致）**：

```
apple, banana, orange, grapes, pear, strawberry, kiwi, watermelon
```

> ⚠️ 类别名是靠**字符串**查概率的（`probabilities.get(rule.label, 0.0)`），对不上就是恒为 0，**不报错**。改类别务必跑一遍：
>
> ```bash
> python rk3568/fruit-train/05_check_labels.py
> ```

**模型训练**（换数据集或加类别时）：

```bash
cd rk3568/fruit-train
python 01_prepare_dataset.py     # 拉数据集并切出 8 类
python 02_train.py               # YOLO11 训练
python 03_export_onnx.py         # 导出 ONNX（会校验输出个数）
bash   04_pack_rknn_bundle.sh    # 打成给转换机的离线包
```

> RKNN 转换只能在 **Linux x86_64** 上跑（RKNN-Toolkit2 没有 arm64 版）。板子上只有 `rknnlite`，只负责推理。转换包拿去 Ubuntu 机器执行 `04_vm_build_fruit8_rknn.sh`。

---

## 6. 凭据与安全

- 真实密钥只在本地 `secrets.h`，**永不入库**。
- 装一次提交前守卫（每个队友在自己机器上跑一次，钩子不进版本库）：

  ```bash
  bash tools/install-hooks.sh
  ```

  之后每次 `git commit` 都会自动跑凭据检查，发现残留直接拦下。

- 手动跑守卫：

  ```bash
  python tools/check_literals.py
  ```

  它会按**历史真实出现过的字面值**比对（不是宽泛正则，所以零误报），发现残留直接退出码 1。

- `tools/scan_secrets.py` 是更宽的模式扫描，用来找"新引入的、还没登记进守卫"的凭据。
- 内网 IP（`192.168.43.x`）也抽进了 `secrets.h`——它们不是密钥，但换现场就得改，放在源码里迟早漏一个。

> 🔴 **如果这些密钥曾经明文提交过，光删掉不够。** DashScope 的 `sk-` 和百度那对 key 建议直接去控制台作废重发。

---

## 7. 当前进度

**已完成**

- ✅ 融合判定逻辑 1:1 移植到 Python 3.7（`fruit_fusion.py`）
- ✅ 多帧聚合层 `vision_observer.py`（复刻 XIAO 的 5 帧滑窗语义）
- ✅ 8099 服务 `fruit_fusion_service.py`（含转发 8094、幂等去重）
- ✅ 1400ms 桥接 `fruit_fusion_bridge.py`
- ✅ 268 个测试全绿，全部通过 Python 3.7 语法校验
- ✅ 8 类训练 + RKNN 转换流水线（含类别名一致性守卫）
- ✅ 板端服务代码与 systemd 单元

**待办**

- ⬜ 板端 8088 / 8094 的本地代码与板上实际运行版本一致性**尚未核对**——这是上板第一件事
- ⬜ 8 类模型训练与 RKNN 转换（卡在转换机口令）
- ⬜ 打印链路选型：384 点光栅 vs ESC/POS + GBK
- ⬜ `top1` 与 `normalize` 两种概率模式二选一
- ⬜ 原 81 个接口里仍有一部分未在 RK3568 侧实现

**明确不做**

- ❌ 板端本地部署视觉大模型（NPU 只有 ~0.8 TOPS、内存 4GB，跑不动）
- ❌ 把 HX711 / I2S 搬到 RK3568（实时时序，留在 MCU）

**关于 `mimiclaw`**：原工程里的 `mimiclaw`（未纳入本仓库） 是开源项目 [`memovai/mimiclaw`](https://github.com/memovai/mimiclaw) 的克隆，不是本项目的代码，**没有放进这个仓库**。需要的话自行 clone 上游。

---

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| `docs/RK3568-迁移总览.md` | **主文档**。架构、接口契约、缺口矩阵、上电顺序 |
| `docs/交接文档-v2.md` | 板端交接细节（端口、接口、验收） |
| `docs/交接文档-v1.md` | 早期版本，仅供追溯 |
| `docs/明天开工说明.md` | 分步操作清单 + 踩坑记录 |
| `rk3568/fruit-fusion/README.md` | 融合模块专项说明 |
| `rk3568/fruit-model/README.md` | 水果模型与 RKNN 转换 |
| `firmware/vision-node/README.md` | 视觉节点专项说明 |

---

## 9. 约定

- 板端 Python 代码必须兼容 **Python 3.7.3**：不能用海象运算符、字典合并、`list[int]` 这类 3.9+ 语法。
- 提交前自测（在仓库根目录跑）：

  ```bash
  cd rk3568/fruit-fusion
  python test_fruit_fusion.py
  python test_vision_observer.py
  python test_fruit_fusion_service.py
  python test_fruit_fusion_bridge.py
  cd ../..
  python tools/check_literals.py
  ```
