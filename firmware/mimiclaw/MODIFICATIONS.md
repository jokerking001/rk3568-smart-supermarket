# MimiClaw（本项目副本）

## 来源与许可

这是开源项目 **MimiClaw** 的副本，在上游基础上做了「接入智慧超市」的改造：

- 上游：https://github.com/memovai/mimiclaw
- 许可：**MIT License, Copyright (c) 2026 Ziboyan Wang**（见本目录 `LICENSE`，**请勿删除**）
- 上游文档：`README.md` / `README_CN.md` / `README_JA.md`

MIT 许可允许修改和再分发，条件是保留版权声明和许可全文 —— 所以 `LICENSE` 必须跟着走。

## 本副本做了什么改动

### 新增文件（上游没有）

| 文件 | 作用 |
|---|---|
| `main/tools/tool_store.c` / `.h` | **`store_query` 工具**：让 LLM 直接读门店主控的实时数据 |
| `main/gateway/alert_server.c` / `.h` | **18791 告警服务**：收门店主控的「人工服务」请求，转飞书通知 |

`store_query` 支持的动作（LLM 通过 JSON schema 调用）：

```text
summary  products  alerts  trend  weight  customer_analytics
update_price  service_status  service_update
pricing_review  pricing_status  pricing_apply
restock_review  restock_status  restock_apply
```

> ⚠️ `update_price` 的 schema 里写死了一条约束：**必须先用 `products` 拿到精确条码，
> 再用条码改价，不允许凭模糊商品名改价**。改这块时别把这条去掉。

### 改动文件

| 文件 | 改动 |
|---|---|
| `main/tools/tool_registry.c` | 注册 `store_query` |
| `main/gateway/ws_server.c` | WebSocket（18789）配合门店顾客端 |
| `main/agent/agent_loop.c`、`context_builder.c` | Agent 循环与工具上下文 |
| `main/llm/llm_proxy.c` | LLM 代理调整 |
| `main/memory/session_mgr.c` | 会话管理（长文件名） |
| `main/wifi/wifi_manager.c` | Wi-Fi 接入（含静态 IP） |
| `main/tools/tool_get_time.c` | 时区 `CST-8` |
| `main/mimi_config.h` | 新增 `MIMI_ALERT_PORT`(18791)、`MIMI_OPENAI_API_URL`(DeepSeek)、`MIMI_TIMEZONE` |
| `main/mimi_secrets.h.example` | 新增 `MIMI_SECRET_STORE_URL` 等占位 |
| `main/mimi.c`、`main/CMakeLists.txt` | 接入上面的新模块 |
| `partitions.csv`、`sdkconfig.defaults.esp32s3` | 分区与编译选项（代码变大，分区要跟着调） |

## 编译

标准 ESP-IDF 工程：

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p <PORT> flash monitor
```

## 密钥

`main/mimi_secrets.h` **不在版本库里**（已在 `.gitignore`）。照着模板建：

```bash
cd firmware/mimiclaw/main
cp mimi_secrets.h.example mimi_secrets.h
```

然后填这些：

| 宏 | 说明 |
|---|---|
| `MIMI_SECRET_WIFI_SSID` / `MIMI_SECRET_WIFI_PASSWORD` | 门店 Wi-Fi |
| `MIMI_SECRET_API_KEY` | LLM API key（默认走 DeepSeek） |
| `MIMI_SECRET_STORE_URL` | 门店主控地址，例如 `http://10.176.240.215:8094` |
| `MIMI_SECRET_STORE_API_KEY` | 管理接口鉴权头，**要和主控 `secrets.h` 里的值一致** |
| `MIMI_SECRET_STATIC_IP` / `_NETMASK` / `_GATEWAY` | 可选，固定 IP |
| `MIMI_SECRET_TG_TOKEN`、`MIMI_SECRET_SEARCH_KEY`、`MIMI_SECRET_TAVILY_KEY` | 上游原有的可选项 |

### 飞书通道：走 WebSocket 长连接，**不是** 18790 webhook

上游文档 `docs/im-integration/FEISHU_SETUP.md` 描述的是「ESP32 开 18790 收飞书
事件回调」的 webhook 方案，但**当前代码不是这么实现的**：

- `main/channels/feishu/feishu_bot.c` 的 `feishu_bot_start()` 起的是 `feishu_ws_task`：
  先拉 `https://open.feishu.cn/callback/ws/endpoint` 拿长连接地址，再用
  `esp_websocket_client` 建立 **WebSocket 长连接**收事件。
- `MIMI_FEISHU_WEBHOOK_PORT`(18790) / `MIMI_FEISHU_WEBHOOK_PATH` 在 `mimi_config.h`
  里仍有定义，但**全工程没有任何 `.c` 使用它们** —— HTTP webhook server 并未实现。

**所以「18790 不监听」是正常的，不是故障。** 2026-10-11 实测：
18789（顾客端 WS）与 18791（告警）在听，18790 `ConnectionRefused`。

凭据配好即可用（见上表 `MIMI_SECRET_FEISHU_APP_ID` / `_APP_SECRET`）；
`MIMI_ALERT_FEISHU_CHAT_ID` 是 18791 告警要发到哪个会话。

⚠️ `main/channels/feishu/feishu_bot.h` 第 12 行的注释仍写
"Listens on MIMI_FEISHU_WEBHOOK_PORT" —— 是**过时注释**，别被它带偏。

### ⚠️ 已知安全风险：配置页 `/config` 无鉴权回**明文密钥**

`main/onboard/wifi_onboard.c` 的 `http_get_config()` 把**明文**的 Wi-Fi 密码、
LLM API key、飞书 App Secret、TG token 直接以 JSON 返回，而 `/config`(GET)
与 `/save`(POST) **都没有任何鉴权**。HTTP 服务在 `MIMI_ONBOARD_HTTP_PORT`(80)，
只要设备在网，同网段任何人 `curl http://<IP>/config` 就能拿到全部凭据；
`POST /save` 还能改配置。

**2026-10-11 实测确认**：`curl http://10.176.240.100/config` 直接返回明文。

修复方向（三选一，都需改固件 + 重新编译烧录）：

1. **最小改动**：`http_get_config` 对密钥类字段只回「是否已配置」（或 `cli_****`
   掩码）。⚠️ 必须**三处一起改** —— `onboard_html.h` 的表单回填要跳过掩码值、
   `nvs_sync_field` 遇掩码不写。**只改一处会让「点保存」把掩码当成真值写进 NVS，
   把配置弄坏。**
2. 给 `/config`、`/save` 加一次性口令（HTTP Basic 或 `?token=`），口令存 NVS。
3. 只在 **AP 配网模式**（未连上路由器时）开放 `/config`，STA 模式下直接 404。

未修之前：**不要把 MimiClaw 暴露到公网**，也不要在不信任的局域网里开机。

## 隧道配置

`cloudflared-admin.yml.example` 是外网访问的配置模板（把内网服务映射到公网域名）。
**真实配置不入库** —— 里面有隧道 UUID 和凭据文件路径，另存为 `cloudflared-admin.yml` 自己填。

## 没进这个副本的东西

上游仓库之外、但不属于本项目的目录：`android-app/`、`usb_host_uvc_probe_idf/`、
`build/`、`tmp/`、`managed_components/`（依赖，`idf.py` 会自己拉）。
