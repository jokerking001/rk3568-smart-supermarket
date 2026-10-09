// ============================================================
//  ⚠️ 本文件属于**原主控形态**，从机模式（SLAVE_MODE=1）下整段不编译。
// ============================================================
//  RK3568 接管主控后，这个模块的职责已经搬到板端：
//      低功耗模式 → 板子插电，已废弃
//  保留代码是为了 SLAVE_MODE=0 时能原样回退，不是从机固件的一部分。
//
//  为什么用 #if 而不是把文件挪走：Arduino 编译 sketch 目录下所有 .cpp，
//  挪走会让 SLAVE_MODE=0 也编不过；加保护则两套形态共存、互不干扰。
// ============================================================
#include "Slave_Config.h"

#if !SLAVE_MODE

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

#endif  // !SLAVE_MODE
