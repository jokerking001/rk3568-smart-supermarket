#ifndef RFID_READER_H
#define RFID_READER_H

#include <Arduino.h>

#define RFID_RX_PIN 21
#define RFID_TX_PIN 15

void RFID_Init();
void RFID_HandleLoop();
String RFID_GetLastUID();
bool RFID_IsNewCard();

// 在末尾添加：
void RFID_TestCommunication();

#endif
