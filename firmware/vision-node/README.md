# XIAO ESP32S3 Sense 水果视觉与重量融合

模型标签：`apple`、`banana`、`grapes`。摄像头使用 XIAO ESP32S3 Sense 板载 OV3660。

HX711 保持在 `Work7_20` 主程序上，XIAO 不连接也不读取 HX711。XIAO 与主程序加入相同 Wi-Fi 后，会把视觉概率 POST 到融合服务地址；主程序使用已有 `Scale_GetWeight()` 完成重量复核。

## 先配 secrets.h

WiFi 账号和融合服务地址都不在源码里，放在 `secrets.h`：

```bash
cp secrets.h.example secrets.h   # 然后填真实值
```

`secrets.h` 已在 `.gitignore` 里，不会被提交。三个宏：

| 宏 | 含义 |
|---|---|
| `WIFI_SSID_CFG` / `WIFI_PASSWORD_CFG` | 和 RK3568 同一局域网的 Wi-Fi |
| `FUSION_URL_CFG` | 融合服务地址，如 `http://<RK3568-IP>:8099/api/vision-fusion` |

> 宏名带 `_CFG` 后缀是必需的：`.ino` 里已经有同名的 `static` 变量，宏名撞车会被预处理器就地展开成语法错误。

## Arduino IDE

1. 装 Edge Impulse 导出的分类库。库源码本仓库已经带了
   （同目录下的 `xiao-esp32s3-fruits-classify_inferencing/`），
   用「项目 → 加载库 → 添加 .ZIP 库」装对应的 zip，
   或者把该目录复制到 `~/Arduino/libraries/` 下。
2. 打开 `xiao_ov3660_weight_fusion.ino`。
3. 开发板选择 `XIAO_ESP32S3`，启用 `OPI PSRAM`，USB CDC 设为启用。
4. 安装 ESP32 Arduino Core 3.0.6 或兼容版本后编译上传。

已验证的 Arduino CLI 构建命令（把 `<repo>` 换成本地 clone 路径）：

```bash
arduino-cli compile \
  --fqbn esp32:esp32:XIAO_ESP32S3 \
  --libraries <repo>/firmware/vision-node \
  <repo>/firmware/vision-node
```

HX711 的去皮、标定和重量读取继续沿用 `firmware/store-controller/HX711_Scale.cpp`，
不需要在 XIAO 上重复配置。

## 摄像头监看页

XIAO 启动后会在串口打印 IP 地址。手机或电脑连接同一 Wi-Fi，打开 `http://<XIAO-IP>/` 即可查看最新摄像头画面、视觉结果和主程序返回的融合结果。

## 融合逻辑

- XIAO 只执行视觉推理并上传三个类别概率。
- 主程序连续读取已有 HX711 五次；重量低于 25g 或五次波动超过 4g 时不确认。
- 主程序使用视觉 78% + 重量匹配 22% 融合。
- 融合分数至少 72%、视觉至少 55%、重量匹配至少 15% 才自动确认。
- 不确定结果只输出“请人工确认”，不会自动加入购物车。
- 融合结果可从主程序的 `/api/vision-fusion/latest` 获取。

`PRODUCT_RULES` 中的典型重量是初始值。比赛使用固定样品时，应实测每类样品并收紧 `typicalGrams` 与 `toleranceGrams`，融合效果会明显提升。
