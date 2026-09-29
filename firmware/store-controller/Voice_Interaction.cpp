#include "Voice_Interaction.h"
#include "secrets.h"        // 私密配置（密钥/地址），不进版本库
#include <driver/i2s.h>
#include "led.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "AI_Test.h"
#include <Wire.h>
#include "HC_SR04.h"
#include "Mode_LowPower.h"
#include "WebServer.h"

#include <time.h>
#include <project_1062783_inferencing.h>

#define XL9555_ADDR 0x20
#define I2C_SDA 10
#define I2C_SCL 11

// 【新增】：定义全局语音队列
String pendingVoiceTask = "";

extern String global_voice_q;
extern String global_voice_a;
extern bool global_voice_show;

static bool notify_miniclaw_human_service();

// I2S 工作模式
static enum { MODE_IDLE, MODE_RECORD, MODE_PLAY, MODE_WAKEUP } currentMode = MODE_IDLE;

// 区分当前录音是“按键触发”还是“语音唤醒”
static bool isVoiceTriggered = false;

// ========== 录音相关配置 ==========
#define MAX_RECORD_SECONDS 10                 
#define MAX_SAMPLES (8000 * MAX_RECORD_SECONDS) 
static int16_t* recordBuffer = nullptr;
static size_t   recordLength = 0;
bool     isRecording = false;

// ========== 百度语音识别配置 ==========
const char* baidu_api_key    = BAIDU_API_KEY;
const char* baidu_secret_key = BAIDU_SECRET_KEY;

// ==========================================
// 🔌 硬件底层控制：控制 XL9555 唤醒功放
// ==========================================
static void SetSpeakerPower(bool enable) {
  Wire.beginTransmission(XL9555_ADDR);
  Wire.write(0x02); 
  Wire.write(enable ? 0x01 : 0x00); 
  Wire.endTransmission();
}

// ==========================================
// 🎤 I2S 录音与播放驱动封装
// ==========================================
static void startRecording()
{
  if (currentMode == MODE_RECORD) return;
  if (currentMode != MODE_IDLE) i2s_driver_uninstall(I2S_NUM_0);

  i2s_config_t i2s_in = {};
  i2s_in.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  i2s_in.sample_rate          = 8000; 
  i2s_in.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT; 
  i2s_in.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT; 
  i2s_in.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  i2s_in.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  i2s_in.dma_buf_count        = 4;
  i2s_in.dma_buf_len          = 128;

  i2s_pin_config_t pin_in = {};
  pin_in.bck_io_num   = I2S_SCK;
  pin_in.ws_io_num     = I2S_WS;
  pin_in.data_in_num   = I2S_SD;
  pin_in.data_out_num  = I2S_PIN_NO_CHANGE;

  i2s_driver_install(I2S_NUM_0, &i2s_in, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pin_in);
  currentMode = MODE_RECORD;
}

static void startPlayback()
{
  if (currentMode == MODE_PLAY) return;
  if (currentMode != MODE_IDLE) i2s_driver_uninstall(I2S_NUM_0);

  // 🌟 核心补丁：播放前唤醒功放
  SetSpeakerPower(true);
  delay(20); 

  i2s_config_t i2s_out = {};
  i2s_out.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  i2s_out.sample_rate          = 8000; 
  i2s_out.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  
  // 🌟 关键修复：必须改回双声道！因为硬件电路决定了功放只听“右声道”
  i2s_out.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT; 
  
  i2s_out.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  i2s_out.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  i2s_out.dma_buf_count        = 4;  
  i2s_out.dma_buf_len          = 256; 
  i2s_out.tx_desc_auto_clear   = true; 

  i2s_pin_config_t pin_out = {};
  pin_out.bck_io_num    = I2S_BCLK;
  pin_out.ws_io_num     = I2S_LRC;
  pin_out.data_out_num  = I2S_DOUT;
  pin_out.data_in_num   = I2S_PIN_NO_CHANGE;

  i2s_driver_install(I2S_NUM_0, &i2s_out, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pin_out);
  i2s_zero_dma_buffer(I2S_NUM_0);
  currentMode = MODE_PLAY;
}

static void stopI2S()
{
  if (currentMode != MODE_IDLE)
  {
    i2s_driver_uninstall(I2S_NUM_0);
    currentMode = MODE_IDLE;
    
    // 🌟 播放完毕后切断功放电源
    SetSpeakerPower(false);
  }
}

// ==========================================
// 🚀 模块初始化
// ==========================================
void Voice_Init()
{
  Serial.println("语音模块初始化...");
  
  // 1. 强制在语音模块里打通 I2C
  Wire.begin(I2C_SDA, I2C_SCL);
  
  // 2. 强制配置 XL9555 IO0_0 为输出
  Wire.beginTransmission(XL9555_ADDR);
  Wire.write(0x06); 
  Wire.write(0xFE); 
  Wire.endTransmission();

  // 3. 默认关闭功放，切断底噪
  SetSpeakerPower(false);

  if (currentMode != MODE_IDLE) {
    i2s_driver_uninstall(I2S_NUM_0);
    currentMode = MODE_IDLE;
  }
  
  Serial.println("语音模块初始化完成！");
}

void Voice_HandleLoop()
{
  if (pendingVoiceTask.length() > 0) 
  {
    String textToPlay = pendingVoiceTask;
    pendingVoiceTask = ""; 
    Voice_PlayTTS(textToPlay);

    if (isVoiceTriggered) {
        Voice_StartRecording();
    }
  }

}

void Voice_PlayAudio(int16_t* buffer, size_t length)
{
  startPlayback();
  size_t bytesWritten;
  i2s_write(I2S_NUM_0, buffer, length * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
  stopI2S();
}

void Voice_TestSpeaker()
{
  startPlayback();

  int sampleRate = 8000; 
  int duration = 1;
  int totalSamples = sampleRate * duration;
  int16_t* samples = (int16_t*)malloc(totalSamples * sizeof(int16_t));
  if (!samples) return;

  float phase = 0;
  for (int i = 0; i < totalSamples; i++)
  {
    samples[i] = (int16_t)(sin(phase) * 5000);
    phase += 2.0 * PI * 440.0 / sampleRate;
    if (phase > 2.0 * PI) phase -= 2.0 * PI;
  }

  size_t bytesWritten;
  i2s_write(I2S_NUM_0, samples, totalSamples * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
  free(samples);

  stopI2S();
}

void Voice_StartRecording()
{
  if (isRecording) return;

  // 弹出网页字幕提示录音中
  global_voice_q = "正在倾听...";
  global_voice_a = "等待顾客说话...";
  global_voice_show = true;

  startRecording();

  if (recordBuffer == nullptr) {
    // 强制在 PSRAM (SPIRAM) 中分配 160KB 录音缓存，坚决不占用内部 RAM
    recordBuffer = (int16_t*)heap_caps_malloc(MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM); 
  }
  recordLength = 0;

  if (!recordBuffer) {
    Serial.println("内部内存不足，录音缓存分配失败！");
    delay(500); 
    return;
  }

  isRecording = true;
  Serial.println("开始录音...");
  rec_led_set(true);      // 亮双红灯
  led_low_power_mode(false); // 待机灯切到欢迎状态
}

void Voice_StopRecording()
{
  if (!isRecording) return;
  isRecording = false;
  stopI2S();
  Serial.printf("录音结束，长度：%d 字节\n", recordLength * 2);
  rec_led_set(false);     // 熄灭红灯
  led_low_power_mode(true);  // 恢复待机蓝灯
}

String Voice_GetBaiduToken()
{
  HTTPClient http;
  WiFiClientSecure client;
  client.setInsecure(); 

  String url = "https://aip.baidubce.com/oauth/2.0/token";
  url += "?grant_type=client_credentials";
  url += "&client_id=" + String(baidu_api_key);
  url += "&client_secret=" + String(baidu_secret_key);

  http.begin(client, url);
  http.addHeader("Host", "aip.baidubce.com");
  http.addHeader("User-Agent", "ESP32S3-Client");

  int httpCode = http.GET();
  String token = "";
  if (httpCode == 200) {
    String response = http.getString();
    DynamicJsonDocument doc(1024);
    deserializeJson(doc, response);
    token = doc["access_token"].as<String>();
    Serial.println("百度Token获取成功");
  } else {
    Serial.printf("百度Token获取失败，状态码：%d\n", httpCode);
  }
  http.end();
  return token;
}

String Voice_URLEncode(const String& str)
{
  String encodedString = "";
  char c, code0, code1;
  for (int i = 0; i < str.length(); i++) {
    c = str.charAt(i);
    if (c == ' ') {
      encodedString += '+';
    } else if (isalnum(c)) {
      encodedString += c;
    } else {
      code1 = (c & 0xf) + '0';
      if ((c & 0xf) > 9) code1 = (c & 0xf) - 10 + 'A';
      c = (c >> 4) & 0xf;
      code0 = c + '0';
      if (c > 9) code0 = c - 10 + 'A';
      encodedString += '%';
      encodedString += code0;
      encodedString += code1;
    }
  }
  return encodedString;
}

void Voice_PlayTTS(String text) 
{
  String token = Voice_GetBaiduToken();
  if (token.length() == 0) return;

  Serial.println("正在合成语音并流式播报...");

  HTTPClient http;
  WiFiClient client; 

  String url = "http://tsn.baidu.com/text2audio";
  String payload = "tex=" + Voice_URLEncode(text) +
                   "&tok=" + token +
                   "&cuid=esp32_guide" +
                   "&ctp=1" +
                   "&lan=zh" +
                   "&spd=5" +
                   "&pit=5" +
                   "&vol=6" +
                   "&per=103" + 
                   "&aue=5";

  http.begin(client, url);
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");

  int httpCode = http.POST(payload);

  if (httpCode == 200 || httpCode == 206) {
    startPlayback(); 

    WiFiClient* stream = http.getStreamPtr();
    uint8_t buff[512];         
    int16_t stereoBuff[512];   // 🌟 转换后的双声道数据缓冲区

    int len = http.getSize();
    int bytesToRead = len;

    while (http.connected() && (bytesToRead > 0 || len == -1)) {
      if (key_isPressed()) {
        Serial.println("【强制打断】检测到按键，停止播报并切换至录音！");
        break; 
      }
      size_t size = stream->available();
      if (size) {
        int c = stream->readBytes(buff, ((size > sizeof(buff)) ? sizeof(buff) : size));
        
        // 🌟 核心克隆：将单声道(Mono)强行复制给双声道(Stereo)
        int samples = c / 2; 
        int16_t* monoData = (int16_t*)buff;

        for (int i = 0; i < samples; i++) {
          stereoBuff[i * 2]     = monoData[i]; // 左声道
          stereoBuff[i * 2 + 1] = monoData[i]; // 右声道 (功放 NS4168 真正监听的通道)
        }

        size_t bytesWritten;
        i2s_write(I2S_NUM_0, stereoBuff, samples * 4, &bytesWritten, portMAX_DELAY);
        
        if (len > 0) bytesToRead -= c;
      } else {
        delay(1);
      }
    }
    stopI2S(); 

    delay(2000); 
    global_voice_show = false;

    Serial.println("语音播报结束/中止。");
  } else {
    Serial.printf("TTS合成失败，状态码：%d\n", httpCode);
  }
  http.end();
}

void Voice_PlayRecording()
{
  if (!recordBuffer || recordLength == 0) return;

  // ================= 升级版 VAD：去除直流偏置，计算真实波动能量 =================
  long totalSum = 0;
  for (size_t i = 0; i < recordLength; i++) {
    totalSum += recordBuffer[i];
  }
  int16_t dcOffset = totalSum / (long)recordLength;

  long totalAcEnergy = 0;
  for (size_t i = 0; i < recordLength; i++) {
    totalAcEnergy += abs(recordBuffer[i] - dcOffset);
  }
  long trueEnergy = totalAcEnergy / recordLength;
  
  Serial.println("\n----------------------------------------");
  Serial.printf("🔍 【音量探针】系统直流偏置: %d\n", dcOffset);
  Serial.printf("🔍 【音量探针】去除偏置后的真实声音能量: %ld\n", trueEnergy);
  Serial.println("----------------------------------------\n");

  if (trueEnergy < 50) {
    Serial.println("🛑 【拦截】检测到纯环境底噪，顾客未开口说话，取消上传百度！");
    return; 
  }
  // =========================================================================

  Serial.println("开始云端语音识别(百度ASR)...");
  String token = Voice_GetBaiduToken();
  if (token.length() == 0) return;

  String response = "";

  {
    WiFiClientSecure client;
    client.setInsecure();

    if (!client.connect("vop.baidu.com", 443)) {
      Serial.println("连接百度服务器失败！");
      return;
    }

    size_t cloudPcmSize = recordLength * sizeof(int16_t) * 2;
    String url = "/server_api?dev_pid=1537&token=" + token + "&cuid=esp32s3";

    String head = "POST " + url + " HTTP/1.1\r\n";
    head += "Host: vop.baidu.com\r\n";
    head += "Content-Type: audio/pcm;rate=16000\r\n"; 
    head += "Content-Length: " + String(cloudPcmSize) + "\r\n";
    head += "Connection: close\r\n\r\n";
    client.print(head);

    const size_t CHUNK_SAMPLES = 512;
    int16_t upsampleBuffer[CHUNK_SAMPLES * 2]; 
    size_t readIndex = 0;

    while (readIndex < recordLength) {
      size_t samplesToProcess = (recordLength - readIndex > CHUNK_SAMPLES) ? CHUNK_SAMPLES : (recordLength - readIndex);
      for (size_t i = 0; i < samplesToProcess; i++) {
        int16_t sourceSample = recordBuffer[readIndex + i];
        upsampleBuffer[i * 2]     = sourceSample;
        upsampleBuffer[i * 2 + 1] = sourceSample;
      }
      client.write((uint8_t*)upsampleBuffer, samplesToProcess * 2 * sizeof(int16_t));
      readIndex += samplesToProcess;
      delay(2); 
    }

    while (client.connected() || client.available()) {
      if (client.available()) {
        response += client.readStringUntil('\n');
      }
    }
    client.stop();
  } 

  int jsonStart = response.indexOf("{");
  if (jsonStart >= 0) {
    response = response.substring(jsonStart);
    DynamicJsonDocument doc(1024);
    deserializeJson(doc, response);

    String resultText = doc["result"][0].as<String>();
    resultText.trim(); 

    if (resultText == "null" || resultText == "" || resultText == "，" || resultText == "。" || resultText.indexOf("不知道") >= 0 || resultText == "嗯。") {
      Serial.println("识别内容为空或无意义，直接跳过AI思考。");
    } else if (resultText.indexOf("人工服务") >= 0) {
      Serial.println("\n[人工服务] ASR 已识别人工服务请求");
      global_voice_q = resultText;
      global_voice_a = "已通知工作人员，请稍候。";
      global_voice_show = true;
      if (notify_miniclaw_human_service()) {
        Voice_PlayTTS("已通知工作人员，请稍候。");
      }
    } else if (resultText.length() > 0) {
      Serial.println("\n========== 顾客提问 ==========");
      Serial.println(resultText);
      Serial.println("==============================\n");

      Serial.println("正在向千问大模型思考回答...");
      String aiAnswer = AI_ProcessQuestion(resultText); 
      
      Serial.println("\n========== 导购员回答 ==========");
      Serial.println(aiAnswer);
      Serial.println("================================\n");

      Voice_PlayTTS(aiAnswer);
    }
  }
}

void Voice_RecordLoop()
{
  if (!isRecording) return;
  int16_t temp[160];
  size_t bytesRead;
  esp_err_t err = i2s_read(I2S_NUM_0, temp, sizeof(temp), &bytesRead, 0);

  if (err == ESP_OK && bytesRead > 0 && recordBuffer)
  {
    size_t samplesRead = bytesRead / 2;
    if (recordLength + samplesRead < MAX_SAMPLES)
    {
      memcpy(recordBuffer + recordLength, temp, bytesRead);
      recordLength += samplesRead;

      if (isVoiceTriggered && recordLength >= (8000 * 4)) {
          Voice_StopRecording();
          Voice_PlayRecording();
          isVoiceTriggered = false; 
      }
    }
    else
    {
      isRecording = false;
      stopI2S();
      Serial.println("录音已达到最大容量，自动停止");
      
      if (isVoiceTriggered) {
          Voice_PlayRecording();
          isVoiceTriggered = false;
      }
    }
  }
}

void Voice_HandleKey()
{
  if (key_isPressed())
  {
    isVoiceTriggered = false; 
    Voice_StartRecording();
  }
  else
  {
    if (isRecording && !isVoiceTriggered)
    {
      Voice_StopRecording();
      Voice_PlayRecording();
    }
  }
}

// ==========================================
// 边缘 AI 相关逻辑 (保留原貌)
// ==========================================
void Test_SpeakerHardware() 
{
    // ... 原样保留
}

static int16_t* wakeupBuffer = nullptr;
static size_t wakeupIndex = 0;

static void startWakeupListen()
{
  if (currentMode == MODE_WAKEUP) return;
  if (currentMode != MODE_IDLE) i2s_driver_uninstall(I2S_NUM_0);

  i2s_config_t i2s_in = {};
  i2s_in.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  i2s_in.sample_rate          = 16000; 
  i2s_in.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT; 
  i2s_in.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT; 
  i2s_in.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  i2s_in.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  i2s_in.dma_buf_count        = 4;
  i2s_in.dma_buf_len          = 256;

  i2s_pin_config_t pin_in = {};
  pin_in.bck_io_num   = I2S_SCK;
  pin_in.ws_io_num     = I2S_WS;
  pin_in.data_in_num   = I2S_SD;
  pin_in.data_out_num  = I2S_PIN_NO_CHANGE;

  i2s_driver_install(I2S_NUM_0, &i2s_in, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pin_in);
  currentMode = MODE_WAKEUP;

  if (wakeupBuffer == nullptr) {
    wakeupBuffer = (int16_t*)heap_caps_malloc(EI_CLASSIFIER_RAW_SAMPLE_COUNT * sizeof(int16_t), MALLOC_CAP_SPIRAM);
  }
  wakeupIndex = 0;
}

int get_wakeup_audio_data(size_t offset, size_t length, float *out_ptr) {
    numpy::int16_to_float(&wakeupBuffer[offset], out_ptr, length);
    return 0;
}


// ── 转人工通知：miniclaw 地址 ──────────────────────────────
#define MINICLAW_IP      "192.168.43.100"   // miniclaw 的局域网 IP
#define MINICLAW_PORT    18791

static unsigned long s_last_human_service_notify = 0;

static bool notify_miniclaw_human_service()
{
    unsigned long now = millis();
    // 冷却 30 秒，避免重复发送
    if (s_last_human_service_notify != 0 &&
        now - s_last_human_service_notify < 30000) return false;
    s_last_human_service_notify = now;

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[人工服务] WiFi 未连接，跳过通知");
        return false;
    }

    WiFiClient client;
    HTTPClient http;

    String url = "http://" + String(MINICLAW_IP) + ":" + String(MINICLAW_PORT) + "/alert";
    http.begin(client, url);
    http.addHeader("Content-Type", "application/json");

    String requestId = WebServer_CreateHumanServiceRequest();
    if (requestId.length() == 0) {
        Serial.println("[人工服务] 已有活动工单，跳过重复通知");
        return false;
    }

    StaticJsonDocument<256> doc;
    doc["type"]  = "human_service";
    doc["store"] = "智慧超市";
    doc["request_id"] = requestId;
    doc["terminal"] = "1号自助终端";
    char time_buf[32];
    struct tm time_info = {};
    if (getLocalTime(&time_info, 100) && time_info.tm_year + 1900 >= 2024) {
        strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", &time_info);
    } else {
        strlcpy(time_buf, "时间未同步", sizeof(time_buf));
    }
    doc["time"]  = time_buf;
    String body;
    serializeJson(doc, body);

    int code = http.POST(body);
    if (code > 0) {
        Serial.printf("[人工服务] 已通知 miniclaw, HTTP %d\n", code);
    } else {
        Serial.printf("[人工服务] 通知 miniclaw 失败: %s\n", http.errorToString(code).c_str());
    }
    http.end();
    return code > 0 && code < 300;
}

#define AUTO_SLEEP_NOISE_SECONDS 10  // 🌟 定义宏：连续10秒纯噪声则进入低功耗

void Voice_WakeupLoop()
{
  // ================= 0. 全局冷却与互锁机制 =================
  static unsigned long wakeup_cooldown = 0;
  static int continuous_noise_count = 0; 
  
  if (isRecording || pendingVoiceTask.length() > 0 || currentMode == MODE_PLAY || currentMode == MODE_RECORD) {
    if (currentMode == MODE_WAKEUP) stopI2S(); 
    wakeup_cooldown = millis() + 1500; 
    continuous_noise_count = 0; 
    return;
  }

  if (millis() < wakeup_cooldown) return;

  if (currentMode != MODE_WAKEUP) startWakeupListen();

  size_t bytesRead;
  int16_t tempBuf[128];
  esp_err_t err = i2s_read(I2S_NUM_0, tempBuf, sizeof(tempBuf), &bytesRead, 0);

  if (err == ESP_OK && bytesRead > 0 && wakeupBuffer) {
    size_t samplesRead = bytesRead / 2;
    for (size_t i = 0; i < samplesRead; i++) {
      wakeupBuffer[wakeupIndex++] = tempBuf[i];
      
      if (wakeupIndex >= EI_CLASSIFIER_RAW_SAMPLE_COUNT) {
        
        long totalSum = 0;
        for(size_t e = 0; e < EI_CLASSIFIER_RAW_SAMPLE_COUNT; e++) {
          totalSum += wakeupBuffer[e];
        }
        int16_t currentDcOffset = totalSum / (long)EI_CLASSIFIER_RAW_SAMPLE_COUNT;
        
        for(size_t e = 0; e < EI_CLASSIFIER_RAW_SAMPLE_COUNT; e++) {
          wakeupBuffer[e] = wakeupBuffer[e] - currentDcOffset;
        }

        signal_t signal;
        signal.total_length = EI_CLASSIFIER_RAW_SAMPLE_COUNT;
        signal.get_data = &get_wakeup_audio_data;
        ei_impulse_result_t result = {0};

        EI_IMPULSE_ERROR r = run_classifier(&signal, &result, false);

        if (r == EI_IMPULSE_OK) {
          bool isTriggered = false;
          bool isCurrentSecondNoise = false; 
          
          for (size_t ix = 0; ix < EI_CLASSIFIER_LABEL_COUNT; ix++) {
            
            // 【唤醒判断 1：导购员】
            if (strcmp(result.classification[ix].label, "导购员") == 0 && result.classification[ix].value > 0.75) 
            {
              bool isPersonNear = HC_SR04_IsPersonNear(); 
              
              if (isPersonNear) { 
                Serial.printf("\n🎉 【多模态唤醒成功】检测到前方有人(距离<%dcm)，且精准捕捉: %s\n", DETECT_DISTANCE, result.classification[ix].label);
                isTriggered = true;
                break;
              }
            }

            // 🌟 【唤醒判断 2：人工服务】
            if (strcmp(result.classification[ix].label, "人工服务") == 0 && result.classification[ix].value > 0.75)
            {
              Serial.println("\n🛎️ 【运维呼叫】精准捕捉关键词：人工服务！");
              notify_miniclaw_human_service();
            }
            
            // 【噪声判断】
            if ((strcmp(result.classification[ix].label, "噪声") == 0 || 
                 strcmp(result.classification[ix].label, "噪声2") == 0) && 
                result.classification[ix].value > 0.70) 
            {
              isCurrentSecondNoise = true;
            }
          }

          // ================= 触发退出低功耗（唤醒打招呼） =================
          if (isTriggered) {
             stopI2S(); 
             wakeupIndex = 0; 
             isVoiceTriggered = false; 
             continuous_noise_count = 0; 

             if (LowPower_GetMode() != MODE_NORMAL) {
                 LowPower_SetMode(MODE_NORMAL); 
                 led_low_power_mode(false);     
                 pendingVoiceTask = "您好呀，有什么能够帮到您呢？";
             }
             return; 
          } 
          // ================= 触发进入低功耗（环境无声待机） =================
          else {
             if (isCurrentSecondNoise) {
                 continuous_noise_count++;
                 
                 if (continuous_noise_count >= AUTO_SLEEP_NOISE_SECONDS) {
                     if (LowPower_GetMode() != MODE_MODEM_SLEEP) {
                         Serial.println("\n💤 【自动休眠触发】连续 10 秒无人说话，恢复低功耗待机！\n");
                         LowPower_SetMode(MODE_MODEM_SLEEP); 
                         led_low_power_mode(true);           
                     }
                     continuous_noise_count = 0;         
                 }
             } else {
                 continuous_noise_count = 0;
             }
          }
        }
        
        wakeupIndex = 0; 
      }
    }
  }
}

static int16_t* pureTestBuffer = nullptr;
static size_t pureTestIndex = 0;
static bool isPureTesting = false;

int get_pure_test_audio(size_t offset, size_t length, float *out_ptr) {
  numpy::int16_to_float(&pureTestBuffer[offset], out_ptr, length);
  return 0;
}

void Test_Pure_AI_Model() {
  if (key_isPressed()) {
    if (!isPureTesting) {
      Serial.println("\n【纯净测试】按键已按下，开始录制测试音频...");
      
      i2s_driver_uninstall(I2S_NUM_0);
      delay(20); 
      
      i2s_config_t i2s_in = {};
      i2s_in.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
      i2s_in.sample_rate          = 16000;
      i2s_in.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT; 
      i2s_in.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT; 
      i2s_in.communication_format = I2S_COMM_FORMAT_STAND_I2S;
      i2s_in.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
      i2s_in.dma_buf_count        = 4;
      i2s_in.dma_buf_len          = 256;

      i2s_pin_config_t pin_in = {};
      pin_in.bck_io_num   = I2S_SCK;
      pin_in.ws_io_num    = I2S_WS;
      pin_in.data_in_num  = I2S_SD;
      pin_in.data_out_num = I2S_PIN_NO_CHANGE;

      i2s_driver_install(I2S_NUM_0, &i2s_in, 0, NULL);
      i2s_set_pin(I2S_NUM_0, &pin_in);

      if (!pureTestBuffer) {
          pureTestBuffer = (int16_t*)heap_caps_malloc(EI_CLASSIFIER_RAW_SAMPLE_COUNT * sizeof(int16_t), MALLOC_CAP_SPIRAM);
      }
      
      memset(pureTestBuffer, 0, EI_CLASSIFIER_RAW_SAMPLE_COUNT * sizeof(int16_t));
      pureTestIndex = 0;
      isPureTesting = true;
    }

    if (pureTestIndex < EI_CLASSIFIER_RAW_SAMPLE_COUNT) {
      size_t bytesRead = 0; 
      int16_t tempBuf[128] = {0};
      
      esp_err_t read_err = i2s_read(I2S_NUM_0, tempBuf, sizeof(tempBuf), &bytesRead, portMAX_DELAY);
      
      if (read_err == ESP_OK && bytesRead > 0) {
        size_t samplesRead = bytesRead / 2;
        for (size_t i = 0; i < samplesRead && pureTestIndex < EI_CLASSIFIER_RAW_SAMPLE_COUNT; i++) {
          pureTestBuffer[pureTestIndex++] = tempBuf[i];
        }
      }
    }
  } 
  else {
    if (isPureTesting) {
      isPureTesting = false;
      i2s_driver_uninstall(I2S_NUM_0); 
      
      Serial.println("【纯净测试】按键已松开，麦克风已关闭。");
      
      if (pureTestIndex < EI_CLASSIFIER_RAW_SAMPLE_COUNT) {
        Serial.printf("【纯净测试】提前松手 (已录 %d 点)，正在自动用静音补齐剩余尾部...\n", pureTestIndex);
        while (pureTestIndex < EI_CLASSIFIER_RAW_SAMPLE_COUNT) {
          pureTestBuffer[pureTestIndex++] = 0; 
        }
      }

      Serial.println("【纯净测试】声音处理完毕，正在进行 VAD 音量检测...");
      
      long totalEnergy = 0;
      for (size_t i = 0; i < EI_CLASSIFIER_RAW_SAMPLE_COUNT; i++) {
        totalEnergy += abs(pureTestBuffer[i]);
      }
      long avgEnergy = totalEnergy / EI_CLASSIFIER_RAW_SAMPLE_COUNT;
      Serial.printf("【VAD检测】当前音频平均能量值: %ld\n", avgEnergy);

      if (avgEnergy < 200) {
          Serial.println("🛑 【拦截成功】检测到当前为纯空气底噪，声音太小，不进行 AI 推理！");
      } 
      else {
          Serial.println("✅ 【VAD通过】检测到有效声音，开始 AI 推理...");
          
          signal_t signal;
          signal.total_length = EI_CLASSIFIER_RAW_SAMPLE_COUNT;
          signal.get_data = &get_pure_test_audio;
          ei_impulse_result_t result = {0};

          EI_IMPULSE_ERROR r = run_classifier(&signal, &result, false);

          if (r == EI_IMPULSE_OK) {
            Serial.println("\n========== 模型得分分析 ==========");
            for (size_t ix = 0; ix < EI_CLASSIFIER_LABEL_COUNT; ix++) {
              Serial.printf("    %s: %.5f\n", result.classification[ix].label, result.classification[ix].value);
            }
            Serial.println("==================================");
            
            for (size_t ix = 0; ix < EI_CLASSIFIER_LABEL_COUNT; ix++) {
              if (strcmp(result.classification[ix].label, "导购员") == 0 && result.classification[ix].value > 0.8) {
                Serial.println("🎉 【唤醒】精确捕捉到关键词：导购员！");
              }
              if (strcmp(result.classification[ix].label, "人工服务") == 0 && result.classification[ix].value > 0.8) {
                Serial.println("🛎️ 【唤醒】精确捕捉到关键词：人工服务！");
              }
            }
          } else {
            Serial.printf("模型运行失败，底层错误码：%d\n", r);
          }
      }
    }
  }
}

void Tool_Data_Forwarder() {
  i2s_driver_uninstall(I2S_NUM_0);
  delay(20); 

  i2s_config_t i2s_in = {};
  i2s_in.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  i2s_in.sample_rate          = 16000;
  i2s_in.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT; 
  i2s_in.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT; 
  i2s_in.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  i2s_in.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  i2s_in.dma_buf_count        = 8;  
  i2s_in.dma_buf_len          = 256;

  i2s_pin_config_t pin_in = {};
  pin_in.bck_io_num   = I2S_SCK;
  pin_in.ws_io_num    = I2S_WS;
  pin_in.data_in_num  = I2S_SD;
  pin_in.data_out_num = I2S_PIN_NO_CHANGE;

  i2s_driver_install(I2S_NUM_0, &i2s_in, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pin_in);

  Serial.begin(115200);

  int16_t tempBuf[128];
  size_t bytesRead;

  while(true) {
    esp_err_t err = i2s_read(I2S_NUM_0, tempBuf, sizeof(tempBuf), &bytesRead, portMAX_DELAY);
    if (err == ESP_OK && bytesRead > 0) {
      size_t samples = bytesRead / 2;
      for (size_t i = 0; i < samples; i++) {
        Serial.println(tempBuf[i]); 
      }
    }
  }
}
