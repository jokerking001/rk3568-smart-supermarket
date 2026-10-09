#ifndef SLAVE_LINK_H
#define SLAVE_LINK_H

#include <Arduino.h>

// ============================================================
//  Slave_Link —— 从机与 RK3568 的通信层
// ============================================================
//
//  两个方向：
//
//    上报（从机 → RK）
//      · 称重   POST http://<RK>:8099/api/scale/sample   grams=<克数>
//               按 SLAVE_SCALE_INTERVAL_MS（500ms）节奏持续发，
//               **不是**只在读数变化时发 —— RK 侧判「样本过期」的
//               阈值是 1500ms，断供就会一直判过期。
//      · RFID   POST http://<RK>:8094/...
//      · 扫码枪 POST http://<RK>:8095/...
//      · 温湿度 POST http://<RK>:8094/api/env/report   DHT22 读数
//               （60 秒一条。传感器在从机上，大屏在 RK 上，中间要这条）
//
//    接收（RK → 从机），本机开一个极简 HTTP 服务在 SLAVE_HTTP_PORT：
//      · POST /api/tts      body: text=<要播报的文本>  → 喇叭播报
//      · POST /api/tare                                 → 远程去皮
//      · GET  /api/status                               → 状态 JSON
//
//  为什么不用 ESP32 的 WebServer 库：本工程自己有一个
//  WebServer.h / WebServer.cpp（原主控的 81 端点服务），名字撞车。
//  从机只需要三个路由，用 WiFiServer 手搓反而更可控。
// ============================================================

// 生命周期：建 HTTP 服务。必须在 wifi_init() 之后调用。
void SlaveLink_Init();

// 主循环：处理入站 HTTP 请求 + 按节奏上报称重。
// 放在 loop() 里调用，内部非阻塞。
void SlaveLink_HandleLoop();

// ---------------- 上报接口（从机 → RK）----------------

// 称重。grams 为克数，stable 为「窗口内极差 ≤4g」。
// 返回 true 表示 RK 收下了。
bool SlaveLink_PostScale(float grams, bool stable, unsigned long ageMs);

// RFID 卡号。uid 为读到的卡号字符串。
bool SlaveLink_PostRfid(const String& uid);

// USB 扫码枪条码。
bool SlaveLink_PostBarcode(const String& code);

// 温湿度（DHT22）。temperature 为摄氏度，humidity 为百分比。
// 由 Plus.cpp 的 DHT22_HandleLoop() 每 60 秒调一次。
bool SlaveLink_PostEnv(float temperature, float humidity);

// ---------------- 统计 ----------------
void SlaveLink_PrintStats();

#endif  // SLAVE_LINK_H
