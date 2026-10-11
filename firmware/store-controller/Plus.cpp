#include "Plus.h" 

// 从机模式下要把 DHT22 读数报给 RK3568（见本文件末尾的 DHT22_HandleLoop）。
// 必须引 Slave_Config.h 才知道自己是什么角色 —— 这个宏在头文件里，
// 不在 .ino 里，原因见 Slave_Config.h 顶部那段。
#include "Slave_Config.h"
#if SLAVE_MODE
#include "Slave_Link.h"
#endif

// DHT22 编译开关见 Slave_Config.h（现场未接线时置 0）。
// 关掉后：不构造对象、不初始化、不轮询 —— 串口也不会再每 60 秒刷
// 「[警告] DHT22 读取失败」，避免把「没接」误读成「坏了」。
#if ENABLE_DHT22
DHT dht(DHT_PIN, DHT_TYPE);
#endif

// 缓存数据
float currentTemp = 0.0;
float currentHum = 0.0;
unsigned long previousMillis = 0;
// 采样/上报间隔。
//   从机模式：跟着 Slave_Config.h 的 SLAVE_ENV_INTERVAL_MS（60s），
//             别在两处各写一个数字，改一处漏一处就静默不同步了。
//   主控模式：维持原值 60s（原工程这个数只用于串口打印）。
#if ENABLE_DHT22
#if SLAVE_MODE
const long interval = SLAVE_ENV_INTERVAL_MS;
#else
const long interval = 60000;
#endif
#endif

// 初始化模块
void Plus_Init()
{
#if ENABLE_DHT22
    dht.begin();
    Serial.println("DHT22 初始化完成...");
#else
    // 明确打一行。原来的「DHT22 初始化完成」是骗人的 —— dht.begin()
    // 不验证硬件，没接线也照样打印。这里直接说清楚是「按配置禁用」。
    Serial.println("DHT22 已禁用（ENABLE_DHT22=0，硬件未接线）");
#endif
}

// 一次性打印测试 (供 setup 调用)
void Plus_PrintTest()
{
    Serial.println("====== 温湿度传感器一次性测试 ======");
#if !ENABLE_DHT22
    Serial.println("已禁用（ENABLE_DHT22=0），跳过。");
    Serial.println("==================================");
    return;
#else
    // 强制读取一次
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    
    if (isnan(h) || isnan(t))
    {
        Serial.println("DHT22 读取失败！请检查引脚号、上拉电阻及接线。");
    } 
    else 
    {
        Serial.print("当前温度: "); 
        Serial.print(t); 
        Serial.println(" °C");
        
        Serial.print("当前湿度: "); 
        Serial.print(h); 
        Serial.println(" %");
    }
    Serial.println("==================================");
#endif
}

// 循环非阻塞读取 (供 loop 调用)
void DHT22_HandleLoop()
{
#if !ENABLE_DHT22
    // 硬件未接线：直接返回，不读、不报、不刷日志。
    return;
#else
    unsigned long currentMillis = millis();
    
    // 检查是否经过了 interval
    if (currentMillis - previousMillis >= interval)
    {
        previousMillis = currentMillis;
        
        float h = dht.readHumidity();
        float t = dht.readTemperature();
        
        // 容错处理：如果读取失败，保留上一次数据
        if (isnan(h) || isnan(t))
        {
            Serial.println("[警告] DHT22 读取失败，跳过本次发送");
            return;
        }
        
        currentTemp = t;
        currentHum = h;
        
        // 通过串口发送一次数据
        Serial.printf("实时数据发送 -> 温度: %.1f°C, 湿度: %.1f%%\n", currentTemp, currentHum);

#if SLAVE_MODE
        // 从机形态：传感器在本机、大屏在 RK3568，读数要报上去，
        // 否则 `/api/env` 永远是 available:false（大屏显示「传感器未接」）。
        // 原主控形态读完只拿去拼 AI 提示词（AI_Test.cpp），所以那边不用报。
        SlaveLink_PostEnv(currentTemp, currentHum);
#endif
    }
#endif
}

// 获取当前温度
float getTemperature()
{
    return currentTemp;
}

// 获取当前湿度
float getHumidity()
{
    return currentHum;
}

// 预留空函数实现
void RC522_HandleLoop()
{
}

void HX711_HandleLoop()
{
}