// ============================================================
//  store-controller 主程序
// ============================================================
//
//  ⚠️ 这个固件现在有**两种角色**，用 SLAVE_MODE 切换：
//
//    SLAVE_MODE = 1（默认）—— 从机
//        RK3568 接管主控后，本机降级为「硬实时外设从机」：
//        只保留 HX711 称重 / I2S 语音 / WS2812 / DHT22 / RFID /
//        USB 扫码枪，把数据上报给 RK3568，并接收 RK 的 TTS 播报指令。
//        商品、订单、收银、会员、网页、库存、报表这些主控职责
//        已经全部搬到 RK3568 的 rk3568-store（8094）。
//
//    SLAVE_MODE = 0 —— 原主控（保留，便于对照/回退）
//        迁移前的完整形态：81 个 HTTP 端点 + 本地商品数据 + AI 问答。
//
//  迁移依据：docs/RK3568-迁移总览.md
//    §4   目标架构：ESP32-S3 从"主控"降级为"外设从机"
//    §8   决策 2：保留一块 ESP32-S3 当外设从机
//    §8.1 接口口径：称重建议 UART、语音 HTTP
//         （本实现称重默认走 HTTP，理由见 Slave_Config.h 里的说明）
// ============================================================

#define SLAVE_MODE 1

// ---- 两种角色都要用的（实时外设）----
#include "WIFI_Test.h"
#include "led.h"
#include "Voice_Interaction.h"
#include "HX711_Scale.h"
#include "RFID_Reader.h"
#include "USB_BarcodeScanner.h"
#include "Plus.h"

#if SLAVE_MODE
#include "Slave_Link.h"
#else
// ---- 只有原主控才需要的（已被 RK3568 取代）----
#include "WebServer.h"
#include "AI_Test.h"
#include "Product_Data.h"
#include "HC_SR04.h"
#include "Mode_LowPower.h"
#include "Inventory_Monitor.h"
#endif

extern bool isRecording;


#if SLAVE_MODE
// ============================================================
//  从机模式
// ============================================================
void setup()
{
  Serial.begin(115200);
  delay(100);
  Serial.println("=== 从机（外设）启动 ===");

  // 🛡️ 最高优先级：灯光、按键、语音先抢 DMA 和 JTAG 资源。
  // 顺序沿用原工程 —— Voice_Init 必须早于其他外设，否则 I2S 抢不到。
  led_init();
  key_init();
  Voice_Init();
  delay(500);

  // 联网：TTS 要调百度云，上报要连 RK。
  // 带 30 秒超时 —— 原来这里是死等循环，现场没网就永远起不来。
  wifi_init(30000);

  // 实时外设
  Scale_Init();
  RFID_Init();
  Plus_Init();
  USB_BarcodeScanner_Init();

  // 与 RK3568 的通信层（开 HTTP 服务，供 RK 下发 TTS / 远程去皮）
  SlaveLink_Init();

  Serial.println("=== 从机就绪 ===");
}

void loop()
{
  // ---- 语音交互 ----
  Voice_HandleLoop();   // 处理异步 TTS 播报任务（RK 下发的播报也走这里）
  Voice_HandleKey();    // 实体按键：按下开始录音，松开提交
  Voice_RecordLoop();   // 录音数据流轮询填充
  Voice_WakeupLoop();

  // ---- 称重（内部 500ms 节奏采样，由 SlaveLink 按同样节奏上报）----
  Scale_HandleLoop();

  // ---- RFID ----
  RFID_HandleLoop();
  if (RFID_IsNewCard()) {
    String uid = RFID_GetLastUID();
    if (uid.length() > 0) {
      SlaveLink_PostRfid(uid);
    }
  }

  // ---- USB 扫码枪 ----
  // 录音期间让路：原工程注释里明确写过它和 I2S 抢 DMA。
  if (!isRecording) {
    String scanCode;
    while (USB_BarcodeScanner_Read(scanCode)) {
      scanCode.trim();
      if (scanCode.length() > 0) {
        Serial.println("【扫码枪】获取条码: [" + scanCode + "]");
        SlaveLink_PostBarcode(scanCode);
      }
    }
  }

  // ---- 温湿度 ----
  DHT22_HandleLoop();

  // ---- 与 RK3568 通信：处理入站请求 + 按 500ms 上报称重 ----
  SlaveLink_HandleLoop();
}

#else
// ============================================================
//  原主控模式（迁移前形态，保留用于对照/回退）
// ============================================================
void setup()
{
  // 1. 串口最先开启，用于调试打印
  Serial.begin(115200);
  delay(100);
  Serial.println("=== 开始系统初始化 ===");

  // 2. 🛡️ 【最高优先级】优先初始化灯光、按键与语音，霸占 DMA 和 JTAG 资源！
  led_init();
  key_init();
  Voice_Init(); // 👈 必须挪到最前面！
  delay(500);

  // 3. 初始化基础网络与网页服务
  wifi_init();
  Product_Data_Init();
  WebServer_Init();

  // 4. 初始化普通外设（称重、超声波等，它们不占用高带宽 DMA）
  HC_SR04_Init();
  LowPower_Init();
  Scale_Init();
  DailyStats_Init();
  RFID_Init();

  // 5. 🔌 【最低优先级】最后初始化 USB 扫码枪！让它去捡剩下的 DMA 通道
  USB_BarcodeScanner_Init();

  delay(1000);
  Plus_Init();
  delay(1000);
  Plus_PrintTest();

  // 6. 库存监控（缺货/低库存/临期 → 通知 miniclaw）
  InventoryMonitor_Init();

  Serial.println("=== 系统整合版已就绪 ===");
}

void loop()
{
  AI_HandleLoop();

  // ================= 🌟 传统按键语音交互核心链路 =================
  Voice_HandleLoop();   // 处理异步 TTS 播报任务
  Voice_HandleKey();    // 监听实体按键：按下开始录音，松开自动提交
  Voice_RecordLoop();   // 录音数据流轮询填充

  // 🛡️ 彻底封印边缘 AI 与采样工具，避免抢占 I2S 麦克风资源
  Voice_WakeupLoop();
  // ==================================================================

  Scale_HandleLoop();
  RFID_HandleLoop();

  // USB 扫码枪逻辑保持不变
  if (!isRecording) {
    String scanCode;
    while (USB_BarcodeScanner_Read(scanCode)) {
      scanCode.trim();
      if (scanCode.length() > 0) {
        Serial.println("【扫码枪】获取纯净条码: [" + scanCode + "]");
        WebServer_SubmitScanGunCode(scanCode, "usb-host");
      }
    }
  }

  // DHT22_HandleLoop();
  InventoryMonitor_HandleLoop();
  ElegantOTA.loop();
}
#endif
