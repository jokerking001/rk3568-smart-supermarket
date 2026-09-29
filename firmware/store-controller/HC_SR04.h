#ifndef HC_SR04_H
#define HC_SR04_H

#include <Arduino.h>

#define TRIG_PIN  40
#define ECHO_PIN  38
#define DETECT_DISTANCE  80

void HC_SR04_Init();
float HC_SR04_GetDistance();
bool HC_SR04_IsPersonNear();
void HC_SR04_HandleLoop();   // 新增：封装循环处理

#endif