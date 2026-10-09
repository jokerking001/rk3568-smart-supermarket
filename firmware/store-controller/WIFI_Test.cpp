#include "WIFI_Test.h"
#include "secrets.h"        // 私密配置（密钥/地址），不进版本库
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>


    //连接WIFI
const char* ssid1 = WIFI_STA_SSID;
const char* password1 = WIFI_STA_PASSWORD;

void wifi_init(unsigned long timeoutMs)
{
    // 开启串口
    Serial.begin(115200);

    // 开始连接 WiFi
    WiFi.begin(ssid1, password1);
    Serial.printf("正在连接 WiFi（最多等 %lu 秒）...\n", timeoutMs / 1000);

    // 等待连接成功。
    // 原来这里是死循环，现场没网就永远起不来 —— 从机模式下称重/RFID/按键
    // 这些本地外设本来就不依赖网络，不该被 WiFi 卡住整个启动。
    // timeoutMs 传 0 表示回到原来的无限等待。
    unsigned long startedAt = millis();
    while (WiFi.status() != WL_CONNECTED)
    {
        if (timeoutMs > 0 && (millis() - startedAt) >= timeoutMs)
        {
            Serial.println();
            Serial.printf("WiFi 连接超时（%lu 秒），继续启动。\n", timeoutMs / 1000);
            Serial.println("离线仍可跑：称重 / RFID / 扫码枪 / 按键录音。");
            Serial.println("需要联网的部分（TTS 播报、上报 RK）会一直重试。");
            return;
        }
        delay(500);
        Serial.print(".");
    }

    // 连接成功提示
    Serial.println();
    Serial.println("WiFi 连接成功！");
    Serial.print("IP 地址：");
    Serial.println(WiFi.localIP());

    configTime(8 * 3600, 0, "ntp.aliyun.com", "pool.ntp.org");
    struct tm timeinfo;
    if (getLocalTime(&timeinfo, 8000)) {
        Serial.println("NTP 时间同步成功");
    } else {
        Serial.println("NTP 时间暂未同步，后台任务稍后会继续获取");
    }
}


    //创建热点局域网
const char* ssid2 = WIFI_AP_SSID;
const char* password2 = WIFI_AP_PASSWORD;
void WIFI_Set()
{
  Serial.begin(115200);

  // 创建热点
  WiFi.softAP(ssid2, password2);
  delay(2000);

  // 打印 热点IP
  Serial.print("Wi-Fi 接入的IP：");
  Serial.println(WiFi.softAPIP());  
}


String url = "http://apis.juhe.cn/simpleWeather/query";
String city = "南昌";
String key = JUHE_WEATHER_KEY;
    //发送网络请求
void WIFI_Requst()
{
// 创建 HTTPClient 对象
  HTTPClient http;

  // 指定访问 URL
  http.begin(url+"?city="+city+"&key="+key);

  // 接收 HTTP 响应状态码
  int http_code = http.GET();

  Serial.printf("HTTP 状态码：%d\n", http_code);

  // 获取响应正文
  String response = http.getString();
  Serial.print("响应数据：");
  Serial.println(response);

  // 关闭连接
  http.end();

  // 创建 DynamicJsonDocument 对象
  DynamicJsonDocument doc(1024);

  // 解析 JSON 数据
  deserializeJson(doc, response);

  // 从解析的 JSON 数据中获取值
  unsigned int temp = doc["result"]["realtime"]["temperature"].as<unsigned int>();
  String info = doc["result"]["realtime"]["info"].as<String>();
  int aqi = doc["result"]["realtime"]["aqi"].as<int>();

  Serial.printf("温度：%d, 天气：%s, 空气指数: %d\n", temp, info, aqi);
}


