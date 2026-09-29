#include "HC_SR04.h"
#include <Arduino.h>
#include "Mode_LowPower.h"
#include "led.h"

static bool personPresent = false;

void HC_SR04_Init()
{
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);
  personPresent = false;
}

float HC_SR04_GetDistance()
{
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duration = pulseIn(ECHO_PIN, HIGH, 30000);

  if (duration == 0)
  {
    return -1;
  }

  float distance = duration * 0.034 / 2;
  return distance;
}

bool HC_SR04_IsPersonNear()
{
  float dist = HC_SR04_GetDistance();
  if (dist > 0 && dist < DETECT_DISTANCE)
  {
    return true;
  }
  return false;
}

void HC_SR04_HandleLoop()
{
  static unsigned long lastCheck = 0;

  if (millis() - lastCheck >= 200)
  {
    lastCheck = millis();
    bool nowState = HC_SR04_IsPersonNear();

    if (nowState && !personPresent)
    {
      personPresent = true;
      Serial.println("有人靠近！唤醒模式打开");
      LowPower_SetMode(MODE_NORMAL);
      led_low_power_mode(false); // 取消低功耗，亮起翠绿色表示欢迎顾客
    }
    else if (!nowState && personPresent)
    {
      personPresent = false;
      Serial.println("人已离开！进入低功耗模式");
      LowPower_SetMode(MODE_MODEM_SLEEP);
      led_low_power_mode(true);  // 进入低功耗待机，亮起幽幽的冰川蓝
    }
  }
}