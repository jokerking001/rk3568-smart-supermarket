# RK3568 智慧超市 · 视觉大模型（VLM）识图问答

在原有工程（视觉 8088 / 雷达 8091 / 融合 8090 / 采集 8093 / 收银 8094 / 扫码枪 8095 /
OCR 8096 / 水果 8089）之上，新增一条**「摄像头拍图 → 视觉大模型用中文回答」**的链路。

模型跑在电脑上，板子只负责拍图和展示。

---

## 1. 为什么模型不放在板子上

| 资源 | RK3568 实测 | 结论 |
|---|---|---|
| 可用内存 | 约 2.86 GB | 装不下可用的视觉语言模型 |
| 根分区可用 | 约 1.4 GB | 连量化权重都放不下 |
| NPU | 0.8 TOPS，只支持 RKNN | RKNN 生态没有 VLM 运行时 |
| SoC 温度 | 已约 62 °C（空载 8 服务） | 再加大模型会持续高温 |

电脑侧（RTX 4060 Laptop 8 GB + 16 GB 内存）跑 **Qwen2.5-VL 3B** 量化版刚好合适。

---

## 2. 架构

```text
┌──────────────── 板子 10.181.229.215 ────────────────┐
│  UVC 摄像头 → rk3568-vision (8088) /raw.jpg          │
│                     ↑ 本地取帧，不开第二个摄像头消费者  │
│  rk3568-vlm (8092)  ── HTTP ──┐                      │
│     网页 /api/vlm/ask         │                      │
└───────────────────────────────┼──────────────────────┘
                                │  {question, board_ip}
                                ▼
┌──────────────── 电脑 10.181.229.84 ──────────────────┐
│  vlm_service.py (8097)                               │
│    1. 反向来板子拉 /raw.jpg（板端不传 base64，省 33%）│
│    2. 交给视觉大模型（Ollama 本地 / OpenAI 兼容云端） │
│    3. 返回中文回答                                    │
└──────────────────────────────────────────────────────┘
```

设计要点：

- **图片由电脑侧主动来板子拉**，板端只发问题。热点链路带宽很紧张，少传一份 base64。
- **板端不复用摄像头**：只读 `127.0.0.1:8088/raw.jpg`，与融合服务、水果服务一致，
  不会出现「UVC 只能有一个消费者」的抢帧问题。
- **后端可切换**：本地 Ollama（离线）或任意 OpenAI 兼容多模态接口（云端，零下载）。
- 价格/库存/订单仍然只由 8094 的 SQLite 提供，**不让大模型猜**。

---

## 3. 文件

本目录（`rk3568/vlm/`）：

```text
vlm_service.py            电脑侧 VLM 服务（多后端 + 自带网页）
vlm_config.json           电脑侧配置
board/vlm_client_service.py  板端识图问答服务（Python 3.7 兼容）
board/rk3568-vlm.service     板端 systemd unit
deploy_to_board.sh           一键部署到板端
test_vlm_plumbing.py         管线自测（不需要真实模型）
mock_vlm_backend.py          假 VLM 后端，配合上面的自测用
README.md                    本文档
```

板端：

```text
/home/linaro/ai/vlm/vlm_client_service.py
/home/linaro/ai/vlm/vlm_client_config.json
/etc/systemd/system/rk3568-vlm.service
```

大文件（因为 C/F 盘空间紧张，单独放 E 盘）：

```text
E:\rk3568-vlm\OllamaSetup.exe     Ollama 安装包（1.57 GB）
E:\Ollama\                        Ollama 安装目录
E:\OllamaModels\                  模型目录（OLLAMA_MODELS）
```

---

## 4. 电脑侧

### 4.1 磁盘空间（重要）

实测：`C:` 仅剩 **520 MB**、`F:` 仅剩 **5.5 GB**、`E:` 剩 **44 GB**。
Ollama 安装包 1.57 GB + 模型约 3.2 GB，**只能放 E 盘**，否则装到一半会失败。

### 4.2 安装 Ollama

```powershell
# 1) 下载（走代理，直连很慢）
cd E:\rk3568-vlm
curl -L --proxy http://127.0.0.1:58829 -C - -o OllamaSetup.exe `
  https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe

# 2) 静默安装到 E 盘（默认会装到 C 盘，务必指定 /DIR）
.\OllamaSetup.exe /VERYSILENT /DIR="E:\Ollama"
```

模型目录固定在 E 盘（写入用户环境变量，重开终端生效）：

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "E:\OllamaModels", "User")
```

### 4.3 拉模型

```powershell
ollama pull qwen2.5vl:3b        # 约 3.2 GB
ollama list
ollama run qwen2.5vl:3b "描述这张图片" --image E:\some.jpg   # 快速验证
```

### 4.4 启动 VLM 服务

```powershell
cd rk3568/vlm
python vlm_service.py --port 8097 --board 10.181.229.215
```

浏览器打开 `http://127.0.0.1:8097/`。自检：

```powershell
python vlm_service.py --check
```

> Windows 防火墙需要放行 8097，否则板子连不上。首次运行会弹窗，选「允许」。

### 4.5 换后端（不想下载 4.7 GB 时）

改 `vlm_config.json`：

```json
{
  "backend": "openai",
  "openai_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "openai_model": "qwen-vl-max",
  "api_key": "sk-xxxxxxxx",
  "proxy": "http://127.0.0.1:58829"
}
```

或运行时覆盖：

```powershell
python vlm_service.py --backend openai --model qwen-vl-max --api-key sk-xxx
```

---

## 5. 板端

```bash
BOARD=10.181.229.215 PC=10.181.229.84 bash deploy_to_board.sh
```

脚本做：建目录 → scp → `py_compile` → 写配置 → 装 unit → `daemon-reload` → `enable` →
`restart` → 打印 `is-active` 与状态接口。**不碰其它已有服务。**

手工验证：

```bash
ssh linaro@10.181.229.215
python3 /home/linaro/ai/vlm/vlm_client_service.py --check
curl -s --noproxy '*' http://127.0.0.1:8092/api/vlm/status
curl -s --noproxy '*' -X POST -H 'Content-Type: application/json' \
  -d '{"question":"画面里有什么？"}' http://127.0.0.1:8092/api/vlm/ask
```

网页：`http://10.181.229.215:8092/`

---

## 6. 自测（不需要真实模型）

沿用主项目「先用现有能力把整条链路跑通」的做法：**模型还没到位时，
用一个假后端验证除权重以外的全部环节**。这样真模型一到位，出问题就必定在权重。

```powershell
# 21 项管线自测：请求形状 / data URI / 鉴权 / 历史 / 统计 / 错误路径
python test_vlm_plumbing.py --service http://127.0.0.1:8097

# 额外测真实板端取帧路径（热点慢，请耐心）
python test_vlm_plumbing.py --service http://127.0.0.1:8097 --board 10.181.229.215
```

**实测结果：21 passed, 0 failed。**

端到端联调（板端 8092 → 电脑 8097 → 假后端，中间取板子真实画面）：

```powershell
# 1) 起假后端
python mock_vlm_backend.py --port 18098
# 2) 电脑侧切到假后端
curl.exe -s --noproxy "*" -X POST -H "Content-Type: application/json" `
  -d '{\"backend\":\"openai\",\"openai_base\":\"http://127.0.0.1:18098/v1\",\"openai_model\":\"mock-vl\",\"api_key\":\"test\"}' `
  http://127.0.0.1:8097/api/vlm/config
# 3) 让板子自己发一次真实提问
curl.exe -s --noproxy "*" -X POST -H "Content-Type: application/json" `
  -d '{\"question\":\"画面里有什么商品？\"}' http://10.181.229.215:8092/api/vlm/ask
```

**实测结果：`ok:true`，`image_bytes: 55337`（板子真实画面 54 KB），端到端 13.3 秒。**
之后记得把后端切回 `ollama`。

---

## 7. 接口

### 电脑侧 `vlm_service.py`（8097）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 识图问答网页（画面 + 提问 + 回答 + 历史） |
| GET | `/health` | 存活探针 |
| GET | `/api/vlm/status` | 后端就绪状态、板端可达性、统计 |
| GET | `/api/vlm/frame?board=<ip>` | 代理板端 `/raw.jpg`（绕过跨域） |
| GET | `/api/vlm/history` | 最近 20 次问答 |
| POST | `/api/vlm/ask` | `{question, board_ip?, image_b64?, source}` |
| POST | `/api/vlm/config` | 热更新 backend / model / api_key / board_ip |

### 板端 `vlm_client_service.py`（8092）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 识图问答网页 |
| GET | `/health` | 存活探针 |
| GET | `/api/vlm/status` | 电脑侧可达性、模型名、统计 |
| GET | `/api/vlm/history` | 最近 20 次问答 |
| POST | `/api/vlm/ask` | `{question}` → 取帧 → 调电脑 → 中文回答 |
| POST | `/api/vlm/config` | `{pc_url}` 等热更新 |

---

## 8. 故障处置

**电脑侧网页「后端不可用」**
Ollama 没起。`ollama serve` 或确认托盘进程在；再 `curl http://127.0.0.1:11434/api/tags`。

**板端 `pc_reachable: false`**
① 电脑 IP 变了（热点会重新分配）→ `POST /api/vlm/config {"pc_url":"http://新IP:8097"}`；
② Windows 防火墙没放行 8097；③ 电脑侧服务没启动。

**第一次提问特别慢**
Ollama 首次要把模型读进显存，30–90 秒正常。之后每次约 2–10 秒。

**板端取帧失败**
先看视觉服务：`curl -s --noproxy '*' http://127.0.0.1:8088/api/vision/result`。
若 8088 在监听但不返回，按主项目文档做 NPU 受控重启。

**回答质量差**
通用 Qwen2.5-VL 不认识超市自有 SKU。需要先做「商品图片采集 → 训练 → `sku_map` 映射」，
这部分见主项目交接文档第 10 节。

---

## 9. 已知限制与待办

- [ ] 板端 IP / 电脑 IP 都会随热点变化，目前靠 `vlm_config.json` 手改；
      后续可让板端在 8092 页面上一键改。
- [ ] 还没有把「识图结果」接进购物车。正确顺序是先用 `/api/admin/sku-map`
      建标签→SKU 映射，再让 `/api/vision/observe` 自动加购。
- [ ] 热点链路带宽有限（实测下载占用时，板↔电脑只有约 2 KB/s）。
      正常空载需复测；若长期偏低，考虑给板子插网线或换 5 GHz。
- [ ] 未做并发保护：两个浏览器同时提问会同时压 Ollama，可能排队。
- [ ] 未接入鉴权。局域网内任何设备都能调用 8097。

---

## 10. 本轮踩到的坑

| 现象 | 根因 | 处理 |
|---|---|---|
| `curl -o /f/...` 报 `Failed to open the file` | 系统 curl（`C:\Windows\system32\curl`）不认 MSYS 的 `/f/`、`/e/` 绝对路径 | `cd` 到目标目录后用相对路径 |
| 装 Ollama 中途失败 | `C:` 仅 520 MB、`F:` 仅 5.5 GB | 安装包与模型全部放 `E:`，安装时加 `/DIR` |
| 板↔电脑传输只有约 2 KB/s | 同一 2.4 GHz 热点被我自己的 1.5 GB 下载占满 | 大文件下载与板端联调不要同时做 |
| `nvidia-smi` 报 `Failed to initialize NVML: Unknown Error` | 待查（设备管理器显示 RTX 4060 状态正常，驱动 32.0.16.1062） | 以 Ollama 启动日志里的 `library=cuda` / `library=cpu` 为准 |
