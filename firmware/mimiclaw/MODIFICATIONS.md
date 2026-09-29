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
| `MIMI_SECRET_STORE_URL` | 门店主控地址，例如 `http://192.168.43.44` |
| `MIMI_SECRET_STORE_API_KEY` | 管理接口鉴权头，**要和主控 `secrets.h` 里的值一致** |
| `MIMI_SECRET_STATIC_IP` / `_NETMASK` / `_GATEWAY` | 可选，固定 IP |
| `MIMI_SECRET_TG_TOKEN`、`MIMI_SECRET_SEARCH_KEY`、`MIMI_SECRET_TAVILY_KEY` | 上游原有的可选项 |

## 隧道配置

`cloudflared-admin.yml.example` 是外网访问的配置模板（把内网服务映射到公网域名）。
**真实配置不入库** —— 里面有隧道 UUID 和凭据文件路径，另存为 `cloudflared-admin.yml` 自己填。

## 没进这个副本的东西

上游仓库之外、但不属于本项目的目录：`android-app/`、`usb_host_uvc_probe_idf/`、
`build/`、`tmp/`、`managed_components/`（依赖，`idf.py` 会自己拉）。
