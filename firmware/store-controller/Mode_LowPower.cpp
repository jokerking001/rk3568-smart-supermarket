#include "Mode_LowPower.h"
#include <WiFi.h>

static LowPowerMode currentMode = MODE_NORMAL;

void LowPower_Init()
{
  currentMode = MODE_NORMAL;
  WiFi.setSleep(WIFI_PS_NONE);
  Serial.println("低功耗模块初始化完成，当前模式：Normal");
}

void LowPower_SetMode(LowPowerMode mode)
{
  if (mode == currentMode)
  {
    return;
  }

  switch (mode)
  {
    case MODE_NORMAL:
      WiFi.setSleep(WIFI_PS_NONE);
      break;

    case MODE_MODEM_SLEEP:
      WiFi.setSleep(WIFI_PS_MAX_MODEM);
      break;

    case MODE_DEEP_SLEEP:
      break;
  }

  currentMode = mode;
  LowPower_PrintStatus();
}

LowPowerMode LowPower_GetMode()
{
  return currentMode;
}

void LowPower_PrintStatus()
{
  wifi_ps_type_t psType = WiFi.getSleep();

  Serial.print("当前WiFi休眠类型：");
  switch (psType)
  {
    case WIFI_PS_NONE:
      Serial.print("Normal");
      break;
    case WIFI_PS_MIN_MODEM:
      Serial.print("Modem_Sleep（最小）");
      break;
    case WIFI_PS_MAX_MODEM:
      Serial.print("Modem_Sleep");
      break;
    default:
      Serial.print("未知");
      break;
  }
  Serial.println();
}