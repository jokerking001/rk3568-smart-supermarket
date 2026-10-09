// ============================================================
//  ⚠️ 本文件属于**原主控形态**，从机模式（SLAVE_MODE=1）下整段不编译。
// ============================================================
//  RK3568 接管主控后，这个模块的职责已经搬到板端：
//      超声波人体感应 → 已被 8091 雷达替代
//  保留代码是为了 SLAVE_MODE=0 时能原样回退，不是从机固件的一部分。
//
//  为什么用 #if 而不是把文件挪走：Arduino 编译 sketch 目录下所有 .cpp，
//  挪走会让 SLAVE_MODE=0 也编不过；加保护则两套形态共存、互不干扰。
// ============================================================
#include "Slave_Config.h"

#if !SLAVE_MODE

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

#endif  // !SLAVE_MODE
