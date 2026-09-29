#ifndef USB_BARCODE_SCANNER_H
#define USB_BARCODE_SCANNER_H

#include <Arduino.h>

void USB_BarcodeScanner_Init();
bool USB_BarcodeScanner_Read(String& code);
bool USB_BarcodeScanner_IsConnected();
const char* USB_BarcodeScanner_State();

#endif
