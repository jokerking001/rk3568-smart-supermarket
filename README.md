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
| AI 助理 | ESP32-S3（ESP-IDF） | `firmware/mimiclaw/` | LLM Agent、商家/顾客双角色、WebSocket 18789、人工服务告警 18791 |

> `firmware/mimiclaw/` 是开源项目 [memovai/mimiclaw](https://github.com/memovai/mimiclaw)（MIT）的改造副本，
> 版权归原作者。改了什么、怎么编译、密钥怎么配，见该目录的 `MODIFICATIONS.md`。

**为什么保留 ESP32-S3？** HX711 是位翻转（bit-bang）时序、I2S 麦克风/喇叭需要硬实时 DMA 和 GPIO 中断——这些在 Linux 上要么做不稳，要么要额外写内核驱动。放到 RK3568 上收益极低、风险极高。所以"换主控"换的是**决策与业务逻辑**，不是把硬件时序也搬过去。

---

## 2. 仓库结构

```
.
├── firmware/                       ESP32 侧固件
│   ├── store-controller/           ESP32-S3 Arduino 主控（原 Work7_20）
│   ├── vision-node/                XIAO ESP32-S3 Sense 视觉节点
│   ├── thermal-printer/            ESP32-S3 ESP-IDF 热敏打印机（原 re_min）
│   └── mimiclaw/                   ESP32-S3 ESP-IDF AI 助理（上游 memovai/mimiclaw 的改造副本）
│
├── rk3568/                         RK3568 板端
│   ├── store-backend/              8094 商品/购物车/订单 + 8095 扫码 + 8096 OCR
│   │   ├── store_ext.py            8094 扩展层：会员/RFID/审批/打印队列/顾客端
│   │   ├── store_ext_routes.py     8094 扩展层 80 条路由 + 权限分级
│   │   ├── qr_svg.py               /qr-svg 内联二维码（零依赖）
│   │   └── web/                    原工程 10 个页面的逐字节副本
│   ├── fruit-model/                8089 水果识别（YOLO11 + RKNN）
│   ├── fruit-fusion/               8099 视觉 × 重量融合判定  ← 本次新增
│   ├── fruit-train/                8 类水果模型训练 + RKNN 转换流水线
│   ├── services/                   8088 视觉 / 8090 融合 / 8091 雷达 / 8093 数据集采集
│   └── vlm/                        8092 视觉大模型代理（mock 可跑）
│
├── docs/                           迁移总览、交接文档、开工说明
├── tools/                          守卫与核对小工具（凭据 / py37 / 部署漂移）
└── .gitignore
```

---

## 3. 三步上手

### 3.1 克隆

仓库是**公开**的，直接拉就行，不需要账号、不需要邀请：

```bash
git clone https://github.com/jokerking001/rk3568-smart-supermarket.git
cd rk3568-smart-supermarket
```

**给队友的一页速查**（第一次上手，四条命令）：

```bash
git clone https://github.com/jokerking001/rk3568-smart-supermarket.git
cd rk3568-smart-supermarket
bash tools/install-hooks.sh                      # 装提交前守卫（每人一次，钩子不进版本库）
# 配密钥，见 3.2 —— 不配的话固件编不过，这是最常见的一个坑
```

> **不需要 fork，不需要邀请。** 只想看代码 / 跑 RK3568 那部分的话，clone 完就能用。
> 只有要往仓库里推代码，才需要被加成 collaborator（见下）。
>
> 如果 `github.com` 连不上（国内很常见）：挂代理，或改用
> `https://ghproxy.net/https://github.com/...` 这类镜像前缀拉取。

> 想改成 SSH 也可以（配一次就不用输凭据）：
>
> ```bash
> ssh-keygen -t ed25519 -C "你的邮箱"        # 一路回车即可
> cat ~/.ssh/id_ed25519.pub                  # 复制这行内容
> ```
>
> 粘到 GitHub → Settings → SSH and GPG keys → New SSH key，然后把远端地址换成
> `git@github.com:jokerking001/rk3568-smart-supermarket.git`。
>
> ⚠️ **拉取不需要鉴权，但推送需要。** 你要往仓库里提交，得先让管理员把你加成
> collaborator（仓库页 → Settings → Collaborators → Add people），**接受邮件邀请之后**
> 才能 push。之后配一次凭据：
>
> ```bash
> # Windows：装 Git for Windows 后自带
> git config --global credential.helper manager
> # macOS
> git config --global credential.helper osxkeychain
> ```
>
> 再 `git push` 时，Username 填 GitHub 用户名，Password 粘 **PAT**
> （Settings → Developer settings → Personal access tokens，勾 `repo` 权限）——
> GitHub 已经不能用账号登录密码推送了。**别把 token 写进 URL 后提交到任何地方。**

> 拉下来先跑一次 `bash tools/install-hooks.sh`（见第 6 节），之后提交会自动过守卫。

### 3.2 配 `secrets.h`（必做，否则编不过）

所有密钥、WiFi、内网地址都从源码里抽出来了，只在本地存在：

```bash
# 在仓库根目录执行（四条命令，逐个照抄即可）
cp firmware/store-controller/secrets.h.example      firmware/store-controller/secrets.h
cp firmware/vision-node/secrets.h.example           firmware/vision-node/secrets.h
cp firmware/thermal-printer/main/secrets.h.example  firmware/thermal-printer/main/secrets.h
cp firmware/mimiclaw/main/mimi_secrets.h.example    firmware/mimiclaw/main/mimi_secrets.h
```

然后打开这四个文件，把占位值换成真实值。**它们都已被 `.gitignore` 忽略，不会进版本库**
（前三个由根目录 `.gitignore` 的 `secrets.h` 拦住，`mimi_secrets.h` 由
`firmware/mimiclaw/.gitignore` 拦住 —— 名字不同，别以为漏了）。

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
| mimiclaw | `MIMI_SECRET_WIFI_SSID` / `MIMI_SECRET_WIFI_PASS` | 同门店路由器 |
| mimiclaw | `MIMI_SECRET_API_KEY` / `MIMI_SECRET_MODEL` / `MIMI_SECRET_MODEL_PROVIDER` | LLM 供应商与密钥 |
| mimiclaw | `MIMI_SECRET_STORE_URL` | 指向主控，**RK3568 迁移后要改** |
| mimiclaw | `MIMI_SECRET_TG_TOKEN` / `FEISHU_*` / `SEARCH_KEY` / `TAVILY_KEY` | 不用就留空 |

> `firmware/mimiclaw/main/mimi_secrets.h.example` 里带注释说明每个宏的用途，
> 照着填即可。**MimiClaw 原本是独立上游项目，上游地址与本地改动见
> `firmware/mimiclaw/MODIFICATIONS.md`。**

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

**一个模块一个部署脚本**，别手工 scp。手工漏一步（漏传 `web/`、漏装 unit、漏重启）
不会报错，只会让某个端口静默不工作 —— 8099 就是这么一直没起来的。

```bash
BOARD=<板子IP> bash rk3568/store-backend/deploy_to_board.sh   # 8094 + 8095 + 8096
BOARD=<板子IP> bash rk3568/fruit-fusion/deploy_to_board.sh    # 8099
BOARD=<板子IP> bash rk3568/vlm/deploy_to_board.sh             # 8092

# 一键验收：健康检查 + 部署一致性 + 598 项回归
python tools/board_acceptance.py --board <板子IP>
```

板端 IP 跟着热点变（热点一换就变），所以**别依赖脚本里的默认值**，一律显式传 `BOARD=`。

> `fruit-fusion/deploy_to_board.sh` 里有一道闸门：模型文件
> `/home/linaro/ai/models/fruit8_yolo11n_i8.rknn` 不在就**不重启 8089**。
> 因为重启了它只会变成 `model_loaded: false` —— 服务是活的却不干活，比不重启更难查。

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
| 8094 | 门店后端 | 商品 / 购物车 / 订单 + **原工程那套完整页面**（会员/审批/顾客端） |
| 8095 | 扫码枪 | 条码解析 |
| 8096 | OCR | 票据识别 |
| **8099** | **水果 视觉 × 重量 融合** | **本次新增** |

> 8090 和 8099 都叫"融合"，但完全无关。8090 是雷达背景变化 + 视觉人形判断"有没有顾客在"，8099 才是"这个水果是什么、多重、收不收"。

### 4.1 8094 的扩展层（补完原工程 52 个缺口）

`store_service.py` 只管收银主链路（商品/购物车/订单/打印），原工程另外那 52 个端点
（管理员登录、会员、RFID、临期改价与补货审批、打印光栅队列、收银台页、移动支付、
报表/大屏/AI 分析、服务工单、顾客端 PWA、顾客分析、店铺配置）由扩展层补齐：

- `store_ext.py` —— 模型层，和 `store_service.py` **共用同一个 sqlite 连接和锁**
- `store_ext_routes.py` —— HTTP 层，80 条路由，挂在 `StoreHandler` 前面先问
- `web/` —— 原工程 10 个页面的**逐字节副本**（`tools/extract_legacy_web.py` 生成，可 `--check` 复核）

**三个必须知道的约定**（踩了会静默出错，不会报错）：

1. **它是静默降级的**。`store_ext.py` 导入失败时 `store_service.py` 照常启动，
   只是原工程页面全 404、`/` 退回内建看板。启动日志里那行
   `扩展层已挂载：80 条路由` 就是判断依据。设 `STORE_EXT=0` 可显式关掉。
2. **`/api/order-history` 返回的是裸 JSON 数组**，而且**旧的在前**。
   页面 `orders = await or.json()` 之后直接 `orders.forEach(...)`；包一层对象或者顺序反了，
   页面会**静默**变成订单列表空白 + 营收 ¥0.00。
3. **`POST /api/admin/refund` 的 `orderId` 是上面那个数组的下标**，不是订单主键
   （原工程 `orders[orderId]` 的语义，页面 `refundOrder(i)` 就是这么传的）。
   想按真实主键退，用 `order_id` 参数。

认证也和原工程不同：原工程只看 `admin_auth=1` 这个 cookie，**谁手设一个就是管理员**。
现在换成了服务端 `auth_sessions` 真会话 + `HttpOnly` cookie，页面一行没改。

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

### 6.1 板端 Python 3.7 守卫

板子是 Debian 10 + **Python 3.7.3**，开发机是 3.10+。`list[int]`、`s.removeprefix()`、`a | b` 这类写法在电脑上跑得好好的，**上板才炸** —— 本地测不出来，所以单独做了个守卫：

```bash
python tools/check_py37.py            # 扫 rk3568/ 下的板端代码
python tools/check_py37.py --all      # 连 tools/ 一起扫
python tools/check_py37.py <路径...>   # 只扫指定文件/目录
```

两层检查：

| 层 | 手段 | 抓什么 |
|---|---|---|
| 语法 | `ast.parse(feature_version=(3,7))` | 海象 `:=`、`match`、位置限定参数 `/`、f-string 的 `=` 说明符 |
| 语义 | 走 AST | PEP 585 泛型下标、`removeprefix/removesuffix`、`math.lcm` 等、能静态确定的 dict 合并 |

> 语义层**刻意不用正则**——正则会命中注释和文档字符串里提到的 `list[int]`（第一版就是这么误报的）。
>
> **已知漏报（宁漏不误）**：`a | b` 两个变量无法静态区分字典合并还是集合求并，因此不报。写代码时自己留意。

守卫本身也有自测，同时验证"该抓的抓到"和"不该报的不报"：

```bash
python tools/test_check_py37.py
```

上面 `install-hooks.sh` 装的 pre-commit 钩子已经把这个守卫接进去了——每次提交会对**暂存的 `.py`** 跑一遍。

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
- ✅ **原工程 81 个接口中那 52 个缺口已全部补齐**（`store_ext.py` + `store_ext_routes.py`
  + 原工程 10 个页面的逐字节副本），8094 的回归从 38 项扩到 **598 项**，全绿
- ✅ 顺手堵掉原工程的认证漏洞（`admin_auth=1` 谁设谁是管理员）

**待办**

- ⬜ 板端 8088 / 8094 的本地代码与板上实际运行版本一致性**尚未核对**——这是上板第一件事
- ⬜ 8 类模型训练与 RKNN 转换（卡在转换机口令）
- ⬜ 打印链路选型：384 点光栅 vs ESC/POS + GBK
- ⬜ `top1` 与 `normalize` 两种概率模式二选一
- ⬜ ESP32-S3 从机固件（HX711 称重 + I2S 语音 + WS2812 + DHT22 + 按键）——
  缺口表里剩下的都是固件侧的，不归 8094 管
- ⬜ SKU 识别流水线骨架（标注工具 / 训练脚本 / INT8 校准 / `deploy_model.sh` 替换流程）

**明确不做**

- ❌ 板端本地部署视觉大模型（NPU 只有 ~0.8 TOPS、内存 4GB，跑不动）
- ❌ 把 HX711 / I2S 搬到 RK3568（实时时序，留在 MCU）

**关于 `firmware/mimiclaw`**：它源自开源项目 [`memovai/mimiclaw`](https://github.com/memovai/mimiclaw)
（MIT License, Copyright (c) 2026 Ziboyan Wang），**已纳入本仓库**，是四个组成部分之一。
上游归属、本地改动清单、编译方法、密钥说明见 `firmware/mimiclaw/MODIFICATIONS.md`。
**`LICENSE` 必须保留，不要删。**

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

- 板端 Python 代码必须兼容 **Python 3.7.3**：不能用海象运算符、字典合并、`list[int]` 这类 3.9+ 语法。改完跑 `python tools/check_py37.py` 确认。
- 提交前自测（在仓库根目录跑）：

  ```bash
  cd rk3568/fruit-fusion
  python test_fruit_fusion.py
  python test_vision_observer.py
  python test_fruit_fusion_service.py
  python test_fruit_fusion_bridge.py
  cd ../..
  python tools/check_literals.py          # 凭据残留
  python tools/check_py37.py              # 板端 3.7 兼容
  python tools/test_check_py37.py         # 上面那个守卫自己的自测
  python tools/test_check_board_sync.py   # 部署漂移核对工具的自测
  python tools/test_board_acceptance.py   # 板端验收脚本的自测（不需要板子）
  python tools/test_stress_test.py        # 压测脚本的自测（不需要板子）
  python tools/test_calibrate_int8.py     # INT8 校准工具的自测（不需要板子）
  ```

  收银后端这边分两种：

  ```bash
  cd rk3568/store-backend

  # 不需要起服务，纯进程内自测（共 521 项）
  python test_store_ext.py     # 227 项：扩展层模型 + HTTP 层
  python test_qr_svg.py        # 294 项：二维码编码器

  # 打 HTTP 接口的端到端回归（38 项），**要先把服务跑起来**
  python store_service.py --port 8094 --db /tmp/store.db --receipt-dir /tmp/receipts &
  python test_ocr_parse.py && python test_scanner_decode.py
  python test_store_e2e.py --base http://127.0.0.1:8094
  ```

  板端回归基线合计 **598 项**（store 38 + scanner 20 + ocr 19 + 扩展层 227 + 二维码 294）。

  > `test_store_e2e.py` 的 `--base` 既能打本地也能打板端
  > （`--base http://<板端IP>:8094`），所以它同时也是端到端验收脚本。

---

## 10. 日常协作

主线分支就是 `main`，**没有多余的分支仪式**——这套工程是几个人分头改不同模块，撞车概率低。

**开工前**

```bash
git pull --rebase        # 用 rebase 而不是 merge，历史干净
```

**收工前**

```bash
git add -A
git commit -m "模块: 干了什么"
git pull --rebase        # 先拉再推，避免非快进被拒
git push
```

**几条硬规矩**

| 规矩 | 为什么 |
|---|---|
| **`secrets.h` 永不提交** | 已在 `.gitignore` 里。谁把密钥推上去，就得去控制台作废重发 |
| 改完板端 `.py` 跑一次 `check_py37.py` | 本地能跑 ≠ 板上能跑，板子是 3.7.3 |
| 提交前跑 `bash tools/install-hooks.sh`（每人一次） | 装完这两道守卫自动跑，不用记 |
| 二进制模型（`.pt/.onnx/.rknn`）不进仓库 | 已在 `.gitignore`。走 `artifacts/` 或另外传 |
| 冲突别硬推 | `git pull --rebase` 冲突就手动解，解完 `git rebase --continue`。**不要 `push -f`** |

> 万一 `git push` 被拒说 non-fast-forward，**不要 `-f`**，先 `git pull --rebase` 再看。

---

## 11. 部署漂移核对（本地 vs 板端）

这个项目**出过「本地改了但没部署」的事故**——本地跑得好好的，板上还是旧版，排查半天才发现
服务里跑的是上一个版本。手工 `scp` 没有版本概念，只能靠比对哈希兜。

板子在手边时，一条命令核对全部部署文件：

```bash
python tools/check_board_sync.py --board <板端IP>
```

输出逐项标状态：

| 标记 | 含义 | 怎么办 |
|---|---|---|
| `OK` | 本地与板端哈希一致 | 不用管 |
| `不一致` | **本地改了没部署** | 跑对应模块的部署脚本 |
| `板端缺失` | 没部署过，或路径变了 | 检查板端路径 / 跑部署脚本 |
| `本地缺失` | 映射表过期 | 修 `tools/board_sync_manifest.json` |

不连板子也能用：

```bash
python tools/check_board_sync.py --manifest            # 只打印「本地文件 → 板端路径」映射表
python tools/check_board_sync.py --local-root <目录>    # 拿本地目录假装板端（离线核对）
python tools/test_check_board_sync.py                  # 工具自测（造假板端，验能抓漂移）
```

映射表在 `tools/board_sync_manifest.json`。**里面的板端路径不是猜的**——来源是各模块
systemd unit 的 `WorkingDirectory` / `ExecStart`，以及部署脚本里实际的 `scp` 目标。
改了部署路径记得同步改映射表。清单里标 `required: false` 的（测试脚本等）只提示，不影响退出码。

> ⚠️ 有个坑它会主动提醒：**本地文件带 CRLF 行尾**时，`scp` 上去的行尾和 Linux 版不同，
> 哈希必然不一致。看到这个警告先跑 `git config core.autocrlf false` 重新检出，
> 别当成"没部署"白查半天。
>
> 另外，部署到板端的是**工作区文件**（`scp` 直接传），所以核对比的是工作区，
> 不是 git 里的 blob。

---

## 12. 上板第一天：三条命令

板子插电、接上同一个网之后，按顺序跑这三条。**前一条不绿就别往下走。**

```bash
# 1) 一键验收 —— 服务健康 + 部署一致性 + 598 项回归，一次跑完
python tools/board_acceptance.py --board <板端IP>

# 2) 30 分钟持续压测 —— 温度 / FPS / 雷达掉线率 / NPU 错误
python tools/stress_test_30min.py --board <板端IP>

# 3) INT8 校准（模型要换的时候才需要）
python tools/calibrate_int8.py collect --board <板端IP> --count 200
```

### 12.1 一键验收 `board_acceptance.py`

把「上板要做的三件事」合成一条命令：

| 阶段 | 做什么 | 失败意味着 |
|---|---|---|
| 0 | 先探 `:8094` 通不通 | 板子没上电 / 不在同一网段 / IP 变了 —— 立刻收工，不干等 |
| 1 | 十个服务健康检查（8088–8096 + 8099，并发探） | 某个服务没起来，去看对应 systemd unit |
| 2 | 本地 vs 板端哈希核对 | **本地改了没部署** —— 跑对应模块的部署脚本 |
| 3 | 598 项回归（板端执行） | 板端代码有问题 |

```bash
python tools/board_acceptance.py --board 192.168.43.44                 # 全跑
python tools/board_acceptance.py --board 192.168.43.44 --skip-regression   # 只看健康+一致性，快
python tools/board_acceptance.py --board 192.168.43.44 --skip-sync         # 没配 SSH 时
python tools/board_acceptance.py --local                                   # 在板子本机上跑
```

它**显式禁用代理**——本机 shell 常继承 `http_proxy`，不关掉的话连 `192.168.x.x`
会被绕出去超时，看起来像"板子不在线"。它还区分「端口拒绝」（服务没起来）
和「超时」（网络不通），省得查错方向。

### 12.2 压测 `stress_test_30min.py`

只打**只读、幂等**的查询接口——压测绝不能往业务里写数据，不然 30 分钟后库存和订单全乱。

判定用**三态**：`pass` / `fail` / `unknown`。

> ⚠️ **`unknown` 不算通过。** 没测到数据（比如 SSH 不通拿不到温度）时会明确报
> 「没测到」，而不是给你一个假绿。压测脚本报绿却什么都没测到，比不跑更危险。

阈值可覆盖：

```bash
python tools/stress_test_30min.py --board <IP> \
  --success-rate 99 --vision-p95 500 --temp-max 85 --radar-drop 5
```

报告落在 `artifacts/stress_<时间戳>.{json,txt}`——JSON 是原始采样，可以拿来画温度曲线。

### 12.3 INT8 校准 `calibrate_int8.py`

**校准集和验证集的要求不一样，别混：**

| | 从哪来 | 能不能有重复帧 |
|---|---|---|
| **校准集** | 板端 8088 `/raw.jpg`（**原始帧，不带检测框**），必须在真机位真光照下抓 | **可以**——校准只要激活值范围 |
| **验证集** | 带标注的数据集，**按采集会话切分** | **不可以**——相邻帧几乎一样，随机拆会让精度虚高 |

用带检测框的图（`/fruit.jpg`）做校准是错的——框是推理产物，会把输入分布带偏。

```bash
python tools/calibrate_int8.py collect --board <IP> --count 200 --interval 1.0
python tools/calibrate_int8.py pack --onnx <模型.onnx> --calib calib --out dist/calib_bundle
# 拷到转换机：tar czf calib_bundle.tar.gz -C dist/calib_bundle .
# 转换机上：  bash vm_calibrate_int8.sh
python tools/calibrate_int8.py compare --result result.json
```

`pack` 产出自包含包（校准图 + onnx + 转换脚本 + 元数据），**全程可离线**。
生成的 shell 脚本已校验是 LF——CRLF 到转换机会报 `/bin/bash^M`。

> Windows 上 `chmod` 设不了 POSIX 执行位，所以调用一律写
> `bash vm_calibrate_int8.sh`，别用 `./vm_calibrate_int8.sh`。
