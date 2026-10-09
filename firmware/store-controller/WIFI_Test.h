#include "Arduino.h"   // 必须加！这是 Arduino 的“总开关”

#ifndef __WIFI_TEST_H
#define __WIFI_TEST_H

void wifi_init(unsigned long timeoutMs = 30000);
void WIFI_Set();
void WIFI_Requst();

#endif
