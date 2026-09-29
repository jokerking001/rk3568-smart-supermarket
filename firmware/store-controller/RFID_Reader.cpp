#include "RFID_Reader.h"

static String lastUID = "";
static bool newCard = false;
static unsigned long lastCardTime = 0;

void RFID_Init() {
  Serial2.begin(115200, SERIAL_8N1, RFID_RX_PIN, RFID_TX_PIN);
  Serial.println("[RFID] init GPIO18(RX) GPIO21(TX) @115200");
}

void RFID_HandleLoop() {
  if (Serial2.available() >= 12) {
    uint8_t buf[12];
    int idx = 0;
    unsigned long start = millis();
    while (idx < 12 && (millis() - start) < 200) {
      if (Serial2.available()) buf[idx++] = Serial2.read();
    }
    if (idx < 12) return;
    if (buf[0] != 0x04 || buf[1] != 0x0C || buf[3] != 0x30) return;
    uint8_t cs = 0;
    for (int i = 0; i < 11; i++) cs ^= buf[i];
    if ((uint8_t)(~cs) != buf[11]) return;
    if (buf[4] != 0x00) return;
    char hex[9];
    sprintf(hex, "%02X%02X%02X%02X", buf[7], buf[8], buf[9], buf[10]);
    String uid = String(hex);
    unsigned long now = millis();
    if (uid != lastUID || (now - lastCardTime) > 2000) {
      lastUID = uid; lastCardTime = now; newCard = true;
      Serial.printf("[RFID] card: %s\n", uid.c_str());
    }
    while (Serial2.available()) Serial2.read();
  }
}

String RFID_GetLastUID() { newCard = false; return lastUID; }
bool RFID_IsNewCard() { return newCard; }

// 在文件末尾添加：
void RFID_TestCommunication() {
  if (Serial2.available() > 0) {
    Serial.print("[RFID Test] 收到原始字节: ");
    while (Serial2.available() > 0) {
      uint8_t byte = Serial2.read();
      Serial.printf("%02X ", byte); // 以十六进制格式打印每个字节
      delay(2); // 防止读取过快
    }
    Serial.println(""); // 换行
  }
}
