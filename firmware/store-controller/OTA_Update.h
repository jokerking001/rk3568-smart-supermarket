#ifndef OTA_UPDATE_H
#define OTA_UPDATE_H

#include <Arduino.h>
#include <WiFi.h>

// ============================================================
//  OTA_Update —— 从机（ESP32-S3）的无线固件升级
// ============================================================
//
//  为什么不用 ElegantOTA：
//    原主控用 `ElegantOTA.loop()`（见 Work7_20.ino 的 #else 分支），
//    它挂在 ESP32 的 `WebServer` 库上。而本工程自己有一个
//    WebServer.h / WebServer.cpp（原主控的 81 端点服务）—— 名字撞车，
//    而且那个 .cpp 在 SLAVE_MODE=1 下**整体不参与编译**。
//    于是 ElegantOTA 在从机形态下根本没被链接进去，也没有可挂载的
//    HTTP 服务。改用 ESP32 核心自带的 `Update` 库直接操作 OTA 分区，
//    挂在本机手搓的 WiFiServer（Slave_Link.cpp，SLAVE_HTTP_PORT）上。
//
//  分区前提（firmware/store-controller/partitions.csv 已就绪）：
//    otadata  0x0E000  0x2000
//    app0     0x10000  0x1E0000   ← ota_0
//    app1     0x1F0000 0x1E0000   ← ota_1
//  两个槽各 1.875MB，当前固件约 1.51MB，余量约 360KB。
//
//  ⚠️ 只支持 `Content-Length` 整体上传，**不支持** `Transfer-Encoding: chunked`。
//     从机是手搓的 socket 服务，不解析 chunked 分块。
//     本机 curl / Python requests / RK 侧的脚本默认都用 Content-Length，够用；
//     但 `curl -T file` 在某些版本会走 chunked，**必须**改用
//     `curl -X POST --data-binary @firmware.bin`。
//
//  ⚠️ 上传期间**主循环被独占**（1.28MB 约 10~30 秒）。这段时间里
//     称重不上报、语音不响应 —— RK 侧会看到「样本过期」，属预期。
//     所以 OTA 要挑空闲时段做，别在顾客结算时刷。
// ============================================================

// 启动时打印分区信息。在 SlaveLink_Init() 之后调一次即可。
void OTA_Init();

// 处理 /api/ota/* 请求。path 允许带 query（如 `?reboot=1`）。
//   POST /api/ota/upload     body = 固件原始字节（application/octet-stream）
//                            可选头 X-Firmware-MD5: <32位小写hex>
//                            可选 query ?reboot=1 → 烧完自动重启进新固件
//   POST /api/ota/reboot     重启（进入刚烧好的固件）
//   POST /api/ota/rollback   切回**另一个槽**并重启（新固件有问题时用）
//   GET  /api/ota/status     分区/进度 JSON
//
// 注意：上传成功后**默认不自动重启**。原因——重启会掐断正在返回的
// HTTP 响应，客户端只会看到「连接被重置」，分不清是「烧成功重启了」
// 还是「烧失败崩了」。要自动重启就显式带 ?reboot=1。
void OTA_HandleRequest(WiFiClient& c, const String& method, const String& path,
                       int contentLength, const String& md5);

// 状态 JSON。给 /api/ota/status 和 /api/status 复用。
String OTA_StatusJson();

#endif  // OTA_UPDATE_H
