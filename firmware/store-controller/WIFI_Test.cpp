#include "WIFI_Test.h"
#include "secrets.h"        // 私密配置（密钥/地址），不进版本库
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>


    //连接WIFI
const char* ssid1 = WIFI_STA_SSID;
const char* password1 = WIFI_STA_PASSWORD;

void wifi_init()
{
    // 开启串口
    Serial.begin(115200);

    // 开始连接 WiFi
    WiFi.begin(ssid1, password1);
    Serial.println("正在连接 WiFi...");

    // 等待连接成功
    while (WiFi.status() != WL_CONNECTED)
    {
        delay(500);
        Serial.print(".");
    }

    // 连接成功提示
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


