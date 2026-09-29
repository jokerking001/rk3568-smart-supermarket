#include <Arduino.h>
#include "secrets.h"        // 私密配置（WiFi / 融合服务地址），不进版本库
#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <esp_camera.h>
#include <xiao-esp32s3-fruits-classify_inferencing.h>
#include "edge-impulse-sdk/dsp/image/image.hpp"

// XIAO ESP32S3 Sense camera pins (OV3660).
#define CAM_PWDN -1
#define CAM_RESET -1
#define CAM_XCLK 10
#define CAM_SIOD 40
#define CAM_SIOC 39
#define CAM_Y9 48
#define CAM_Y8 11
#define CAM_Y7 12
#define CAM_Y6 14
#define CAM_Y5 16
#define CAM_Y4 18
#define CAM_Y3 17
#define CAM_Y2 15
#define CAM_VSYNC 38
#define CAM_HREF 47
#define CAM_PCLK 13

#define STATUS_LED 21

static const char *WIFI_SSID = WIFI_SSID_CFG;
static const char *WIFI_PASSWORD = WIFI_PASSWORD_CFG;
static const char *FUSION_URL = FUSION_URL_CFG;

static constexpr uint16_t RAW_WIDTH = 320;
static constexpr uint16_t RAW_HEIGHT = 240;
static constexpr uint32_t INFERENCE_INTERVAL_MS = 1400;
static constexpr size_t VISION_WINDOW_SIZE = 5;
static constexpr uint32_t FUSION_HTTP_TIMEOUT_MS = 3000;

static camera_config_t cameraConfig = {
  .pin_pwdn = CAM_PWDN, .pin_reset = CAM_RESET, .pin_xclk = CAM_XCLK,
  .pin_sscb_sda = CAM_SIOD, .pin_sscb_scl = CAM_SIOC,
  .pin_d7 = CAM_Y9, .pin_d6 = CAM_Y8, .pin_d5 = CAM_Y7, .pin_d4 = CAM_Y6,
  .pin_d3 = CAM_Y5, .pin_d2 = CAM_Y4, .pin_d1 = CAM_Y3, .pin_d0 = CAM_Y2,
  .pin_vsync = CAM_VSYNC, .pin_href = CAM_HREF, .pin_pclk = CAM_PCLK,
  .xclk_freq_hz = 20000000, .ledc_timer = LEDC_TIMER_0, .ledc_channel = LEDC_CHANNEL_0,
  .pixel_format = PIXFORMAT_JPEG, .frame_size = FRAMESIZE_QVGA,
  .jpeg_quality = 12, .fb_count = 1, .fb_location = CAMERA_FB_IN_PSRAM,
  .grab_mode = CAMERA_GRAB_WHEN_EMPTY
};

static uint8_t *snapshotBuffer = nullptr;
static uint8_t *latestJpeg = nullptr;
static size_t latestJpegLength = 0;
static size_t latestJpegCapacity = 0;
static uint32_t lastInferenceMs = 0;
static WebServer web(80);
static String latestResult = "{\"ok\":false,\"msg\":\"等待第一次识别\"}";
static float probabilityWindow[VISION_WINDOW_SIZE][3] = {};
static size_t probabilityWindowCount = 0;
static size_t probabilityWindowIndex = 0;
static uint32_t observationSequence = 0;
static String bootId;

static void updateProbabilityWindow(const float values[3], float means[3], float *maxSpread) {
  memcpy(probabilityWindow[probabilityWindowIndex], values, sizeof(probabilityWindow[0]));
  probabilityWindowIndex = (probabilityWindowIndex + 1) % VISION_WINDOW_SIZE;
  if (probabilityWindowCount < VISION_WINDOW_SIZE) probabilityWindowCount++;

  *maxSpread = 0;
  for (size_t label = 0; label < 3; label++) {
    float sum = 0, minValue = 1, maxValue = 0;
    for (size_t sample = 0; sample < probabilityWindowCount; sample++) {
      float value = probabilityWindow[sample][label];
      sum += value;
      minValue = min(minValue, value);
      maxValue = max(maxValue, value);
    }
    means[label] = sum / probabilityWindowCount;
    *maxSpread = max(*maxSpread, maxValue - minValue);
  }
}

static int cameraGetData(size_t offset, size_t length, float *out) {
  size_t pixel = offset * 3;
  for (size_t i = 0; i < length; i++, pixel += 3) {
    out[i] = (snapshotBuffer[pixel] << 16) | (snapshotBuffer[pixel + 1] << 8) | snapshotBuffer[pixel + 2];
  }
  return 0;
}

static bool captureImage() {
  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame) return false;

  // Keep the exact frame used for inference so the dashboard never competes
  // with the classifier for direct access to the camera.
  if (frame->len > latestJpegCapacity) {
    uint8_t *largerBuffer = static_cast<uint8_t *>(ps_malloc(frame->len));
    if (!largerBuffer) {
      esp_camera_fb_return(frame);
      return false;
    }
    free(latestJpeg);
    latestJpeg = largerBuffer;
    latestJpegCapacity = frame->len;
  }
  memcpy(latestJpeg, frame->buf, frame->len);
  latestJpegLength = frame->len;

  bool ok = fmt2rgb888(frame->buf, frame->len, PIXFORMAT_JPEG, snapshotBuffer);
  esp_camera_fb_return(frame);
  if (!ok) return false;
  ei::image::processing::crop_and_interpolate_rgb888(
    snapshotBuffer, RAW_WIDTH, RAW_HEIGHT, snapshotBuffer,
    EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT);
  return true;
}

static void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 10000) delay(200);
}

static void runVision() {
  if (!captureImage()) { Serial.println("Camera capture failed."); return; }
  ei::signal_t signal;
  signal.total_length = EI_CLASSIFIER_INPUT_WIDTH * EI_CLASSIFIER_INPUT_HEIGHT;
  signal.get_data = cameraGetData;
  ei_impulse_result_t result{};
  EI_IMPULSE_ERROR error = run_classifier(&signal, &result, false);
  if (error != EI_IMPULSE_OK) { Serial.printf("Inference failed: %d\n", error); return; }

  float probabilities[3] = {0, 0, 0};
  for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    float vision = result.classification[i].value;
    const char *label = result.classification[i].label;
    Serial.printf("%s: %.1f%%\n", label, vision * 100);
    if (strcmp(label, "apple") == 0) probabilities[0] = vision;
    else if (strcmp(label, "banana") == 0) probabilities[1] = vision;
    else if (strcmp(label, "grapes") == 0) probabilities[2] = vision;
  }
  float means[3] = {0, 0, 0};
  float spread = 0;
  updateProbabilityWindow(probabilities, means, &spread);
  if (probabilityWindowCount < VISION_WINDOW_SIZE) {
    Serial.printf("Vision window warming up: %u/%u\n", (unsigned)probabilityWindowCount,
                  (unsigned)VISION_WINDOW_SIZE);
    return;
  }
  float first = 0, second = 0;
  for (float value : means) {
    if (value > first) { second = first; first = value; }
    else if (value > second) second = value;
  }
  float margin = first - second;
  ensureWiFi();
  if (WiFi.status() != WL_CONNECTED) { Serial.println("WiFi unavailable; result not sent."); return; }
  String observationId = bootId + "-" + String(++observationSequence);
  String body = "observation_id=" + observationId
              + "&captured_ms=" + String(millis())
              + "&sample_count=" + String(probabilityWindowCount)
              + "&spread=" + String(spread, 5)
              + "&margin=" + String(margin, 5)
              + "&apple=" + String(means[0], 5)
              + "&banana=" + String(means[1], 5)
              + "&grapes=" + String(means[2], 5);
  int status = -1;
  String response;
  for (int attempt = 0; attempt < 2 && status != 200; attempt++) {
    HTTPClient http;
    http.setConnectTimeout(FUSION_HTTP_TIMEOUT_MS);
    http.setTimeout(FUSION_HTTP_TIMEOUT_MS);
    http.begin(FUSION_URL);
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    status = http.POST(body);
    response = status > 0 ? http.getString() : "";
    http.end();
    if (status != 200 && attempt == 0) delay(250);
  }
  latestResult = response.length() ? response : "{\"ok\":false,\"msg\":\"主程序暂时不可达\"}";
  Serial.printf("Main controller (%d): %s\n", status, response.c_str());
  digitalWrite(STATUS_LED, status == 200 ? LOW : HIGH);
}

static void handleCapture() {
  if (!latestJpeg || latestJpegLength == 0) {
    web.send(503, "text/plain", "Waiting for the first inference frame");
    return;
  }
  WiFiClient client = web.client();
  web.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  web.setContentLength(latestJpegLength);
  web.send(200, "image/jpeg", "");
  client.write(latestJpeg, latestJpegLength);
}

static void handleDashboard() {
  static const char page[] PROGMEM = R"HTML(
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>水果视觉与重量融合</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f7;color:#18202a;font-family:Arial,"Microsoft YaHei",sans-serif}.wrap{max-width:760px;margin:auto;padding:18px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.head h1{font-size:20px;margin:0}.head span{font-size:12px;color:#007a5c;background:#e8f6f1;padding:5px 8px;border-radius:4px}.camera{background:#111;border-radius:8px;overflow:hidden;aspect-ratio:4/3}.camera img{width:100%;height:100%;object-fit:cover;display:block}.card{margin-top:14px;background:#fff;border:1px solid #e1e6ea;border-radius:8px;padding:16px}.status{font-size:18px;font-weight:700}.meta{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.meta div{background:#f7f9fa;padding:10px;border-radius:6px}.meta span{display:block;font-size:11px;color:#687386}.meta b{font-size:16px}.raw{font:12px monospace;color:#687386;white-space:pre-wrap;margin-top:12px}@media(max-width:480px){.wrap{padding:12px}.meta{grid-template-columns:1fr 1fr}}</style>
<body><main class="wrap"><header class="head"><h1>水果视觉与重量融合</h1><span id="state">连接中</span></header><section class="camera"><img id="camera" alt="摄像头画面"></section><section class="card"><div class="status" id="decision">等待识别结果</div><div class="meta"><div><span>识别商品</span><b id="name">-</b></div><div><span>融合置信度</span><b id="confidence">-</b></div><div><span>HX711 重量</span><b id="weight">-</b></div><div><span>重量稳定</span><b id="stable">-</b></div></div><div class="raw" id="raw"></div></section></main><script>const img=document.getElementById('camera');function refreshFrame(){img.onload=()=>setTimeout(refreshFrame,500);img.onerror=()=>setTimeout(refreshFrame,1000);img.src='/capture?t='+Date.now()}refreshFrame();async function pull(){try{let d=await (await fetch('/result?t='+Date.now())).json();document.getElementById('state').textContent=d.ok?'已连接主程序':'等待主程序';document.getElementById('decision').textContent=d.accepted?'已确认：'+d.name:'请人工确认';document.getElementById('name').textContent=d.name||'-';document.getElementById('confidence').textContent=d.confidence!==undefined?(d.confidence*100).toFixed(1)+'%':'-';document.getElementById('weight').textContent=d.weight_g!==undefined?d.weight_g.toFixed(1)+' g':'-';document.getElementById('stable').textContent=d.stable?'稳定':'待稳定';document.getElementById('raw').textContent=JSON.stringify(d,null,2)}catch(e){document.getElementById('state').textContent='等待识别'}}pull();setInterval(pull,1000)</script></body></html>)HTML";
  web.send_P(200, "text/html; charset=utf-8", page);
}

void setup() {
  Serial.begin(115200);
  delay(1200);
  bootId = String((uint32_t)esp_random(), HEX);
  pinMode(STATUS_LED, OUTPUT); digitalWrite(STATUS_LED, HIGH);

  if (!psramFound()) { Serial.println("PSRAM is required. Enable OPI PSRAM in board settings."); while (true) delay(1000); }
  snapshotBuffer = static_cast<uint8_t *>(ps_malloc(RAW_WIDTH * RAW_HEIGHT * 3));
  if (!snapshotBuffer) { Serial.println("Failed to allocate camera buffer."); while (true) delay(1000); }
  esp_err_t cameraError = esp_camera_init(&cameraConfig);
  if (cameraError != ESP_OK) { Serial.printf("Camera init failed: 0x%x\n", cameraError); while (true) delay(1000); }
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor && sensor->id.PID == OV3660_PID) { sensor->set_vflip(sensor, 1); sensor->set_brightness(sensor, 1); sensor->set_saturation(sensor, 0); }
  ensureWiFi();
  web.on("/", HTTP_GET, handleDashboard);
  web.on("/capture", HTTP_GET, handleCapture);
  web.on("/result", HTTP_GET, []() { web.send(200, "application/json; charset=utf-8", latestResult); });
  web.begin();
  Serial.printf("Vision node ready, IP: %s\n", WiFi.localIP().toString().c_str());
  Serial.println("Open http://<XIAO-IP>/ to view camera and fusion result.");
}

void loop() {
  web.handleClient();
  if (millis() - lastInferenceMs < INFERENCE_INTERVAL_MS) { delay(10); return; }
  lastInferenceMs = millis();
  runVision();
}
