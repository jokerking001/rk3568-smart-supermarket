#ifndef AI_TEST_H
#define AI_TEST_H

#include <Arduino.h>

// API 配置
extern const char* ai_api_key;
extern const char* ai_api_url;

// 函数声明
void AI_Init();
String AI_Ask(String question);
String AI_Analyze(String question);      // 商家分析专用，无字数限制
String AI_ProcessQuestion(String question);
void AI_HandleLoop();

#endif