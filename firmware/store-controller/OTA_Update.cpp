#include "OTA_Update.h"
#include "Slave_Config.h"

#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>

// ============================================================
//  从机 OTA 实现
// ============================================================
//
//  为什么整个文件被 ENABLE_OTA 包住，而不是在 Slave_Link.cpp 里
//  加 #if：这样 Slave_Link.cpp 的调用点是**无条件**的，少一处
//  容易忘同步的条件编译。关掉开关时端点统一回 404，行为可预期。
// ============================================================

#if ENABLE_OTA

// ---- 统计 ----
static unsigned long statOtaOk = 0, statOtaFail = 0;
static unsigned long statOtaBytes = 0;
static String lastOtaError = "";

// ------------------------------------------------------------
//  HTTP 小工具
//  （与 Slave_Link.cpp 里的 sendJson 同形。之所以不复用：那边是
//   static，且它只会写 200/404 两个状态码，这里要 400/500/507。
//   6 行代码，重复的代价低于把两处耦合起来。）
// ------------------------------------------------------------
static void otaSendJson(WiFiClient& c, int code, const String& json)
{
  const char* reason = "OK";
  if (code == 400)      reason = "Bad Request";
  else if (code == 404) reason = "Not Found";
  else if (code == 500) reason = "Internal Server Error";
  else if (code == 507) reason = "Insufficient Storage";

  c.printf("HTTP/1.1 %d %s\r\n", code, reason);
  c.print("Content-Type: application/json; charset=utf-8\r\n");
  c.printf("Content-Length: %d\r\n", json.length());
  c.print("Connection: close\r\n\r\n");
  c.print(json);
}

// 从 "/api/ota/upload?reboot=1" 里取 query 值
static String queryValue(const String& path, const char* key)
{
  int q = path.indexOf('?');
  if (q < 0) return "";
  String k = String(key) + "=";
  int p = path.indexOf(k, q);
  if (p < 0) return "";
  int start = p + k.length();
  int end = path.indexOf('&', start);
  return (end < 0) ? path.substring(start) : path.substring(start, end);
}

// 去掉 query，只留路径
static String pathOnly(const String& path)
{
  int q = path.indexOf('?');
  return (q < 0) ? path : path.substring(0, q);
}

// ------------------------------------------------------------
//  核心：把 socket 上的固件字节流式写进 OTA 分区
// ------------------------------------------------------------
//  ⚠️ 绝不把 body 读进 String —— 1.5MB 的固件会把堆直接撑爆
//     （从机还要跑 I2S 录音/TTS，堆本来就紧）。
static bool otaStreamToFlash(WiFiClient& c, int contentLength,
                             const String& md5, String& err)
{
  if (contentLength <= 0) {
    err = "missing or zero Content-Length";
    return false;
  }

  const esp_partition_t* next = esp_ota_get_next_update_partition(NULL);
  if (next == NULL) {
    err = "no OTA partition available";
    return false;
  }
  if ((uint32_t)contentLength > next->size) {
    err = String("firmware too big: ") + contentLength
        + " B > slot " + (unsigned)next->size + " B";
    return false;
  }

  // Update.begin 之后，库里会检查首批数据的 magic（0xE9），
  // 不是合法镜像的话 write() 会返回 0 —— 这就是我们的第一道校验。
  if (!Update.begin((size_t)contentLength, U_FLASH)) {
    err = String("Update.begin: ") + Update.errorString();
    return false;
  }
  if (md5.length() == 32) {
    Update.setMD5(md5.c_str());
  }

  uint8_t buf[1024];
  int written = 0;
  int lastLog = 0;
  unsigned long lastRx = millis();

  while (written < contentLength) {
    int avail = c.available();
    if (avail <= 0) {
      if (millis() - lastRx > SLAVE_OTA_RX_TIMEOUT_MS) {
        err = String("upload stalled at ") + written + "/" + contentLength + " B";
        Update.abort();
        return false;
      }
      delay(1);
      continue;
    }

    int want = contentLength - written;
    if (want > (int)sizeof(buf)) want = sizeof(buf);
    if (want > avail)            want = avail;

    int n = c.read(buf, want);
    if (n <= 0) { delay(1); continue; }

    if (Update.write(buf, (size_t)n) != (size_t)n) {
      err = String("flash write failed at ") + written + " B: " + Update.errorString();
      Update.abort();
      return false;
    }

    written += n;
    lastRx = millis();

    // 每 256KB 打一行，方便串口上盯进度
    if (written - lastLog >= 262144) {
      lastLog = written;
      Serial.printf("[ota] %d / %d B (%.0f%%)\n",
                    written, contentLength, 100.0 * written / contentLength);
    }
  }

  // end(true) = 即使剩余字节没写满也接受；它会顺带
  // esp_ota_set_boot_partition() 把启动分区指到刚写好的槽。
  if (!Update.end(true)) {
    err = String("Update.end: ") + Update.errorString();
    return false;
  }

  statOtaBytes += written;
  return true;
}

// ------------------------------------------------------------
//  端点分发
// ------------------------------------------------------------
void OTA_HandleRequest(WiFiClient& c, const String& method, const String& path,
                       int contentLength, const String& md5)
{
  const String p = pathOnly(path);

  // ---------------- GET /api/ota/status ----------------
  if (method == "GET" && p == "/api/ota/status") {
    otaSendJson(c, 200, OTA_StatusJson());
    return;
  }

  // ---------------- POST /api/ota/upload ----------------
  if (method == "POST" && p == "/api/ota/upload") {
    String err;
    unsigned long t0 = millis();
    bool ok = otaStreamToFlash(c, contentLength, md5, err);
    unsigned long dt = millis() - t0;

    if (ok) {
      statOtaOk++;
      lastOtaError = "";
      const esp_partition_t* next = esp_ota_get_next_update_partition(NULL);
      // 注意：此时 next 已经指向**刚写完的槽**（因为 Update.end 改了 boot 分区）
      const esp_partition_t* boot = esp_ota_get_boot_partition();
      Serial.printf("[ota] 烧录成功 %d B / %lu ms，启动分区 -> %s\n",
                    contentLength, dt, boot ? boot->label : "?");
      (void)next;

      String j = String("{\"ok\":true,\"bytes\":") + contentLength
               + ",\"ms\":" + dt
               + ",\"boot_partition\":\"" + String(boot ? boot->label : "?") + "\""
               + ",\"reboot_required\":" + ((queryValue(path, "reboot") == "1") ? "false" : "true")
               + "}";

      if (queryValue(path, "reboot") == "1") {
        otaSendJson(c, 200, j);
        // 给 lwIP 一点时间把响应发出去再重启。
        // WiFiClient 没有真正可用的 flush()（Stream::flush 是空实现），
        // 所以只能靠 delay —— 响应体只有 100 多字节，800ms 足够。
        delay(800);
        Serial.println("[ota] 按请求重启进入新固件");
        ESP.restart();
        return;
      }
      otaSendJson(c, 200, j);
      return;
    }

    statOtaFail++;
    lastOtaError = err;
    Serial.printf("[ota] 烧录失败：%s\n", err.c_str());
    int code = err.startsWith("firmware too big") ? 507 : 500;
    otaSendJson(c, code, String("{\"ok\":false,\"error\":\"") + err + "\"}");
    return;
  }

  // ---------------- POST /api/ota/reboot ----------------
  if (method == "POST" && p == "/api/ota/reboot") {
    otaSendJson(c, 200, "{\"ok\":true,\"message\":\"rebooting\"}");
    delay(800);
    Serial.println("[ota] 手动重启");
    ESP.restart();
    return;
  }

  // ---------------- POST /api/ota/rollback ----------------
  // 语义：把启动分区切到**另一个槽**再重启。
  //   · 刚 OTA 完发现新固件有问题 → 切回旧槽（旧槽是我们刚才在跑的，
  //     一定是能启动的），这条路径安全。
  //   · 若从没 OTA 过，两个槽都是同一个固件，切了也没差别。
  //
  // 为什么不用 esp_ota_mark_app_invalid_rollback_and_reboot()：
  // 那套依赖 CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE，Arduino 核心默认
  // **没开**，调用会返回 ESP_ERR_NOT_SUPPORTED，等于命令不可用。
  // 直接 esp_ota_set_boot_partition 不依赖任何配置，可靠得多。
  if (method == "POST" && p == "/api/ota/rollback") {
    const esp_partition_t* running = esp_ota_get_running_partition();
    const esp_partition_t* other   = esp_ota_get_next_update_partition(NULL);

    if (other == NULL) {
      otaSendJson(c, 500, "{\"ok\":false,\"error\":\"no other OTA slot\"}");
      return;
    }
    esp_err_t e = esp_ota_set_boot_partition(other);
    if (e != ESP_OK) {
      String msg = String("{\"ok\":false,\"error\":\"") + esp_err_to_name(e) + "\"}";
      otaSendJson(c, 500, msg);
      return;
    }
    String j = String("{\"ok\":true,\"from\":\"") + (running ? running->label : "?")
             + "\",\"to\":\"" + other->label + "\",\"message\":\"rebooting\"}";
    otaSendJson(c, 200, j);
    delay(800);
    Serial.printf("[ota] 回滚：%s -> %s，重启\n",
                  running ? running->label : "?", other->label);
    ESP.restart();
    return;
  }

  otaSendJson(c, 404, "{\"ok\":false,\"message\":\"unknown ota endpoint\"}");
}

// ------------------------------------------------------------
//  状态
// ------------------------------------------------------------
String OTA_StatusJson()
{
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* boot    = esp_ota_get_boot_partition();
  const esp_partition_t* next    = esp_ota_get_next_update_partition(NULL);

  String j = String("{\"ok\":true");
  j += ",\"running\":\"" + String(running ? running->label : "?") + "\"";
  j += ",\"running_addr\":" + String(running ? (unsigned)running->address : 0);
  j += ",\"boot\":\"" + String(boot ? boot->label : "?") + "\"";
  j += ",\"next_slot\":\"" + String(next ? next->label : "?") + "\"";
  j += ",\"slot_size\":" + String(next ? (unsigned)next->size : 0);
  j += ",\"free_sketch\":" + String(ESP.getFreeSketchSpace());
  j += ",\"uploaded_ok\":" + String(statOtaOk);
  j += ",\"uploaded_fail\":" + String(statOtaFail);
  j += ",\"uploaded_bytes\":" + String(statOtaBytes);
  if (lastOtaError.length() > 0) {
    j += ",\"last_error\":\"" + lastOtaError + "\"";
  }
  j += "}";
  return j;
}

// ------------------------------------------------------------
//  初始化
// ------------------------------------------------------------
void OTA_Init()
{
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* next    = esp_ota_get_next_update_partition(NULL);

  Serial.printf("[ota] 运行分区 %s @ 0x%06X，下次写入 %s @ 0x%06X（%u B）\n",
                running ? running->label : "?",
                running ? (unsigned)running->address : 0,
                next ? next->label : "?",
                next ? (unsigned)next->address : 0,
                next ? (unsigned)next->size : 0);
  Serial.println("[ota] POST /api/ota/upload?reboot=1  body=固件原始字节（Content-Length 必须给）");
  Serial.println("[ota] POST /api/ota/reboot | POST /api/ota/rollback | GET /api/ota/status");
}

#else   // ENABLE_OTA == 0

// 关掉 OTA 时端点统一 404 —— 调用点不用改，行为可预期。
static void otaSend404(WiFiClient& c)
{
  const char* j = "{\"ok\":false,\"message\":\"OTA disabled (ENABLE_OTA=0)\"}";
  c.print("HTTP/1.1 404 Not Found\r\n");
  c.print("Content-Type: application/json; charset=utf-8\r\n");
  c.printf("Content-Length: %d\r\n", (int)strlen(j));
  c.print("Connection: close\r\n\r\n");
  c.print(j);
}

void OTA_HandleRequest(WiFiClient& c, const String& method, const String& path,
                       int contentLength, const String& md5)
{
  (void)method; (void)path; (void)contentLength; (void)md5;
  otaSend404(c);
}

String OTA_StatusJson()
{
  return String("{\"ok\":false,\"ota\":\"disabled\"}");
}

void OTA_Init()
{
  Serial.println("[ota] 已关闭（ENABLE_OTA=0）");
}

#endif  // ENABLE_OTA
