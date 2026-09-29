#include "Plus.h" 

DHT dht(DHT_PIN, DHT_TYPE);

// 缓存数据
float currentTemp = 0.0;
float currentHum = 0.0;
unsigned long previousMillis = 0;
// 修改为 60000 毫秒 (1分钟)
const long interval = 60000; 

// 初始化模块
void Plus_Init()
{
    dht.begin();
    Serial.println("DHT22 初始化完成...");
}

// 一次性打印测试 (供 setup 调用)
void Plus_PrintTest()
{
    Serial.println("====== 温湿度传感器一次性测试 ======");
    
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
}

// 循环非阻塞读取 (供 loop 调用)
void DHT22_HandleLoop()
{
    unsigned long currentMillis = millis();
    
    // 检查是否经过了 10 秒 (interval 现已改为 10000)
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
        
        // 每 10 秒通过串口发送一次数据
        Serial.printf("实时数据发送 -> 温度: %.1f°C, 湿度: %.1f%%\n", currentTemp, currentHum);
    }
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