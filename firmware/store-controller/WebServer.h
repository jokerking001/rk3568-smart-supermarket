#ifndef __WEBSERVER_H
#define __WEBSERVER_H

#include "Arduino.h" 

void WebServer_Init();   // 初始化Web服务器
void WebServer_SyncToPi(); // 同步商品数据到树莓派
void WebServer_SubmitScanGunCode(const String& code, const char* source);
String WebServer_CreateHumanServiceRequest();

#include <ElegantOTA.h>

#endif
