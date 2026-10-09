#include "Slave_Link.h"
#include "Slave_Config.h"
#include "HX711_Scale.h"
#include "Voice_Interaction.h"

#include <WiFi.h>
#include <HTTPClient.h>

// ============================================================
//  从机通信层实现
// ============================================================

static WiFiServer slaveServer(SLAVE_HTTP_PORT);

// ---- 统计 ----
static unsigned long statScaleOk = 0, statScaleFail = 0;
static unsigned long statRfidOk = 0, statRfidFail = 0;
static unsigned long statBarcodeOk = 0, statBarcodeFail = 0;
static unsigned long statEnvOk = 0, statEnvFail = 0;
static unsigned long statTtsServed = 0, statTareServed = 0;
static unsigned long consecScaleFail = 0;
static unsigned long lastScalePost = 0;

// ------------------------------------------------------------
//  HTTP 小工具
// ------------------------------------------------------------

static String rkUrl(int port, const char* path)
{
  return String("http://") + RK_HOST + ":" + String(port) + path;
}

static bool httpPostForm(const String& url, const String& body)
{
  HTTPClient http;
  // 超时压短：主循环里还有 I2S 录音/TTS，阻塞久了会爆音。
  http.setConnectTimeout(SLAVE_HTTP_TIMEOUT_MS);
  http.setTimeout(SLAVE_HTTP_TIMEOUT_MS);
  if (!http.begin(url)) return false;
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  int code = http.POST(body);
  http.end();
  return code >= 200 && code < 300;
}

static String urlDecode(const String& s)
{
  String out;
  out.reserve(s.length());
  for (size_t i = 0; i < s.length(); i++) {
    char ch = s[i];
    if (ch == '+') {
      out += ' ';
    } else if (ch == '%' && i + 2 < s.length()) {
      auto hexVal = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
      };
      int v1 = hexVal(s[i + 1]);
      int v2 = hexVal(s[i + 2]);
      if (v1 >= 0 && v2 >= 0) {
        out += (char)((v1 << 4) | v2);
        i += 2;
      } else {
        out += ch;
      }
    } else {
      out += ch;
    }
  }
  return out;
}

// 从 x-www-form-urlencoded 里取一个字段
static String formValue(const String& body, const char* key)
{
  String k = String(key) + "=";
  int p = body.indexOf(k);
  if (p < 0) return "";
  int start = p + k.length();
  int end = body.indexOf('&', start);
  String raw = (end < 0) ? body.substring(start) : body.substring(start, end);
  return urlDecode(raw);
}

static void sendJson(WiFiClient& c, int code, const String& json)
{
  c.printf("HTTP/1.1 %d %s\r\n", code, code == 200 ? "OK" : "Not Found");
  c.print("Content-Type: application/json; charset=utf-8\r\n");
  c.printf("Content-Length: %d\r\n", json.length());
  c.print("Connection: close\r\n\r\n");
  c.print(json);
}

// ------------------------------------------------------------
//  入站请求：RK → 从机
//     POST /api/tts    text=<文本>   → 交给异步播报队列
//     POST /api/tare                 → 远程去皮
//     GET  /api/status               → 状态
// ------------------------------------------------------------
static void handleInbound()
{
  WiFiClient c = slaveServer.available();
  if (!c) return;

  // 等请求行到达
  unsigned long t0 = millis();
  while (!c.available() && millis() - t0 < 100) delay(1);
  if (!c.available()) { c.stop(); return; }

  String reqLine = c.readStringUntil('\n');
  reqLine.trim();

  int contentLength = 0;
  while (c.available()) {
    String h = c.readStringUntil('\n');
    h.trim();
    if (h.length() == 0) break;
    if (h.startsWith("Content-Length:")) {
      contentLength = h.substring(15).toInt();
    }
  }

  String body;
  if (contentLength > 0) {
    unsigned long t1 = millis();
    while ((int)c.available() < contentLength && millis() - t1 < 200) delay(1);
    body.reserve(contentLength);
    for (int i = 0; i < contentLength && c.available(); i++) {
      body += (char)c.read();
    }
  }

  String method, path;
  int sp1 = reqLine.indexOf(' ');
  int sp2 = (sp1 > 0) ? reqLine.indexOf(' ', sp1 + 1) : -1;
  if (sp1 > 0 && sp2 > sp1) {
    method = reqLine.substring(0, sp1);
    path   = reqLine.substring(sp1 + 1, sp2);
  }

  if (method == "POST" && path == "/api/tts") {
    String text = formValue(body, "text");
    if (text.length() > 0) {
      // 不在这里直接播报：Voice_PlayTTS 要取百度 token + 下载音频，
      // 会阻塞好几秒。丢进现成的异步队列，由 Voice_HandleLoop() 处理。
      pendingVoiceTask = text;
      statTtsServed++;
      Serial.printf("[slave] 收到 TTS 请求（%d 字），已入队\n", text.length());
    }
    sendJson(c, 200, "{\"ok\":true}");
  }
  else if (method == "POST" && path == "/api/tare") {
    Scale_Tare();
    statTareServed++;
    Serial.println("[slave] 远程去皮完成");
    sendJson(c, 200, "{\"ok\":true}");
  }
  else if (method == "GET" && path == "/api/status") {
    String j = String("{\"ok\":true,\"role\":\"slave\",\"rk\":\"") + RK_HOST + "\""
             + ",\"scale_ok\":" + String(statScaleOk)
             + ",\"scale_fail\":" + String(statScaleFail)
             + ",\"tts_served\":" + String(statTtsServed)
             + ",\"tare_served\":" + String(statTareServed) + "}";
    sendJson(c, 200, j);
  }
  else {
    sendJson(c, 404, "{\"ok\":false,\"message\":\"not found\"}");
  }

  delay(1);
  c.stop();
}

// ------------------------------------------------------------
//  上报：从机 → RK
// ------------------------------------------------------------
bool SlaveLink_PostScale(float grams, bool stable, unsigned long ageMs)
{
#if SLAVE_USE_UART_SCALE && (SLAVE_UART_TX_PIN >= 0)
  // UART 帧：一行文本，便于 RK 侧桥接脚本解析
  //   SCALE <grams> <stable> <age_ms>\n
  Serial1.printf("SCALE %.1f %d %lu\n", grams, stable ? 1 : 0, ageMs);
  statScaleOk++;
  return true;
#else
  String body = "grams=" + String(grams, 1)
              + "&stable=" + String(stable ? 1 : 0)
              + "&age_ms=" + String(ageMs);
  bool ok = httpPostForm(rkUrl(RK_FUSION_PORT, RK_PATH_SCALE_SAMPLE), body);
  if (ok) {
    statScaleOk++;
    consecScaleFail = 0;
  } else {
    statScaleFail++;
    consecScaleFail++;
    // 限流日志：RK 不在线时不要每 500ms 刷一行
    if (consecScaleFail == 1 ||
        consecScaleFail % SLAVE_LOG_EVERY_N_FAILS == 0) {
      Serial.printf("[slave] 称重上报失败 %lu 次（%s:%d 不可达？）\n",
                    consecScaleFail, RK_HOST, RK_FUSION_PORT);
    }
  }
  return ok;
#endif
}

bool SlaveLink_PostRfid(const String& uid)
{
  // ⚠️ 别改回 `/api/rfid-poll` —— 那是**页面轮询用**的 GET 接口
  // （语义是「有没有新卡事件」，方向相反）。从机是事件**生产者**，
  // 要打 `/api/rfid/report`，见 store_ext_routes.py 的 h_rfid_report。
  // 之前写成 /api/rfid-poll，RK 侧只有 GET 注册 → POST 直接 404，
  // 而从机只累加失败计数、不报错，属于最难发现的那类问题。
  bool ok = httpPostForm(rkUrl(RK_STORE_PORT, RK_PATH_RFID_REPORT),
                         "uid=" + uid);
  if (ok) statRfidOk++; else statRfidFail++;
  Serial.printf("[slave] RFID 上报 %s: %s\n", uid.c_str(), ok ? "ok" : "fail");
  return ok;
}

bool SlaveLink_PostBarcode(const String& code)
{
  // ⚠️ 8095 的接收端点是 `/api/scanner/inject`，**没有** `/api/scan`。
  // `/api/scan` 是 8094 收银后端的端点，而 8095 自己收到 inject 之后
  // 会带上 session / add_to_cart 转发给 8094 —— 那条转发链已经写好了，
  // 从机只要往 8095 的 inject 打就行（见 scanner_service.py:418）。
  //
  // 不带 `add_to_cart`：原工程 `WebServer_SubmitScanGunCode()` 也只是把码
  // 存进「最近扫码结果」等页面来取，**不加购**。加购是页面拿到码之后的事，
  // 从机不该替它决定。
  String body = "code=" + code + "&source=" + SLAVE_BARCODE_SOURCE;
  bool ok = httpPostForm(rkUrl(RK_SCANNER_PORT, RK_PATH_SCANNER_INJECT), body);
  if (ok) statBarcodeOk++; else statBarcodeFail++;
  Serial.printf("[slave] 条码上报 %s: %s\n", code.c_str(), ok ? "ok" : "fail");
  return ok;
}

bool SlaveLink_PostEnv(float temperature, float humidity)
{
  // ⚠️ 打的是 `/api/env/report`，**不是** `/api/env` ——
  // 后者是给大屏读的 GET（返回最新值 + 新鲜度），从机是事件**生产者**，
  // 方向相反。写错的表现和当初 RFID 一样：RK 侧 404，从机只累加失败
  // 计数、不报错，属于最难发现的那类问题。
  //
  // 数值用 String(x, 1) 保留一位小数：DHT22 本身精度就到 0.1℃ / 0.1%RH，
  // 多发位数没有意义，还多占几个字节。
  String body = "temperature=" + String(temperature, 1)
              + "&humidity=" + String(humidity, 1)
              + "&source=esp32s3-dht22";
  bool ok = httpPostForm(rkUrl(RK_STORE_PORT, RK_PATH_ENV_REPORT), body);
  if (ok) statEnvOk++; else statEnvFail++;
  Serial.printf("[slave] 温湿度上报 %.1fC/%.1f%%: %s\n",
                temperature, humidity, ok ? "ok" : "fail");
  return ok;
}

// ------------------------------------------------------------
//  主循环
// ------------------------------------------------------------
void SlaveLink_HandleLoop()
{
  // 1) 处理 RK 下发的请求（TTS / 去皮 / 状态查询）
  handleInbound();

  // 2) 按固定节奏上报称重。
  //    **必须持续发**，不能只在读数变化时发 ——
  //    RK 侧判「样本过期」的阈值是 1500ms，断供就会一直判过期。
  unsigned long now = millis();
  if (now - lastScalePost >= SLAVE_SCALE_INTERVAL_MS) {
    lastScalePost = now;

    float weight = 0;
    bool stable = false;
    unsigned long ageMs = 0;

    if (Scale_GetSnapshot(&weight, &stable, &ageMs)) {
      SlaveLink_PostScale(weight, stable, ageMs);
    }
    // 样本不足 5 个（开机头 2.5 秒）或已过期时静默跳过：
    // RK 侧本来也要求 ≥5 个样本才算数，发过去只会被判无效。
  }
}

void SlaveLink_Init()
{
  slaveServer.begin();
  slaveServer.setNoDelay(true);
  Serial.printf("[slave] HTTP 服务已启动，端口 %d\n", SLAVE_HTTP_PORT);
  Serial.printf("[slave] 上报目标 称重 -> %s:%d/api/scale/sample\n",
                RK_HOST, RK_FUSION_PORT);
  Serial.printf("[slave]           语音 <- POST /api/tts（本机 %d 端口）\n",
                SLAVE_HTTP_PORT);

#if SLAVE_USE_UART_SCALE && (SLAVE_UART_TX_PIN >= 0)
  Serial1.begin(SLAVE_UART_BAUD, SERIAL_8N1, -1, SLAVE_UART_TX_PIN);
  Serial.printf("[slave] 称重改走 UART，TX 引脚 %d @ %d\n",
                SLAVE_UART_TX_PIN, SLAVE_UART_BAUD);
#elif SLAVE_USE_UART_SCALE
  Serial.println("[slave] 警告：打开了 UART 称重但没指定引脚，回退到 HTTP");
#endif
}

void SlaveLink_PrintStats()
{
  Serial.printf("[slave] 统计：称重 ok=%lu fail=%lu | RFID ok=%lu fail=%lu"
                " | 条码 ok=%lu fail=%lu | 温湿度 ok=%lu fail=%lu"
                " | TTS=%lu 去皮=%lu\n",
                statScaleOk, statScaleFail,
                statRfidOk, statRfidFail,
                statBarcodeOk, statBarcodeFail,
                statEnvOk, statEnvFail,
                statTtsServed, statTareServed);
}
