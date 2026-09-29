#ifndef MODE_LOWPOWER_H
#define MODE_LOWPOWER_H

#include <Arduino.h>

enum LowPowerMode
{
  MODE_NORMAL,
  MODE_MODEM_SLEEP,
  MODE_DEEP_SLEEP
};

void LowPower_Init();
void LowPower_SetMode(LowPowerMode mode);
LowPowerMode LowPower_GetMode();
void LowPower_PrintStatus();   // 新增：打印当前状态

#endif