#include "WIFI_Test.h"
#include "WebServer.h"
#include "AI_Test.h"
#include "Product_Data.h"
#include "HC_SR04.h"
#include "led.h"
#include "Mode_LowPower.h"
#include "Voice_Interaction.h"
#include "Plus.h"
#include "HX711_Scale.h"
#include "RFID_Reader.h" // 队友新增的模块
#include "USB_BarcodeScanner.h"
#include "Inventory_Monitor.h"

extern bool isRecording;

void setup() {
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

void loop() {
  AI_HandleLoop();
  // HC_SR04_HandleLoop();
  
  // ================= 🌟 恢复传统按键语音交互核心链路 =================
  Voice_HandleLoop();   // 处理异步 TTS 播报任务
  Voice_HandleKey();    // 监听实体按键：按下开始录音，松开自动提交
  Voice_RecordLoop();   // 录音数据流轮询填充
  
  // 🛡️ 彻底封印边缘 AI 与采样工具，避免抢占 I2S 麦克风资源
  Voice_WakeupLoop(); 
  // Tool_Collect_Noise_Final();
  // Test_Pure_AI_Model();
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
