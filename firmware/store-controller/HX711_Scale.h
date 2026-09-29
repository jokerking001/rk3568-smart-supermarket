#ifndef HX711_SCALE_H
#define HX711_SCALE_H

#include <Arduino.h>

#define HX711_DT_PIN   12
#define HX711_SCK_PIN  14

// 校准系数（需根据实际传感器标定调整）
#define SCALE_CAL_FACTOR  21000.0f

void Scale_Init();
float Scale_GetWeight();        // 返回重量（克），返回 -1 表示异常
void Scale_Tare();              // 手动去皮
bool Scale_IsProductRemoved();  // 检测商品是否被取走（重量骤降 >30g）
void Scale_HandleLoop();        // 封装循环处理
bool Scale_GetSnapshot(float *weight, bool *stable, unsigned long *ageMs);

#endif
