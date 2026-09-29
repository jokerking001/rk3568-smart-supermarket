# 水果检测模型 → RKNN 转换包（VM 侧，一次运行）

这个包是给**转换机**（Ubuntu x86_64，例如 `zwb@192.168.190.160`）用的。
板端（RK3568）**不需要**这个包——板端只接收最后产出的 `.rknn` 文件。

## 为什么必须在这台 VM 上跑

RKNN-Toolkit2 只提供 **Linux x86_64** 的 wheel：

- 板端是 aarch64，装不了 toolkit（只有 `rknnlite` 推理运行时）
- 本机 WSL 被安全策略拦截，Docker 未安装，Windows 没有对应 wheel
- 所以转换只能在这台 VM 上完成

## 包内容

| 文件 | 说明 |
| --- | --- |
| `vm_build_fruit_rknn.sh` | 一键入口：建环境 → 备数据 → 转换 → 报告 |
| `build_fruit_rknn.py` | 实际转换脚本（可单独调用） |
| `fruits_yolo11s.pt` | 水果 YOLO11s 权重（apple / carrot / orange，CC-BY-4.0） |
| `calib_640/` | 80 张 640×640 INT8 校准图（从数据集降采样） |

包内自带权重和校准图，所以**整个流程可以离线完成**，不需要 VM 联网。

## 跑起来

把整个目录拷进 VM（VMware 拖拽、共享目录、或 `pscp -r` 都行），然后：

```bash
cd <包所在目录>
bash vm_build_fruit_rknn.sh
```

先看一眼环境是否满足，可以只做预检（不会改任何东西）：

```bash
bash vm_build_fruit_rknn.sh --check-only
```

只出 INT8 模型（省一半时间）：

```bash
bash vm_build_fruit_rknn.sh --dtypes i8
```

## 脚本会做什么

1. **环境检查** — 确认 x86_64 / python 版本 / `python3-venv`
2. **虚拟环境** — 优先复用文档里已有的 `~/venvs/rknn-toolkit2`；没有就新建并安装
   `rknn-toolkit2==2.3.2` + `ultralytics` + `numpy<2`
3. **权重** — 用包内的 `fruits_yolo11s.pt`（没有才去 hf-mirror 下载）
4. **校准集** — 用包内的 `calib_640/`（没有才去 hf-mirror 下载并降采样）
5. **转换** — 重新导出 ONNX → 校验输出布局 → 构建 INT8 / FP 两个 `.rknn`
6. **报告** — 打印文件大小与 md5，并给出推送到板端的命令

## 一个关键坑：原始 ONNX 不能用

随模型发布的 `my_model-simplified.onnx` 是 **OAK / Luxonis 格式，只有 3 个输出**
（`output1..3_yolov6r2`，每个尺度一个融合张量）。

板端的后处理 `yolo11_infer.py` 期望的是 **9 个输出**的 RKNN 布局
（每个尺度一组 `box_dfl / class_scores / score_sum`，即 `pair = len(outputs) // 3`）。
直接拿 3 输出的模型去推理会 `IndexError`。

所以脚本**必须**从 `.pt` 用 ultralytics 重新导出一次 ONNX（标准单输出图），
由 RKNN 自己切成 9 个输出——板端代码一行都不用改。

转换脚本会自己检测这一点：如果发现是 3 输出的 ONNX 又带了 `--no-export`，
它会直接报错退出，不会浪费一次转换。

## 产出

```
~/fruit_rknn/out/fruits_yolo11s_i8.rknn     ← 板端要的就是这个
~/fruit_rknn/out/fruits_yolo11s_fp.rknn
~/fruit_rknn/out/report.json                ← 布局校验 + 运行时输出形状
```

## 推到板端

```bash
pscp ~/fruit_rknn/out/fruits_yolo11s_i8.rknn linaro@<板端IP>:/home/linaro/ai/models/
```

板端 `rk3568-fruit.service` 每 2 秒重试加载模型，文件到位后会**自动生效**，
不用重启。确认：

```bash
curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/status
```

看到 `"model_loaded": true` 就成了。

## 排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `RKNN-Toolkit2 only runs on x86_64` | 跑错机器了，必须在 x86_64 Linux |
| `venv creation failed` | `sudo apt install -y python3-venv python3-pip` |
| 装依赖卡住 | 换镜像：`PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple bash vm_build_fruit_rknn.sh` |
| `no calibration images` | 确认 `calib_640/` 里有 jpg，或让 VM 联网自动下载 |
| 下载失败 | 设 `HF_MIRROR`，或直接用包内文件（包内已含全部素材） |
| `IndexError` / 输出数不对 | ONNX 布局问题，别加 `--no-export`，让脚本从 `.pt` 重导 |
| INT8 精度差 | 校准图不具代表性。换更多真实场景照片放进 `calib_640/` 再跑 |

## 来源与许可

- 权重与数据集：`johnatanvq/fruits-yolo-model`、`johnatanvq/fruits-dataset`
- 许可：**CC-BY-4.0**，商用需保留署名（见 `artifacts/README.md`）
- 类别：`apple` / `carrot` / `orange`
