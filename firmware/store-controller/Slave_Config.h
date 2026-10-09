#ifndef SLAVE_CONFIG_H
#define SLAVE_CONFIG_H

// 先把私密配置引进来：现场要在这里覆盖 RK_HOST。
// 用 __has_include 是必要的 —— secrets.h 不进版本库，
// 别人 clone 下来只有 secrets.h.example，直接 #include 会编译不过。
#if defined(__has_include)
#  if __has_include("secrets.h")
#    include "secrets.h"
#  endif
#endif

// ============================================================
//  从机（ESP32-S3）配置  ——  store-controller 降级后的参数
// ============================================================
//
//  角色变更：本固件从「门店主控」降级为「硬实时外设从机」。
//  主控职责（商品/订单/收银/会员/网页/库存/报表）全部移交
//  RK3568 的 rk3568-store（8094）；本机只保留 HX711 称重、
//  I2S 语音、WS2812、DHT22、RFID、USB 扫码枪，
//  并把数据上报给 RK3568。
//
//  通信方式（依据 docs/RK3568-迁移总览.md §8.1）：
//
//    · 称重  → HTTP POST 给 RK 的融合服务（**默认**）
//              也可以切 UART（打开 SLAVE_USE_UART_SCALE），
//              但 UART 要额外占引脚，而本机 IO 已经很紧：
//                WS2812(18) 按键(39) HX711(12,14) DHT22(41)
//                RFID(21,15) I2S(8,9,46,47,48,13)
//              所以默认走 HTTP —— 不占 IO，且 RK 侧零改动。
//
//    · 语音  → HTTP：RK 调本机的 POST /api/tts 让喇叭播报
//    · RFID / 扫码枪 → HTTP POST 给 RK
//
//  RK 侧已经留好口子，两种方式都不需要改 RK 代码：
//  融合服务 8099 的 POST /api/scale/sample 收 grams=<克数>。
// ============================================================

// ------------------------------------------------------------
//  RK3568 地址
// ------------------------------------------------------------
// 现场按实际网段改。原主控是 192.168.43.44，RK3568 换成自己的 IP。
#ifndef RK_HOST
#define RK_HOST "192.168.43.44"
#endif

#define RK_FUSION_PORT    8099   // 融合服务：POST /api/scale/sample
#define RK_STORE_PORT     8094   // 收银后端：RFID 上报
#define RK_SCANNER_PORT   8095   // 扫码枪服务：条码注入

// ------------------------------------------------------------
//  上报路径
// ------------------------------------------------------------
// 路径集中在这里，避免散落在 .cpp 里写错 —— 这三条**都踩过坑**：
// 写错的表现是 RK 侧 404，而从机只累加失败计数、不报错，很难发现。
//
//  · 称重 → 8099 /api/scale/sample     （融合服务已就绪，无需改动 RK）
//  · RFID → 8094 /api/rfid/report      ⚠️ **不是** /api/rfid-poll：
//        后者是页面轮询用的 GET，语义是「有没有新卡事件」，方向相反。
//        /api/rfid/report 是为从机新加的接收端点（h_rfid_report）。
//  · 条码 → 8095 /api/scanner/inject   ⚠️ **不是** /api/scan：
//        /api/scan 是 8094 收银后端的端点。8095 收到 inject 后会自己
//        带上 session / add_to_cart 转发给 8094，那条链已经写好了。
#define RK_PATH_SCALE_SAMPLE   "/api/scale/sample"
#define RK_PATH_RFID_REPORT    "/api/rfid/report"
#define RK_PATH_SCANNER_INJECT "/api/scanner/inject"

// 条码来源标记。**用原工程的 "usb-host"**，这样 8094 的 scan_events
// 和页面上显示的 source 与迁移前逐字一致，排障时不会因为换个名字
// 而分不清「这码是谁扫的」。
#define SLAVE_BARCODE_SOURCE   "usb-host"

// ------------------------------------------------------------
//  本机 HTTP 服务
// ------------------------------------------------------------
// 接收 RK 指令：TTS 播报 / 远程去皮 / 状态查询。
// 注意不要用 80 —— 原主控的 81 端点服务已经废弃，端口让出来。
#define SLAVE_HTTP_PORT   8080

// ------------------------------------------------------------
//  上报节奏
// ------------------------------------------------------------
// 称重上报间隔。**不要大于 500ms**：
// RK 侧融合服务判「样本过期」的阈值是 1500ms，且要 ≥5 个样本、
// 窗口内极差 ≤4g 才算稳定。500ms 节奏下 2.5 秒凑够 5 个样本；
// 再慢就会一直被判过期，秤等于没接。
// 对应 HX711_Scale.cpp 里的 scaleInterval，两者保持一致。
#define SLAVE_SCALE_INTERVAL_MS   500

// 单次 HTTP 超时。设短一点：主循环里还有 I2S 录音和 TTS 在跑，
// 网络抖动时阻塞太久会爆音。
#define SLAVE_HTTP_TIMEOUT_MS     400

// 连续失败多少条打一次串口。避免 RK 不在线时刷屏。
#define SLAVE_LOG_EVERY_N_FAILS   20

// ------------------------------------------------------------
//  可选：称重改走 UART
// ------------------------------------------------------------
// 打开后称重不再走 HTTP，而是从 SLAVE_UART_TX_PIN 发帧出去，
// 由 RK 侧的串口→HTTP 桥接转给 8099。
// **需要先确认引脚**：本机 IO 已被占满，SLAVE_UART_TX_PIN 默认
// 留 -1 表示未指定，此时即使打开开关也会回退到 HTTP。
#define SLAVE_USE_UART_SCALE      0
#define SLAVE_UART_TX_PIN        (-1)
#define SLAVE_UART_BAUD           115200

#endif  // SLAVE_CONFIG_H
