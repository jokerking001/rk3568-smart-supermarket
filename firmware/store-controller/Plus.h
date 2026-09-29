#ifndef PLUS_H
#define PLUS_H

#include <Arduino.h>
#include <DHT.h>


#define DHT_PIN 41
#define DHT_TYPE DHT22

// ---------------- 接口声明 ----------------
void Plus_Init();
void Plus_PrintTest();
void DHT22_HandleLoop();

// 供外部获取数据的接口
float getTemperature();
float getHumidity();

// 预留给后续模块的空接口
void RC522_HandleLoop();
void HX711_HandleLoop();

#endif