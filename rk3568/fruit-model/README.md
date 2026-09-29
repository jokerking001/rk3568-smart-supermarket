# 水果识别模型 — RKNN 转换包

目标：把预训练的水果检测模型转成 RK3568 可加载的 `.rknn`，替换/并列于当前
COCO YOLO11n，实现水果识别。

## 1. 已就绪的素材

| 文件 | 说明 |
|---|---|
| `artifacts/fruits_yolo11s.pt` | 预训练权重（YOLO11s，19 MB），类别 apple / carrot / orange |
| `artifacts/fruits_yolo11s.onnx` | 原始 ONNX（**OAK/Luxonis 导出，不能直接用**，见第 3 节） |
| `dataset/images` + `dataset/labels` | 160 张 4096×3072 图，YOLO 格式标注，472 个实例 |
| `dataset/classes.txt` | `apple` / `carrot` / `orange` |
| `convert/calib_640/` | 80 张 640×640 标定图（6.4 MB），用于 INT8 量化 |
| `convert/build_fruit_rknn.py` | **一键脚本**：重导出 ONNX → 校验 → 转 i8/fp → 自检 |
| `board_ref/` | 板端现有推理与视觉服务源码（只读参考） |

数据集来源：HuggingFace `johnatanvq/fruits-dataset`（CC-BY-4.0），
模型来源：`johnatanvq/fruits-yolo-model`（CC-BY-4.0）。
国内直连 huggingface.co 不通，本包通过 `hf-mirror.com` 镜像下载。

标注实例分布：apple 171 / carrot 189 / orange 112。

## 2. 在转换机上执行（Linux x86_64 + rknn-toolkit2）

### 推荐：用一键包（自包含、可离线）

```text
dist/fruit_rknn_vm_bundle.zip          # 22.9 MB，拖进 VM 即可
dist/fruit_rknn_vm_bundle.tar.gz
```

包内已含权重与 80 张标定图，**VM 不需要联网**。在转换机上：

```bash
unzip fruit_rknn_vm_bundle.zip && cd fruit_rknn_vm_bundle
bash vm_build_fruit_rknn.sh              # 建环境 → 备数据 → 转换 → 报告
bash vm_build_fruit_rknn.sh --check-only # 只做环境预检，不改任何东西
bash vm_build_fruit_rknn.sh --dtypes i8  # 只出 INT8
```

脚本会优先复用文档里已有的 `~/venvs/rknn-toolkit2`；否则新建并安装
`rknn-toolkit2==2.3.2` + `ultralytics` + `numpy<2`。产出在 `~/fruit_rknn/out/`。

### 或者：手工调用

```bash
# 把本目录（convert/）上传到转换机，例如 /home/zwb/fruit/
source /home/zwb/venvs/rknn-toolkit2/bin/activate

cd /home/zwb/fruit
python3 build_fruit_rknn.py \
    --pt fruits_yolo11s.pt \
    --calib-dir calib_640 \
    --out-dir out \
    --platform rk3568 \
    --dtypes i8,fp
```

产出：

```text
out/fruits_yolo11s_i8.rknn    # INT8，部署用
out/fruits_yolo11s_fp.rknn    # FP，精度对照用
out/report.json               # 结构 + 运行时输出自检
```

脚本会自动做三件事：用 ultralytics 从 `.pt` 重导出标准 ONNX、
校验输出层是否符合板端要求、转换后加载模型确认输出层数。

## 3. 为什么必须重导出 ONNX（关键）

随模型下载的 ONNX 是为 OAK/Luxonis 导出的，**只有 3 个输出**
（`output1_yolov6r2` / `output2_yolov6r2` / `output3_yolov6r2`，每个尺度一个融合张量）。

板端 `yolo11_infer.py` 的后处理是：

```python
pair = len(outputs) // 3
for i in range(3):
    boxes.append(flatten(box_process(outputs[pair * i])))
    probs.append(flatten(outputs[pair * i + 1]))   # outputs[3] -> IndexError
```

它要求的是 RKNN 转换后的**九输出**布局（每个尺度一组
`box_dfl / class_scores / score_sum`）。喂 3 输出的模型会直接 IndexError。

从 `.pt` 用 ultralytics 重导出会得到标准的单输出 ONNX
（`[1, 4+nc, 8400]`），RKNN 再把它拆成 9 个输出——正好是板端已经在处理的形式，
所以**板端代码不需要改**，只需把 `CLASSES` 换成 3 类水果。

## 4. 转换后的部署步骤（板端）

**已定方案：并列新服务**，不动 8088 的 COCO 视觉服务（融合服务仍依赖它检测 person）。
水果识别独立跑在 **8089**，`rk3568-fruit.service` 已部署并 active。

```bash
# 1) 上传（本机执行）
pscp out/fruits_yolo11s_i8.rknn linaro@<BOARD_IP>:/home/linaro/ai/models/

# 2) 等 2 秒，服务会自动重试加载，无需重启
curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/status
#    看到 "model_loaded": true 即成功

# 3) 看检测结果
curl -s --noproxy '*' http://127.0.0.1:8089/api/fruit/result
```

关键设计：水果服务**不打开摄像头**，复用视觉服务的
`http://127.0.0.1:8088/raw.jpg`（UVC 只能有一个消费者），所以与融合服务并存无冲突。

接口：

| 路径 | 说明 |
|---|---|
| `GET /api/fruit/status` | 健康、模型信息、计数器、取帧状态 |
| `GET /api/fruit/result` | 检测框 + 置信度 + 时延 |
| `GET /` | 简易网页 |

可用 `--model / --port / --classes / --conf / --nms / --raw-url` 覆盖，便于临时验证。

### 已验证：整条链路已打通

在等 `.rknn` 期间，用现有 COCO 模型在临时端口 8099 干跑过同一个服务，
证明**除权重外全部就绪**：

```text
model_loaded : true
inference_ms : 121.25   pipeline_ms : 213.12   loop_fps : 4.69
detections   : 4 条（含 confidence / box）
infer_errors : 0        fetch_errors : 0
```

即 取帧 → letterbox → 9 输出 DFL 解码 → NMS → JSON 契约 全部验证通过。
`.rknn` 一到位只需换模型文件，代码无需改动。

## 5. 需要留意的取舍

- ~~**换模型 = 丢 COCO 能力**~~ —— **已解决**：采用并列服务方案。COCO 模型留在 8088
  供融合服务检测 person，水果模型独立跑 8089，两者互不影响，融合服务不受影响。
- **训练集只有 160 张**。这个模型是能用的起点，但类别仅 3 种且样本偏少。
  若要更丰富的品类（香蕉、葡萄、橙子、苹果等）或更高精度，
  需要用更大数据集重训——本包里的 160 张图 + 标注可直接作为起点扩充。
- 数据集与模型均为 CC-BY-4.0，注意保留署名。

## 6. 当前状态

| 项 | 状态 |
|---|---|
| 权重 / 数据集 / 标定集 | 已就绪 |
| 转换脚本 + 一键包 | 已就绪（`bash -n` 校验通过，包完整性已校验） |
| 板端服务 `rk3568-fruit`（8089） | 已部署、active、开机自启 |
| 服务整条链路（除权重） | 已用 COCO 模型干跑验证通过 |
| `.rknn` 本体 | **未生成** —— 卡在转换机口令 |

阻塞原因：RKNN 转换必须跑在 Linux x86_64（板端 aarch64 无 toolkit wheel、
本机 WSL 被安全策略拦截、Docker 未安装）。文档记录的转换机
`zwb@192.168.190.160` 在线（Ubuntu 22.04），但给定口令被拒——
且 **SSH（22）与 FTP（21，vsFTPd 3.0.5）两套独立认证栈都拒绝**，
说明是口令本身不对，不是 SSH 配置问题。

**解除方式（二选一）**：

1. 提供正确的转换机用户名/口令；
2. 用户自己把 `dist/fruit_rknn_vm_bundle.zip` 拷进 VM，跑
   `bash vm_build_fruit_rknn.sh`，再把产出的 `fruits_yolo11s_i8.rknn`
   放到板端 `/home/linaro/ai/models/`。

两条路的后续都一样：服务 2 秒内自动加载模型，无需重启。
