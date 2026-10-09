// ============================================================
//  ⚠️ 本文件属于**原主控形态**，从机模式（SLAVE_MODE=1）下整段不编译。
// ============================================================
//  RK3568 接管主控后，这个模块的职责已经搬到板端：
//      81 个 HTTP 端点、网页渲染、商品/订单/会员/管理接口 → 8094 收银后端
//  保留代码是为了 SLAVE_MODE=0 时能原样回退，不是从机固件的一部分。
//
//  为什么用 #if 而不是把文件挪走：Arduino 编译 sketch 目录下所有 .cpp，
//  挪走会让 SLAVE_MODE=0 也编不过；加保护则两套形态共存、互不干扰。
// ============================================================
#include "Slave_Config.h"

#if !SLAVE_MODE

#include "WebServer.h"
#include "secrets.h"        // 私密配置（密钥/地址），不进版本库
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>
#include <SPIFFS.h>
#include <mbedtls/sha256.h>
#include <map>
#include <freertos/semphr.h>
#include <time.h>
#include <math.h>
#include "qrcode.h"
#include "AI_Test.h"
#include "Product_Data.h"
#include "HX711_Scale.h"
#include "RFID_Reader.h"
#include "Plus.h"
#include "Voice_Interaction.h"
#include "USB_BarcodeScanner.h"

// ========== 语音字幕全局同步变量 ==========
String global_voice_q = "";
String global_voice_a = "";
bool global_voice_show = false; 
// ==========================================

// 扫码由前端JS直连模块A
// 扫码由前端JS直连模块A的/qr-scan, 不走ESP-NOW

// 内网地址（PI_URL / MIMICLAW_ALERT_URL / MIMICLAW_STORE_KEY）见 secrets.h

AsyncWebServer server(80);

struct VisionWeightRule { const char *label; const char *name; float typical; float tolerance; };
static const VisionWeightRule VISION_WEIGHT_RULES[] = {
  {"apple", "苹果", 180.0f, 90.0f}, {"banana", "香蕉", 140.0f, 80.0f}, {"grapes", "葡萄", 300.0f, 220.0f}
};
static String latestVisionFusion = "{\"ok\":false,\"msg\":\"暂无视觉结果\"}";

static const char *PRINT_JOB_FILE = "/printjob.bin";
static const char *PRINT_JOB_TEMP = "/printjob.tmp";
static File printUploadFile;
static bool printUploadOk = false;
static size_t printUploadExpected = 0;
static size_t printUploadWritten = 0;
static bool printJobReady = false;
static uint32_t printJobId = 0;
static uint16_t printJobWidth = 0;
static uint16_t printJobHeight = 0;
static String printJobKind = "";

static void savePrintJobMeta() {
  if (!printJobReady) {
    SPIFFS.remove("/printjob.meta");
    return;
  }
  File meta = SPIFFS.open("/printjob.meta", "w");
  if (!meta) return;
  meta.printf("%lu,%u,%u,%s", (unsigned long)printJobId, printJobWidth,
              printJobHeight, printJobKind.c_str());
  meta.close();
}

static void loadPrintJobMeta() {
  if (!SPIFFS.exists(PRINT_JOB_FILE) || !SPIFFS.exists("/printjob.meta")) return;
  File meta = SPIFFS.open("/printjob.meta", "r");
  if (!meta) return;
  String value = meta.readString(); meta.close();
  int p1 = value.indexOf(','), p2 = value.indexOf(',', p1 + 1), p3 = value.indexOf(',', p2 + 1);
  if (p1 <= 0 || p2 <= p1 || p3 <= p2) return;
  printJobId = strtoul(value.substring(0, p1).c_str(), nullptr, 10);
  printJobWidth = value.substring(p1 + 1, p2).toInt();
  printJobHeight = value.substring(p2 + 1, p3).toInt();
  printJobKind = value.substring(p3 + 1);
  File job = SPIFFS.open(PRINT_JOB_FILE, "r");
  size_t expected = (size_t)printJobWidth * printJobHeight / 8;
  printJobReady = job && job.size() == expected;
  if (job) job.close();
}

static float visionWeightScore(const VisionWeightRule& rule, float grams) {
  float score = 1.0f - fabsf(grams - rule.typical) / rule.tolerance;
  return constrain(score, 0.0f, 1.0f);
}

static const char *CUSTOMER_INTENTS[] = {
  "price", "location", "availability", "promotion", "budget_recommendation",
  "alternative", "expiry", "payment", "human_service", "other"
};
static const int CUSTOMER_INTENT_COUNT = sizeof(CUSTOMER_INTENTS) / sizeof(CUSTOMER_INTENTS[0]);
static uint32_t customerIntentCounts[CUSTOMER_INTENT_COUNT] = {0};
static uint32_t customerHourlyQuestions[24] = {0};
static uint32_t customerQuestionTotal = 0;
static uint32_t customerResolvedTotal = 0;
static uint32_t customerCartAdds = 0;
static uint32_t customerOrders = 0;
static uint32_t customerAnalyticsDirty = 0;

static int classifyCustomerQuestion(const String &q) {
  if (q.indexOf("价格") >= 0 || q.indexOf("多少钱") >= 0 || q.indexOf("几元") >= 0) return 0;
  if (q.indexOf("哪里") >= 0 || q.indexOf("位置") >= 0 || q.indexOf("哪个区") >= 0) return 1;
  if (q.indexOf("有货") >= 0 || q.indexOf("库存") >= 0 || q.indexOf("缺货") >= 0) return 2;
  if (q.indexOf("优惠") >= 0 || q.indexOf("促销") >= 0 || q.indexOf("打折") >= 0) return 3;
  if (q.indexOf("预算") >= 0 || q.indexOf("以内") >= 0 || q.indexOf("推荐") >= 0) return 4;
  if (q.indexOf("替代") >= 0 || q.indexOf("相似") >= 0 || q.indexOf("换一个") >= 0) return 5;
  if (q.indexOf("过期") >= 0 || q.indexOf("保质期") >= 0 || q.indexOf("临期") >= 0) return 6;
  if (q.indexOf("支付") >= 0 || q.indexOf("付款") >= 0 || q.indexOf("结账") >= 0) return 7;
  if (q.indexOf("人工") >= 0 || q.indexOf("工作人员") >= 0 || q.indexOf("帮助") >= 0) return 8;
  return 9;
}

static void saveCustomerAnalytics() {
  DynamicJsonDocument doc(2048);
  doc["questions"] = customerQuestionTotal;
  doc["resolved"] = customerResolvedTotal;
  doc["cart_adds"] = customerCartAdds;
  doc["orders"] = customerOrders;
  JsonObject intents = doc.createNestedObject("intents");
  for (int i = 0; i < CUSTOMER_INTENT_COUNT; i++) intents[CUSTOMER_INTENTS[i]] = customerIntentCounts[i];
  JsonArray hours = doc.createNestedArray("hours");
  for (int i = 0; i < 24; i++) hours.add(customerHourlyQuestions[i]);
  File f = SPIFFS.open("/customer_analytics.json", "w");
  if (f) { serializeJson(doc, f); f.close(); customerAnalyticsDirty = 0; }
}

static void loadCustomerAnalytics() {
  if (!SPIFFS.exists("/customer_analytics.json")) return;
  File f = SPIFFS.open("/customer_analytics.json", "r");
  if (!f) return;
  DynamicJsonDocument doc(2048);
  DeserializationError err = deserializeJson(doc, f); f.close();
  if (err) return;
  customerQuestionTotal = doc["questions"] | 0;
  customerResolvedTotal = doc["resolved"] | 0;
  customerCartAdds = doc["cart_adds"] | 0;
  customerOrders = doc["orders"] | 0;
  for (int i = 0; i < CUSTOMER_INTENT_COUNT; i++) customerIntentCounts[i] = doc["intents"][CUSTOMER_INTENTS[i]] | 0;
  for (int i = 0; i < 24; i++) customerHourlyQuestions[i] = doc["hours"][i] | 0;
}

static void markCustomerAnalyticsDirty() {
  if (++customerAnalyticsDirty >= 10) saveCustomerAnalytics();
}

static String serviceRequestId = "";
static String serviceRequestStatus = "idle";
static String serviceRequestCustomer = "";
static String serviceRequestDetail = "";
static unsigned long serviceRequestCreatedAt = 0;
static unsigned long serviceRequestAcceptedAt = 0;
static unsigned long serviceRequestCompletedAt = 0;

static String pricingProposalId = "";
static String pricingProposalCode = "";
static String pricingProposalName = "";
static float pricingOldPrice = 0;
static float pricingNewPrice = 0;
static int pricingDaysLeft = 0;
static String pricingProposalStatus = "none";

static String restockProposalId = "";
static String restockProposalCode = "";
static String restockProposalName = "";
static String restockProposalRisk = "none";
static int restockBeforeStock = 0;
static int restockSoldToday = 0;
static int restockRecommendedQty = 0;
static float restockCoverageDays = 0;
static String restockProposalStatus = "none";
static String lastVisionObservationId = "";
static String lastVisionObservationResponse = "";

static void saveWorkflowState() {
  DynamicJsonDocument doc(3072);
  JsonObject service = doc.createNestedObject("service");
  service["id"] = serviceRequestId; service["status"] = serviceRequestStatus;
  service["customer"] = serviceRequestCustomer; service["detail"] = serviceRequestDetail;
  JsonObject pricing = doc.createNestedObject("pricing");
  pricing["id"] = pricingProposalId; pricing["code"] = pricingProposalCode;
  pricing["name"] = pricingProposalName; pricing["old"] = pricingOldPrice;
  pricing["new"] = pricingNewPrice; pricing["days"] = pricingDaysLeft;
  pricing["status"] = pricingProposalStatus;
  JsonObject restock = doc.createNestedObject("restock");
  restock["id"] = restockProposalId; restock["code"] = restockProposalCode;
  restock["name"] = restockProposalName; restock["risk"] = restockProposalRisk;
  restock["before"] = restockBeforeStock; restock["sold"] = restockSoldToday;
  restock["qty"] = restockRecommendedQty; restock["coverage"] = restockCoverageDays;
  restock["status"] = restockProposalStatus;

  SPIFFS.remove("/workflow.tmp");
  File temp = SPIFFS.open("/workflow.tmp", "w");
  if (!temp) return;
  bool ok = serializeJson(doc, temp) > 0;
  temp.flush(); temp.close();
  if (!ok) { SPIFFS.remove("/workflow.tmp"); return; }
  SPIFFS.remove("/workflow.bak");
  if (SPIFFS.exists("/workflow.json")) SPIFFS.rename("/workflow.json", "/workflow.bak");
  if (!SPIFFS.rename("/workflow.tmp", "/workflow.json")) {
    if (SPIFFS.exists("/workflow.bak")) SPIFFS.rename("/workflow.bak", "/workflow.json");
  } else {
    SPIFFS.remove("/workflow.bak");
  }
}

static void loadWorkflowState() {
  if (!SPIFFS.exists("/workflow.json") && SPIFFS.exists("/workflow.bak")) {
    SPIFFS.rename("/workflow.bak", "/workflow.json");
  }
  File file = SPIFFS.open("/workflow.json", "r");
  if (!file) return;
  DynamicJsonDocument doc(3072);
  DeserializationError err = deserializeJson(doc, file);
  file.close();
  if (err) return;
  serviceRequestId = doc["service"]["id"] | "";
  serviceRequestStatus = doc["service"]["status"] | "idle";
  serviceRequestCustomer = doc["service"]["customer"] | "";
  serviceRequestDetail = doc["service"]["detail"] | "";
  if (serviceRequestStatus == "waiting" || serviceRequestStatus == "accepted") serviceRequestCreatedAt = millis();
  pricingProposalId = doc["pricing"]["id"] | "";
  pricingProposalCode = doc["pricing"]["code"] | "";
  pricingProposalName = doc["pricing"]["name"] | "";
  pricingOldPrice = doc["pricing"]["old"] | 0.0f;
  pricingNewPrice = doc["pricing"]["new"] | 0.0f;
  pricingDaysLeft = doc["pricing"]["days"] | 0;
  pricingProposalStatus = doc["pricing"]["status"] | "none";
  restockProposalId = doc["restock"]["id"] | "";
  restockProposalCode = doc["restock"]["code"] | "";
  restockProposalName = doc["restock"]["name"] | "";
  restockProposalRisk = doc["restock"]["risk"] | "none";
  restockBeforeStock = doc["restock"]["before"] | 0;
  restockSoldToday = doc["restock"]["sold"] | 0;
  restockRecommendedQty = doc["restock"]["qty"] | 0;
  restockCoverageDays = doc["restock"]["coverage"] | 0.0f;
  restockProposalStatus = doc["restock"]["status"] | "none";
}

static bool createRestockProposal() {
  if (restockProposalStatus == "pending") return true;
  int bestIndex = -1;
  float bestCoverage = 99999.0f;
  for (int i = 0; i < PRODUCT_COUNT; i++) {
    Product &p = products[i];
    if (p.qrCode.length() == 0 || p.isWeigh || p.stock < 0) continue;
    float coverage = p.todaySold > 0 ? (float)p.stock / p.todaySold : 99999.0f;
    if ((p.stock < 5 || coverage < 3.0f) && coverage < bestCoverage) {
      bestCoverage = coverage;
      bestIndex = i;
    }
  }
  if (bestIndex < 0) return false;

  Product &p = products[bestIndex];
  int target = p.todaySold > 0 ? p.todaySold * 3 : 10;
  int qty = max(0, target - p.stock);
  if (qty == 0) return false;

  restockProposalId = "RS-" + String((uint32_t)millis());
  restockProposalCode = p.qrCode;
  restockProposalName = p.name;
  restockBeforeStock = p.stock;
  restockSoldToday = p.todaySold;
  restockRecommendedQty = qty;
  restockCoverageDays = p.todaySold > 0 ? (float)p.stock / p.todaySold : 99999.0f;
  restockProposalRisk = p.todaySold >= 8 && restockCoverageDays < 1.0f ? "sudden_hot_sale" : "normal_low_stock";
  restockProposalStatus = "pending";
  saveWorkflowState();
  return true;
}

static int productDaysUntilExpiry(const Product &p) {
  if (p.mfgDate.length() < 10 || p.shelfLife <= 0 || time(nullptr) < 1700000000) return 99999;
  struct tm expiry = {};
  sscanf(p.mfgDate.c_str(), "%d-%d-%d", &expiry.tm_year, &expiry.tm_mon, &expiry.tm_mday);
  expiry.tm_year -= 1900;
  expiry.tm_mon = expiry.tm_mon - 1 + p.shelfLife;
  expiry.tm_hour = 12;
  time_t expiryTime = mktime(&expiry);
  return expiryTime > 0 ? (int)((expiryTime - time(nullptr)) / 86400) : 99999;
}

static bool createPricingProposal() {
  if (pricingProposalStatus == "pending") return true;
  int bestIndex = -1;
  int bestDays = 99999;
  for (int i = 0; i < PRODUCT_COUNT; i++) {
    if (products[i].qrCode.length() == 0 || products[i].stock <= 0) continue;
    int days = productDaysUntilExpiry(products[i]);
    if (days >= 0 && days <= 60 && days < bestDays) { bestDays = days; bestIndex = i; }
  }
  if (bestIndex < 0) return false;
  float discount = bestDays <= 30 ? 0.5f : 0.7f;
  pricingProposalId = "PR-" + String((uint32_t)millis());
  pricingProposalCode = products[bestIndex].qrCode;
  pricingProposalName = products[bestIndex].name;
  pricingOldPrice = products[bestIndex].price;
  pricingNewPrice = roundf(pricingOldPrice * discount * 100.0f) / 100.0f;
  pricingDaysLeft = bestDays;
  pricingProposalStatus = "pending";
  saveWorkflowState();
  return true;
}

String WebServer_CreateHumanServiceRequest() {
  if (serviceRequestStatus == "waiting" || serviceRequestStatus == "accepted") {
    Serial.printf("[人工服务] 工单仍在处理中，跳过重复创建: %s\n", serviceRequestId.c_str());
    return "";
  }
  serviceRequestId = "HS-" + String((uint32_t)millis());
  serviceRequestStatus = "waiting";
  serviceRequestCreatedAt = millis();
  serviceRequestAcceptedAt = 0;
  serviceRequestCompletedAt = 0;
  saveWorkflowState();
  Serial.printf("[人工服务] 工单已创建: %s\n", serviceRequestId.c_str());
  return serviceRequestId;
}

static bool notifyMimiClawHumanService(const String &requestId,
                                       const String &customer,
                                       const String &detail) {
  if (WiFi.status() != WL_CONNECTED || requestId.length() == 0) return false;

  WiFiClient client;
  HTTPClient http;
  http.setTimeout(5000);
  if (!http.begin(client, MIMICLAW_ALERT_URL)) return false;
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<512> doc;
  doc["type"] = "human_service";
  doc["store"] = "智慧超市";
  doc["request_id"] = requestId;
  doc["terminal"] = customer.length() ? "手机App / " + customer : "手机App";
  doc["detail"] = detail;
  char timeBuf[32];
  struct tm timeInfo = {};
  if (getLocalTime(&timeInfo, 100) && timeInfo.tm_year + 1900 >= 2024) {
    strftime(timeBuf, sizeof(timeBuf), "%Y-%m-%d %H:%M:%S", &timeInfo);
  } else {
    strlcpy(timeBuf, "时间未同步", sizeof(timeBuf));
  }
  doc["time"] = timeBuf;
  String body;
  serializeJson(doc, body);
  int status = http.POST(body);
  http.end();
  return status >= 200 && status < 300;
}

// 扫码枪状态（ESP32-S3 USB Host 直连；树莓派 HTTP 接口保留为备用）
static String lastScanGunCode = "";
static unsigned long lastScanGunTime = 0;
static String lastScanGunSource = "";
static SemaphoreHandle_t scanGunMutex = nullptr;

void WebServer_SubmitScanGunCode(const String& code, const char* source) {
  if (code.length() == 0) return;
  if (scanGunMutex && xSemaphoreTake(scanGunMutex, pdMS_TO_TICKS(50)) != pdTRUE) return;
  lastScanGunCode = code;
  lastScanGunTime = millis();
  lastScanGunSource = source ? source : "unknown";
  if (scanGunMutex) xSemaphoreGive(scanGunMutex);
  Serial.printf("[ScanGun/%s] received: %s\n", source ? source : "unknown", code.c_str());
}

static String takeScanGunResult() {
  if (scanGunMutex && xSemaphoreTake(scanGunMutex, pdMS_TO_TICKS(50)) != pdTRUE)
    return "{\"code\":\"\",\"time\":0,\"source\":\"busy\"}";
  String response = "{\"code\":\"" + lastScanGunCode + "\",\"time\":" + String(lastScanGunTime)
                  + ",\"source\":\"" + lastScanGunSource + "\"}";
  lastScanGunCode = "";
  lastScanGunTime = 0;
  lastScanGunSource = "";
  if (scanGunMutex) xSemaphoreGive(scanGunMutex);
  return response;
} 

static void clearScanGunResult() {
  if (scanGunMutex && xSemaphoreTake(scanGunMutex, pdMS_TO_TICKS(50)) != pdTRUE) return;
  lastScanGunCode = "";
  lastScanGunTime = 0;
  lastScanGunSource = "";
  if (scanGunMutex) xSemaphoreGive(scanGunMutex);
}

// 用户列表
struct UserInfo { String password; String role; String rfid_uid; };
static std::map<String, UserInfo> users;
static bool usersLoaded = false;

// ── 会员系统 ──
struct Member { String id; String name; String phone; int points; float totalSpent; String rfid_uid; };
static std::map<String, Member> members;
static bool membersLoaded = false;

static void saveMembers() {
  DynamicJsonDocument doc(4096);
  for (auto& kv : members) {
    JsonObject obj = doc.createNestedObject(kv.first);
    obj["n"] = kv.second.name; obj["p"] = kv.second.phone;
    obj["pt"] = kv.second.points; obj["ts"] = kv.second.totalSpent;
    obj["rf"] = kv.second.rfid_uid;
  }
  File f = SPIFFS.open("/members.json", "w");
  if (f) { serializeJson(doc, f); f.close(); }
}
static void loadMembers() {
  if (!SPIFFS.exists("/members.json")) return;
  File f = SPIFFS.open("/members.json", "r");
  if (!f) return;
  DynamicJsonDocument doc(4096);
  if (deserializeJson(doc, f)) { f.close(); return; }
  f.close();
  for (JsonPair kv : doc.as<JsonObject>()) {
    Member m;
    m.id = kv.key().c_str();
    m.name = kv.value()["n"].as<String>();
    m.phone = kv.value()["p"].as<String>();
    m.points = kv.value()["pt"].as<int>();
    m.totalSpent = kv.value()["ts"].as<float>();
    m.rfid_uid = kv.value()["rf"].as<String>();
    members[m.id] = m;
  }
}

// 角色判断
static String getUserRole(const String& user) {
  if (users.count(user)) return users[user].role;
  return "staff";
}

static String urlDecodeSimple(const String& s) {
  String out; out.reserve(s.length());
  for (size_t i=0; i<s.length(); i++) {
    char c=s[i];
    if (c=='%' && i+2<s.length()) { char h[3]={s[i+1],s[i+2],0}; out+=(char)strtol(h,NULL,16); i+=2; }
    else if (c=='+') out+=' ';
    else out+=c;
  }
  return out;
}

static String getCookieVal(AsyncWebServerRequest *request, const String& key) {
  if (!request->hasHeader("Cookie")) return "";
  String c = request->getHeader("Cookie")->value();
  int p = c.indexOf(key+"=");
  if (p<0) return "";
  p += key.length()+1;
  int e = c.indexOf(';', p);
  if (e<0) e = c.length();
  return urlDecodeSimple(c.substring(p, e));
}

// ── 密码哈希：SHA-256，带 $sha256$ 前缀区分明文 ──
static String hashPassword(const String& pwd) {
  uint8_t hash[32];
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  mbedtls_sha256_update(&ctx, (const unsigned char*)pwd.c_str(), pwd.length());
  mbedtls_sha256_finish(&ctx, hash);
  mbedtls_sha256_free(&ctx);
  String hex = "$sha256$";
  char buf[3];
  for (int i = 0; i < 32; i++) { sprintf(buf, "%02x", hash[i]); hex += buf; }
  return hex;
}

// 密码校验：兼容 $sha256$ 哈希和旧明文格式
static bool checkPassword(const String& input, const String& stored) {
  if (stored.startsWith("$sha256$"))
    return stored == hashPassword(input);
  return stored == input; // 旧明文格式
}

static bool isRequestManager(AsyncWebServerRequest *request) {
  String u = getCookieVal(request, "admin_user");
  if (u == "admin") return true; // admin永远为店长
  if (u.length()==0 || !users.count(u)) return false;
  return users[u].role == "manager";
}

static bool requireManager(AsyncWebServerRequest *request) {
  if (isRequestManager(request)) return true;
  request->send(403, "application/json", "{\"ok\":false,\"msg\":\"权限不足，仅店长可操作\"}");
  return false;
}

// 操作日志（内存环形，最大50条）
#define MAX_LOGS 50
static String opLogs[MAX_LOGS];
static int opLogCount = 0;
static void addOpLog(const String& user, const String& action, const String& detail) {
  if (opLogCount >= MAX_LOGS) {
    for (int i = 0; i < MAX_LOGS - 1; i++) opLogs[i] = opLogs[i + 1];
    opLogCount = MAX_LOGS - 1;
  }
  String t = String(millis() / 3600000) + "h";
  opLogs[opLogCount++] = "{\"t\":\"" + t + "\",\"u\":\"" + user + "\",\"a\":\"" + action + "\",\"d\":\"" + detail + "\"}";
  // 持久化到SPIFFS
  String j = "[";
  for (int i = 0; i < opLogCount; i++) { if (i > 0) j += ","; j += opLogs[i]; }
  j += "]";
  File f = SPIFFS.open("/oplog.json", "w");
  if (f) { f.print(j); f.close(); }
}
static void loadOpLogs() {
  if (!SPIFFS.exists("/oplog.json")) return;
  File f = SPIFFS.open("/oplog.json", "r");
  if (!f) return;
  String content = f.readString();
  f.close();
  DynamicJsonDocument doc(8192);
  if (deserializeJson(doc, content)) return;
  JsonArray arr = doc.as<JsonArray>();
  opLogCount = min((int)arr.size(), MAX_LOGS);
  for (int i = 0; i < opLogCount; i++) {
    String s; serializeJson(arr[i], s); opLogs[i] = s;
  }
}

// 店铺设置（按用户存储，每人的个性化设置独立）
struct StoreCfg { String name; String emoji; String color; };
static std::map<String, StoreCfg> userStoreCfgs;
static const StoreCfg defaultCfg = {"商家管理中心", "📊", "#1a1a2e"};

static StoreCfg& getStoreCfg(const String& user) {
  if (user.length() > 0 && userStoreCfgs.count(user)) return userStoreCfgs[user];
  return userStoreCfgs["admin"]; // 回退到admin配置
}

static void saveStoreCfg() {
  DynamicJsonDocument doc(4096);
  for (auto& kv : userStoreCfgs) {
    JsonObject obj = doc.createNestedObject(kv.first);
    obj["n"] = kv.second.name; obj["e"] = kv.second.emoji; obj["c"] = kv.second.color;
  }
  File f = SPIFFS.open("/storecfg.json", "w");
  if (f) { serializeJson(doc, f); f.close(); }
}
static void loadStoreCfg() {
  if (!SPIFFS.exists("/storecfg.json")) return;
  File f = SPIFFS.open("/storecfg.json", "r");
  if (!f) return;
  DynamicJsonDocument doc(4096);
  if (deserializeJson(doc, f)) { f.close(); return; }
  f.close();
  // 新格式: {"admin":{"n":"...","e":"...","c":"..."},"staff1":{...}}
  // 旧格式: {"n":"...","e":"...","c":"..."} — 迁移到admin名下
  JsonObject root = doc.as<JsonObject>();
  if (root.containsKey("n")) {
    // 旧格式迁移
    userStoreCfgs["admin"].name  = root["n"].as<String>();
    userStoreCfgs["admin"].emoji = root["e"].as<String>();
    userStoreCfgs["admin"].color = root["c"].as<String>();
    if (userStoreCfgs["admin"].name.length() == 0) userStoreCfgs["admin"] = defaultCfg;
    saveStoreCfg(); // 写回新格式
    return;
  }
  for (JsonPair kv : root) {
    String user = kv.key().c_str();
    JsonObject obj = kv.value().as<JsonObject>();
    userStoreCfgs[user].name  = obj["n"].as<String>();
    userStoreCfgs[user].emoji = obj["e"].as<String>();
    userStoreCfgs[user].color = obj["c"].as<String>();
  }
  // 确保admin始终有配置
  if (!userStoreCfgs.count("admin")) userStoreCfgs["admin"] = defaultCfg;
}

// QR扫码登录会话
struct QRSession { String user; bool confirmed; unsigned long createdAt; };
static std::map<String, QRSession> qrSessions;

static void cleanupExpiredQRSessions() {
  unsigned long now = millis();
  for (auto it = qrSessions.begin(); it != qrSessions.end(); ) {
    if (now - it->second.createdAt > 120000) it = qrSessions.erase(it);
    else ++it;
  }
}

void WebServer_SyncToPi() { /* deprecated */ }

// ── 服务端QR码生成（HTML表格渲染，零兼容问题）────────────
static uint8_t* qrBuf = nullptr;
static int qrSize = 0;
static void qrDisplayFunc(esp_qrcode_handle_t qrcode) {
  qrSize = esp_qrcode_get_size(qrcode);
  free(qrBuf); qrBuf = nullptr;
  qrBuf = (uint8_t*)malloc(qrSize * qrSize);
  if (qrBuf) for (int y=0; y<qrSize; y++) for (int x=0; x<qrSize; x++)
    qrBuf[y*qrSize+x] = esp_qrcode_get_module(qrcode, x, y) ? 1 : 0;
}

void WebServer_Init() {
  if (!scanGunMutex) scanGunMutex = xSemaphoreCreateMutex();
  loadCustomerAnalytics();
  loadWorkflowState();
  loadPrintJobMeta();
  // Public customer entry points. The customer surface is read-only except for
  // cart/order/service actions already exposed by the customer page.
  server.on("/customer/qr", HTTP_GET, [](AsyncWebServerRequest *request) {
    String url = "http://" + WiFi.localIP().toString() + "/customer";
    String html = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>顾客入口二维码</title><style>body{font-family:sans-serif;text-align:center;margin:24px;color:#172033}iframe{width:280px;height:280px;border:0}code{display:block;word-break:break-all;color:#667085}</style><h2>扫码进入智慧超市</h2><iframe src='/qr-svg?text=" + url + "'></iframe><code>" + url + "</code></html>";
    request->send(200, "text/html; charset=utf-8", html);
  });
  // Register the broader route after /customer/qr because this server matches
  // routes in registration order.
  server.on("/customer", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->redirect("/");
  });
  server.on("/manifest.webmanifest", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "application/manifest+json", "{\"name\":\"MimiStore 智慧超市\",\"short_name\":\"MimiStore\",\"start_url\":\"/customer\",\"scope\":\"/\",\"display\":\"standalone\",\"background_color\":\"#07111f\",\"theme_color\":\"#0369a1\",\"icons\":[{\"src\":\"/icon.svg\",\"sizes\":\"any\",\"type\":\"image/svg+xml\",\"purpose\":\"any maskable\"}]}");
  });
  server.on("/icon.svg", HTTP_GET, [](AsyncWebServerRequest *request) {
    static const char icon[] = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='42' fill='#0369a1'/><path d='M42 68h108l-9 83H51z' fill='#fff'/><path d='M57 68c4-25 17-38 39-38s35 13 39 38' fill='none' stroke='#fff' stroke-width='12' stroke-linecap='round'/><circle cx='76' cy='103' r='8' fill='#0369a1'/><circle cx='116' cy='103' r='8' fill='#0369a1'/><path d='M72 128h48' stroke='#0369a1' stroke-width='8' stroke-linecap='round'/></svg>";
    request->send(200, "image/svg+xml", icon);
  });
  server.on("/sw.js", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "application/javascript", "const C='mimi-shell-v2';self.addEventListener('install',e=>e.waitUntil(caches.open(C).then(c=>c.addAll(['/customer','/manifest.webmanifest','/icon.svg'])).then(()=>self.skipWaiting())));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;e.respondWith(fetch(e.request).then(r=>{const copy=r.clone();caches.open(C).then(c=>c.put(e.request,copy));return r}).catch(()=>caches.match(e.request)));});");
  });
  server.on("/api/customer/session", HTTP_GET, [](AsyncWebServerRequest *request) {
    String sid = "C-" + String((uint32_t)esp_random(), HEX);
    request->send(200, "application/json", "{\"ok\":true,\"role\":\"customer\",\"session_id\":\"" + sid + "\"}");
  });
  server.on("/api/customer/analytics-event", HTTP_POST, [](AsyncWebServerRequest *request) {
    String event = request->hasParam("event", true) ? request->getParam("event", true)->value() : "";
    if (event == "question") {
      String q = request->hasParam("q", true) ? request->getParam("q", true)->value() : "";
      if (q.length() == 0 || q.length() > 240) { request->send(400, "application/json", "{\"ok\":false}"); return; }
      customerQuestionTotal++;
      customerIntentCounts[classifyCustomerQuestion(q)]++;
      time_t now = time(nullptr); struct tm tmNow = {}; localtime_r(&now, &tmNow);
      if (now > 1700000000 && tmNow.tm_hour >= 0 && tmNow.tm_hour < 24) customerHourlyQuestions[tmNow.tm_hour]++;
    } else if (event == "resolved") customerResolvedTotal++;
    else if (event == "cart") customerCartAdds++;
    else if (event == "order") { customerOrders++; saveCustomerAnalytics(); }
    else { request->send(400, "application/json", "{\"ok\":false}"); return; }
    markCustomerAnalyticsDirty();
    request->send(200, "application/json", "{\"ok\":true}");
  });
  server.on("/api/admin/customer-analytics", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (!request->hasHeader("X-MimiClaw-Key") || request->getHeader("X-MimiClaw-Key")->value() != MIMICLAW_STORE_KEY) {
      request->send(401, "application/json", "{\"ok\":false,\"msg\":\"unauthorized\"}"); return;
    }
    DynamicJsonDocument doc(2048);
    doc["ok"] = true; doc["questions"] = customerQuestionTotal; doc["resolved"] = customerResolvedTotal;
    doc["unresolved"] = customerQuestionTotal > customerResolvedTotal ? customerQuestionTotal - customerResolvedTotal : 0;
    doc["cart_adds"] = customerCartAdds; doc["orders"] = customerOrders;
    doc["resolution_rate"] = customerQuestionTotal ? (float)customerResolvedTotal / customerQuestionTotal : 0;
    doc["cart_rate"] = customerQuestionTotal ? (float)customerCartAdds / customerQuestionTotal : 0;
    JsonObject intents = doc.createNestedObject("intents");
    for (int i = 0; i < CUSTOMER_INTENT_COUNT; i++) intents[CUSTOMER_INTENTS[i]] = customerIntentCounts[i];
    JsonArray hours = doc.createNestedArray("hours");
    for (int i = 0; i < 24; i++) hours.add(customerHourlyQuestions[i]);
    String json; serializeJson(doc, json); request->send(200, "application/json; charset=utf-8", json);
  });
  server.on("/api/customer/chat", HTTP_GET, [](AsyncWebServerRequest *request) {
    String question = request->hasParam("q") ? request->getParam("q")->value() : "";
    if (question.length() == 0 || question.length() > 240) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"请输入问题\"}");
      return;
    }
    String prompt = Product_Data_ToText() +
      "\n你是面向顾客的门店导购，只能回答公开商品、价格、可购买库存、商品位置、促销、替代商品和人工服务问题。"
      "不能透露成本、总库存、销售额、员工、审计日志，也不能执行补货、改价、退款或经营分析。"
      "如果客户要求商家操作，请引导其发起人工服务。\n顾客问题：" + question;
    String answer = AI_Ask(prompt);
    answer.replace("\\", "\\\\"); answer.replace("\"", "\\\"");
    answer.replace("\r", ""); answer.replace("\n", "\\n");
    request->send(200, "application/json; charset=utf-8", "{\"ok\":true,\"role\":\"customer\",\"answer\":\"" + answer + "\"}");
  });
  server.on("/qr-svg", HTTP_GET, [](AsyncWebServerRequest *request) {
    String text = request->hasParam("text") ? request->getParam("text")->value() : "";
    if (text.length() == 0) { request->send(400); return; }
    esp_qrcode_config_t cfg = ESP_QRCODE_CONFIG_DEFAULT();
    cfg.max_qrcode_version = 10;
    cfg.display_func = qrDisplayFunc;
    esp_qrcode_generate(&cfg, text.c_str());
    if (!qrBuf || qrSize <= 0) { request->send(500); return; }
    int s = 4; // 每格像素
    String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{margin:0;display:flex;justify-content:center;align-items:center;background:#fff}table{border-collapse:collapse}</style></head><body><table>";
    for (int y = 0; y < qrSize; y++) {
      html += "<tr>";
      for (int x = 0; x < qrSize; x++) {
        html += qrBuf[y*qrSize+x] ? "<td style='width:"+String(s)+"px;height:"+String(s)+"px;background:#000'></td>"
                                   : "<td style='width:"+String(s)+"px;height:"+String(s)+"px;background:#fff'></td>";
      }
      html += "</tr>";
    }
    html += "</table></body></html>";
    request->send(200, "text/html; charset=utf-8", html);
  });

// 语音字幕实时同步接口 (强健修复版)
  server.on("/api/voice-subtitles", HTTP_GET, [](AsyncWebServerRequest *request) {
    
    // 1. 安全处理顾客提问
    String safe_q = global_voice_q; 
    safe_q.replace("\\", "\\\\");  // 必须先转义反斜杠，防止吃字符
    safe_q.replace("\"", "\\\"");  // 转义双引号，不再篡改为单引号
    safe_q.replace("\r", "");      // 🚨 强力剔除回车符 (这就是导致前端 JSON 崩溃的真凶)
    safe_q.replace("\n", "\\n");   // 正确转义换行符，前端接收后可完美分段
    safe_q.replace("\t", "    ");  // 替换掉会破坏 JSON 的制表符

    // 2. 安全处理 AI 导购的回答
    String safe_a = global_voice_a; 
    safe_a.replace("\\", "\\\\");
    safe_a.replace("\"", "\\\""); 
    safe_a.replace("\r", "");      // 🚨 强力剔除回车符
    safe_a.replace("\n", "\\n");   // 正确转义换行符
    safe_a.replace("\t", "    ");

    // 3. 严格遵循标准 JSON 格式拼接
    String json = "{\"show\":" + String(global_voice_show ? "true" : "false") +
                  ",\"q\":\"" + safe_q + "\",\"a\":\"" + safe_a + "\"}";
                  
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
    String host = request->host();
    host.toLowerCase();
    if (host.startsWith("admin.mimistore.icu")) {
      AsyncWebServerResponse *redirect = request->beginResponse(302);
      redirect->addHeader("Location", "/admin");
      request->send(redirect);
      return;
    }
    static const char html[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0369a1">
<link rel="manifest" href="/manifest.webmanifest">
<title>智慧超市 - 顾客端</title>
<style>
/* ==========================================
   🧊 冰川高亮科技风与全域重置
   ========================================== */
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: linear-gradient(-45deg, #1e40af, #0369a1, #0ea5e9, #0e7490, #1e40af);
  background-size: 400% 400%; animation: gradientBg 15s ease infinite;
  min-height: 100vh; padding-bottom: 60px; overflow-x: hidden; color: #fff;
}
@keyframes gradientBg { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }

.circle { position: fixed; border-radius: 50%; filter: blur(70px); z-index: -1; opacity: 0.4; }
.circle-1 { width: 350px; height: 350px; top: 10%; right: 10%; background: linear-gradient(135deg, #06b6d4, #ffffff); }
.circle-2 { width: 350px; height: 350px; bottom: 10%; left: 10%; background: linear-gradient(135deg, #38bdf8, #10b981); }

/* ==========================================
   🧩 全局玻璃拟态容器
   ========================================== */
.glass-panel {
  background: rgba(255, 255, 255, 0.1); backdrop-filter: blur(15px); -webkit-backdrop-filter: blur(15px);
  border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 16px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.15);
}

.header {
  background: rgba(0, 0, 0, 0.15); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  border-bottom: 1px solid rgba(255,255,255,0.15); padding: 16px 20px; text-align: center; position: relative; z-index: 100;
}
.header h1 { font-size: 20px; font-weight: 700; color: #fff; text-shadow: 0 2px 8px rgba(0,0,0,0.3); }

/* ==========================================
   🌊 tabs 导航
   ========================================== */
.tabs {
  display: flex; background: rgba(0, 0, 0, 0.2); backdrop-filter: blur(15px); -webkit-backdrop-filter: blur(15px);
  border-bottom: 1px solid rgba(255,255,255,0.1); position: sticky; top: 0; z-index: 99;
}
.tab {
  flex: 1; text-align: center; padding: 14px 0; font-size: 14px; font-weight: 600;
  color: rgba(255,255,255,0.7); cursor: pointer; transition: 0.3s;
  border-bottom: 3px solid transparent; text-shadow: 0 1px 2px rgba(0,0,0,0.3);
}
.tab.active { color: #fff; border-bottom-color: #fff; text-shadow: 0 0 8px rgba(255,255,255,0.6); }

.page { display: none; }
.page.active { display: block; animation: fadeIn 0.4s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

/* ==========================================
   📸 页面 1: 扫码购物
   ========================================== */
.scan-area {
  text-align: center; margin: 15px 20px; padding: 30px 20px;
  border: 2px dashed rgba(255, 255, 255, 0.7); box-shadow: 0 0 20px rgba(255,255,255,0.1) inset;
}
.scan-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: #fff; margin-right: 6px; box-shadow: 0 0 10px #fff; animation: dot 1s infinite;
}
@keyframes dot { 0%,100%{opacity:1; transform:scale(1);} 50%{opacity:.3; transform:scale(0.8);} }
.scan-result {
  background: rgba(255, 255, 255, 0.2); border-radius: 12px; padding: 12px; margin: 10px 20px;
  border-left: 4px solid #fff; border-right: 1px solid rgba(255,255,255,0.15);
  border-top: 1px solid rgba(255,255,255,0.15); border-bottom: 1px solid rgba(255,255,255,0.15);
}

.cart-section { margin: 10px 20px; }
.section-title { font-size: 16px; font-weight: 700; color: #fff; margin: 18px 0 12px; padding-left: 8px; border-left: 4px solid #fff; text-shadow: 0 1px 3px rgba(0,0,0,0.2); }

.cart-item {
  background: rgba(255, 255, 255, 0.1); border: 1px solid rgba(255,255,255,0.15);
  border-radius: 14px; padding: 14px; margin-bottom: 10px; display: flex; align-items: center; gap: 12px; transition: 0.3s;
}
.cart-item:hover { background: rgba(255,255,255,0.15); transform: translateX(2px); }
.cart-item .icon { font-size: 32px; flex-shrink: 0; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.15)); }
.cart-item .info { flex: 1; }
.cart-item .name { font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 4px; }
.cart-item .price { font-size: 18px; font-weight: 700; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,0.2); }
.cart-item .qty { display: flex; align-items: center; gap: 8px; }
.cart-item .qty button {
  width: 28px; height: 28px; border: none; border-radius: 8px; background: rgba(255,255,255,0.2);
  color: #fff; font-size: 16px; cursor: pointer; transition: 0.2s;
}
.cart-item .qty button:hover { background: rgba(255,255,255,0.3); }

.cart-total { margin: 15px 20px; padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.15); }
.cart-total span { font-size: 16px; font-weight: 600; }
.cart-total .price { font-size: 26px; color: #fff; font-weight: 800; text-shadow: 0 2px 4px rgba(0,0,0,0.3); }
.cart-empty { text-align: center; padding: 40px; color: rgba(255,255,255,0.6); font-size: 14px; }
.btn-clear {
  font-size: 13px; background: rgba(255, 255, 255, 0.15); color: #fff; border: 1px solid rgba(255,255,255,0.3);
  padding: 6px 16px; border-radius: 8px; cursor: pointer; float: right; margin-right: 20px; transition: 0.3s;
}
.btn-clear:hover { background: rgba(255,255,255,0.25); }

.btn-checkout {
  width: calc(100% - 40px); margin: 15px 20px; padding: 16px;
  background: linear-gradient(90deg, #10b981, #059669, #10b981); background-size: 200% auto;
  border: none; border-radius: 14px; font-size: 18px; font-weight: 800; cursor: pointer; color: white;
  box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2); transition: 0.5s; letter-spacing: 2px;
}
.btn-checkout:hover { background-position: right center; transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }

/* ==========================================
   ⚖️ 页面 2: 称重计价
   ========================================== */
#page-weigh .glass-panel { padding: 25px 20px; text-align: center; margin-bottom: 20px; }
#weighValue { font-size: 54px; font-weight: 800; color: #fff; text-shadow: 0 2px 10px rgba(0,0,0,0.3); margin: 5px 0; }
.weigh-item-card {
  background: rgba(255,255,255,0.08); border: 2px solid rgba(255,255,255,0.2); border-radius: 12px; padding: 14px;
  text-align: center; cursor: pointer; transition: 0.3s;
}
.weigh-item-card.active { border-color: #fff; background: rgba(255,255,255,0.2); box-shadow: 0 0 15px rgba(255,255,255,0.2); }

#weighBtn {
  width: 100%; margin-top: 15px; padding: 16px; border: none; border-radius: 12px; font-size: 16px; font-weight: 700; transition: 0.3s;
}
#weighBtn:not(:disabled) { background: linear-gradient(135deg, #10b981, #0ea5e9); color: white; box-shadow: 0 4px 12px rgba(0,0,0,0.2); cursor: pointer; }
#weighBtn:disabled { background: rgba(255,255,255,0.15); color: rgba(255,255,255,0.5); cursor: not-allowed; }

/* ==========================================
   📦 页面 3: 商品浏览
   ========================================== */
.product-list { padding: 5px 20px 20px; }
.product-card {
  background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255,255,255,0.15);
  border-radius: 14px; padding: 15px; margin-bottom: 12px; display: flex; align-items: center; gap: 14px; transition: 0.3s; cursor: pointer;
}
.product-card:hover { background: rgba(255,255,255,0.12); transform: scale(1.02); }
.product-img { width: 60px; height: 60px; background: rgba(255,255,255,0.1); border-radius: 12px; font-size: 36px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.product-info { flex: 1; }
.product-name { font-size: 16px; font-weight: 700; color: #fff; margin-bottom: 4px; }
.product-meta { font-size: 12px; color: rgba(255,255,255,0.6); margin-bottom: 8px; }
.product-bottom { display: flex; justify-content: space-between; align-items: center; }
.product-price { font-size: 18px; font-weight: 800; color: #06b6d4; text-shadow: 0 1px 2px rgba(0,0,0,0.2); }
.product-stock {
  font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 12px; text-align: center;
  background: rgba(255,255,255,0.2); color: #fff; border: 1px solid rgba(255,255,255,0.3);
}
.product-stock.warn { background: rgba(251,146,60,0.3); color: #fff; border-color: rgba(251,146,60,0.4); }
.product-stock.danger { background: rgba(239,68,68,0.3); color: #fff; border-color: rgba(239,68,68,0.4); }

/* ==========================================
   🤖 页面 4: AI助手
   ========================================== */
.ai-section { padding: 20px; }
.ai-input {
  width: 100%; padding: 14px 16px; background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.25);
  border-radius: 12px; font-size: 15px; color: #fff; outline: none; transition: 0.3s;
}
.ai-input:focus { border-color: #fff; background: rgba(255,255,255,0.25); box-shadow: 0 0 12px rgba(255,255,255,0.3); }
.ai-input::placeholder { color: rgba(255,255,255,0.5); }
.ai-btn {
  width: 100%; padding: 14px; background: linear-gradient(135deg, #0ea5e9, #3b82f6); color: white;
  border: none; border-radius: 12px; font-size: 16px; font-weight: 700; cursor: pointer;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15); margin-bottom: 16px; transition: 0.3s; letter-spacing: 1px;
}
.ai-btn:active { transform: scale(0.98); }
.ai-tag-btn {
  padding: 6px 12px; background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.25);
  border-radius: 10px; font-size: 12px; cursor: pointer; color: #fff; font-weight: 600; transition: 0.2s;
}
.ai-tag-btn:hover { background: rgba(255,255,255,0.3); }
.ai-result {
  margin-top: 15px; padding: 16px; background: rgba(0,0,0,0.1); border: 1px solid rgba(255,255,255,0.15);
  border-radius: 14px; font-size: 14px; line-height: 1.6; min-height: 60px; border-left: 4px solid #fff; white-space: pre-wrap;
}

/* ==========================================
   🧩 弹窗系统 (Modal)
   ========================================== */
.modal-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(8px);
  z-index: 1000; display: flex; align-items: center; justify-content: center; animation: fadeIn 0.2s ease;
}
.modal-overlay.hidden { display: none; }
.modal-card {
  background: linear-gradient(135deg, #0369a1, #1e40af); border: 1px solid rgba(255,255,255,0.2);
  border-radius: 20px; width: 92%; max-width: 420px; max-height: 85vh; overflow-y: auto;
  box-shadow: 0 20px 40px rgba(0,0,0,0.3);
}
.modal-header { padding: 18px 20px; border-bottom: 1px solid rgba(255,255,255,0.15); display: flex; justify-content: space-between; align-items: center; }
.modal-header h3 { font-size: 18px; font-weight: 700; color: #fff; }
.modal-close {
  width: 32px; height: 32px; border-radius: 50%; border: none; background: rgba(255,255,255,0.15);
  color: #fff; font-size: 18px; cursor: pointer; transition: 0.2s;
}
.modal-close:hover { background: rgba(255,255,255,0.25); }
.modal-body { padding: 18px 20px; }

.order-item { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px dashed rgba(255,255,255,0.15); font-size: 14px; color: rgba(255,255,255,0.8); }
.order-total-row { display: flex; justify-content: space-between; padding: 16px 0; font-size: 18px; font-weight: 700; }
.order-total-row .price { color: #fff; font-size: 24px; text-shadow: 0 2px 4px rgba(0,0,0,0.3); }

.pay-methods { display: flex; gap: 12px; margin-top: 10px; }
.pay-method {
  flex: 1; padding: 14px 8px; border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
  text-align: center; cursor: pointer; font-size: 13px; font-weight: 700; transition: 0.3s; background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.7);
}
.pay-method.active { border-color: #fff; background: rgba(255,255,255,0.2); color: #fff; box-shadow: 0 0 12px rgba(255,255,255,0.2); }
.pay-method .icon { font-size: 32px; display: block; margin-bottom: 6px; }

.qr-area { text-align: center; margin: 18px 0; padding: 20px; background: #fff; border-radius: 16px; box-shadow: 0 4px 15px rgba(0,0,0,0.15); }
.qr-area img { width: 180px; height: 180px; }

.btn-row { display: flex; gap: 12px; margin-top: 18px; }
.btn-pay {
  flex: 1; padding: 15px; border: none; border-radius: 12px; font-size: 16px; font-weight: 800; cursor: pointer; color: white; transition: 0.3s;
}
.btn-pay.primary { background: linear-gradient(135deg, #10b981, #0ea5e9); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
.btn-pay.secondary { background: rgba(255,255,255,0.15); color: #fff; }
.btn-pay.primary:active { transform: translateY(2px); }

/* 全局吐司提示 */
.toast {
  position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
  background: rgba(16,185,129,0.95); backdrop-filter: blur(5px); color: white; padding: 12px 24px;
  border-radius: 20px; font-weight: 700; z-index: 9999; box-shadow: 0 5px 15px rgba(0,0,0,0.2);
  animation: toastIn 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275); pointer-events: none;
}
.toast.dup { background: rgba(251,146,60,0.95); }
.toast.err { background: rgba(239,68,68,0.95); }
@keyframes toastIn { from { opacity: 0; transform: translateX(-50%) translateY(-30px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }

/* 🌟 语音字幕悬浮窗 UI（支持超长文本完美换行折行） */
.subtitle-overlay {
  position: fixed; bottom: 85px; left: 50%; transform: translateX(-50%) translateY(150%); width: 90%; max-width: 500px;
  background: rgba(3, 105, 161, 0.95); backdrop-filter: blur(15px); border: 1px solid rgba(255,255,255,0.25);
  border-radius: 20px; padding: 18px 24px; box-shadow: 0 15px 35px rgba(0,0,0,0.3);
  z-index: 9999; opacity: 0; transition: all 0.5s cubic-bezier(0.2, 1.2, 0.3, 1);
}
.subtitle-overlay.show { transform: translateX(-50%) translateY(0); opacity: 1; }
.sub-row { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 8px; width: 100%; }
.sub-row:last-child { margin-bottom: 0; }
.sub-icon { font-size: 20px; line-height: 1.2; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.2)); flex-shrink: 0; }
.sub-text-q { font-size: 15px; color: #fff; font-weight: 700; line-height: 1.5; flex: 1; word-break: break-word; white-space: pre-wrap; text-shadow: 0 1px 2px rgba(0,0,0,0.2); }
.sub-text-a { font-size: 15px; color: #e0f2fe; font-weight: 400; line-height: 1.6; flex: 1; word-break: break-word; white-space: pre-wrap; }

.reg-input {
  width:100%; padding:14px; background:rgba(255,255,255,0.1); border:1px solid rgba(255,255,255,0.2);
  border-radius:12px; font-size:15px; color:#fff; margin-bottom:10px; text-align:center; font-family:inherit; outline:none; transition:0.3s;
}
.reg-input:focus { border-color:#fff; box-shadow:0 0 10px rgba(255,255,255,0.3); }
</style>
</head>
<body>

<div class="circle circle-1"></div>
<div class="circle circle-2"></div>

<div class="header">
  <h1>🛒 智慧超市终端</h1>
  <div style="margin-top:10px; display:flex; justify-content:center; gap:10px; align-items:center">
    <div id="memberBar" style="display:none; background:rgba(255,255,255,0.15); border:1px solid rgba(255,255,255,0.3); color:#fff; border-radius:10px; padding:6px 14px; font-size:13px; font-weight:700;"></div>
    <button onclick="showRegForm()" style="background:rgba(255,255,255,0.15); color:#fff; border:1px solid rgba(255,255,255,0.25); border-radius:10px; padding:6px 14px; font-size:13px; font-weight:600; cursor:pointer; transition:0.3s;" onmouseover="this.style.background='rgba(255,255,255,0.25)'" onmouseout="this.style.background='rgba(255,255,255,0.15)'">🎫 注册会员</button>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('scan')">📷 扫码购物</div>
  <div class="tab" onclick="switchTab('weigh')">⚖️ 智能称重</div>
  <div class="tab" onclick="switchTab('products')">📦 全部商品</div>
  <div class="tab" onclick="switchTab('ai')">🤖 AI 导购</div>
</div>

<div class="page active" id="page-scan">
  <div class="glass-panel scan-area">
    <div style="font-size:16px; font-weight:700;"><span class="scan-dot"></span>等待扫描...</div>
    <div style="font-size:13px; color:rgba(255,255,255,0.6); margin-top:8px;">请将商品条码/二维码对准识别区域</div>
  </div>
  <div id="scanResult"></div>
  
  <div class="cart-section">
    <div class="section-title">🛍️ 购物清单</div>
    <div id="cartList"><div class="glass-panel cart-empty">空空如也，快去扫码添加商品吧</div></div>
  </div>
  
  <div class="glass-panel cart-total" id="cartTotal" style="display:none">
    <span>合计金额</span>
    <span class="price" id="totalPrice">¥0.00</span>
  </div>
  <button class="btn-clear" onclick="clearCart()">🗑️ 清空清单</button>
  <button class="btn-checkout" id="btnCheckout" onclick="openCheckout()" style="display:none">💳 结账付款</button>
</div>

<div class="page" id="page-weigh">
  <div class="glass-panel" style="margin:15px 20px; padding:25px 20px; text-align:center;">
    <div style="font-size:14px; color:rgba(255,255,255,0.7); margin-bottom:8px;">当前托盘重量</div>
    <div id="weighValue">0.0</div>
    <div style="font-size:14px; color:rgba(255,255,255,0.6)">克 (g)</div>
    <div id="weighStatus" style="font-size:13px; color:rgba(255,255,255,0.5); margin-top:10px;">等待放置物品...</div>
  </div>
  
  <div style="margin:10px 20px">
    <div class="section-title">📦 请选择您要称重的商品</div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px" id="weighProducts"></div>
  </div>
  
  <div class="glass-panel" style="margin:15px 20px; padding:18px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <span style="color:rgba(255,255,255,0.7);">选中商品</span>
      <span id="weighItemName" style="font-weight:700; font-size:16px;">未选择</span>
    </div>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <span style="color:rgba(255,255,255,0.7);">标准单价</span>
      <span id="weighPrice" style="font-weight:700; color:#fff;">¥0.00</span>
    </div>
    <div style="display:flex; justify-content:space-between; align-items:center; padding-top:12px; border-top:1px dashed rgba(255,255,255,0.2);">
      <span style="font-weight:700; font-size:16px;">小计</span>
      <span id="weighTotal" style="font-size:24px; font-weight:800; color:#fff; text-shadow:0 1px 4px rgba(0,0,0,0.2);">¥0.00</span>
    </div>
    <button onclick="weighAddToCart()" id="weighBtn" disabled>请先选择商品并放置物品</button>
  </div>
</div>

<div class="page" id="page-products">
  <div class="product-list">
    <div class="section-title">📦 超市全品类</div>
    <div id="productGrid"></div>
  </div>
</div>

<div class="page" id="page-ai">
  <div class="glass-panel ai-section" style="margin:15px 20px;">
    <div style="font-size:14px; color:rgba(255,255,255,0.7); margin-bottom:16px; text-align:center;">💡 向智能导购提问，如：可乐在哪个区？有折扣吗？</div>
    
    <div style="display:flex; gap:10px; align-items:center; margin-bottom:16px;">
      <input type="text" id="aiQuestion" class="ai-input" placeholder="输入你想了解的内容..." style="flex:1">
      <button onclick="voiceAskAI()" style="width:50px; height:50px; border-radius:14px; background:linear-gradient(135deg, #10b981, #0ea5e9); color:white; border:none; font-size:22px; cursor:pointer; flex-shrink:0; box-shadow:0 4px 10px rgba(0,0,0,0.15);">🎤</button>
    </div>
    <button class="ai-btn" onclick="askAI()">🚀 发送提问</button>
    
    <div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:10px;">
      <button class="ai-tag-btn" onclick="askAI('今天有什么推荐的商品？')">🔥 今日推荐</button>
      <button class="ai-tag-btn" onclick="askAI('我预算20元，推荐适合晚上吃的零食')">💰 预算推荐</button>
      <button class="ai-tag-btn" onclick="askAI('有没有正在促销或临期优惠的商品？')">🏷️ 今日优惠</button>
      <button class="ai-tag-btn" onclick="askAI('这个商品缺货的话，有什么相似替代品？')">🔁 缺货替代</button>
    </div>
    
    <div class="ai-result" id="aiResult">
      <span style="color:rgba(255,255,255,0.6); font-style:italic;">AI 回答将在此处生成...</span>
    </div>
  </div>
</div>

<div class="modal-overlay hidden" id="checkoutModal">
  <div class="modal-card">
    <div class="modal-header">
      <h3>🧾 确认订单信息</h3>
      <button class="modal-close" onclick="closeCheckout()">✕</button>
    </div>
    <div class="modal-body" id="checkoutBody"></div>
  </div>
</div>

<div class="modal-overlay hidden" id="payModal">
  <div class="modal-card">
    <div class="modal-header">
      <h3>💳 扫码完成支付</h3>
      <button class="modal-close" onclick="cancelPay()">✕</button>
    </div>
    <div class="modal-body" id="payBody"></div>
  </div>
</div>

<div class="subtitle-overlay" id="subtitleBox">
  <div class="sub-row">
    <div class="sub-icon">🗣️</div>
    <div class="sub-text-q" id="subQ">...</div>
  </div>
  <div class="sub-row">
    <div class="sub-icon">🤖</div>
    <div class="sub-text-a" id="subA">...</div>
  </div>
</div>

<script>
// ===============================================================
// 💡 100% 完整复刻原版数据交互模型，确保功能绝对平移
// ===============================================================
var allProducts = [];
var productMap = {};
var cart = [];
var scanTimer = null;
var lastSeenCode = '';
var lastAddedCode = '';
var lastAddedTime = 0;
var MODULE_A_IP = "192.168.43.13";
var USE_PI_FALLBACK = false;
var currentOrderId = -1;
var currentOrderTotal = 0;
var currentPayMethod = 'cash';
var payPollTimer = null;
var payPollCount = 0;
var qrRetry = 0;
var currentMember = null; 
var memberRFIDTimer = null;
var customerSessionId=localStorage.getItem('mimi_customer_session');
if(!customerSessionId){customerSessionId='cust_'+Math.random().toString(36).slice(2,18);localStorage.setItem('mimi_customer_session',customerSessionId);}
function customerMetric(event,q){var b='event='+encodeURIComponent(event)+(q?'&q='+encodeURIComponent(q):'');fetch('/api/customer/analytics-event',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b}).catch(function(){});}

function startMemberRFID(){
  memberRFIDTimer = setInterval(async function(){
    try{
      var r = await fetch('/api/member/rfid-poll');
      var d = await r.json();
      if(d.card && d.memberId && !currentMember){
        currentMember = {id: d.memberId, name: d.name, points: 0};
        fetch('/api/member/lookup?code=MEM:'+d.memberId).then(function(r2){return r2.json()}).then(function(md){
          if(md.ok) currentMember = md;
          showMemberWelcome();
        });
      }
    }catch(e){}
  }, 800);
}
startMemberRFID();

function findProduct(code) {
  if (productMap[code]) return productMap[code];
  return allProducts.find(function(x){
    return x.qr_code === code || x.name === code || x.name.indexOf(code) >= 0;
  });
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active')});
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});
  ['scan','weigh','products','ai'].forEach(function(t,i){
    if(t===tab){
      document.querySelectorAll('.tab')[i].classList.add('active');
      document.getElementById('page-'+t).classList.add('active');
    }
  });
  if(tab==='scan') startScan(); else stopScan();
  if(tab==='weigh') startWeighPoll(); else stopWeighPoll();
  if(tab==='products') loadProducts();
}

function startScan(){stopScan();doScan();scanTimer=setInterval(doScan,200);}
function stopScan(){if(scanTimer){clearInterval(scanTimer);scanTimer=null;}}

async function doScan(){
  if(window._skipScan){return;}
  try{
    var r=await fetch('http://' + MODULE_A_IP + '/qr-scan');
    var d=await r.json();
    var code='';
    if(d.ok && d.text) code=(d.text||'').trim();
    if(!code){if(lastSeenCode)lastSeenCode='';return;}
    var now=Date.now();
    if(code===lastSeenCode)return;
    lastSeenCode=code;
    if(code===lastAddedCode&&(now-lastAddedTime)<5000)return;
    if(code.startsWith('MEM:')||code.startsWith('mem:')){
      try{
        var mr=await fetch('/api/member/lookup?code='+encodeURIComponent(code));
        var md=await mr.json();
        if(md.ok){currentMember=md;showMemberWelcome();}
        else showToast(md.msg||'未找到会员','err');
      }catch(e){}
      return;
    }
    if(allProducts.length===0)await loadProducts();
    var p=findProduct(code);
    if(!p){showToast('未知码: ['+code+']','err');return;}
    lastAddedCode=code;lastAddedTime=now;
    addToCart(code);
    showScanResult(code);
  }catch(e){}
}

function showMemberWelcome(){
  var el=document.getElementById('memberBar');
  el.innerHTML='<span>👤 '+currentMember.name+' | 积分: '+currentMember.points+'</span> <button onclick="logoutMember()" style="margin-left:8px;background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:6px;padding:2px 8px;font-size:11px;color:#fff;cursor:pointer">退出</button>';
  el.style.display='block';
  showToast('🎉 欢迎 '+currentMember.name+'！积分: '+currentMember.points,'ok');
}
function logoutMember(){currentMember=null;document.getElementById('memberBar').style.display='none';}

function showRegForm(){
  var h='<div style="text-align:center;">';
  h+='<h3 style="margin-bottom:16px; color:#fff;">🎫 新会员注册</h3>';
  h+='<input id="regName" class="reg-input" placeholder="请输入您的姓名">';
  h+='<input id="regPhone" class="reg-input" type="tel" placeholder="手机号 (仅限注册一次)">';
  h+='<div style="display:flex;gap:12px; margin-top:10px;">';
  h+='<button onclick="doRegister()" class="btn-pay primary" style="flex:1">马上注册</button>';
  h+='<button onclick="closeModal()" class="btn-pay secondary" style="flex:1">暂不注册</button>';
  h+='</div></div>';
  document.getElementById('checkoutBody').innerHTML=h;
  document.getElementById('checkoutModal').classList.remove('hidden');
  setTimeout(function(){var el=document.getElementById('regName');if(el)el.focus();},300);
}

async function doRegister(){
  var name=document.getElementById('regName').value.trim();
  var phone=document.getElementById('regPhone').value.trim();
  if(!name){alert('请输入姓名');return;}
  var r=await fetch('/api/member/register',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'name='+encodeURIComponent(name)+'&phone='+encodeURIComponent(phone)});
  var d=await r.json();
  if(d.ok){
    var h='<div style="text-align:center;">';
    h+='<div style="font-size:48px; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.2));">🎫</div>';
    h+='<h3 style="color:#fff; margin:10px 0;">'+name+'，注册成功</h3>';
    h+='<p style="color:rgba(255,255,255,0.8); margin:8px 0">您的会员专属码：<br><b style="font-size:22px;color:#fff;">'+d.qr+'</b></p>';
    h+='<div style="background:#fff; padding:10px; border-radius:16px; display:inline-block; margin:8px 0;"><iframe src="/qr-svg?text='+encodeURIComponent(d.qr)+'" style="width:180px;height:180px;border:none;display:block"></iframe></div>';
    h+='<p style="font-size:12px;color:rgba(255,255,255,0.7); margin-bottom:15px;">截屏保存上方二维码<br>结账前扫描即可享受会员权益</p>';
    h+='<div style="display:flex; gap:10px; margin-bottom:10px;">';
    h+='<button onclick="bindRFIDNow(\''+d.id+'\')" class="btn-pay" style="flex:1; background:rgba(255,255,255,0.2); border:1px solid rgba(255,255,255,0.3)">💳 绑实体卡</button>';
    h+='<button onclick="closeModal()" class="btn-pay primary" style="flex:1">完 成</button></div>';
    h+='<div id="bindHint" style="font-size:13px;color:#fff"></div></div>';
    document.getElementById('checkoutBody').innerHTML=h;
  }else{alert('注册失败: '+(d.msg||''));}
}

function showModal(h){var el=document.getElementById('checkoutBody');el.innerHTML=h;document.getElementById('checkoutModal').classList.remove('hidden');}
function closeModal(){document.getElementById('checkoutModal').classList.add('hidden');if(window._bindTimer)clearInterval(window._bindTimer);}
var _bindTimer=null;

function bindRFIDNow(mid){
  var hint=document.getElementById('bindHint');
  hint.textContent='⏳ 请将实体卡贴近读卡器...';
  _bindTimer=setInterval(async function(){
    var r=await fetch('/api/rfid-poll');var d=await r.json();
    if(d.card&&d.uid){
      clearInterval(_bindTimer);
      var resp=await fetch('/api/member/bind-rfid',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'uid='+d.uid+'&id='+mid});
      var rd=await resp.json();
      hint.textContent=rd.ok?'✅ 绑定成功！刷卡或扫码均可识别':'❌ 绑定失败，请重试';
    }
  },500);
}

function showScanResult(code){
  var p=findProduct(code);
  var el=document.getElementById('scanResult');
  if(p){
    var dp=getDiscountedPrice(p),disc=getExpiryDiscount(p);
    var badge=disc<1?' <span style="background:#ef4444;color:white;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;">'+(disc===0.5?'5折':'7折')+'</span>':'';
    el.innerHTML='<div class="scan-result" style="color:#fff"><b style="font-size:16px;">'+p.icon+' '+p.name+'</b>'+badge+'<br><span style="color:#fff;font-size:20px;font-weight:800;">¥'+dp.toFixed(2)+'</span>'+(disc<1?' <span style="text-decoration:line-through;color:rgba(255,255,255,0.5);font-size:13px">¥'+p.price.toFixed(2)+'</span>':'')+'</div>';
    setTimeout(function(){el.innerHTML='';},3000);
  }
}

function getExpiryDiscount(p){
  if(!p||!p.mfg_date||!p.shelf_life||p.mfg_date==='-')return 1.0;
  var parts=p.mfg_date.split('-');if(parts.length<3)return 1.0;
  var mfg=new Date(parseInt(parts[0]),parseInt(parts[1])-1,parseInt(parts[2]));
  var exp=new Date(mfg);exp.setMonth(exp.getMonth()+p.shelf_life);
  var days=Math.ceil((exp-new Date())/(86400000));
  if(days<=30)return 0.5; 
  if(days<=60)return 0.7; 
  return 1.0; 
}
function getDiscountedPrice(p){return p.price*getExpiryDiscount(p);}

function addToCart(code){
  var p=findProduct(code);
  var existing=cart.find(function(x){return x.code===code;});
  if(!p || p.stock <= (existing ? existing.qty : 0)) return;
  var f=existing;
  if(f)f.qty++;else cart.push({code:code,qty:1});
  customerMetric('cart');
  renderCart();refreshProductStock();
}
function cartQty(code,d){
  var f=cart.find(function(x){return x.code===code;});
  if(!f)return;
  f.qty+=d;
  if(f.qty<=0)cart=cart.filter(function(x){return x.code!==code;});
  renderCart();refreshProductStock();
}
function clearCart(){cart=[];lastAddedCode='';lastSeenCode='';renderCart();refreshProductStock();}

function renderCart(){
  var el=document.getElementById('cartList');
  var t=0;
  if(cart.length===0){
    el.innerHTML='<div class="glass-panel cart-empty">空空如也，快去扫码添加商品吧</div>';
  }else{
    var h='';
    cart.forEach(function(i){
      var p=findProduct(i.code);
      h+='<div class="cart-item">';
      h+='<div class="icon">'+(p?p.icon:'📦')+'</div>';
      var dp=getDiscountedPrice(p),disc=getExpiryDiscount(p);
      var badge=disc<1?' <span style="background:#ef4444;color:white;padding:2px 6px;border-radius:8px;font-size:10px;font-weight:700;">'+(disc===0.5?'5折':'7折')+'临期</span>':'';
      h+='<div class="info"><div class="name">'+(p?p.name:i.code)+badge+'</div>';
      h+='<div class="price">¥'+dp.toFixed(2)+''+(disc<1?' <s style="color:rgba(255,255,255,0.5);font-size:12px;font-weight:400;">¥'+p.price.toFixed(2)+'</s>':'')+'</div></div>';
      h+='<div class="qty"><button onclick="cartQty(\''+i.code+'\',-1)">−</button>';
      h+='<span style="font-size:16px;font-weight:700;color:#fff;min-width:24px;text-align:center">'+i.qty+'</span>';
      h+='<button onclick="cartQty(\''+i.code+'\',1)">+</button></div></div>';
      t+=i.qty*dp;
    });
    el.innerHTML=h;
  }
  document.getElementById('cartTotal').style.display=cart.length?'flex':'none';
  document.getElementById('totalPrice').textContent='¥'+t.toFixed(2);
  document.getElementById('btnCheckout').style.display=cart.length?'block':'none';
}

async function loadProducts(){
  try{
    var r=await fetch('/api/products');
    var list=await r.json();
    allProducts=list;
    list.forEach(function(p){productMap[p.qr_code]=p;});
    renderProductList();
  }catch(e){}
}

function renderProductList(){
  var h='';
  allProducts.forEach(function(p){
    if(!p.name||!p.qr_code)return;
    var sc=p.stock<5?' product-stock danger':p.stock<15?' product-stock warn':' product-stock';
    h+='<div class="product-card" onclick="addToCart(\''+p.qr_code+'\')">';
    h+='<div class="product-img">'+p.icon+'</div>';
    h+='<div class="product-info">';
    h+='<div class="product-name">'+p.name+'</div>';
    h+='<div class="product-meta">生产: '+p.mfg_date+' | 保质: '+p.shelf_life+'个月</div>';
    h+='<div class="product-bottom">';
    h+='<span class="product-price">¥'+p.price.toFixed(2)+'</span>';
    h+='<span class="'+sc+'" id="ps-'+p.qr_code+'">库存: '+p.stock+'件</span>';
    h+='</div></div></div>';
  });
  var grid=document.getElementById('productGrid');
  if(grid)grid.innerHTML=h;
  refreshProductStock();
}

function refreshProductStock(){
  allProducts.forEach(function(p){
    var el=document.getElementById('ps-'+p.qr_code);
    if(!el)return;
    var inCart=0;
    var ci=cart.find(function(x){return x.code===p.qr_code;});
    if(ci)inCart=ci.qty;
    var avail=p.stock-inCart;
    if(avail<0)avail=0;
    el.textContent='库存: '+avail+'件';
    var sc='product-stock';
    if(avail<=0||avail<5)sc='product-stock danger';
    else if(avail<15)sc='product-stock warn';
    el.className=sc;
  });
}

function askAI(q){
  q=q||document.getElementById("aiQuestion").value.trim();
  if(q===""){alert("请先输入内容");return;}
  document.getElementById("aiQuestion").value='';
  var rd=document.getElementById("aiResult");
  customerMetric('question',q);
  rd.innerHTML="<span style='color:rgba(255,255,255,0.7);font-style:italic;'>🤖 大脑正在高速运转中...</span>";
  var done=false;
  function fallback(){if(done)return;done=true;fetch("/api/customer/chat?q="+encodeURIComponent(q)).then(function(r){return r.json();}).then(function(r){rd.innerText=r.answer||r.msg||'暂时无法回答';}).catch(function(){rd.innerHTML="<span style='color:#ef4444'>提问遇到了一点小麻烦，请重试</span>";});}
  try{
    var ws=new WebSocket('ws://10.176.240.100:18789/');
    var timer=setTimeout(function(){try{ws.close();}catch(e){}fallback();},30000);
    ws.onopen=function(){ws.send(JSON.stringify({type:'message',chat_id:customerSessionId,content:q}));};
    ws.onmessage=function(ev){
      try{var m=JSON.parse(ev.data);var text=m.content||'';if(text.indexOf('working')>=0){rd.innerText='MimiClaw 正在查询门店数据...';return;}done=true;clearTimeout(timer);rd.innerText=text||'暂时无法回答';if(text)customerMetric('resolved');ws.close();}catch(e){fallback();}
    };
    ws.onerror=fallback;
  }catch(e){fallback();}
}
function voiceAskAI(){alert('请按下终端机上的物理语音按钮进行提问');}

function openCheckout(){
  if(cart.length===0)return;
  currentOrderId=-1;
  lastQueuedReceipt=-1;
  var items=[],total=0;
  cart.forEach(function(i){
    var p=findProduct(i.code);
    var dp=getDiscountedPrice(p);
    if(p){items.push(p.name+' <span style="color:rgba(255,255,255,0.6)">x'+i.qty+'</span>  <b style="color:#fff">¥'+(dp*i.qty).toFixed(2)+'</b>');total+=dp*i.qty;}
  });
  
  var memberDisc=0;
  if(currentMember){memberDisc=total*0.05;total-=memberDisc;}
  currentOrderTotal=total;
  
  var h='';
  if(currentMember)h+='<div style="font-size:14px;color:#fff;font-weight:700;margin-bottom:10px;padding:8px;background:rgba(255,255,255,0.2);border-radius:8px;">👤 尊贵的 '+currentMember.name+' 享 95 折特权 | 当前积分: '+currentMember.points+'</div>';
  
  h+='<div style="background:rgba(0,0,0,0.1); border-radius:12px; padding:12px; margin-bottom:15px; max-height:180px; overflow-y:auto;">';
  items.forEach(function(l){h+='<div class="order-item">'+l+'</div>';});
  h+='</div>';
  
  h+='<div class="order-total-row"><span>支付合计</span><span class="price">¥'+total.toFixed(2)+(memberDisc>0?'<br><span style="font-size:12px;color:#fff;display:block;text-align:right;">(已优惠¥'+memberDisc.toFixed(2)+')</span>':'')+'</span></div>';
  h+='<div style="font-size:13px;color:rgba(255,255,255,0.6);margin-bottom:8px">选择结账方式</div>';
  h+='<div class="pay-methods">';
  h+='<div class="pay-method active" onclick="selectPay(this,&apos;cash&apos;)"><span class="icon">💵</span>现金支付</div>';
  h+='<div class="pay-method" onclick="selectPay(this,&apos;wx&apos;)"><span class="icon">🟢</span>微信扫码</div>';
  h+='<div class="pay-method" onclick="selectPay(this,&apos;alipay&apos;)"><span class="icon">🔵</span>支付宝扫码</div>';
  h+='</div>';
  h+='<div class="btn-row"><button class="btn-pay secondary" onclick="closeCheckout()">返回修改</button>';
  h+='<button class="btn-pay primary" onclick="doCheckout()">确 认 付 款</button></div>';
  
  document.getElementById('checkoutBody').innerHTML=h;
  document.getElementById('checkoutModal').classList.remove('hidden');
}

function closeCheckout(){
  document.getElementById('checkoutModal').classList.add('hidden');
  currentPayMethod='cash';currentOrderId=-1;
}

function selectPay(el,method){
  currentPayMethod=method;
  document.querySelectorAll('#checkoutBody .pay-method').forEach(function(m){m.classList.remove('active')});
  el.classList.add('active');
}

async function doCheckout(){
  if(cart.length===0)return;
  var itemsStr='',total=0;
  cart.forEach(function(i,idx){
    var p=findProduct(i.code);
    var dp=getDiscountedPrice(p);
    if(p){if(idx>0)itemsStr+=', ';itemsStr+=p.name+'x'+i.qty;total+=dp*i.qty;}
  });
  if(currentMember)total*=0.95; 
  try{
    var r=await fetch('/api/checkout?items='+encodeURIComponent(itemsStr)+'&total='+total.toFixed(2)+'&method='+currentPayMethod);
    var d=await r.json();
    if(!d.ok){showToast('订单通道拥挤，请重试','err');return;}
    currentOrderId=d.orderId;currentOrderTotal=total;
  }catch(e){showToast('网络有点小情绪，请重试','err');return;}
  if(currentPayMethod==='cash'){doCashPayment();}else{showPayModal(currentPayMethod);}
}

async function doCashPayment(){
  try{
    var itemsParam=cart.map(function(i){
      var p=findProduct(i.code);return (p?p.qr_code:i.code)+':'+i.qty;
    }).join(',');
    var r=await fetch('/api/pay-confirm?orderId='+currentOrderId+'&items='+encodeURIComponent(itemsParam));
    var d=await r.json();
    if(d.ok)showPaySuccess();else showToast('收银台确认失败','err');
  }catch(e){showToast('网络断开了','err');}
}

function showPayModal(method){
  var payUrl=window.location.protocol+'//'+window.location.host+'/pay?order='+currentOrderId+'&t='+currentOrderTotal.toFixed(2);
  var h='<div style="font-size:13px; color:rgba(255,255,255,0.6); margin-bottom:10px;">流水单号 #'+(currentOrderId+1)+'</div>';
  h+='<div class="order-total-row" style="padding-top:0;"><span>应付总额</span><span class="price">¥'+currentOrderTotal.toFixed(2)+'</span></div>';
  h+='<div class="qr-area">';
  h+='<iframe src="/qr-svg?text='+encodeURIComponent(payUrl)+'" style="width:180px;height:180px;border:none"></iframe>';
  h+='</div>';
  h+='<div style="font-size:14px; color:rgba(255,255,255,0.8); text-align:center; margin-bottom:15px;">📱 请打开手机'+(method==='wx'?'微信':'支付宝')+'扫码支付</div>';
  h+='<div style="margin-top:10px;text-align:center;font-weight:700;color:#fff;" id="pollStatus">⏳ 云端校验支付状态中...</div>';
  h+='<div class="btn-row">';
  h+='<button class="btn-pay secondary" onclick="cancelPay()">取消支付</button>';
  h+='<button style="flex:1;background:rgba(255,255,255,0.1);color:#fff;font-size:13px;border:1px solid rgba(255,255,255,0.2);border-radius:12px;cursor:pointer" onclick="manualConfirm()">遇到问题？强制放行</button>';
  h+='</div>';
  document.getElementById('payBody').innerHTML=h;
  document.getElementById('payModal').classList.remove('hidden');
  startPayPolling();
}

function startPayPolling(){
  payPollCount=0;qrRetry=0;
  if(payPollTimer)clearInterval(payPollTimer);
  payPollTimer=setInterval(async function(){
    payPollCount++;
    try{
      var r=await fetch('/api/pay-status?order='+currentOrderId);
      var d=await r.json();
      var el=document.getElementById('pollStatus');
      if(el){
        if(d.confirmed){
          el.innerHTML='✅ 支付成功！正在生成凭证...'; el.style.color='#fff';
          clearInterval(payPollTimer);confirmPayAndFinish();
        }else if(payPollCount>60){
          el.innerHTML='⏰ 支付超时，请重新发起'; el.style.color='#ef4444'; clearInterval(payPollTimer);
        }else{
          var dots='';for(var i=0;i<(payPollCount%4);i++)dots+='.';
          el.innerHTML='<span style="color:#06b6d4">⏳ 正在等待手机端付款'+dots+'</span>';
        }
      }
    }catch(e){}
  },2000);
}

async function confirmPayAndFinish(){
  try{
    var itemsParam=cart.map(function(i){
      var p=findProduct(i.code);return (p?p.qr_code:i.code)+':'+i.qty;
    }).join(',');
    var r=await fetch('/api/pay-confirm?orderId='+currentOrderId+'&items='+encodeURIComponent(itemsParam));
    var d=await r.json();
    if(d.ok){customerMetric('order');document.getElementById('payModal').classList.add('hidden');closeCheckout();showPaySuccess();}
    else showToast('系统结算异常','err');
  }catch(e){showToast('网络断开了','err');}
}

function cancelPay(){if(payPollTimer)clearInterval(payPollTimer);document.getElementById('payModal').classList.add('hidden');}
function manualConfirm(){if(payPollTimer)clearInterval(payPollTimer);confirmPayAndFinish();}

function showPaySuccess(){
  queueReceiptPrint();
  var methodNames={cash:'💵 现金支付',wx:'🟢 微信支付',alipay:'🔵 支付宝'};
  var now=new Date();
  var timeStr=now.getFullYear()+'-'+(now.getMonth()+1).toString().padStart(2,'0')+'-'+now.getDate().toString().padStart(2,'0')+' '+now.getHours().toString().padStart(2,'0')+':'+now.getMinutes().toString().padStart(2,'0')+':'+now.getSeconds().toString().padStart(2,'0');
  
  var h='<div style="text-align:center; padding:20px 0;">';
  h+='<div style="width:70px; height:70px; border-radius:50%; background:#fff; color:#0ea5e9; font-size:40px; display:flex; align-items:center; justify-content:center; margin:0 auto 16px; box-shadow:0 4px 15px rgba(0,0,0,0.15); animation:popIn 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275);">✓</div>';
  h+='<div style="font-size:24px; font-weight:800; margin-bottom:6px; color:#fff;">支付成功</div>';
  h+='<div style="font-size:13px; color:rgba(255,255,255,0.6); margin-bottom:15px;">交易单号 #'+(currentOrderId+1)+' | '+timeStr+'</div>';
  
  h+='<div style="background:rgba(0,0,0,0.1); border-radius:14px; padding:15px; margin-bottom:15px; text-align:left;">';
  h+='<table style="width:100%; font-size:14px; border-collapse:collapse; color:rgba(255,255,255,0.9);">';
  h+='<tr style="border-bottom:1px solid rgba(255,255,255,0.15);"><th style="text-align:left; padding-bottom:8px; font-weight:400; color:rgba(255,255,255,0.6);">商品明细</th><th style="text-align:right; padding-bottom:8px; font-weight:400; color:rgba(255,255,255,0.6);">数量</th><th style="text-align:right; padding-bottom:8px; font-weight:400; color:rgba(255,255,255,0.6);">实付</th></tr>';
  cart.forEach(function(i){
    var p=findProduct(i.code);var dp=getDiscountedPrice(p);
    h+='<tr><td style="padding:8px 0; font-weight:600; color:#fff;">'+(p?p.icon+' '+p.name:i.code)+'</td><td style="text-align:right; padding:8px 0;">x'+i.qty+'</td><td style="text-align:right; padding:8px 0; font-weight:700;">¥'+dp.toFixed(2)+'</td></tr>';
  });
  h+='</table></div>';
  
  h+='<div style="display:flex; justify-content:space-between; font-size:15px; margin-bottom:8px; color:rgba(255,255,255,0.8);"><span>支付通道</span><span style="color:#fff; font-weight:600;">'+ (methodNames[currentPayMethod]||'💵 现金支付') +'</span></div>';
  h+='<div style="display:flex; justify-content:space-between; align-items:center; font-size:20px; font-weight:700; margin-bottom:15px; border-top:1px dashed rgba(255,255,255,0.2); padding-top:12px;"><span>实付金额</span><span style="color:#fff; font-size:26px; text-shadow:0 2px 4px rgba(0,0,0,0.2);">¥'+currentOrderTotal.toFixed(2)+'</span></div>';
  
  if(currentMember) {
    h+='<div style="background:rgba(255,255,255,0.15); border:1px solid rgba(255,255,255,0.25); border-radius:10px; padding:10px; font-size:13px; color:#fff; margin-bottom:15px;">';
    h+='👤 会员 '+currentMember.name+' 本次积 <b>'+Math.floor(currentOrderTotal)+'</b> 分 <br><span style="color:rgba(255,255,255,0.6); font-size:12px;">可用总积分: <b style="color:#fff;">'+(currentMember.points+Math.floor(currentOrderTotal))+'</b></span></div>';
  }
  
  h+='<div class="btn-row"><button class="btn-pay primary" onclick="finishCheckout()" style="background:#fff; color:#1e40af;">完 成 并 返 回</button></div>';
  h+='</div>';
  
  document.getElementById('checkoutBody').innerHTML=h;
  document.getElementById('checkoutModal').classList.remove('hidden');
}

var lastQueuedReceipt=-1;
function canvasToPrinterJob(canvas,kind){
  var ctx=canvas.getContext('2d');
  var pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;
  var rowBytes=canvas.width>>3;
  var packed=new Uint8Array(rowBytes*canvas.height);
  for(var y=0;y<canvas.height;y++)for(var x=0;x<canvas.width;x++){
    var p=(y*canvas.width+x)*4;
    var light=pixels[p]*0.299+pixels[p+1]*0.587+pixels[p+2]*0.114;
    if(pixels[p+3]>64&&light<150)packed[y*rowBytes+(x>>3)]|=0x80>>(x&7);
  }
  return fetch('/api/printer/submit?width='+canvas.width+'&height='+canvas.height+'&kind='+encodeURIComponent(kind),{
    method:'POST',headers:{'Content-Type':'application/octet-stream'},body:packed
  }).then(function(r){return r.json()});
}

function drawPrintDocument(title,lines,maxHeight){
  // 384-dot head with a centered 320-dot (about 40 mm) receipt area.
  var width=384,left=32,right=352,lineHeight=30;
  var measure=document.createElement('canvas').getContext('2d');
  measure.font='23px "Microsoft YaHei","PingFang SC",sans-serif';
  var wrapped=[];
  lines.forEach(function(line){
    if(line===''){wrapped.push('');return;}
    var current='';
    Array.from(line).forEach(function(ch){
      if(measure.measureText(current+ch).width>right-left&&current){wrapped.push(current);current=ch;}
      else current+=ch;
    });
    wrapped.push(current);
  });
  var height=Math.min(maxHeight||480,480);
  var canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
  var ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);ctx.fillStyle='#000';
  ctx.textBaseline='top';ctx.textAlign='center';ctx.font='bold 32px "Microsoft YaHei","PingFang SC",sans-serif';ctx.fillText(title,width/2,10);
  ctx.fillRect(left,52,right-left,2);ctx.textAlign='left';ctx.font='23px "Microsoft YaHei","PingFang SC",sans-serif';
  var y=64;wrapped.forEach(function(line){if(y+lineHeight<=height-6){ctx.fillText(line,left,y);y+=lineHeight;}});
  return canvas;
}

function queueReceiptPrint(){
  if(currentOrderId<0||lastQueuedReceipt===currentOrderId)return;
  lastQueuedReceipt=currentOrderId;
  var lines=[];
  lines.push('订单号：#'+(currentOrderId+1));
  lines.push('时间：'+new Date().toLocaleString('zh-CN',{hour12:false}));
  lines.push('------------------------');
  cart.forEach(function(item){var p=findProduct(item.code);if(p)lines.push(p.name+'  x'+item.qty+'  ¥'+(getDiscountedPrice(p)*item.qty).toFixed(2));});
  lines.push('------------------------');
  lines.push('支付方式：'+({cash:'现金',wx:'微信',alipay:'支付宝'}[currentPayMethod]||currentPayMethod));
  lines.push('实付金额：¥'+currentOrderTotal.toFixed(2));
  if(currentMember)lines.push('会员：'+currentMember.name+'  本次积分：'+Math.floor(currentOrderTotal));
  lines.push('');lines.push('谢谢惠顾，欢迎再次光临');
  canvasToPrinterJob(drawPrintDocument('智慧超市购物小票',lines,480),'receipt').then(function(d){
    if(!d.ok){lastQueuedReceipt=-1;showToast('小票打印队列繁忙','err');}
  }).catch(function(){lastQueuedReceipt=-1;showToast('小票未能加入打印队列','err');});
}

async function finishCheckout(){
  if(currentMember){
    var pts=Math.floor(currentOrderTotal);
    try{
      var r=await fetch('/api/member/add-points',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+currentMember.id+'&pts='+pts+'&spent='+currentOrderTotal.toFixed(2)});
      var d=await r.json();
      if(d.ok)currentMember.points=d.points;
    }catch(e){}
  }
  closeCheckout();clearCart();
  if(currentMember){currentMember=null;document.getElementById('memberBar').style.display='none';}
  setTimeout(function(){loadProducts();},500);
}

function showToast(msg,type){
  var t=document.createElement('div');
  t.className='toast'+(type?' '+type:'');t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(function(){t.style.opacity='0';t.style.transform='translateX(-50%) translateY(-20px)';t.style.transition='all 0.3s ease';setTimeout(function(){t.remove();},300);},2000);
}

setInterval(async function(){try{var r=await fetch('/api/cam-status');var d=await r.json();window._skipScan=d.busy;}catch(e){window._skipScan=false;}},2000);
loadProducts().then(function(){startScan();});

// ===== 称重计价 =====
var weighProducts=[]; 
var weighPollTimer=null;
var selectedWeighItem=null;
var currentWeight=0;
var lastStableWeights=[];

function renderWeighProducts(){
  var h='';
  weighProducts.forEach(function(p,i){
    h+='<div class="weigh-item-card" onclick="selectWeighItem('+i+')" id="wp-'+i+'">';
    h+='<div style="font-size:36px; filter:drop-shadow(0 2px 4px rgba(0,0,0,0.15));">'+p.icon+'</div>';
    h+='<div style="font-size:14px; font-weight:700; color:#fff; margin-top:8px;">'+p.name+'</div>';
    h+='<div style="font-size:12px; color:#06b6d4; margin-top:4px; font-weight:600;">¥'+p.price.toFixed(2)+'<span style="color:rgba(255,255,255,0.6)">/500g</span></div>';
    h+='</div>';
  });
  document.getElementById('weighProducts').innerHTML=h;
}

function selectWeighItem(idx){
  selectedWeighItem=weighProducts[idx];
  document.querySelectorAll('[id^="wp-"]').forEach(function(el){el.classList.remove('active')});
  var el=document.getElementById('wp-'+idx);
  if(el)el.classList.add('active');
  document.getElementById('weighItemName').innerHTML=selectedWeighItem.icon+' '+selectedWeighItem.name;
  document.getElementById('weighPrice').textContent='¥'+selectedWeighItem.price.toFixed(2)+'/500g';
  updateWeighTotal();
}

function updateWeighTotal(){
  var total=0;
  if(selectedWeighItem&&currentWeight>2){
    total=(currentWeight/500)*selectedWeighItem.price;
    document.getElementById('weighTotal').textContent='¥'+total.toFixed(2);
    document.getElementById('weighBtn').disabled=false;
    document.getElementById('weighBtn').innerHTML='🛒 加入购物车清单 (¥'+total.toFixed(2)+')';
  }else{
    document.getElementById('weighTotal').textContent='¥0.00';
    document.getElementById('weighBtn').disabled=true;
    document.getElementById('weighBtn').textContent='👈 先选商品，再将物品放上托盘';
  }
}

function startWeighPoll(){
  stopWeighPoll();
  weighProducts=allProducts.filter(function(p){return p.isWeigh;});
  if(weighProducts.length===0)weighProducts=[{name:'散装糖果',price:18,icon:'🍬'},{name:'散装坚果',price:25,icon:'🥜'}];
  renderWeighProducts();
  weighPollTimer=setInterval(async function(){
    try{
      var r=await fetch('/api/weight');
      var d=await r.json();
      if(!d.ok)return;
      currentWeight=d.weight_g;
      document.getElementById('weighValue').textContent=currentWeight.toFixed(1);
      lastStableWeights.push(currentWeight);
      if(lastStableWeights.length>10)lastStableWeights.shift();
      var stable=true;
      if(lastStableWeights.length>=5){
        for(var i=1;i<lastStableWeights.length;i++){
          if(Math.abs(lastStableWeights[i]-lastStableWeights[i-1])>3){stable=false;break;}
        }
      }
      var st=document.getElementById('weighStatus');
      if(currentWeight<2){st.textContent='等待放置物品...';st.style.color='rgba(255,255,255,0.5)';}
      else if(!stable){st.innerHTML='<span class="scan-dot" style="background:#fff;box-shadow:0 0 8px #fff;"></span>正在精确计算中...';st.style.color='#fff';}
      else{st.textContent='✓ 重量已稳定锁定';st.style.color='#fff';}
      updateWeighTotal();
    }catch(e){}
  },300);
}

function stopWeighPoll(){
  if(weighPollTimer){clearInterval(weighPollTimer);weighPollTimer=null;}
}

function weighAddToCart(){
  if(!selectedWeighItem||currentWeight<2)return;
  var total=(currentWeight/500)*selectedWeighItem.price;
  var label=selectedWeighItem.name+' ('+currentWeight.toFixed(0)+'g)';
  var wcode='WEIGH_'+selectedWeighItem.name+'_'+total.toFixed(2)+'_'+Date.now();
  allProducts.push({qr_code:wcode,name:label,price:parseFloat(total.toFixed(2)),stock:99,today_sold:0,mfg_date:'-',shelf_life:0,icon:selectedWeighItem.icon});
  productMap[wcode]=allProducts[allProducts.length-1];
  addToCart(wcode);
  showToast('✅ 成功加入: '+label+' (¥'+total.toFixed(2)+')','ok');
  selectedWeighItem=null;currentWeight=0;lastStableWeights=[];
  document.querySelectorAll('[id^="wp-"]').forEach(function(el){el.classList.remove('active')});
  document.getElementById('weighItemName').textContent='未选择商品';
  document.getElementById('weighPrice').textContent='¥0.00';
  updateWeighTotal();
  renderWeighProducts();
}

var lastGunCode='';
setInterval(async function(){
  try{
    var r=await fetch('/api/customer/scan-event');
    var d=await r.json();
    if(d.code&&d.code.length>=8&&d.code!==lastGunCode){
      lastGunCode=d.code;
      var p=findProduct(d.code);
      if(!p)return;
      addToCart(d.code);
      showScanResult(d.code);
    }
  }catch(e){}
},800);

let idleTimer;
function resetIdleTimer(){
  clearTimeout(idleTimer);
  idleTimer=setTimeout(function(){window.location.href="/bigscreen";},30000);
}
document.addEventListener("click",resetIdleTimer);
document.addEventListener("touchstart",resetIdleTimer);
document.addEventListener("touchmove",resetIdleTimer);
document.addEventListener("mousemove",resetIdleTimer);
resetIdleTimer();

setInterval(async function(){
  try {
    var r = await fetch('/api/voice-subtitles');
    var d = await r.json();
    var box = document.getElementById('subtitleBox');
    if(d.show){
      document.getElementById('subQ').textContent = d.q;
      document.getElementById('subA').textContent = d.a;
      box.classList.add('show');
    } else {
      box.classList.remove('show');
    }
  } catch(e) {}
}, 500);

if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){});}

</script>
</body>
</html>
    )rawliteral";

    request->send(request->beginResponse_P(200, "text/html", html));
  });

  // AI提问接口
  server.on("/api/ask", HTTP_GET, [](AsyncWebServerRequest *request) {
    String question = request->hasParam("q") ? request->getParam("q")->value() : "";
    String fullPrompt = Product_Data_ToText()
                      + "\n用户问题：" + question
                      + "\n你是顾客导购，只能回答公开商品、价格、购买建议、促销和人工服务问题。"
                        "不得提供经营分析、销售额、补货、改价、退款、员工或审计信息，也不得执行任何商家操作。"
                        "所有数字必须严格使用上面给出的数据，不得自己编造。";
    String aiResult = AI_Ask(fullPrompt);
    request->send(200, "text/plain", aiResult);
  });

  // 全部商品列表
  server.on("/api/products", HTTP_GET, [](AsyncWebServerRequest *request) {
    String j = "[";
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (i > 0) j += ",";
      j += "{\"qr_code\":\"" + products[i].qrCode + "\",\"name\":\"" + products[i].name
        + "\",\"price\":" + String(products[i].price, 2) + ",\"stock\":" + String(products[i].stock)
        + ",\"today_sold\":" + String(products[i].todaySold) + ",\"mfg_date\":\"" + products[i].mfgDate
        + "\",\"shelf_life\":" + String(products[i].shelfLife) + ",\"icon\":\"" + products[i].icon + "\",\"isWeigh\":" + (products[i].isWeigh ? "true" : "false") + "}";
    }
    j += "]";
    AsyncWebServerResponse *resp = request->beginResponse(200, "application/json; charset=utf-8", j);
    request->send(resp);
  });

  // 创建订单
  server.on("/api/checkout", HTTP_GET, [](AsyncWebServerRequest *request) {
    String items = request->hasParam("items") ? request->getParam("items")->value() : "";
    float total = request->hasParam("total") ? request->getParam("total")->value().toFloat() : 0.0f;
    String method = request->hasParam("method") ? request->getParam("method")->value() : "cash";
    if (items.length() == 0 || total <= 0) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"参数错误\"}");
      return;
    }
    int orderId = Product_CreateOrder(items, total, method);
    String resp = "{\"ok\":true,\"orderId\":" + String(orderId) + ",\"total\":" + String(total, 2) + "}";
    AsyncWebServerResponse *r = request->beginResponse(200, "application/json; charset=utf-8", resp);
    request->send(r);
  });

  // 支付确认 — 扣库存 + 累销量
  server.on("/api/pay-confirm", HTTP_GET, [](AsyncWebServerRequest *request) {
    int orderId = request->hasParam("orderId") ? request->getParam("orderId")->value().toInt() : -1;
    String itemsParam = request->hasParam("items") ? request->getParam("items")->value() : "";
    if (orderId < 0 || itemsParam.length() == 0) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"参数错误\"}");
      return;
    }
    if (!Product_ConfirmOrder(orderId)) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"订单不存在或已支付\"}");
      return;
    }
    Serial.printf("[PayConfirm] raw items: %s\n", itemsParam.c_str());
    // 解析 items: "6901234567890:2,6901234567891:1"
    int pos = 0;
    while (pos < (int)itemsParam.length()) {
      int sep = itemsParam.indexOf(':', pos);
      int end = itemsParam.indexOf(',', sep);
      if (end < 0) end = itemsParam.length();
      if (sep > pos) {
        String code = itemsParam.substring(pos, sep);
        int qty = itemsParam.substring(sep + 1, end).toInt();
        Serial.printf("[PayConfirm] parsed: code='%s' qty=%d\n", code.c_str(), qty);
        if (qty > 0) {
          Product_DeductStock(code, qty);
          Product_AddSold(code, qty);
          Product_Save();
          {
            float actualPrice = 0;
            const Product* p = Product_FindByQR(code);
            if (!p && code.startsWith("WEIGH_")) {
              // 称重商品: WEIGH_香蕉_12.50_1783334696719
              int u1 = code.indexOf('_', 6);
              int u2 = code.indexOf('_', u1 + 1);
              String pname = code.substring(6, u1);
              actualPrice = code.substring(u1 + 1, u2 > 0 ? u2 : code.length()).toFloat();
              for (int i = 0; i < PRODUCT_COUNT; i++)
                if (products[i].name == pname) { p = &products[i]; break; }
            }
            float realAmount = actualPrice > 0 ? actualPrice : (p ? p->price * qty : 0);
            if (p || actualPrice > 0) { Serial.printf("[Stats] +¥%.2f x%d\n", realAmount, qty); DailyStats_AddSale(realAmount, qty); }
            else Serial.printf("[Stats] FindByQR failed for: %s\n", code.c_str());
          }
        }
      }
      pos = end + 1;
      if (pos <= 0) break;
    }
    request->send(200, "application/json", "{\"ok\":true,\"msg\":\"支付成功\"}");
  });

  // 订单历史
  server.on("/api/order-history", HTTP_GET, [](AsyncWebServerRequest *request) {
    String j = Product_OrderHistoryToJson();
    AsyncWebServerResponse *r = request->beginResponse(200, "application/json; charset=utf-8", j);
    request->send(r);
  });

  // 供树莓派拉取实时商品数据的接口
  server.on("/api/esp32-products", HTTP_GET, [](AsyncWebServerRequest *request) {
    String j = "[";
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (i > 0) j += ",";
      j += "{\"qr_code\":\"" + products[i].qrCode + "\",\"name\":\"" + products[i].name
        + "\",\"price\":" + String(products[i].price, 2) + ",\"stock\":" + String(products[i].stock)
        + ",\"today_sold\":" + String(products[i].todaySold) + ",\"mfg_date\":\"" + products[i].mfgDate
        + "\",\"shelf_life\":" + String(products[i].shelfLife) + ",\"icon\":\"" + products[i].icon + "\",\"isWeigh\":" + (products[i].isWeigh ? "true" : "false") + "}";
    }
    j += "]";
    AsyncWebServerResponse *r = request->beginResponse(200, "application/json; charset=utf-8", j);
    request->send(r);
  });

  // ─── 用户管理：初始化 ─────────────────────────────────
  if (!usersLoaded) {
    usersLoaded = true;
    loadOpLogs();
    loadStoreCfg();
    if (!membersLoaded) { membersLoaded = true; loadMembers(); }
    users["admin"] = {hashPassword("admin123"), "manager"};
    if (SPIFFS.exists("/users.json")) {
      File f = SPIFFS.open("/users.json", "r");
      if (f) {
        DynamicJsonDocument doc(4096);
        DeserializationError err = deserializeJson(doc, f);
        f.close();
        if (!err) {
          for (JsonPair kv : doc.as<JsonObject>()) {
            String key = kv.key().c_str();
            // 兼容旧格式 "user":"pwd" 和新格式 "user":{"p":"pwd","r":"role"}
            if (kv.value().is<JsonObject>()) {
              users[key].password = kv.value()["p"].as<String>();
              users[key].role = kv.value()["r"].as<String>();
              users[key].rfid_uid = kv.value()["uid"].as<String>();
            } else {
              users[key].password = kv.value().as<String>();
              // 旧格式没有角色字段，保留已有角色；admin始终为店长
              if (users.count(key)) users[key].password = kv.value().as<String>(); // 只覆盖密码，保留角色
              else users[key] = {kv.value().as<String>(), "staff"};
            }
          }
        }
      }
    }
    users["admin"].role = "manager"; // admin始终为店长，不可降级
  }

  // 商家登录接口（支持登录和注册）
  server.on("/admin/login", HTTP_POST, [](AsyncWebServerRequest *request) {
    String user = request->hasParam("user", true) ? request->getParam("user", true)->value() : "";
    String pwd  = request->hasParam("pwd", true)  ? request->getParam("pwd", true)->value()  : "";
    String mode = request->hasParam("mode", true) ? request->getParam("mode", true)->value() : "login";
    if (mode == "reg") {
      // 注册
      if (user.length() < 2) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"用户名至少2位\"}"); return; }
      if (pwd.length() < 3)  { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"密码至少3位\"}"); return; }
      if (users.count(user)) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"用户名已存在\"}"); return; }
      String role = request->hasParam("role", true) ? request->getParam("role", true)->value() : "staff";
      users[user] = {hashPassword(pwd), role};
      // 保存到SPIFFS（新格式）
      DynamicJsonDocument doc(4096);
      for (auto& kv : users) {
        JsonObject obj = doc.createNestedObject(kv.first);
        obj["p"] = kv.second.password;
        obj["r"] = kv.second.role;
        obj["uid"] = kv.second.rfid_uid;
      }
      File f = SPIFFS.open("/users.json", "w");
      if (f) { serializeJson(doc, f); f.close(); }
      addOpLog(user, "register", "注册新账户，角色:" + role);
      AsyncWebServerResponse *resp = request->beginResponse(200, "application/json", "{\"ok\":true,\"role\":\"" + role + "\"}");
      resp->addHeader("Set-Cookie", "admin_auth=1; Path=/");
      resp->addHeader("Set-Cookie", ("admin_user=" + user + "; Path=/").c_str());
      resp->addHeader("Set-Cookie", ("admin_role=" + role + "; Path=/").c_str());
      request->send(resp);
      return;
    }
    // 登录
    if (users.count(user) && checkPassword(pwd, users[user].password)) {
      // 旧明文密码首次登录时自动升级为SHA-256哈希
      if (!users[user].password.startsWith("$sha256$")) {
        users[user].password = hashPassword(pwd);
        DynamicJsonDocument doc(4096);
        for (auto& kv : users) {
          JsonObject obj = doc.createNestedObject(kv.first);
          obj["p"] = kv.second.password; obj["r"] = kv.second.role; obj["uid"] = kv.second.rfid_uid;
        }
        File f = SPIFFS.open("/users.json", "w");
        if (f) { serializeJson(doc, f); f.close(); }
      }
      String role = users[user].role;
      addOpLog(user, "login", "登录后台");
      AsyncWebServerResponse *resp = request->beginResponse(200, "application/json", "{\"ok\":true,\"role\":\"" + role + "\"}");
      resp->addHeader("Set-Cookie", "admin_auth=1; Path=/");
      resp->addHeader("Set-Cookie", ("admin_user=" + user + "; Path=/").c_str());
      resp->addHeader("Set-Cookie", ("admin_role=" + role + "; Path=/").c_str());
      request->send(resp);
      return;
    }
    request->send(200, "application/json", "{\"ok\":false,\"msg\":\"用户名或密码错误\"}");
  });

  // ── 扫码登录 ──
  // 生成二维码sid
  server.on("/admin/qr-init", HTTP_GET, [](AsyncWebServerRequest *request) {
    cleanupExpiredQRSessions();
    String sid;
    for (int i = 0; i < 16; i++) sid += "0123456789abcdef"[random(16)];
    qrSessions[sid] = {"", false, millis()};
    String qrUrl = "https://admin.mimistore.icu/admin/qr-confirm?sid=" + sid;
    String json = "{\"ok\":true,\"sid\":\"" + sid + "\",\"url\":\"" + qrUrl + "\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  // 电脑端轮询扫码状态
  server.on("/admin/qr-status", HTTP_GET, [](AsyncWebServerRequest *request) {
    String sid = request->hasParam("sid") ? request->getParam("sid")->value() : "";
    if (!qrSessions.count(sid)) {
      request->send(200, "application/json", "{\"ok\":false,\"msg\":\"二维码已过期\"}");
      return;
    }
    QRSession& s = qrSessions[sid];
    if (s.confirmed) {
      String role = users.count(s.user) ? users[s.user].role : "staff";
      String resp = "{\"ok\":true,\"confirmed\":true,\"user\":\"" + s.user + "\",\"role\":\"" + role + "\"}";
      qrSessions.erase(sid);
      request->send(200, "application/json; charset=utf-8", resp);
    } else {
      request->send(200, "application/json", "{\"ok\":true,\"confirmed\":false}");
    }
  });

  // 手机扫码确认页
  server.on("/admin/qr-confirm", HTTP_GET, [](AsyncWebServerRequest *request) {
    String sid = request->hasParam("sid") ? request->getParam("sid")->value() : "";
    if (!qrSessions.count(sid)) {
      request->send(200, "text/html; charset=utf-8", "<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>二维码已过期</title><style>body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f0f2f5;margin:0}.card{background:white;border-radius:12px;padding:30px;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,0.1)}</style></head><body><div class='card'><div style='font-size:48px'>⏰</div><h3>二维码已过期</h3><p style='color:#999'>请刷新电脑端页面重新生成</p></div></body></html>");
      return;
    }
    String html = R"rawliteral(
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码登录确认</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;background:#f0f2f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:white;border-radius:16px;padding:32px 28px;width:90%;max-width:340px;box-shadow:0 4px 24px rgba(0,0,0,0.1);text-align:center}
.icon{font-size:64px;margin-bottom:16px}
h3{font-size:20px;color:#222;margin-bottom:6px}
.sub{font-size:13px;color:#999;margin-bottom:24px}
.btn-confirm{width:100%;padding:14px;background:linear-gradient(135deg,#2ecc71,#27ae60);color:white;border:none;border-radius:12px;font-size:17px;font-weight:600;cursor:pointer;font-family:inherit}
.btn-cancel{margin-top:12px;width:100%;padding:12px;background:#f5f5f5;color:#999;border:none;border-radius:10px;font-size:14px;cursor:pointer;font-family:inherit}
</style>
</head>
<body>
<div class="card" id="card">
<div class="icon">📱</div>
<h3>确认登录商家后台</h3>
<p class="sub">点击下方按钮确认登录</p>
<button class="btn-confirm" onclick="doConfirm()">✅ 确认登录</button>
<button class="btn-cancel" onclick="window.close()">取消</button>
</div>
<script>
async function doConfirm(){
  try{
    var r=await fetch('/admin/qr-do-confirm',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'sid=SID_PLACEHOLDER'});
    var d=await r.json();
    if(d.ok){
      document.getElementById('card').innerHTML='<div class="icon">✅</div><h3>登录成功</h3><p class="sub">电脑端将自动跳转</p>';
    }else{
      alert(d.msg||'确认失败，请重试');
    }
  }catch(e){alert('网络错误');}
}
</script>
</body>
</html>
    )rawliteral";
    html.replace("SID_PLACEHOLDER", sid);
    request->send(200, "text/html; charset=utf-8", html);
  });

  // 手机端确认提交（扫码即确认，无需输密码）
  server.on("/admin/qr-do-confirm", HTTP_POST, [](AsyncWebServerRequest *request) {
    String sid = request->hasParam("sid", true) ? request->getParam("sid", true)->value() : "";
    if (!qrSessions.count(sid)) {
      request->send(200, "application/json", "{\"ok\":false,\"msg\":\"二维码已过期\"}"); return;
    }
    qrSessions[sid].user = "admin";
    qrSessions[sid].confirmed = true;
    addOpLog("admin", "qr_login", "扫码登录");
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // ── 员工管理 ──
  server.on("/api/admin/list-users", HTTP_GET, [](AsyncWebServerRequest *request) {
    String j = "[";
    bool first = true;
    for (auto& kv : users) {
      if (!first) j += ","; first = false;
      j += "{\"user\":\"" + kv.first + "\",\"role\":\"" + kv.second.role + "\",\"rfid_uid\":\"" + kv.second.rfid_uid + "\"}";
    }
    j += "]";
    request->send(200, "application/json; charset=utf-8", j);
  });

  server.on("/api/admin/create-user", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (!requireManager(request)) return;
    String newUser = request->hasParam("newUser", true) ? request->getParam("newUser", true)->value() : "";
    String newPwd  = request->hasParam("newPwd", true)  ? request->getParam("newPwd", true)->value()  : "";
    String newRole = request->hasParam("newRole", true) ? request->getParam("newRole", true)->value() : "staff";
    if (newRole != "manager") newRole = "staff";
    if (newUser.length() < 2) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"用户名至少2位\"}"); return; }
    if (newPwd.length() < 3)  { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"密码至少3位\"}"); return; }
    if (users.count(newUser)) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"用户名已存在\"}"); return; }
    users[newUser] = {hashPassword(newPwd), newRole, ""};
    DynamicJsonDocument doc(4096);
    for (auto& kv : users) {
      JsonObject obj = doc.createNestedObject(kv.first);
      obj["p"] = kv.second.password; obj["r"] = kv.second.role; obj["uid"] = kv.second.rfid_uid;
    }
    File f = SPIFFS.open("/users.json", "w");
    if (f) { serializeJson(doc, f); f.close(); }
    addOpLog(getCookieVal(request, "admin_user"), "create-user", "创建账户:" + newUser + " 角色:" + newRole);
    request->send(200, "application/json", "{\"ok\":true}");
  });

  server.on("/api/admin/delete-user", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (!requireManager(request)) return;
    String target = request->hasParam("user", true) ? request->getParam("user", true)->value() : "";
    String self = getCookieVal(request, "admin_user");
    if (target.length() == 0 || !users.count(target)) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"用户不存在\"}"); return; }
    if (target == self) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"不能删除自己\"}"); return; }
    if (users[target].role == "manager") {
      int mc = 0; for (auto& kv : users) if (kv.second.role == "manager") mc++;
      if (mc <= 1) { request->send(200, "application/json", "{\"ok\":false,\"msg\":\"至少保留一位店长\"}"); return; }
    }
    users.erase(target);
    DynamicJsonDocument doc(4096);
    for (auto& kv : users) {
      JsonObject obj = doc.createNestedObject(kv.first);
      obj["p"] = kv.second.password; obj["r"] = kv.second.role; obj["uid"] = kv.second.rfid_uid;
    }
    File f = SPIFFS.open("/users.json", "w");
    if (f) { serializeJson(doc, f); f.close(); }
    addOpLog(self, "delete-user", "删除账户:" + target);
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // ── RFID 刷卡 ──
  server.on("/api/rfid-poll", HTTP_GET, [](AsyncWebServerRequest *request) {
    bool hasCard = RFID_IsNewCard();
    String uid = hasCard ? RFID_GetLastUID() : "";
    String user = "";
    String role = "";
    if (hasCard && uid.length() > 0) {
      for (auto& kv : users)
        if (kv.second.rfid_uid == uid) { user = kv.first; role = kv.second.role; break; }
    }
    String json = "{\"card\":" + String(hasCard ? "true" : "false")
                + ",\"uid\":\"" + uid + "\",\"user\":\"" + user + "\",\"role\":\"" + role + "\"}";
    RFID_GetLastUID(); // 清除标记，防止重复触发
    request->send(200, "application/json; charset=utf-8", json);
  });

  // 绑定RFID卡到当前登录用户（店长可绑任意，店员只能绑自己）
  server.on("/api/admin/bind-rfid", HTTP_POST, [](AsyncWebServerRequest *request) {
    String uid = request->hasParam("uid", true) ? request->getParam("uid", true)->value() : "";
    String targetUser = request->hasParam("user", true) ? request->getParam("user", true)->value() : "";
    String loginUser = getCookieVal(request, "admin_user");
    if (uid.length() == 0 || targetUser.length() == 0) { request->send(400, "application/json", "{\"ok\":false,\"msg\":\"参数错误\"}"); return; }
    if (!users.count(targetUser)) { request->send(404, "application/json", "{\"ok\":false,\"msg\":\"用户不存在\"}"); return; }
    // 权限：店长可绑任何人，店员只能绑自己
    if (!isRequestManager(request) && targetUser != loginUser) {
      request->send(403, "application/json", "{\"ok\":false,\"msg\":\"只能绑定自己的卡\"}"); return;
    }
    // 检查这张卡是否已被其他用户绑定
    for (auto& kv : users)
      if (kv.second.rfid_uid == uid && kv.first != targetUser) {
        request->send(200, "application/json", "{\"ok\":false,\"msg\":\"此卡已被 "+kv.first+" 绑定\"}"); return;
      }
    users[targetUser].rfid_uid = uid;
    DynamicJsonDocument doc(4096);
    for (auto& kv : users) {
      JsonObject obj = doc.createNestedObject(kv.first);
      obj["p"] = kv.second.password; obj["r"] = kv.second.role; obj["uid"] = kv.second.rfid_uid;
    }
    File f = SPIFFS.open("/users.json", "w");
    if (f) { serializeJson(doc, f); f.close(); }
    addOpLog(loginUser, "bind-rfid", "绑定卡:" + uid + " -> " + targetUser);
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 解绑RFID卡
  server.on("/api/admin/unbind-rfid", HTTP_POST, [](AsyncWebServerRequest *request) {
    String targetUser = request->hasParam("user", true) ? request->getParam("user", true)->value() : "";
    String loginUser = getCookieVal(request, "admin_user");
    if (!users.count(targetUser)) { request->send(404, "application/json", "{\"ok\":false,\"msg\":\"用户不存在\"}"); return; }
    if (!isRequestManager(request) && targetUser != loginUser) {
      request->send(403, "application/json", "{\"ok\":false,\"msg\":\"只能解绑自己的卡\"}"); return;
    }
    addOpLog(loginUser, "unbind-rfid", "解绑卡:" + users[targetUser].rfid_uid + " <- " + targetUser);
    users[targetUser].rfid_uid = "";
    DynamicJsonDocument doc(4096);
    for (auto& kv : users) {
      JsonObject obj = doc.createNestedObject(kv.first);
      obj["p"] = kv.second.password; obj["r"] = kv.second.role; obj["uid"] = kv.second.rfid_uid;
    }
    File f = SPIFFS.open("/users.json", "w");
    if (f) { serializeJson(doc, f); f.close(); }
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 退出登录
  server.on("/logout", HTTP_GET, [](AsyncWebServerRequest *request) {
    AsyncWebServerResponse *resp = request->beginResponse(302);
    resp->addHeader("Location", "/admin");
    resp->addHeader("Set-Cookie", "admin_auth=; Path=/; HttpOnly; Max-Age=0");
    request->send(resp);
  });

  // ── 会员 API ──
  server.on("/api/member/register", HTTP_POST, [](AsyncWebServerRequest *request) {
    String name = request->hasParam("name", true) ? request->getParam("name", true)->value() : "";
    String phone = request->hasParam("phone", true) ? request->getParam("phone", true)->value() : "";
    if (name.length() < 1) { request->send(400, "application/json", "{\"ok\":false,\"msg\":\"请输入姓名\"}"); return; }
    // 手机号唯一性检查
    if (phone.length() > 0) {
      for (auto& kv : members)
        if (kv.second.phone == phone) {
          request->send(400, "application/json", "{\"ok\":false,\"msg\":\"该手机号已注册\"}");
          return;
        }
    }
    String id;
    for (int i = 0; i < 8; i++) id += "0123456789abcdef"[random(16)];
    members[id] = {id, name, phone, 0, 0, ""};
    saveMembers();
    String json = "{\"ok\":true,\"id\":\"" + id + "\",\"qr\":\"MEM:" + id + "\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  // Mobile app member session: only link an existing store membership by phone.
  server.on("/api/customer/member-session", HTTP_POST, [](AsyncWebServerRequest *request) {
    String name = request->hasParam("name", true) ? request->getParam("name", true)->value() : "";
    String phone = request->hasParam("phone", true) ? request->getParam("phone", true)->value() : "";
    name.trim();
    phone.trim();
    if (name.length() < 1 || name.length() > 20 || phone.length() != 11) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"会员资料无效\"}");
      return;
    }

    Member *member = nullptr;
    for (auto& kv : members) {
      if (kv.second.phone == phone) { member = &kv.second; break; }
    }
    if (!member) {
      request->send(200, "application/json; charset=utf-8", "{\"ok\":true,\"member\":false}");
      return;
    }

    StaticJsonDocument<384> doc;
    doc["ok"] = true;
    doc["member"] = true;
    doc["id"] = member->id;
    doc["name"] = member->name;
    doc["phone"] = member->phone;
    doc["points"] = member->points;
    doc["totalSpent"] = member->totalSpent;
    String json;
    serializeJson(doc, json);
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/api/member/lookup", HTTP_GET, [](AsyncWebServerRequest *request) {
    String code = request->hasParam("code") ? request->getParam("code")->value() : "";
    if (!code.startsWith("MEM:")) code = "MEM:" + code;
    String id = code.substring(4);
    if (!members.count(id)) {
      request->send(200, "application/json", "{\"ok\":false,\"msg\":\"未找到会员\"}");
      return;
    }
    Member& m = members[id];
    String json = "{\"ok\":true,\"id\":\"" + id + "\",\"name\":\"" + m.name + "\",\"points\":" + String(m.points) + ",\"totalSpent\":" + String(m.totalSpent, 2) + "}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/api/member/add-points", HTTP_POST, [](AsyncWebServerRequest *request) {
    String id = request->hasParam("id", true) ? request->getParam("id", true)->value() : "";
    int pts = request->hasParam("pts", true) ? request->getParam("pts", true)->value().toInt() : 0;
    float spent = request->hasParam("spent", true) ? request->getParam("spent", true)->value().toFloat() : 0;
    if (!members.count(id)) { request->send(404, "application/json", "{\"ok\":false}"); return; }
    members[id].points += pts;
    members[id].totalSpent += spent;
    saveMembers();
    request->send(200, "application/json", "{\"ok\":true,\"points\":" + String(members[id].points) + "}");
  });

  server.on("/api/member/list", HTTP_GET, [](AsyncWebServerRequest *request) {
    String j = "[";
    bool first = true;
    for (auto& kv : members) {
      if (!first) j += ","; first = false;
      j += "{\"id\":\"" + kv.first + "\",\"name\":\"" + kv.second.name + "\",\"phone\":\"" + kv.second.phone + "\",\"points\":" + String(kv.second.points) + ",\"totalSpent\":" + String(kv.second.totalSpent, 2) + ",\"rfid_uid\":\"" + kv.second.rfid_uid + "\"}";
    }
    j += "]";
    request->send(200, "application/json; charset=utf-8", j);
  });

  // 会员RFID绑定
  server.on("/api/member/bind-rfid", HTTP_POST, [](AsyncWebServerRequest *request) {
    String uid = request->hasParam("uid", true) ? request->getParam("uid", true)->value() : "";
    String mid = request->hasParam("id", true) ? request->getParam("id", true)->value() : "";
    if (uid.length() == 0 || !members.count(mid)) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"参数错误\"}"); return;
    }
    members[mid].rfid_uid = uid;
    saveMembers();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  server.on("/api/member/unbind-rfid", HTTP_POST, [](AsyncWebServerRequest *request) {
    String mid = request->hasParam("id", true) ? request->getParam("id", true)->value() : "";
    if (!members.count(mid)) { request->send(400, "application/json", "{\"ok\":false}"); return; }
    members[mid].rfid_uid = "";
    saveMembers();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  server.on("/api/member/delete", HTTP_POST, [](AsyncWebServerRequest *request) {
    String mid = request->hasParam("id", true) ? request->getParam("id", true)->value() : "";
    if (!members.count(mid)) { request->send(400, "application/json", "{\"ok\":false}"); return; }
    members.erase(mid);
    saveMembers();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 顾客端RFID会员识别（轮询）
  server.on("/api/member/rfid-poll", HTTP_GET, [](AsyncWebServerRequest *request) {
    String uid = RFID_IsNewCard() ? RFID_GetLastUID() : "";
    String mid = "", name = "";
    if (uid.length() > 0) {
      for (auto& kv : members)
        if (kv.second.rfid_uid == uid) { mid = kv.first; name = kv.second.name; break; }
    }
    String json = "{\"card\":" + String(uid.length()>0?"true":"false")
                + ",\"uid\":\"" + uid + "\",\"memberId\":\"" + mid + "\",\"name\":\"" + name + "\"}";
    RFID_GetLastUID();
    request->send(200, "application/json; charset=utf-8", json);
  });

  // ==================== 商家管理后台 ====================
  server.on("/admin", HTTP_GET, [](AsyncWebServerRequest *request) {
    // 检查登录 Cookie
    bool authed = false;
    if (request->hasHeader("Cookie")) {
      String cookie = request->getHeader("Cookie")->value();
      if (cookie.indexOf("admin_auth=1") >= 0) authed = true;
    }
    if (!authed) {
      String loginHTML = R"rawliteral(
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>商家管理中心 - 登录</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif; }
body {
    display: flex; justify-content: center; align-items: center; min-height: 100vh;
    background: linear-gradient(-45deg, #0f172a, #1e1b4b, #2e1065, #0f172a);
    background-size: 400% 400%; animation: gradientBg 15s ease infinite; overflow: hidden;
}
@keyframes gradientBg { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }

/* 装饰背景光晕 */
.circle { position: absolute; border-radius: 50%; filter: blur(80px); z-index: 1; opacity: 0.5; }
.circle-1 { width: 300px; height: 300px; top: 15%; left: 15%; background: linear-gradient(135deg, #3b82f6, #8b5cf6); }
.circle-2 { width: 400px; height: 400px; bottom: 10%; right: 15%; background: linear-gradient(135deg, #ec4899, #8b5cf6); }

/* 玻璃拟态主体卡片 */
.card {
    position: relative; width: 90%; max-width: 400px; padding: 40px;
    background: rgba(255, 255, 255, 0.05); backdrop-filter: blur(15px); -webkit-backdrop-filter: blur(15px);
    border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 20px;
    box-shadow: 0 25px 45px rgba(0, 0, 0, 0.2); z-index: 10; text-align: center;
}

.icon { font-size: 42px; margin-bottom: 10px; filter: drop-shadow(0 0 10px rgba(59,130,246,0.8)); }
.title { font-size: 24px; font-weight: 700; color: #fff; margin-bottom: 5px; letter-spacing: 1px; }
.sub { font-size: 13px; color: rgba(255,255,255,0.5); margin-bottom: 24px; }

/* 顶部选项卡 (分段控制器) */
.tabs { display: flex; background: rgba(255, 255, 255, 0.05); border-radius: 12px; padding: 5px; margin-bottom: 20px; border: 1px solid rgba(255, 255, 255, 0.1); }
.tab-btn { flex: 1; padding: 10px; cursor: pointer; color: rgba(255,255,255,0.5); font-weight: 600; font-size: 14px; border-radius: 8px; transition: 0.3s; }
.tab-btn.active { background: #3b82f6; color: #fff; box-shadow: 0 0 15px rgba(59, 130, 246, 0.4); }

/* 输入框特效 */
.input-group { position: relative; margin-bottom: 20px; text-align: left; }
.input-group input {
    width: 100%; padding: 14px 16px; background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 10px;
    outline: none; color: #fff; font-size: 15px; transition: 0.3s;
}
.input-group input:focus, .input-group input:valid { border-color: #3b82f6; background: rgba(255, 255, 255, 0.15); box-shadow: 0 0 15px rgba(59, 130, 246, 0.3); }
.input-group label { position: absolute; left: 16px; top: 50%; transform: translateY(-50%); color: rgba(255, 255, 255, 0.5); pointer-events: none; transition: 0.3s; font-size: 15px; }
/* 重点修补：如果输入框内有值，标签自动上浮 */
.input-group input:focus ~ label, .input-group input:valid ~ label, .input-group input.has-val ~ label {
    top: -10px; left: 10px; font-size: 12px; color: #3b82f6; padding: 0 5px; background: #11132d; border-radius: 4px;
}

/* 按钮特效 */
button {
    width: 100%; padding: 14px; background: linear-gradient(90deg, #3b82f6, #8b5cf6, #ec4899, #3b82f6); background-size: 300% 100%;
    border: none; border-radius: 10px; color: #fff; font-size: 16px; font-weight: 600; cursor: pointer; transition: 0.5s; letter-spacing: 1px;
    box-shadow: 0 4px 15px rgba(139, 92, 246, 0.4); margin-top: 5px;
}
button:hover { background-position: 100% 0; box-shadow: 0 4px 25px rgba(236, 72, 153, 0.6); transform: translateY(-2px); }
button:active { transform: translateY(0); }
.btn-secondary { background: rgba(255, 255, 255, 0.1); box-shadow: none; color: #bbb; border: 1px solid rgba(255, 255, 255, 0.2); }
.btn-secondary:hover { background: rgba(255, 255, 255, 0.15); box-shadow: none; color: #fff; }

.err { color: #ef4444; font-size: 13px; margin-bottom: 15px; min-height: 18px; text-shadow: 0 0 5px rgba(239,68,68,0.5); }
#regTip { font-size: 12px; color: rgba(255,255,255,0.7); margin-bottom: 15px; padding: 10px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; }

/* QR面板专属 */
.qr-box { background: rgba(255,255,255,0.9); padding: 10px; border-radius: 15px; width: 220px; height: 220px; margin: 0 auto 15px auto; box-shadow: 0 0 20px rgba(59, 130, 246, 0.5); }
</style>
</head>
<body>
<div class="circle circle-1"></div>
<div class="circle circle-2"></div>

<div class="card" id="loginCard">
    <div class="icon">🔐</div>
    <div class="title">商家管理中心</div>
    <div class="sub">登录或注册新账户</div>

    <div class="tabs">
        <div class="tab-btn active" id="tabLogin" onclick="switchMode('login')">密码登录</div>
        <div class="tab-btn" id="tabReg" onclick="switchMode('reg')">账户注册</div>
        <div class="tab-btn" id="tabQr" onclick="switchMode('qr')">扫码进入</div>
    </div>
    
    <div class="err" id="err"></div>

    <div id="panelPwd">
        <form onsubmit="doSubmit();return false">
            <div class="input-group">
                <input type="text" id="user" required autocomplete="off" oninput="this.className=this.value?'has-val':''">
                <label>用户名</label>
            </div>
            <div class="input-group">
                <input type="password" id="pwd" required onkeydown="if(event.key==='Enter')doSubmit()" oninput="this.className=this.value?'has-val':''">
                <label>密码</label>
            </div>
            <div class="input-group" id="groupPwd2" style="display:none">
                <input type="password" id="pwd2" onkeydown="if(event.key==='Enter')doSubmit()" oninput="this.className=this.value?'has-val':''">
                <label>确认密码</label>
            </div>
            
            <div id="regTip" style="display:none">💡 新注册账号默认为<b>店员</b>，店长权限请联系管理员开通</div>
            
            <button type="button" id="btn" onclick="doSubmit()">系统身份验证</button>
        </form>
    </div>

    <div id="panelQR" style="display:none">
        <div class="qr-box"><iframe id="qrFrame" src="" style="width:100%;height:100%;border:none;border-radius:10px"></iframe></div>
        <p style="font-size:13px;color:rgba(255,255,255,0.6);margin-bottom:15px">📱 请用手机扫描二维码确认登录</p>
        <button type="button" class="btn-secondary" onclick="switchMode('login')">返回账号登录</button>
    </div>
</div>

<script>
// ===============================================================
// 💡 原汁原味的 JS 业务逻辑 (零删减，完全保留了你的高级特性)
// ===============================================================
var curMode='login';
var qrTimer=null;
var rfidTimer=null;

function switchMode(m){
    curMode=m;
    
    // 更新 Tab 样式 (使用 classList 适配高级 UI)
    ['login','reg','qr'].forEach(function(t){
        var el=document.getElementById('tab'+t.charAt(0).toUpperCase()+t.slice(1));
        if(t===m) { el.classList.add('active'); }
        else { el.classList.remove('active'); }
    });
    
    // 面板切换 (细微修改了 pwd2 的显隐逻辑，以适应外层的 label 组)
    document.getElementById('panelPwd').style.display=(m==='qr')?'none':'block';
    document.getElementById('panelQR').style.display=(m==='qr')?'block':'none';
    document.getElementById('groupPwd2').style.display=(m==='reg')?'block':'none';
    document.getElementById('regTip').style.display=(m==='reg')?'block':'none';
    document.getElementById('btn').textContent=(m==='reg')?'注 册 账 户':'登 录 系 统';
    document.getElementById('err').textContent='';
    
    if(m==='qr'){ startQRLoop(); } else { stopQRLoop(); }
}

function stopQRLoop(){ if(qrTimer){clearInterval(qrTimer);qrTimer=null;} }

// RFID改为全局后台轮询——不管在哪个栏，刷卡直接进
async function startRFIDLoop(){
    stopRFIDLoop();
    rfidTimer=setInterval(async function(){
        try{
            var r=await fetch('/api/rfid-poll');
            var d=await r.json();
            if(d.card && d.user){
                stopRFIDLoop();
                document.cookie='admin_auth=1; path=/';
                document.cookie='admin_user='+encodeURIComponent(d.user)+'; path=/';
                document.cookie='admin_role='+(d.role||'staff')+'; path=/';
                setTimeout(function(){location.href='/admin?login=1';},500);
            }
        }catch(e){}
    },300);
}
function stopRFIDLoop(){ if(rfidTimer){clearInterval(rfidTimer);rfidTimer=null;} }

// 全局后台RFID监听——任何栏位刷卡都直接登录
startRFIDLoop();

async function startQRLoop(){
    var el=document.getElementById('qrFrame');
    try{
        var r=await fetch('/admin/qr-init');
        var d=await r.json();
        if(!d.ok){document.getElementById('err').textContent='二维码生成失败';return;}
        el.src='/qr-svg?text='+encodeURIComponent(d.url);
        stopQRLoop();
        qrTimer=setInterval(async function(){
            try{
                var sr=await fetch('/admin/qr-status?sid='+d.sid);
                var sd=await sr.json();
                if(sd.confirmed){
                    stopQRLoop();
                    document.cookie='admin_auth=1; path=/';
                    document.cookie='admin_user='+encodeURIComponent(sd.user)+'; path=/';
                    document.cookie='admin_role='+(sd.role||'staff')+'; path=/';
                    location.href='/admin?login=1';
                }
            }catch(e){}
        },1500);
    }catch(e){document.getElementById('err').textContent='网络错误';}
}

async function doSubmit(){
    var user=document.getElementById('user').value.trim();
    var pwd=document.getElementById('pwd').value;
    if(!user||!pwd){document.getElementById('err').textContent='请填写用户名和密码';return;}
    
    if(curMode==='reg'){
        var pwd2=document.getElementById('pwd2').value;
        if(pwd!==pwd2){document.getElementById('err').textContent='两次密码不一致';return;}
        if(pwd.length<3){document.getElementById('err').textContent='密码至少3位';return;}
    }
    
    try{
        var body='user='+encodeURIComponent(user)+'&pwd='+encodeURIComponent(pwd)+'&mode='+curMode;
        var r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body});
        var d=await r.json();
        if(d.ok){
            var usr=document.getElementById('user').value.trim();
            document.cookie='admin_auth=1; path=/';
            document.cookie='admin_user='+encodeURIComponent(usr)+'; path=/';
            document.cookie='admin_role='+(d.role||'staff')+'; path=/';
            location.href='/admin?login=1';
        }
        else {
            document.getElementById('err').textContent=d.msg||'操作失败';
        }
    }catch(e){
        document.getElementById('err').textContent='网络错误，请检查ESP32连接';
    }
    return false;
}
</script>
</body>
</html>
      )rawliteral";
      
      request->send(200, "text/html; charset=utf-8", loginHTML);
      return;
    }
    static const char html[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>商家管理中心</title>
<!-- 用纯Canvas画趋势图，无需CDN -->
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;background:#f0f2f5;min-height:100vh;color:#333}
.topbar{background:linear-gradient(135deg,#1a1a2e,#16213e);color:white;padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between}
.topbar h1{font-size:18px;font-weight:600}
.topbar a{color:#aab;text-decoration:none;font-size:13px;padding:6px 14px;border:1px solid #334;border-radius:6px}
.topbar a:hover{color:white;border-color:#667}
.container{max-width:1200px;margin:0 auto;padding:20px}

.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.stat-card{background:white;border-radius:12px;padding:16px 18px;box-shadow:0 1px 3px rgba(0,0,0,0.06);display:flex;align-items:center;gap:12px}
.stat-card .icon{width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}
.stat-card .icon.green{background:#eafaf1;color:#27ae60}
.stat-card .icon.blue{background:#e8f4fd;color:#2980b9}
.stat-card .icon.orange{background:#fef5e7;color:#e67e22}
.stat-card .icon.red{background:#fdedec;color:#e74c3c}
.stat-card .icon.purple{background:#f4ecf7;color:#8e44ad}
.stat-card .val{font-size:24px;font-weight:700;color:#222}
.stat-card .lbl{font-size:12px;color:#999;margin-top:2px}

.section{background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.06);margin-bottom:20px;overflow:hidden}
.section-hd{padding:14px 20px;border-bottom:1px solid #f0f0f0;display:flex;justify-content:space-between;align-items:center}
.section-hd h3{font-size:15px;font-weight:600;color:#222}
.section-bd{padding:18px 20px}

.ai-tabs{display:flex;margin-bottom:14px;border-bottom:2px solid #eee}
.ai-tab{padding:10px 18px;font-size:13px;cursor:pointer;color:#999;border-bottom:2px solid transparent;margin-bottom:-2px;background:none;border-top:none;border-left:none;border-right:none;font-family:inherit}
.ai-tab.active{color:#667eea;border-bottom-color:#667eea;font-weight:600}
.ai-result{padding:14px;background:#f8f9fc;border-radius:10px;font-size:14px;line-height:1.8;color:#444;min-height:100px;white-space:pre-wrap;border-left:4px solid #667eea}
.ai-result.loading{color:#999;animation:pulse 1.5s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.ai-btn{display:inline-flex;align-items:center;gap:6px;padding:10px 20px;background:linear-gradient(135deg,#667eea,#764ba2);color:white;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}

table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#f8f9fc;padding:9px 10px;text-align:left;font-weight:600;color:#666;font-size:12px;border-bottom:2px solid #eee}
td{padding:9px 10px;border-bottom:1px solid #f5f5f5}tr:hover{background:#fafbfc}

.badge{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600;display:inline-block}
.badge.good{background:#eafaf1;color:#27ae60}.badge.warn{background:#fef5e7;color:#e67e22}.badge.danger{background:#fdedec;color:#e74c3c}

.order-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #f5f5f5}
.order-items{font-size:14px;color:#333;flex:1}
.order-total{font-size:16px;font-weight:700;color:#e74c3c;min-width:70px;text-align:right}
.order-time{font-size:12px;color:#aaa;min-width:50px;text-align:right;margin-left:12px}
.empty-state{text-align:center;padding:40px;color:#bbb;font-size:14px}
.refreshed{font-size:12px;color:#999}

.scan-panel{display:flex;gap:16px;flex-wrap:wrap}
.scan-right{flex:2;min-width:300px}
.form-group{margin-bottom:10px}
.form-group label{display:block;font-size:13px;color:#666;margin-bottom:3px;font-weight:600}
.form-group input,.form-group select{width:100%;padding:9px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:14px;font-family:inherit;outline:none}
.form-group input:focus{border-color:#667eea}
.btn-submit{padding:10px 24px;background:linear-gradient(135deg,#2ecc71,#27ae60);color:white;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;margin-top:6px}
.btn-submit.danger{background:linear-gradient(135deg,#e74c3c,#c0392b)}
.scan-msg{padding:10px;border-radius:8px;font-size:13px;margin-bottom:10px}
.scan-msg.ok{background:#eafaf1;color:#27ae60}.scan-msg.err{background:#fdedec;color:#e74c3c}.scan-msg.info{background:#e8f4fd;color:#2980b9}
</style>
</head>
<body>
<div class="topbar" id="topBar" style="background:linear-gradient(135deg,#1a1a2e,#16213e)">
  <h1 id="storeTitle">📊 商家管理中心</h1>
  <div>
    <a href="#" onclick="exportReport();return false" style="color:#fff;text-decoration:none;font-size:12px;padding:6px 10px;border:1px solid rgba(255,255,255,0.3);border-radius:6px;margin-right:8px;text-shadow:0 1px 2px rgba(0,0,0,0.3)">📥 导出日报</a>
    <span style="font-size:12px;color:#fff;margin-right:12px;text-shadow:0 1px 2px rgba(0,0,0,0.3)" id="currentTime"></span>
    <span style="font-size:12px;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,0.4)" id="loginUser">👤 ...</span>
    <a href="/logout" style="color:#fff;text-decoration:none;font-size:12px;margin-left:12px;text-shadow:0 1px 2px rgba(0,0,0,0.3)">退出</a>
  </div>
</div>
<div class="container">

  <div class="stats-grid">
    <div class="stat-card"><div class="icon green">💰</div><div><div class="val" id="sRevenue">¥0</div><div class="lbl">今日营业额</div></div></div>
    <div class="stat-card"><div class="icon blue">📋</div><div><div class="val" id="sOrders">0</div><div class="lbl">今日订单数</div></div></div>
    <div class="stat-card"><div class="icon purple">🛒</div><div><div class="val" id="sAvgOrder">¥0</div><div class="lbl">平均客单价</div></div></div>
    <div class="stat-card"><div class="icon orange">⚠️</div><div><div class="val" id="sLowStock">0</div><div class="lbl">库存预警商品</div></div></div>
    <div class="stat-card"><div class="icon red">⏰</div><div><div class="val" id="sExpiry">0</div><div class="lbl">临期/过期商品</div></div></div>
  </div>

  <div style="display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:20px">
    <div class="section" style="margin-bottom:0"><div class="section-hd"><h3>📈 近7天趋势</h3><button onclick="resetTrend()" style="margin-left:12px;padding:2px 10px;border:1px solid #ddd;border-radius:6px;background:white;font-size:11px;cursor:pointer;color:#999">重置</button></div><div class="section-bd" style="position:relative;height:300px"><canvas id="trendChart"></canvas></div></div>
    <div class="section" style="margin-bottom:0"><div class="section-hd"><h3>🚨 动态补货预警</h3></div><div class="section-bd" id="restockAlerts" style="max-height:300px;overflow-y:auto"><div class="empty-state">加载中...</div></div></div>
  </div>

  <div class="section">
    <div class="section-hd"><h3>📷 扫码进货</h3><span class="refreshed">摄像头QR码 + 扫码枪条形码</span></div>
    <div class="section-bd">
      <div style="text-align:center;padding:30px 20px" id="scanStart">
        <div style="font-size:48px;margin-bottom:16px">📷🔫</div>
        <div style="margin-bottom:20px;color:#666">选择扫码方式开始进货</div>
        <button onclick="startCamScan()" style="padding:14px 32px;background:linear-gradient(135deg,#667eea,#764ba2);color:white;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;font-family:inherit;margin:0 8px">📷 摄像头扫码</button>
        <button onclick="startGunScan()" style="padding:14px 32px;background:linear-gradient(135deg,#e67e22,#d35400);color:white;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;font-family:inherit;margin:0 8px">🔫 扫码枪</button>
        <input type="text" id="gunInput" style="position:fixed;top:-100px;left:-100px;width:1px;height:1px;opacity:0" autocomplete="off">
      </div>
      <div style="text-align:center;padding:20px;color:#667eea;font-size:15px;font-weight:600;display:none" id="scanWaiting">
        <span class="scan-dot" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#667eea;margin-right:8px;animation:dot 1s infinite"></span>
        正在识别中，请将条码对准扫描设备...
        <button onclick="stopAllScan()" style="margin-left:16px;padding:6px 16px;background:#f5f5f5;color:#999;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit">取消</button>
      </div>
      <div id="scanForm" style="display:none">
        <div id="scanMsg"></div>
        <div id="existingProduct" style="display:none">
          <div class="form-group"><label>商品名称</label><input type="text" id="rName" disabled></div>
          <div class="form-group"><label>当前库存</label><input type="text" id="rStock" disabled></div>
          <div class="form-group"><label>进货数量</label><input type="number" id="rQty" value="10" min="1"></div>
          <button class="btn-submit" onclick="doRestock()">✅ 确认进货</button>
        </div>
        <div id="newProduct" style="display:none">
          <div class="form-group"><label>条码</label><input type="text" id="nCode" disabled></div>
          <div class="form-group"><label>商品名称 *</label><input type="text" id="nName"></div>
          <div class="form-group"><label>单价 (元) *</label><input type="number" id="nPrice" step="0.01" placeholder="9.90"></div>
          <div class="form-group"><label>入库数量</label><input type="number" id="nStock" value="20" min="1"></div>
          <div class="form-group"><label>生产日期</label><input type="date" id="nMfg"></div>
          <div class="form-group"><label>保质期 (月)</label><input type="number" id="nLife" value="12" min="1"></div>
          <div class="form-group"><label>图标</label>
            <select id="nIcon" style="width:100%;padding:9px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:22px;font-family:inherit">
              <option value="🥤">🥤 饮料</option><option value="🍫">🍫 零食</option><option value="🍜">🍜 食品</option>
              <option value="🧻">🧻 日用品</option><option value="💧">💧 水</option><option value="🍟">🍟 膨化</option>
              <option value="🥛">🥛 乳制品</option><option value="🍪">🍪 饼干</option><option value="📦">📦 其他</option>
            </select>
          </div>
          <div class="form-group"><label style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" id="nWeigh" style="width:auto"> ⚖️ 称重商品（按克计价，出现在称重tab）</label></div>
          <button class="btn-submit" onclick="doAddProduct()">➕ 新增商品</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 主导航 -->
  <div style="display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap" id="mainTabs">
    <button class="btn-main-tab active" onclick="switchMainTab('dashboard')" id="tabDashboard">📊 仪表盘</button>
    <button class="btn-main-tab" onclick="switchMainTab('ai')" id="tabAi">🤖 AI分析</button>
    <button class="btn-main-tab" onclick="switchMainTab('members')" id="tabMembers">👥 会员</button>
    <button class="btn-main-tab" onclick="switchMainTab('logs')" id="tabLogs">📜 日志</button>
    <button class="btn-main-tab" onclick="switchMainTab('settings')" id="tabSettings">⚙️ 设置</button>
  </div>

  <style>
  .btn-main-tab{padding:10px 18px;background:white;border:1.5px solid #e0e0e0;border-radius:10px;font-size:13px;cursor:pointer;color:#666;font-family:inherit;font-weight:500}
  .btn-main-tab.active{background:#667eea;color:white;border-color:#667eea}
  </style>

  <div id="secDashboard">
  <div class="section manager-only" id="secAi">
    <div class="section-hd"><h3>🤖 AI 智能分析</h3><span class="refreshed" id="aiRefreshed"></span></div>
    <div class="section-bd">
      <div class="ai-tabs">
        <button class="ai-tab active" onclick="runAnalysis('full')">📋 综合报告</button>
        <button class="ai-tab" onclick="runAnalysis('restock')">📦 进货建议</button>
        <button class="ai-tab" onclick="runAnalysis('promotion')">🏷️ 促销建议</button>
        <button class="ai-tab" onclick="runAnalysis('expiry')">⏰ 临期预警</button>
      </div>
      <div id="aiResult" class="ai-result loading">点击上方按钮，AI 将自动分析...</div>
      <button class="ai-btn" onclick="runAnalysis('full')" style="margin-top:10px" id="btnAnalyze">✨ 一键生成报告</button>
    </div>
  </div>

  <div class="section" id="secProducts">
    <div class="section-hd"><h3>📦 商品管理</h3><span class="refreshed" id="prodRefreshed"></span><input id="prodSearch" placeholder="🔍 搜索商品..." oninput="filterProducts()" style="margin-left:16px;padding:6px 12px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:13px;width:180px;font-family:inherit"></div>
    <div class="section-bd" style="overflow-x:auto">
      <table>
        <thead><tr><th>商品</th><th>单价</th><th>库存</th><th>今日销量</th><th>今日销售额</th><th>生产日期</th><th>保质期</th><th>到期状态</th><th>库存状态</th><th>补货建议</th><th>操作</th></tr></thead>
        <tbody id="productTable"></tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <div class="section-hd"><h3>🧾 订单记录</h3><span class="refreshed" id="orderRefreshed"></span></div>
    <div class="section-bd" id="orderList"><div class="empty-state">暂无订单</div></div>
  </div>
</div> <!-- end secDashboard -->

  <!-- 店铺设置面板（店长可见）-->
  <div class="section" id="secMembers" style="display:none">
    <div class="section-hd"><h3>👥 会员管理</h3><span class="refreshed" id="memRefreshed"></span></div>
    <div class="section-bd">
      <div style="margin-bottom:12px;display:flex;gap:8px">
        <input id="newMemName" placeholder="姓名" style="width:80px;padding:8px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:13px;font-family:inherit">
        <input id="newMemPhone" placeholder="手机号" style="width:120px;padding:8px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:13px;font-family:inherit">
        <button onclick="addMember()" style="padding:8px 16px;background:#2ecc71;color:white;border:none;border-radius:8px;font-size:13px;cursor:pointer">+ 添加</button>
      </div>
      <div id="memberList" style="max-height:400px;overflow-y:auto"><div class="empty-state">加载中...</div></div>
      <div style="margin-top:12px;font-size:12px;color:#999" id="rfidBindHint"></div>
    </div>
  </div>

  <div class="section" id="secSettings" style="display:none">
    <div class="section-hd"><h3>⚙️ 店铺设置</h3></div>
    <div class="section-bd">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;max-width:500px">
        <div class="form-group"><label>店铺名称</label><input id="cfgName" placeholder="我的小店"></div>
        <div class="form-group"><label>图标 Emoji</label><input id="cfgEmoji" placeholder="🏪"></div>
        <div class="form-group"><label>主题色</label><input id="cfgColor" type="color" style="height:40px;padding:4px"></div>
      </div>
      <button class="btn-submit" onclick="saveSettings()" style="margin-top:12px">💾 保存设置</button>
      <span style="color:#27ae60;font-size:13px;margin-left:12px;display:none" id="cfgSaved">已保存</span>

      <hr style="margin:20px 0;border:none;border-top:1px solid #eee" class="manager-only">
      <h4 style="margin-bottom:12px;color:#333" class="manager-only">👥 员工账号管理</h4>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end;max-width:600px;margin-bottom:12px" class="manager-only">
        <div class="form-group" style="margin:0"><label>用户名</label><input id="newEmpUser" placeholder="新员工"></div>
        <div class="form-group" style="margin:0"><label>密码</label><input id="newEmpPwd" type="password" placeholder="至少3位"></div>
        <div class="form-group" style="margin:0"><label>角色</label>
          <select id="newEmpRole" style="width:100%;padding:9px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:14px;font-family:inherit">
            <option value="staff">店员</option><option value="manager">店长</option>
          </select>
        </div>
        <button class="btn-submit" onclick="createEmployee()" style="margin:0;white-space:nowrap">➕ 创建</button>
      </div>
      <div id="empListMsg" style="font-size:12px;color:#27ae60;margin-bottom:8px;display:none" class="manager-only"></div>
      <div id="empList" style="max-height:200px;overflow-y:auto" class="manager-only"></div>
    </div>
  </div>

  <!-- 操作日志面板 -->
  <div class="section" id="secLogs" style="display:none">
    <div class="section-hd"><h3>📜 操作日志</h3><span class="refreshed">最近50条记录</span></div>
    <div class="section-bd" style="max-height:400px;overflow-y:auto" id="logList"><div class="empty-state">加载中...</div></div>
  </div>

<script>
// ===== 工具 =====
// 读取角色cookie
var adminRole=(document.cookie.split(';').find(function(c){return c.trim().startsWith('admin_role=')})||'=staff').split('=')[1]||'staff';
var isManager=(adminRole==='manager');
function nowStr(){var d=new Date();return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')+':'+d.getSeconds().toString().padStart(2,'0');}
function calcExpiry(mfgDate,shelfMonths){
  var parts=mfgDate.split('-');if(parts.length<3)return{label:'未知',level:'good'};
  var mfg=new Date(parseInt(parts[0]),parseInt(parts[1])-1,parseInt(parts[2]));
  var exp=new Date(mfg);exp.setMonth(exp.getMonth()+shelfMonths);
  var days=Math.ceil((exp-new Date())/(86400000));
  if(days<0)return{label:'已过期',level:'danger'};
  if(days<=30)return{label:'仅剩'+days+'天',level:'danger'};
  if(days<=60)return{label:'剩'+days+'天',level:'warn'};
  return{label:'正常',level:'good'};
}
function filterProducts(){loadDashboard();}
async function resetTrend(){if(confirm('重置7天趋势数据？将清空所有历史记录。')){await fetch('/api/admin/reset-trend',{method:'POST'});loadDashboard();}}
// ── 会员管理 ──
async function loadMemberList(){
  try{var r=await fetch('/api/member/list');var list=await r.json();
  var h='';var now=new Date().toLocaleString();
  list.forEach(function(m){
    h+='<div style="padding:10px 0;border-bottom:1px solid #f0f0f0;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">';
    h+='<span>👤 <b>'+m.name+'</b> <span style="font-size:11px;color:#999">'+(m.phone?'📱'+m.phone+' ':'')+'积分:'+m.points+' | 累计:¥'+m.totalSpent.toFixed(0)+'</span></span>';
    h+='<span style="display:flex;gap:4px">';
    if(m.rfid_uid)h+='<span style="font-size:11px;color:#27ae60">💳 '+m.rfid_uid+'</span><button onclick="unbindMemRFID(\''+m.id+'\')" style="background:#fef5e7;color:#e67e22;border:none;padding:2px 6px;border-radius:4px;cursor:pointer;font-size:10px">解绑</button>';
    else h+='<button onclick="startMemBindRFID(\''+m.id+'\')" style="background:#e8f4fd;color:#2980b9;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">绑卡</button>';
    h+='<button onclick="adjustMemPoints(\''+m.id+'\')" style="background:#f0f0f0;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">调分</button>';
    h+='<button onclick="deleteMember(\''+m.id+'\')" style="background:#fdedec;color:#e74c3c;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">🗑</button>';
    h+='</span></div>';
  });
  document.getElementById('memberList').innerHTML=h||'<div class="empty-state">暂无会员</div>';
  document.getElementById('memRefreshed').textContent='更新于 '+now;
  }catch(e){}
}
async function addMember(){
  var name=document.getElementById('newMemName').value.trim();
  var phone=document.getElementById('newMemPhone').value.trim();
  if(!name){alert('请输入姓名');return;}
  var r=await fetch('/api/member/register',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'name='+encodeURIComponent(name)+'&phone='+encodeURIComponent(phone)});
  var d=await r.json();
  if(d.ok){document.getElementById('newMemName').value='';document.getElementById('newMemPhone').value='';loadMemberList();}else{alert(d.msg||'失败');}
}
var memBindMid=null,memBindTimer=null;
function startMemBindRFID(mid){
  memBindMid=mid;
  document.getElementById('rfidBindHint').innerHTML='⏳ 请刷RFID卡...';
  if(memBindTimer)clearInterval(memBindTimer);
  memBindTimer=setInterval(async function(){
    var r=await fetch('/api/rfid-poll');var d=await r.json();
    if(d.card&&d.uid){
      clearInterval(memBindTimer);
      var resp=await fetch('/api/member/bind-rfid',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'uid='+d.uid+'&id='+memBindMid});
      var rd=await resp.json();
      document.getElementById('rfidBindHint').innerHTML=rd.ok?'✅ 绑定成功':'❌ 绑定失败';
      loadMemberList();
    }
  },500);
}
async function unbindMemRFID(mid){
  await fetch('/api/member/unbind-rfid',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+mid});
  loadMemberList();
}
async function deleteMember(mid){
  if(!confirm('确认删除该会员？'))return;
  await fetch('/api/member/delete',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+mid});
  loadMemberList();
}
async function adjustMemPoints(mid){
  var pts=prompt('调整积分（正数加、负数减）：');
  if(!pts)return;
  await fetch('/api/member/add-points',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+mid+'&pts='+pts});
  loadMemberList();
}
async function refundOrder(orderId){
  if(!confirm('确认退款该订单？库存将恢复。'))return;
  var r=await fetch('/api/admin/refund',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'orderId='+orderId});
  var d=await r.json();
  if(d.ok){alert('已退款');loadDashboard();}else{alert('退款失败: '+(d.msg||''));}
}
function stockBadge(stock){
  if(stock<=0)return'<span class="badge danger">缺货</span>';
  if(stock<5)return'<span class="badge danger">告急('+stock+')</span>';
  if(stock<15)return'<span class="badge warn">偏低('+stock+')</span>';
  return'<span class="badge good">充足</span>';
}

var allProducts=[];

async function loadDashboard(){
  try{var r=await fetch('/api/products');allProducts=await r.json();}catch(e){return;}
  var orders=[];
  try{var or=await fetch('/api/order-history');orders=await or.json();}catch(e){}

  var totalRevenue=0,paidOrders=0,lowStock=0,nearExpiry=0;
  orders.forEach(function(o){if(o.paid){totalRevenue+=o.total;paidOrders++;}});
  allProducts.forEach(function(p){
    if(!p.name||!p.qr_code)return;
    if(p.stock<10)lowStock++;
    var ex=calcExpiry(p.mfg_date,p.shelf_life);
    if(ex.level==='danger')nearExpiry++;
  });
  var avg=paidOrders>0?(totalRevenue/paidOrders):0;
  document.getElementById('sRevenue').textContent='¥'+totalRevenue.toFixed(2);
  document.getElementById('sOrders').textContent=paidOrders;
  document.getElementById('sAvgOrder').textContent='¥'+avg.toFixed(2);
  document.getElementById('sLowStock').textContent=lowStock;
  document.getElementById('sExpiry').textContent=nearExpiry;

  var sorted=allProducts.filter(function(p){return p.name&&p.qr_code;}).sort(function(a,b){return(b.price*b.today_sold)-(a.price*a.today_sold);});
  var q=(document.getElementById('prodSearch').value||'').toLowerCase();
  var ph='';
  sorted.forEach(function(p){
    if(q&&p.name.toLowerCase().indexOf(q)<0&&p.qr_code.indexOf(q)<0)return;
    var ex=calcExpiry(p.mfg_date,p.shelf_life);
    ph+='<tr><td><b>'+p.icon+' '+p.name+'</b></td>';
    ph+='<td style="color:#e74c3c;font-weight:600">¥'+p.price.toFixed(2)+'</td>';
    ph+='<td style="font-weight:600">'+p.stock+'</td>';
    ph+='<td>'+p.today_sold+'件</td>';
    ph+='<td style="color:#e74c3c;font-weight:600">¥'+(p.price*p.today_sold).toFixed(2)+'</td>';
    ph+='<td style="font-size:12px;color:#888">'+p.mfg_date+'</td>';
    ph+='<td style="font-size:12px;color:#888">'+p.shelf_life+'个月</td>';
    ph+='<td><span class="badge '+(ex.level||'good')+'">'+ex.label+'</span></td>';
    ph+='<td>'+stockBadge(p.stock)+(p.isWeigh?' ⚖️':'')+'</td>';
    var rate=p.today_sold||0.1;
    var daysLeft=Math.round(p.stock/rate);
    var restockHTML=daysLeft<3?'<span style="color:#e74c3c;font-weight:700">⚠ '+daysLeft+'天</span>':daysLeft<7?'<span style="color:#e67e22">'+daysLeft+'天</span>':'<span style="color:#27ae60">'+daysLeft+'天</span>';
    ph+='<td style="font-size:12px">'+restockHTML+'</td>';
    ph+='<td>'+(isManager?'<button onclick="delProduct(\''+p.qr_code+'\',\''+p.name.replace(/'/g," ")+'\')" style="background:#fdedec;color:#e74c3c;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px">🗑</button>':'<span style="font-size:12px;color:#ccc">--</span>')+'</td></tr>';
  });
  document.getElementById('productTable').innerHTML=ph;

  var oh='';
  if(orders.length===0){oh='<div class="empty-state">暂无订单</div>';}
  else{
    for(var i=orders.length-1;i>=0;i--){
      var o=orders[i];
      oh+='<div class="order-row"><div class="order-items">'+o.items+' '+(o.paid?'<span class="badge good">已付</span>':'<span class="badge warn">未付</span>')+(o.method?' <span style="font-size:11px;color:#888">['+(o.method==='wx'?'微信':o.method==='alipay'?'支付宝':'现金')+']</span>':'')+'</div><div class="order-total">¥'+o.total.toFixed(2)+'</div><div class="order-time">'+o.time+(o.paid&&isManager?' <button onclick="refundOrder('+i+')" style="background:#fdedec;color:#e74c3c;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;margin-left:8px">退款</button>':'')+'</div></div>';
    }
  }
  document.getElementById('orderList').innerHTML=oh;
  var ts=nowStr();
  document.getElementById('prodRefreshed').textContent='更新于 '+ts;
  document.getElementById('orderRefreshed').textContent='更新于 '+ts;
  document.getElementById('currentTime').textContent=ts;

  // ── 动态补货预警（双维度：库存可售天数 + 绝对库存下限）──
  var alerts=[];
  sorted.forEach(function(p){
    if(p.stock<=0) return;
    if(p.stock<=5){
      // 绝对库存过低，不论日销多少都预警
      var d=p.today_sold>0?Math.round(p.stock/p.today_sold):0;
      var label=p.today_sold>0?'⚠ 仅剩'+d+'天':'⚠ 库存告急';
      alerts.push({name:p.name,icon:p.icon,stock:p.stock,sold:p.today_sold,days:d,label:label,level:'critical'});
    }else{
      var rate=p.today_sold||0;
      if(rate<=0) return; // 日销为0且库存>5，不预警
      var days=Math.round(p.stock/rate);
      if(days<3) alerts.push({name:p.name,icon:p.icon,stock:p.stock,sold:p.today_sold,days:days,label:'⚠ 仅剩'+days+'天',level:days<=1?'critical':'warn'});
    }
  });
  alerts.sort(function(a,b){return a.days-b.days;});
  var ah='';
  if(alerts.length===0) ah='<div class="empty-state">✅ 库存充足，无需补货</div>';
  else alerts.forEach(function(a){
    var color=a.level==='critical'?'#e74c3c':'#e67e22';
    ah+='<div style="padding:8px 0;border-bottom:1px solid #f0f0f0"><span>'+a.icon+' <b>'+a.name+'</b></span><br><span style="font-size:12px;color:#888">库存'+a.stock+'件 / 日销'+(a.sold||0)+'件</span> <span style="color:'+color+';font-weight:700;float:right">'+a.label+'</span></div>';
  });
  document.getElementById('restockAlerts').innerHTML=ah||'<div class="empty-state">✅ 库存充足</div>';

  // ── 7天趋势图（纯Canvas，无需CDN）──
  try{var tr=await fetch('/api/admin/trend');var td=await tr.json();}catch(e){return;}
  var labels=td.map(function(x){return x.label;});
  var revData=td.map(function(x){return x.r;});
  var soldData=td.map(function(x){return x.s;});
  var c=document.getElementById('trendChart');
  if(!c)return;
  var w=c.parentNode.clientWidth-24,h=c.parentNode.clientHeight-16;
  c.width=w*2;c.height=h*2;c.style.width=w+'px';c.style.height=h+'px';
  var ctx=c.getContext('2d');ctx.scale(2,2);
  var padL=50,padR=45,padT=28,padB=48,pw=w-padL-padR,ph=h-padT-padB;
  var maxR=Math.max.apply(null,revData.concat([1]));
  var maxS=Math.max.apply(null,soldData.concat([1]));
  // 坐标轴
  ctx.strokeStyle='#ddd';ctx.lineWidth=1;
  for(var i=0;i<=4;i++){var y=padT+ph*i/4;ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(w-padR,y);ctx.stroke();}
  // 折线-营业额
  ctx.beginPath();ctx.strokeStyle='#667eea';ctx.lineWidth=2;
  for(var i=0;i<revData.length;i++){var x=padL+pw*i/(revData.length-1);var y=padT+ph-ph*revData[i]/maxR;i==0?ctx.moveTo(x,y):ctx.lineTo(x,y);}
  ctx.stroke();
  // 折线-件数
  ctx.beginPath();ctx.strokeStyle='#27ae60';ctx.lineWidth=2;
  for(var i=0;i<soldData.length;i++){var x=padL+pw*i/(soldData.length-1);var y=padT+ph-ph*soldData[i]/maxS;i==0?ctx.moveTo(x,y):ctx.lineTo(x,y);}
  ctx.stroke();
  // 数据点和标签
  ctx.font='10px "PingFang SC","Microsoft YaHei",sans-serif';
  for(var i=0;i<revData.length;i++){var x=padL+pw*i/(revData.length-1);
    var y=padT+ph-ph*revData[i]/maxR;ctx.fillStyle='#667eea';ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#667eea';ctx.fillText('¥'+revData[i].toFixed(2),x-15,y-8);
    ctx.fillStyle='#999';ctx.textAlign='center';ctx.fillText(labels[i],x,padT+ph+18);ctx.textAlign='start';
  }
  // 图例
  ctx.fillStyle='#667eea';ctx.fillRect(padL,padT+ph+26,10,10);
  ctx.fillStyle='#666';ctx.fillText('营业额(¥)',padL+14,padT+ph+35);
  ctx.fillStyle='#27ae60';ctx.fillRect(padL+100,padT+ph+26,10,10);
  ctx.fillStyle='#666';ctx.fillText('售出件数',padL+114,padT+ph+35);
}

async function delProduct(code,name){
  if(!confirm('删除 "'+name+'"？不可恢复！'))return;
  var r=await fetch('/api/admin/delete',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'code='+encodeURIComponent(code)});
  var d=await r.json();
  if(d.ok){loadDashboard();}else{alert('删除失败');}
}

// ===== AI =====
async function runAnalysis(type){
  document.querySelectorAll('.ai-tab').forEach(function(t){t.classList.remove('active')});
  var idx={full:0,restock:1,promotion:2,expiry:3};
  document.querySelectorAll('.ai-tab')[idx[type]].classList.add('active');
  var el=document.getElementById('aiResult');
  el.className='ai-result loading';el.textContent='AI 分析中...';
  document.getElementById('btnAnalyze').disabled=true;
  try{var r=await fetch('/api/admin/analysis?type='+type);el.className='ai-result';el.textContent=await r.text();document.getElementById('aiRefreshed').textContent='完成于 '+nowStr();}
  catch(e){el.className='ai-result';el.textContent='分析失败';}
  document.getElementById('btnAnalyze').disabled=false;
}

// ===== 扫码进货（点击触发 → 扫到弹窗）=====
var scanMode='';     // 'cam' or 'gun'
var scanTimer=null;
var scannedCode='';
var MODULE_A="192.168.43.13";

function startCamScan(){
  stopAllScan();
  fetch('/api/admin-cam-start'); // 通知ESP32商家在扫码，顾客端暂停
  fetch('/api/scan-gun-clear');
  scanMode='cam';
  document.getElementById('scanStart').style.display='none';
  document.getElementById('scanWaiting').style.display='block';
  document.getElementById('scanWaiting').innerHTML='<span class="scan-dot" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#667eea;margin-right:8px;animation:dot 1s infinite"></span>正在摄像头扫码中，请将QR码对准摄像头... <button onclick="stopAllScan()" style="margin-left:16px;padding:6px 16px;background:#f5f5f5;color:#999;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit">取消</button>';
  scanTimer=setInterval(pollCamera,2000); // 摄像头解码约1.5秒，避免请求堆积
}

function startGunScan(){
  stopAllScan();
  fetch('/api/scan-gun-clear');
  scanMode='gun';
  document.getElementById('scanStart').style.display='none';
  document.getElementById('scanWaiting').style.display='block';
  document.getElementById('scanWaiting').innerHTML='<span class="scan-dot" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#e67e22;margin-right:8px;animation:dot 1s infinite"></span>等待扫码枪，直接扫条码... <button onclick="stopAllScan()" style="margin-left:16px;padding:6px 16px;background:#f5f5f5;color:#999;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit">取消</button>';
  scanTimer=setInterval(pollGunCode,500);
}

function stopAllScan(){
  fetch('/api/admin-cam-stop'); // 通知ESP32商家扫码结束，顾客端恢复
  scanMode='';
  if(scanTimer){clearInterval(scanTimer);scanTimer=null;}
  _lastGun3='';_lastCam='';scannedCode='';
  var inp=document.getElementById('gunInput');
  inp.style.display='none';inp.value='';
  document.getElementById('scanStart').style.display='block';
  document.getElementById('scanWaiting').style.display='none';
  document.getElementById('scanForm').style.display='none';
  document.getElementById('existingProduct').style.display='none';
  document.getElementById('newProduct').style.display='none';
  scannedCode='';
}

var _lastCam='';
async function pollCamera(){
  try{
    var r=await fetch('http://'+MODULE_A+'/qr-scan');
    var d=await r.json();
    console.log('[admin poll]',d);
    if(d.ok&&d.text){
      var code=(d.text||'').trim();
      console.log('[admin poll] code:',code);
      if(code&&code.length>=1&&code!==_lastCam){
        _lastCam=code;scannedCode=code;
        if(scanTimer){clearInterval(scanTimer);scanTimer=null;}
        document.getElementById('scanWaiting').style.display='none';
        showCodeResult(code);
      }
    }
  }catch(e){console.error('[admin poll] error:',e);}
}

// 扫码枪轮询ESP32
var _lastGun3='';
function pollGunCode(){
  fetch('/api/scan-gun-result').then(function(r){return r.json()}).then(function(d){
    if(d.code&&d.code.length>=8&&d.code!==_lastGun3){
      _lastGun3=d.code;scannedCode=d.code;
      clearInterval(scanTimer);scanTimer=null;
      document.getElementById('scanWaiting').style.display='none';
      showCodeResult(d.code);
    }
  }).catch(function(){});
}

// 隐藏输入框（不再使用但保留元素）
document.getElementById('gunInput').addEventListener('keydown',function(e){
  if(scanMode!=='gun')return;
  if(e.key==='Enter'){
    e.preventDefault();
    var code=this.value.trim();
    if(code.length>=8){
      scannedCode=code;
      this.style.display='none';
      this.value='';
      stopAllScan();
      document.getElementById('scanWaiting').style.display='none';
      showCodeResult(code);
    }
  }
});
document.getElementById('gunInput').addEventListener('blur',function(){
  if(scanMode==='gun'){setTimeout(function(){this.focus();}.bind(this),50);}
});

function showCodeResult(code){
  document.getElementById('scanForm').style.display='block';
  document.getElementById('scanMsg').innerHTML='<div class="scan-msg info">🔍 '+code+'</div>';
  var p=allProducts.find(function(x){return x.qr_code===code;});
  if(p){showExistingProduct(p);}else{showNewProduct(code);}
}

function showExistingProduct(p){
  document.getElementById('scanMsg').innerHTML='<div class="scan-msg ok">✅ '+p.icon+' '+p.name+'</div>';
  document.getElementById('rName').value=p.name;
  document.getElementById('rStock').value=p.stock+' 件';
  document.getElementById('rQty').value=10;
  document.getElementById('existingProduct').style.display='block';
  document.getElementById('newProduct').style.display='none';
  document.getElementById('rQty').focus();
}
function showNewProduct(code){
  document.getElementById('scanMsg').innerHTML='<div class="scan-msg info">🆕 新商品条码</div>';
  document.getElementById('nCode').value=code;
  document.getElementById('nName').value='';document.getElementById('nPrice').value='';
  document.getElementById('nStock').value=20;
  document.getElementById('nMfg').value=new Date().toISOString().split('T')[0];
  document.getElementById('nLife').value=12;
  document.getElementById('nWeigh').checked=false;
  document.getElementById('existingProduct').style.display='none';
  document.getElementById('newProduct').style.display='block';
  document.getElementById('nName').focus();
}
async function doRestock(){
  var qty=parseInt(document.getElementById('rQty').value)||0;
  if(qty<=0){alert('请输入数量');return;}
  var r=await fetch('/api/admin/restock',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'code='+encodeURIComponent(scannedCode)+'&qty='+qty});
  var d=await r.json();
  document.getElementById('scanMsg').innerHTML='<div class="scan-msg '+(d.ok?'ok':'err')+'">'+(d.ok?'进货成功':'进货失败')+'</div>';
  if(d.ok){loadDashboard();setTimeout(resetScanPanel,2000);}
}
async function doAddProduct(){
  var name=document.getElementById('nName').value.trim();
  var price=parseFloat(document.getElementById('nPrice').value)||0;
  if(!name||price<=0){alert('请填写名称和单价');return;}
  var isWeigh=document.getElementById('nWeigh').checked?'1':'0';
  var r=await fetch('/api/admin/add-product',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'code='+encodeURIComponent(scannedCode)+'&name='+encodeURIComponent(name)+'&price='+price+'&stock='+(parseInt(document.getElementById('nStock').value)||0)+'&mfg='+encodeURIComponent(document.getElementById('nMfg').value)+'&life='+(parseInt(document.getElementById('nLife').value)||12)+'&icon='+encodeURIComponent(document.getElementById('nIcon').value)+'&weigh='+isWeigh});
  var d=await r.json();
  document.getElementById('scanMsg').innerHTML='<div class="scan-msg '+(d.ok?'ok':'err')+'">'+(d.ok?'新增成功':'失败')+'</div>';
  if(d.ok){loadDashboard();setTimeout(resetScanPanel,2000);}
}
function resetScanPanel(){
  scannedCode='';
  _lastCam='';
  _lastGun3='';
  document.getElementById('gunInput').value='';
  document.getElementById('scanStart').style.display='block';
  document.getElementById('scanForm').style.display='none';
  document.getElementById('existingProduct').style.display='none';
  document.getElementById('newProduct').style.display='none';
}

// ── 主导航切换 ──
var mainTab='dashboard';
function switchMainTab(tab){
  mainTab=tab;
  document.querySelectorAll('.btn-main-tab').forEach(function(b){b.classList.remove('active');});
  var btn=document.getElementById('tab'+tab.charAt(0).toUpperCase()+tab.slice(1));
  if(btn)btn.classList.add('active');
  var showDash=(tab==='dashboard'||tab==='ai'||tab==='export');
  document.getElementById('secDashboard').style.display=showDash?'block':'none';
  document.getElementById('secMembers').style.display=(tab==='members')?'block':'none';
  document.getElementById('secSettings').style.display=(tab==='settings')?'block':'none';
  document.getElementById('secLogs').style.display=(tab==='logs')?'block':'none';
  if(tab==='members') loadMemberList();
  if(tab==='logs')loadLogs();
  if(tab==='settings')loadSettings();
  if(tab==='export'){printDailyReport();switchMainTab('dashboard');}
}

// ── 店铺设置 + 员工管理 ──
async function loadSettings(){
  try{var r=await fetch('/api/admin/storecfg');var d=await r.json();
  document.getElementById('cfgName').value=d.name||'';document.getElementById('cfgEmoji').value=d.emoji||'';document.getElementById('cfgColor').value=d.color||'#1a1a2e';
  }catch(e){}
  loadEmployeeList();
}
async function loadEmployeeList(){
  try{var r=await fetch('/api/admin/list-users');var users=await r.json();
  var h='';
  users.forEach(function(u){
    h+='<div style="padding:10px 0;border-bottom:1px solid #f0f0f0;display:flex;justify-content:space-between;align-items:center;font-size:13px;flex-wrap:wrap">';
    h+='<span>👤 <b>'+u.user+'</b> <span style="color:#999;font-size:11px">('+(u.role==='manager'?'店长':'店员')+')</span></span>';
    h+='<span style="display:flex;align-items:center;gap:6px">';
    if(u.rfid_uid){
      h+='<span style="font-size:11px;color:#27ae60">💳 '+u.rfid_uid+'</span>';
      h+='<button onclick="unbindRFID(\''+u.user+'\')" style="background:#fef5e7;color:#e67e22;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">解绑</button>';
    }else{
      h+='<span style="font-size:11px;color:#ccc">未绑卡</span>';
      h+='<button onclick="startBindRFID(\''+u.user+'\')" style="background:#e8f4fd;color:#2980b9;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">绑卡</button>';
    }
    h+='<button onclick="deleteEmployee(\''+u.user+'\')" style="background:#fdedec;color:#e74c3c;border:none;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">🗑</button>';
    h+='</span></div>';
  });
  document.getElementById('empList').innerHTML=h||'<div class="empty-state">暂无员工</div>';
  }catch(e){}
}
var rfidBindUser=null,rfidBindTimer=null;
function startBindRFID(user){
  rfidBindUser=user;
  document.getElementById('empListMsg').style.display='block';
  document.getElementById('empListMsg').textContent='⏳ 请在RFID读卡器上刷卡...';
  document.getElementById('empListMsg').style.color='#e67e22';
  if(rfidBindTimer)clearInterval(rfidBindTimer);
  rfidBindTimer=setInterval(async function(){
    try{
      var r=await fetch('/api/rfid-poll');
      var d=await r.json();
      if(d.card&&d.uid){
        clearInterval(rfidBindTimer);
        var resp=await fetch('/api/admin/bind-rfid',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'uid='+d.uid+'&user='+encodeURIComponent(rfidBindUser)});
        var rd=await resp.json();
        if(rd.ok){
          document.getElementById('empListMsg').textContent='✅ 已绑定 '+rfidBindUser;
          document.getElementById('empListMsg').style.color='#27ae60';
        }else{
          document.getElementById('empListMsg').textContent='❌ '+(rd.msg||'绑定失败');
          document.getElementById('empListMsg').style.color='#e74c3c';
        }
        setTimeout(function(){document.getElementById('empListMsg').style.display='none';},2000);
        loadEmployeeList();
      }
    }catch(e){}
  },500);
}
async function unbindRFID(user){
  if(!confirm('确认解绑 '+user+' 的RFID卡？'))return;
  try{
    var r=await fetch('/api/admin/unbind-rfid',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'user='+encodeURIComponent(user)});
    var d=await r.json();
    if(d.ok)loadEmployeeList();else alert(d.msg||'操作失败');
  }catch(e){alert('网络错误');}
}
async function createEmployee(){
  var u=document.getElementById('newEmpUser').value.trim();
  var p=document.getElementById('newEmpPwd').value;
  var r=document.getElementById('newEmpRole').value;
  if(!u||!p){alert('请填写用户名和密码');return;}
  if(p.length<3){alert('密码至少3位');return;}
  try{
    var resp=await fetch('/api/admin/create-user',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'newUser='+encodeURIComponent(u)+'&newPwd='+encodeURIComponent(p)+'&newRole='+r});
    var d=await resp.json();
    if(d.ok){
      document.getElementById('newEmpUser').value='';document.getElementById('newEmpPwd').value='';
      var m=document.getElementById('empListMsg');m.style.display='block';m.textContent='✅ 已创建 '+u;
      setTimeout(function(){m.style.display='none';},2000);
      loadEmployeeList();
    }else{alert(d.msg||'创建失败');}
  }catch(e){alert('网络错误: '+e.message);}
}
async function deleteEmployee(user){
  if(!confirm('确认删除员工 "'+user+'"？'))return;
  try{
    var resp=await fetch('/api/admin/delete-user',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'user='+encodeURIComponent(user)});
    var d=await resp.json();
    if(d.ok)loadEmployeeList();else alert(d.msg||'删除失败');
  }catch(e){alert('网络错误: '+e.message);}
}
async function saveSettings(){
  var name=document.getElementById('cfgName').value.trim();
  var emoji=document.getElementById('cfgEmoji').value.trim();
  var color=document.getElementById('cfgColor').value;
  await fetch('/api/admin/storecfg',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'name='+encodeURIComponent(name)+'&emoji='+encodeURIComponent(emoji)+'&color='+encodeURIComponent(color)});
  document.getElementById('cfgSaved').style.display='inline';
  document.getElementById('storeTitle').textContent=emoji+' '+name;
  document.getElementById('topBar').style.background='linear-gradient(135deg,'+color+','+darkenColor(color)+')';
  setTimeout(function(){document.getElementById('cfgSaved').style.display='none';},2000);
}
function darkenColor(hex){var num=parseInt(hex.replace('#',''),16);var r=Math.max(0,(num>>16)-40);var g=Math.max(0,((num>>8)&255)-20);var b=Math.max(0,(num&255)-10);return '#'+((r<<16)+(g<<8)+b).toString(16).padStart(6,'0');}

// ── 操作日志 ──
async function loadLogs(){
  try{var r=await fetch('/api/admin/oplogs');var logs=await r.json();
  var h='';
  if(logs.length===0)h='<div class="empty-state">暂无操作记录</div>';
  else{logs.reverse();logs.forEach(function(l){
    h+='<div style="padding:7px 0;border-bottom:1px solid #f0f0f0;font-size:13px"><span style="color:#999">'+l.t+'</span> <b>'+l.u+'</b> '+l.a+' <span style="color:#888">'+l.d+'</span></div>';
  });}
  document.getElementById('logList').innerHTML=h;
  }catch(e){}
}

// ── 导出日报 ──
function adminReportCanvas(text){
  var width=384,left=24,right=360,lineHeight=20;
  var measure=document.createElement('canvas').getContext('2d');
  measure.font='15px "Microsoft YaHei","PingFang SC",sans-serif';
  var lines=[];
  text.split(/\r?\n/).forEach(function(line){
    if(!line){lines.push('');return;}
    var current='';
    Array.from(line).forEach(function(ch){
      if(measure.measureText(current+ch).width>right-left&&current){lines.push(current);current=ch;}
      else current+=ch;
    });
    lines.push(current);
  });
  var height=Math.min(2400,Math.max(480,28+lines.length*lineHeight));
  var canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
  var ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);ctx.fillStyle='#000';
  ctx.textBaseline='top';ctx.font='15px "Microsoft YaHei","PingFang SC",sans-serif';
  var y=12;lines.forEach(function(line){if(y+lineHeight<height){ctx.fillText(line,left,y);y+=lineHeight;}});
  return canvas;
}
function submitAdminPrint(canvas){
  var pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
  var packed=new Uint8Array((canvas.width>>3)*canvas.height);
  for(var y=0;y<canvas.height;y++)for(var x=0;x<canvas.width;x++){
    var p=(y*canvas.width+x)*4;
    if(pixels[p+3]>64&&(pixels[p]*0.299+pixels[p+1]*0.587+pixels[p+2]*0.114)<150)packed[y*(canvas.width>>3)+(x>>3)]|=0x80>>(x&7);
  }
  return fetch('/api/printer/submit?width='+canvas.width+'&height='+canvas.height+'&kind=report',{
    method:'POST',headers:{'Content-Type':'application/octet-stream'},body:packed
  }).then(function(r){return r.json()});
}
async function printDailyReport(reportText){
  try{
    var report=reportText||await (await fetch('/api/admin/export-report',{cache:'no-store'})).text();
    var result=await submitAdminPrint(adminReportCanvas(report));
    alert(result.ok?'日报已加入打印队列':'打印失败：'+(result.msg||'队列繁忙'));
  }catch(e){alert('日报未能加入打印队列');}
}
async function exportReport(){
  try{
    var response=await fetch('/api/admin/export-report?t='+Date.now(),{cache:'no-store'});
    if(!response.ok)throw new Error('HTTP '+response.status);
    var report=await response.text();
    var blob=new Blob([report],{type:'text/plain;charset=utf-8'});
    var link=document.createElement('a');link.href=URL.createObjectURL(blob);
    link.download='每日经营报告.txt';document.body.appendChild(link);link.click();link.remove();
    setTimeout(function(){URL.revokeObjectURL(link.href)},1000);
    await printDailyReport(report);
  }catch(e){alert('日报导出失败：'+e.message);}
}

// ── 权限控制 ──
(function initRoleUI(){
  if(!isManager){
    // 店员：隐藏营业统计卡片、删除按钮、AI分析、设置入口
    document.querySelectorAll('.manager-only').forEach(function(el){el.style.display='none';});
    // 用CSS隐藏统计卡片，保留扫码进货
    var cards=document.querySelectorAll('.stat-card');
    if(cards.length>=3){cards[0].style.display='none';cards[2].style.display='none';} // 隐藏营业额和客单价
  }
  // 加载店铺名称
  fetch('/api/admin/storecfg').then(function(r){return r.json()}).then(function(d){
    document.getElementById('storeTitle').textContent=(d.emoji||'📊')+' '+(d.name||'商家管理中心');
    if(d.color)document.getElementById('topBar').style.background='linear-gradient(135deg,'+d.color+','+darkenColor(d.color)+')';
  }).catch(function(){});
})();

// ===== 启动 =====
loadDashboard();
setInterval(loadDashboard,30000);
// 读取cookie中的用户名
var cname='admin_user=';
var cookieUser=document.cookie.split(';').find(function(c){return c.trim().startsWith(cname)});
if(cookieUser)document.getElementById('loginUser').textContent='👤 '+decodeURIComponent(cookieUser.split('=')[1])+' ('+(isManager?'店长':'店员')+')';
else document.getElementById('loginUser').textContent='👤 未登录';

setInterval(function(){document.getElementById('currentTime').textContent=nowStr();},10000);
</script>
</body>
</html>

    )rawliteral";
    request->send(request->beginResponse_P(200, "text/html; charset=utf-8", html));
  });


  // 7天趋势数据
  server.on("/api/admin/trend", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "application/json; charset=utf-8", DailyStats_ToJson());
  });
  server.on("/api/admin/reset-trend", HTTP_POST, [](AsyncWebServerRequest *request) {
    DailyStats_Reset();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  server.on("/api/admin/summary", HTTP_GET, [](AsyncWebServerRequest *request) {
    float totalRevenue = 0; int paidOrders = 0;
    String j = Product_OrderHistoryToJson();
    DynamicJsonDocument doc(4096);
    deserializeJson(doc, j);
    JsonArray arr = doc.as<JsonArray>();
    for (JsonVariant v : arr) {
      if (v["paid"].as<bool>()) { totalRevenue += v["total"].as<float>(); paidOrders++; }
    }
    int lowStock = 0;
    for (int i = 0; i < PRODUCT_COUNT; i++)
      if (products[i].stock < 10) lowStock++;

    String resp = "{\"revenue\":" + String(totalRevenue, 2)
                + ",\"orders\":" + String(paidOrders)
                + ",\"avgOrder\":" + String(paidOrders > 0 ? totalRevenue / paidOrders : 0, 2)
                + ",\"lowStock\":" + String(lowStock)
                + ",\"totalProducts\":" + String([&](){int n=0;for(int i=0;i<PRODUCT_COUNT;i++)if(products[i].qrCode.length()>0)n++;return n;}()) + "}";
    request->send(200, "application/json; charset=utf-8", resp);
  });

  // 管理后台 — AI 分析 API
  server.on("/api/admin/analysis", HTTP_GET, [](AsyncWebServerRequest *request) {
    String type = request->hasParam("type") ? request->getParam("type")->value() : "full";

    // 组装商品数据文本
    String productData = "=== 超市商品实时数据 ===\n";
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      productData += (i + 1) + ". " + products[i].name
                  + " | 单价:¥" + String(products[i].price, 2)
                  + " | 库存:" + products[i].stock + "件"
                  + " | 今日售出:" + products[i].todaySold + "件"
                  + " | 今日销售额:¥" + String(products[i].price * products[i].todaySold, 2)
                  + " | 生产日期:" + products[i].mfgDate
                  + " | 保质期:" + products[i].shelfLife + "个月\n";
    }

    // 计算总额
    float dayTotal = 0; int dayQty = 0;
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      dayTotal += products[i].price * products[i].todaySold;
      dayQty += products[i].todaySold;
    }
    productData += "\n今日总销售额:¥" + String(dayTotal, 2) + " | 总销量:" + String(dayQty) + "件";

    // 不同分析类型的提示词
    String prompt;
    if (type == "full") {
      prompt = productData
        + "\n\n你是一位拥有20年经验的超市运营总监。请根据以上数据，生成一份完整的《每日经营分析报告》，必须包含："
        + "\n1.【销售概况】用今日销售额和销量做总览"
        + "\n2.【商品排行】按销售额排名，指出TOP3畅销品和倒数3名滞销品，引用具体数据"
        + "\n3.【库存健康度】指出库存<10件的商品，计算库存天数是否合理"
        + "\n4.【临期预警】根据生产日期+保质期，指出已过期或30天内到期的商品"
        + "\n5.【进货建议】对库存不足或畅销的商品，给出具体建议进货数量（基于今日销量×7天安全库存）"
        + "\n6.【明日预测】基于今日数据预测明日需重点关注的商品"
        + "\n格式要清晰分点，每条建议都要引用具体数字，不得编造任何数据。控制在400字以内。";
    } else if (type == "restock") {
      prompt = productData
        + "\n\n你是一位资深采购经理。请根据以上数据，生成《智能进货建议单》："
        + "\n1. 计算每个商品的日销速度，标注哪些商品库存不足3天销量"
        + "\n2. 对库存<10件的商品给出紧急补货建议，含建议进货量"
        + "\n3. 对今日销量TOP3的商品，建议安全库存量和本次进货量"
        + "\n4. 识别滞销商品（今日0销量且库存>20），给出是否需要退货/促销的判断"
        + "\n每条建议引用具体数字，不得编造。控制在300字以内。";
    } else if (type == "promotion") {
      prompt = productData
        + "\n\n你是一位超市促销策划专家。请根据以上数据，生成《促销策略建议》："
        + "\n1. 找出临期商品（距到期不足60天），设计具体的打折方案（如买一送一、第二件半价、满减等）"
        + "\n2. 对滞销商品（今日销量为0或销量最低的3个），分析原因并给出促销组合策略"
        + "\n3. 对畅销品设计关联促销方案（如买A送B优惠券），提升客单价"
        + "\n4. 给出一个本周推荐的主推商品和理由"
        + "\n方案要具体可执行，引用实际商品名和价格。控制在300字以内。";
    } else if (type == "expiry") {
      prompt = productData
        + "\n\n你是一位食品安全与库存管理专家。请根据以上数据，生成《临期商品预警报告》："
        + "\n1. 逐一计算每个商品距离到期的剩余天数（生产日期+保质期 vs 当前日期）"
        + "\n2. 按紧急程度分级：🔴已过期 | 🟠30天内到期 | 🟡60天内到期 | 🟢安全"
        + "\n3. 对已过期商品给出立即下架建议"
        + "\n4. 对30天内到期商品，给出具体的清仓折扣方案（如5折、买赠等）"
        + "\n5. 统计临期商品潜在损失金额"
        + "\n格式清晰，分级明确。控制在300字以内。";
    } else {
      prompt = productData + "\n\n请对上述数据进行综合分析，给出关键发现和建议。控制在200字以内。";
    }

    String aiResult = AI_Analyze(prompt);
    request->send(200, "text/plain; charset=utf-8", aiResult);
  });

  // 手机扫码支付确认页
  server.on("/pay", HTTP_GET, [](AsyncWebServerRequest *request) {
    int orderId = request->hasParam("order") ? request->getParam("order")->value().toInt() : -1;
    float total = request->hasParam("t") ? request->getParam("t")->value().toFloat() : 0.0f;
    String html = R"payraw(
      <!DOCTYPE html>
      <html lang="zh-CN">
      <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
        <title>确认支付</title>
        <style>
          *{margin:0;padding:0;box-sizing:border-box}
          body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
          .card{background:white;border-radius:16px;padding:30px 24px;width:90%;max-width:360px;box-shadow:0 4px 20px rgba(0,0,0,0.08);text-align:center}
          .amount{font-size:36px;font-weight:700;color:#e74c3c;margin:16px 0}
          .order{font-size:13px;color:#999;margin-bottom:20px}
          .btn{display:block;width:100%;padding:14px;border:none;border-radius:12px;font-size:17px;font-weight:700;cursor:pointer;font-family:inherit;transition:all 0.2s;margin-bottom:10px}
          .btn-confirm{background:linear-gradient(135deg,#07c160,#06ad56);color:white;box-shadow:0 4px 12px rgba(7,193,96,0.3)}
          .btn-confirm:active{transform:scale(0.97)}
          .btn-back{background:#f5f5f5;color:#999}
          .success{color:#27ae60;font-size:18px;font-weight:700;display:none}
          .success.show{display:block}
          .hint{font-size:12px;color:#aaa;margin-top:12px}
        </style>
      </head>
      <body>
        <div class="card">
          <div style="font-size:40px;margin-bottom:8px">🛒</div>
          <div style="font-size:15px;color:#555">智慧超市</div>
          <div class="amount">¥)payraw" + String(total, 2) + R"payraw(</div>
          <div class="order">订单号 #)payraw" + String(orderId + 1) + R"payraw(</div>
          <div id="beforePay">
            <div style="font-size:13px;color:#666;margin-bottom:16px">确认支付上述金额？</div>
            <button class="btn btn-confirm" onclick="confirmPay()">✅ 确认支付</button>
            <button class="btn btn-back" onclick="history.back()">返回</button>
          </div>
          <div class="success" id="afterPay">
            <div style="font-size:48px;margin-bottom:8px">✓</div>
            支付确认成功
            <div class="hint">请返回收银台继续</div>
          </div>
        </div>
        <script>
          var orderId = )payraw" + String(orderId) + R"payraw(;
          async function confirmPay() {
            try {
              var r = await fetch('/api/mobile-pay?order=' + orderId);
              var d = await r.json();
              if (d.ok) {
                document.getElementById('beforePay').style.display = 'none';
                document.getElementById('afterPay').classList.add('show');
              }
            } catch(e) {}
          }
        </script>
      </body>
      </html>
    )payraw";
    request->send(200, "text/html; charset=utf-8", html);
  });

  // 手机确认支付 — 标记订单
  server.on("/api/mobile-pay", HTTP_GET, [](AsyncWebServerRequest *request) {
    int orderId = request->hasParam("order") ? request->getParam("order")->value().toInt() : -1;
    if (orderId < 0) { request->send(400, "application/json", "{\"ok\":false}"); return; }
    Product_SetPending(orderId);
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 轮询支付状态 — 收银台检测手机确认
  server.on("/api/pay-status", HTTP_GET, [](AsyncWebServerRequest *request) {
    int orderId = request->hasParam("order") ? request->getParam("order")->value().toInt() : -1;
    bool confirmed = Product_IsPending(orderId);
    String resp = "{\"confirmed\":" + String(confirmed ? "true" : "false") + ",\"order\":" + String(orderId) + "}";
    request->send(200, "application/json; charset=utf-8", resp);
  });

  // 退款/取消订单
  server.on("/api/admin/refund", HTTP_POST, [](AsyncWebServerRequest *request) {
    int orderId = request->hasParam("orderId", true) ? request->getParam("orderId", true)->value().toInt() : -1;
    if (orderId < 0 || !Product_IsPaid(orderId)) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"订单不存在或未支付\"}");
      return;
    }
    Product_RefundOrder(orderId);
    String user = getCookieVal(request, "admin_user");
    addOpLog(user.length()>0?user:"admin", "refund", "退款订单#" + String(orderId+1));
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 称重模块 — 实时重量 API
  server.on("/api/weight", HTTP_GET, [](AsyncWebServerRequest *request) {
    float w = Scale_GetWeight();
    String json = "{\"weight_g\":" + String(w, 1) + ",\"ok\":" + (w >= 0 ? "true" : "false") + "}";
    request->send(200, "application/json", json);
  });

  // ─── 旧树莓派扫码枪透传接口（兼容备用）──────────────────────
  server.on("/api/scan-gun", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (request->hasParam("code")) {
      WebServer_SubmitScanGunCode(request->getParam("code")->value(), "pi-http");
    } else {
      Serial.println("[ScanGun] POST but no 'code' param found");
    }
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 管理员页轮询扫码枪结果（返回后清除，避免泄露到客户页）
  server.on("/api/scan-gun-result", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "application/json; charset=utf-8", takeScanGunResult());
  });

  // XIAO OV3660 sends visual probabilities; this controller owns HX711 and performs fusion.
  server.on("/api/vision-fusion", HTTP_POST, [](AsyncWebServerRequest *request) {
    String observationId = request->hasParam("observation_id", true)
      ? request->getParam("observation_id", true)->value() : "";
    bool validObservationId = observationId.length() > 0 && observationId.length() <= 40;
    for (size_t i = 0; validObservationId && i < observationId.length(); i++) {
      char c = observationId[i];
      validObservationId = isAlphaNumeric(c) || c == '-' || c == '_';
    }
    if (!validObservationId) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"invalid observation_id\"}");
      return;
    }
    if (observationId == lastVisionObservationId && lastVisionObservationResponse.length() > 0) {
      request->send(200, "application/json; charset=utf-8", lastVisionObservationResponse);
      return;
    }
    float probabilities[3] = {
      request->hasParam("apple", true) ? request->getParam("apple", true)->value().toFloat() : 0,
      request->hasParam("banana", true) ? request->getParam("banana", true)->value().toFloat() : 0,
      request->hasParam("grapes", true) ? request->getParam("grapes", true)->value().toFloat() : 0
    };
    int sampleCount = request->hasParam("sample_count", true)
      ? request->getParam("sample_count", true)->value().toInt() : 0;
    float spread = request->hasParam("spread", true)
      ? request->getParam("spread", true)->value().toFloat() : 1.0f;
    float margin = request->hasParam("margin", true)
      ? request->getParam("margin", true)->value().toFloat() : 0;
    float probabilitySum = probabilities[0] + probabilities[1] + probabilities[2];
    bool validVision = sampleCount >= 5 && spread >= 0 && spread <= 0.25f && margin >= 0.12f &&
                       probabilitySum >= 0.80f && probabilitySum <= 1.20f;
    for (float probability : probabilities) {
      if (probability < 0 || probability > 1) validVision = false;
    }
    float grams = -1;
    bool stable = false;
    unsigned long scaleAgeMs = 0;
    bool scaleOk = Scale_GetSnapshot(&grams, &stable, &scaleAgeMs);
    int best = -1; float bestVision = 0, bestWeight = 0, bestFused = 0;
    for (int i = 0; i < 3; i++) {
      float weight = stable ? visionWeightScore(VISION_WEIGHT_RULES[i], grams) : 0;
      float fused = probabilities[i] * 0.78f + weight * 0.22f;
      if (fused > bestFused) { best = i; bestVision = probabilities[i]; bestWeight = weight; bestFused = fused; }
    }
    bool accepted = validVision && best >= 0 && grams >= 25 && scaleOk && stable &&
                    bestVision >= 0.55f && bestWeight >= 0.15f && bestFused >= 0.72f;
    const char *label = best >= 0 ? VISION_WEIGHT_RULES[best].label : "unknown";
    const char *name = accepted ? VISION_WEIGHT_RULES[best].name : "待确认";
    latestVisionFusion = "{\"ok\":true,\"observation_id\":\"" + observationId
      + "\",\"accepted\":" + String(accepted ? "true" : "false")
      + ",\"label\":\"" + label + "\",\"name\":\"" + name + "\",\"weight_g\":" + String(grams, 1)
      + ",\"stable\":" + String(stable ? "true" : "false") + ",\"vision\":" + String(bestVision, 4)
      + ",\"vision_stable\":" + String(validVision ? "true" : "false")
      + ",\"vision_spread\":" + String(spread, 4) + ",\"vision_margin\":" + String(margin, 4)
      + ",\"scale_age_ms\":" + String(scaleAgeMs)
      + ",\"weight_score\":" + String(bestWeight, 4) + ",\"confidence\":" + String(bestFused, 4) + "}";
    lastVisionObservationId = observationId;
    lastVisionObservationResponse = latestVisionFusion;
    Serial.println("[VisionFusion] " + latestVisionFusion);
    request->send(200, "application/json; charset=utf-8", latestVisionFusion);
  });

  server.on("/api/vision-fusion/latest", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "application/json; charset=utf-8", latestVisionFusion);
  });

  server.on("/api/service-request", HTTP_GET, [](AsyncWebServerRequest *request) {
    String json = "{\"request_id\":\"" + serviceRequestId + "\",\"status\":\"" + serviceRequestStatus
                + "\",\"terminal\":\"1号自助终端\",\"age_ms\":" + String(serviceRequestCreatedAt ? millis() - serviceRequestCreatedAt : 0)
                + ",\"response_ms\":" + String(serviceRequestAcceptedAt ? serviceRequestAcceptedAt - serviceRequestCreatedAt : 0)
                + ",\"resolution_ms\":" + String(serviceRequestCompletedAt ? serviceRequestCompletedAt - serviceRequestCreatedAt : 0)
                + ",\"escalated\":" + String(serviceRequestStatus == "waiting" && serviceRequestCreatedAt && millis() - serviceRequestCreatedAt >= 120000 ? "true" : "false") + "}";
    request->send(200, "application/json", json);
  });

  server.on("/api/customer/service-request", HTTP_POST, [](AsyncWebServerRequest *request) {
    String name = request->hasParam("name", true) ? request->getParam("name", true)->value() : "";
    String detail = request->hasParam("message", true) ? request->getParam("message", true)->value() : "";
    name.trim();
    detail.trim();
    if (name.length() == 0 || name.length() > 40 || detail.length() == 0 || detail.length() > 300) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"请填写问题，最多300字\"}");
      return;
    }
    if (serviceRequestStatus == "waiting" || serviceRequestStatus == "accepted") {
      request->send(409, "application/json", "{\"ok\":false,\"active\":true,\"request_id\":\"" + serviceRequestId +
                    "\",\"status\":\"" + serviceRequestStatus + "\",\"msg\":\"当前已有人工服务工单正在处理\"}");
      return;
    }

    String requestId = WebServer_CreateHumanServiceRequest();
    serviceRequestCustomer = name;
    serviceRequestDetail = detail;
    saveWorkflowState();
    bool notified = notifyMimiClawHumanService(requestId, name, detail);
    String json = "{\"ok\":true,\"request_id\":\"" + requestId +
                  "\",\"status\":\"waiting\",\"notified\":" + String(notified ? "true" : "false") + "}";
    request->send(notified ? 200 : 202, "application/json", json);
  });

  server.on("/api/service-request/update", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (!request->hasHeader("X-MimiClaw-Key") ||
        request->getHeader("X-MimiClaw-Key")->value() != MIMICLAW_STORE_KEY) {
      request->send(401, "application/json", "{\"ok\":false,\"msg\":\"unauthorized\"}");
      return;
    }
    String id = request->hasParam("request_id", true) ? request->getParam("request_id", true)->value() : "";
    String status = request->hasParam("status", true) ? request->getParam("status", true)->value() : "";
    bool validStatus = status == "accepted" || status == "completed" || status == "rejected";
    if (id.length() == 0 || id != serviceRequestId || !validStatus) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"invalid request or status\"}");
      return;
    }
    if (status == serviceRequestStatus) {
      request->send(200, "application/json", "{\"ok\":true,\"duplicate\":true,\"request_id\":\"" + id + "\",\"status\":\"" + status + "\"}");
      return;
    }
    bool validTransition = (serviceRequestStatus == "waiting" && (status == "accepted" || status == "rejected")) ||
                           (serviceRequestStatus == "accepted" && (status == "completed" || status == "rejected"));
    if (!validTransition) {
      request->send(409, "application/json", "{\"ok\":false,\"msg\":\"invalid ticket state transition\"}");
      return;
    }
    serviceRequestStatus = status;
    if (status == "accepted") serviceRequestAcceptedAt = millis();
    if (status == "completed" || status == "rejected") serviceRequestCompletedAt = millis();
    saveWorkflowState();
    addOpLog("feishu-merchant", "service-" + status,
             "工单:" + id + " 响应:" + String(serviceRequestAcceptedAt ? serviceRequestAcceptedAt - serviceRequestCreatedAt : 0) + "ms");
    Serial.printf("[人工服务] 工单 %s 状态更新: %s\n", id.c_str(), status.c_str());
    request->send(200, "application/json", "{\"ok\":true,\"request_id\":\"" + id + "\",\"status\":\"" + status + "\"}");
  });

  server.on("/api/admin/restock-review", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (!createRestockProposal()) {
      request->send(200, "application/json", "{\"ok\":true,\"proposal\":null,\"message\":\"inventory coverage is sufficient\"}");
      return;
    }
    String json = "{\"ok\":true,\"proposal_id\":\"" + restockProposalId
                + "\",\"code\":\"" + restockProposalCode + "\",\"name\":\"" + restockProposalName
                + "\",\"before_stock\":" + String(restockBeforeStock)
                + ",\"sold_today\":" + String(restockSoldToday)
                + ",\"coverage_days\":" + String(restockCoverageDays, 2)
                + ",\"recommended_qty\":" + String(restockRecommendedQty)
                + ",\"risk\":\"" + restockProposalRisk + "\",\"target_days\":3,\"status\":\"" + restockProposalStatus + "\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/api/admin/restock-proposal", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (restockProposalId.length() == 0) {
      request->send(200, "application/json", "{\"ok\":true,\"proposal\":null}");
      return;
    }
    String json = "{\"ok\":true,\"proposal_id\":\"" + restockProposalId
                + "\",\"code\":\"" + restockProposalCode + "\",\"name\":\"" + restockProposalName
                + "\",\"before_stock\":" + String(restockBeforeStock)
                + ",\"sold_today\":" + String(restockSoldToday)
                + ",\"coverage_days\":" + String(restockCoverageDays, 2)
                + ",\"recommended_qty\":" + String(restockRecommendedQty)
                + ",\"risk\":\"" + restockProposalRisk + "\",\"status\":\"" + restockProposalStatus + "\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/api/admin/restock-apply", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (!request->hasHeader("X-MimiClaw-Key") ||
        request->getHeader("X-MimiClaw-Key")->value() != MIMICLAW_STORE_KEY) {
      request->send(401, "application/json", "{\"ok\":false,\"msg\":\"unauthorized\"}");
      return;
    }
    String id = request->hasParam("proposal_id", true) ? request->getParam("proposal_id", true)->value() : "";
    if (id != restockProposalId || restockProposalStatus != "pending") {
      request->send(409, "application/json", "{\"ok\":false,\"msg\":\"proposal is stale or already applied\"}");
      return;
    }
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].qrCode != restockProposalCode) continue;
      if (products[i].stock != restockBeforeStock) {
        restockProposalStatus = "stale";
        saveWorkflowState();
        request->send(409, "application/json", "{\"ok\":false,\"msg\":\"stock changed since review\"}");
        return;
      }
      products[i].stock += restockRecommendedQty;
      Product_Save();
      int verifiedStock = products[i].stock;
      if (verifiedStock != restockBeforeStock + restockRecommendedQty) {
        request->send(500, "application/json", "{\"ok\":false,\"msg\":\"stock verification failed\"}");
        return;
      }
      restockProposalStatus = "applied";
      saveWorkflowState();
      addOpLog("feishu-merchant", "restock-apply",
               products[i].name + " " + String(restockBeforeStock) + " -> " + String(verifiedStock)
               + " 原因:" + restockProposalRisk + " 提案:" + id);
      String json = "{\"ok\":true,\"verified\":true,\"proposal_id\":\"" + id
                  + "\",\"name\":\"" + products[i].name + "\",\"before_stock\":" + String(restockBeforeStock)
                  + ",\"added\":" + String(restockRecommendedQty) + ",\"after_stock\":" + String(verifiedStock) + "}";
      request->send(200, "application/json; charset=utf-8", json);
      return;
    }
    request->send(404, "application/json", "{\"ok\":false,\"msg\":\"product not found\"}");
  });

  server.on("/api/admin/pricing-review", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (!createPricingProposal()) {
      request->send(200, "application/json", "{\"ok\":true,\"proposal\":null,\"message\":\"no products expiring within 60 days\"}");
      return;
    }
    String json = "{\"ok\":true,\"proposal_id\":\"" + pricingProposalId
                + "\",\"code\":\"" + pricingProposalCode + "\",\"name\":\"" + pricingProposalName
                + "\",\"old_price\":" + String(pricingOldPrice, 2)
                + ",\"recommended_price\":" + String(pricingNewPrice, 2)
                + ",\"days_left\":" + String(pricingDaysLeft)
                + ",\"status\":\"" + pricingProposalStatus
                + "\",\"reason\":\"expiry risk; discount follows the store 30/60-day policy\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/api/admin/pricing-proposal", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (pricingProposalId.length() == 0) {
      request->send(200, "application/json", "{\"ok\":true,\"proposal\":null}");
      return;
    }
    String json = "{\"ok\":true,\"proposal_id\":\"" + pricingProposalId
                + "\",\"code\":\"" + pricingProposalCode + "\",\"name\":\"" + pricingProposalName
                + "\",\"old_price\":" + String(pricingOldPrice, 2)
                + ",\"recommended_price\":" + String(pricingNewPrice, 2)
                + ",\"days_left\":" + String(pricingDaysLeft)
                + ",\"status\":\"" + pricingProposalStatus + "\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });

  server.on("/api/admin/pricing-apply", HTTP_POST, [](AsyncWebServerRequest *request) {
    if (!request->hasHeader("X-MimiClaw-Key") ||
        request->getHeader("X-MimiClaw-Key")->value() != MIMICLAW_STORE_KEY) {
      request->send(401, "application/json", "{\"ok\":false,\"msg\":\"unauthorized\"}");
      return;
    }
    String id = request->hasParam("proposal_id", true) ? request->getParam("proposal_id", true)->value() : "";
    if (id != pricingProposalId || pricingProposalStatus != "pending") {
      request->send(409, "application/json", "{\"ok\":false,\"msg\":\"proposal is stale or already applied\"}");
      return;
    }
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].qrCode == pricingProposalCode) {
        if (fabsf(products[i].price - pricingOldPrice) > 0.009f) {
          pricingProposalStatus = "stale";
          saveWorkflowState();
          request->send(409, "application/json", "{\"ok\":false,\"msg\":\"product price changed since review\"}");
          return;
        }
        products[i].price = pricingNewPrice;
        Product_Save();
        pricingProposalStatus = "applied";
        saveWorkflowState();
        addOpLog("miniclaw", "pricing-apply", products[i].name + " ¥" + String(pricingOldPrice, 2) + " -> ¥" + String(pricingNewPrice, 2));
        String json = "{\"ok\":true,\"proposal_id\":\"" + id + "\",\"name\":\"" + products[i].name
                    + "\",\"old_price\":" + String(pricingOldPrice, 2)
                    + ",\"new_price\":" + String(pricingNewPrice, 2) + ",\"verified\":true}";
        request->send(200, "application/json; charset=utf-8", json);
        return;
      }
    }
    request->send(404, "application/json", "{\"ok\":false,\"msg\":\"product not found\"}");
  });

  server.on("/api/scanner-status", HTTP_GET, [](AsyncWebServerRequest *request) {
    String response = String("{\"transport\":\"usb-host\",\"state\":\"")
                    + USB_BarcodeScanner_State() + "\",\"connected\":"
                    + (USB_BarcodeScanner_IsConnected() ? "true" : "false")
                    + ",\"dPlus\":20,\"dMinus\":19}";
    request->send(200, "application/json; charset=utf-8", response);
  });

  // 清除扫码枪缓存（管理员开始新扫描时调用）
  // ── 商家扫码互斥：防止顾客端抢摄像头（30秒自动释放）──
  static bool adminCamMode = false;
  static unsigned long adminCamStart = 0;
  server.on("/api/admin-cam-start", HTTP_GET, [](AsyncWebServerRequest *request) {
    adminCamMode = true; adminCamStart = millis();
    request->send(200, "application/json", "{\"ok\":true}");
  });
  server.on("/api/admin-cam-stop", HTTP_GET, [](AsyncWebServerRequest *request) {
    adminCamMode = false;
    request->send(200, "application/json", "{\"ok\":true}");
  });
  server.on("/api/cam-status", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (adminCamMode && millis() - adminCamStart > 30000) adminCamMode = false; // 30秒自动释放
    request->send(200, "application/json", String("{\"busy\":") + (adminCamMode ? "true" : "false") + "}");
  });

  server.on("/api/scan-gun-clear", HTTP_GET, [](AsyncWebServerRequest *request) {
    clearScanGunResult();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // 顾客页轮询扫码枪事件（返回后清除，避免同一个码被双方消费）
  server.on("/api/customer/scan-event", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "application/json; charset=utf-8", takeScanGunResult());
  });

  // ─── MimiClaw：按条码修改商品价格 ─────────────────────────
  server.on("/api/admin/update-price", HTTP_POST, [](AsyncWebServerRequest *request) {
    const char *apiKey = MIMICLAW_STORE_KEY;
    if (!request->hasHeader("X-MimiClaw-Key") ||
        request->getHeader("X-MimiClaw-Key")->value() != apiKey) {
      request->send(401, "application/json", "{\"ok\":false,\"msg\":\"unauthorized\"}");
      return;
    }

    String code = request->hasParam("code", true) ? request->getParam("code", true)->value() : "";
    float newPrice = request->hasParam("price", true) ? request->getParam("price", true)->value().toFloat() : 0;
    if (code.length() == 0 || newPrice <= 0 || newPrice > 99999) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"invalid code or price\"}");
      return;
    }

    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].qrCode == code) {
        float oldPrice = products[i].price;
        products[i].price = newPrice;
        Product_Save();
        addOpLog("miniclaw", "update-price", products[i].name + " ¥" + String(oldPrice, 2) + " -> ¥" + String(newPrice, 2));
        String result = "{\"ok\":true,\"code\":\"" + code + "\",\"name\":\"" + products[i].name
                      + "\",\"old_price\":" + String(oldPrice, 2)
                      + ",\"new_price\":" + String(newPrice, 2) + "}";
        request->send(200, "application/json", result);
        Serial.printf("[MimiClaw] price updated: %s %.2f -> %.2f\n", products[i].name.c_str(), oldPrice, newPrice);
        return;
      }
    }
    request->send(404, "application/json", "{\"ok\":false,\"msg\":\"product not found\"}");
  });

  // ─── 商家管理：进货接口 ───────────────────────────────────
  server.on("/api/admin/restock", HTTP_POST, [](AsyncWebServerRequest *request) {
    String code = request->hasParam("code", true) ? request->getParam("code", true)->value() : "";
    int qty = request->hasParam("qty", true) ? request->getParam("qty", true)->value().toInt() : 0;
    if (code.length() > 0 && qty > 0) {
      for (int i = 0; i < PRODUCT_COUNT; i++) {
        if (products[i].qrCode == code) {
          products[i].stock += qty;
          Product_Save();
          String loginUser = "unknown";
          if (request->hasHeader("Cookie")) { String c = request->getHeader("Cookie")->value(); int p = c.indexOf("admin_user="); if (p >= 0) { p += 11; int e = c.indexOf(';', p); if (e < 0) e = c.length(); loginUser = c.substring(p, e); } }
          addOpLog(loginUser, "restock", "进货:" + products[i].name + " +" + String(qty) + "件");
          Serial.printf("[Restock] %s +%d -> %d\n", products[i].name.c_str(), qty, products[i].stock);
          break;
        }
      }
    }
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // ─── 商家管理：新增商品接口 ───────────────────────────────
  server.on("/api/admin/add-product", HTTP_POST, [](AsyncWebServerRequest *request) {
    int slot = -1;
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].qrCode.length() == 0 || products[i].name.length() == 0) {
        slot = i; break;
      }
    }
    if (slot < 0) {
      request->send(400, "application/json", "{\"ok\":false,\"msg\":\"slot full\"}");
      return;
    }
    String code = request->hasParam("code", true) ? request->getParam("code", true)->value() : "";
    String name = request->hasParam("name", true) ? request->getParam("name", true)->value() : "";
    float price = request->hasParam("price", true) ? request->getParam("price", true)->value().toFloat() : 0;
    int stock = request->hasParam("stock", true) ? request->getParam("stock", true)->value().toInt() : 0;
    String mfg = request->hasParam("mfg", true) ? request->getParam("mfg", true)->value() : "2026-07-04";
    int life = request->hasParam("life", true) ? request->getParam("life", true)->value().toInt() : 12;
    String icon = request->hasParam("icon", true) ? request->getParam("icon", true)->value() : "📦";
    bool isWeigh = request->hasParam("weigh", true) && request->getParam("weigh", true)->value() == "1";
    if (code.length() > 0 && name.length() > 0 && price > 0) {
      products[slot].qrCode = code;
      products[slot].name = name;
      products[slot].price = price;
      products[slot].stock = stock;
      products[slot].mfgDate = mfg;
      products[slot].shelfLife = life;
      products[slot].icon = icon;
      products[slot].isWeigh = isWeigh;
      Product_Save();
      String loginUser = "unknown";
      if (request->hasHeader("Cookie")) { String c = request->getHeader("Cookie")->value(); int p = c.indexOf("admin_user="); if (p >= 0) { p += 11; int e = c.indexOf(';', p); if (e < 0) e = c.length(); loginUser = c.substring(p, e); } }
      addOpLog(loginUser, "add-product", "新增商品:" + name);
      Serial.printf("[AddProduct] slot %d: %s\n", slot, name.c_str());
    }
    request->send(200, "application/json", "{\"ok\":true,\"slot\":" + String(slot) + "}");
  });

  // ─── 商家管理：删除商品接口 ───────────────────────────────
  server.on("/api/admin/delete", HTTP_POST, [](AsyncWebServerRequest *request) {
    String code = request->hasParam("code", true) ? request->getParam("code", true)->value() : "";
    if (code.length() > 0) {
      for (int i = 0; i < PRODUCT_COUNT; i++) {
        if (products[i].qrCode == code) {
          Serial.printf("[Delete] %s removed\n", products[i].name.c_str());
          String name = products[i].name;
          products[i] = Product();
          Product_Save();
          String loginUser = "unknown";
          if (request->hasHeader("Cookie")) {
            String c = request->getHeader("Cookie")->value();
            int up = c.indexOf("admin_user=");
            if (up >= 0) { up += 11; int end = c.indexOf(';', up); if (end < 0) end = c.length(); loginUser = c.substring(up, end); }
          }
          addOpLog(loginUser, "delete", "删除商品:" + name);
          break;
        }
      }
    }
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // ── 店铺设置（按用户个性化）──
  server.on("/api/admin/storecfg", HTTP_GET, [](AsyncWebServerRequest *request) {
    String user = getCookieVal(request, "admin_user");
    if (user.length() == 0) user = "admin";
    if (!userStoreCfgs.count(user)) userStoreCfgs[user] = defaultCfg;
    StoreCfg& cfg = userStoreCfgs[user];
    String json = "{\"name\":\"" + cfg.name + "\",\"emoji\":\"" + cfg.emoji + "\",\"color\":\"" + cfg.color + "\"}";
    request->send(200, "application/json; charset=utf-8", json);
  });
  server.on("/api/admin/storecfg", HTTP_POST, [](AsyncWebServerRequest *request) {
    String user = getCookieVal(request, "admin_user");
    if (user.length() == 0) user = "admin";
    if (!userStoreCfgs.count(user)) userStoreCfgs[user] = defaultCfg;
    StoreCfg& cfg = userStoreCfgs[user];
    if (request->hasParam("name", true)) cfg.name = request->getParam("name", true)->value();
    if (request->hasParam("emoji", true)) cfg.emoji = request->getParam("emoji", true)->value();
    if (request->hasParam("color", true)) cfg.color = request->getParam("color", true)->value();
    saveStoreCfg();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // ── 操作日志 ──
  server.on("/api/admin/oplogs", HTTP_GET, [](AsyncWebServerRequest *request) {
    String j = "[";
    for (int i = 0; i < opLogCount; i++) { if (i > 0) j += ","; j += opLogs[i]; }
    j += "]";
    request->send(200, "application/json; charset=utf-8", j);
  });

  // Browser-rasterized receipt/report queue. The printer polls these routes,
  // so it remains private on the LAN and does not need a CJK font table.
  server.on("/api/printer/submit", HTTP_POST,
    [](AsyncWebServerRequest *request) {
      int width = request->hasParam("width") ? request->getParam("width")->value().toInt() : 0;
      int height = request->hasParam("height") ? request->getParam("height")->value().toInt() : 0;
      String kind = request->hasParam("kind") ? request->getParam("kind")->value() : "unknown";
      size_t expected = (size_t)width * height / 8;
      if (printJobReady) {
        SPIFFS.remove(PRINT_JOB_TEMP);
        request->send(409, "application/json", "{\"ok\":false,\"msg\":\"打印队列已有任务\"}");
        return;
      }
      if (!printUploadOk || width != 384 || height < 1 || height > 2400 ||
          printUploadExpected != expected || printUploadWritten != expected) {
        if (printUploadFile) printUploadFile.close();
        SPIFFS.remove(PRINT_JOB_TEMP);
        request->send(400, "application/json", "{\"ok\":false,\"msg\":\"位图尺寸或数据错误\"}");
        return;
      }
      if (SPIFFS.exists(PRINT_JOB_FILE)) SPIFFS.remove(PRINT_JOB_FILE);
      if (!SPIFFS.rename(PRINT_JOB_TEMP, PRINT_JOB_FILE)) {
        request->send(500, "application/json", "{\"ok\":false,\"msg\":\"打印任务保存失败\"}");
        return;
      }
      printJobId = (uint32_t)esp_random();
      if (printJobId == 0) printJobId = 1;
      printJobWidth = width; printJobHeight = height; printJobKind = kind.substring(0, 16);
      printJobReady = true; savePrintJobMeta();
      request->send(200, "application/json", "{\"ok\":true,\"job_id\":" + String(printJobId) + "}");
    },
    nullptr,
    [](AsyncWebServerRequest *request, uint8_t *data, size_t len, size_t index, size_t total) {
      if (index == 0) {
        printUploadOk = !printJobReady && total > 0 && total <= 115200;
        printUploadExpected = total; printUploadWritten = 0;
        SPIFFS.remove(PRINT_JOB_TEMP);
        if (printUploadOk) {
          printUploadFile = SPIFFS.open(PRINT_JOB_TEMP, "w");
          printUploadOk = (bool)printUploadFile;
        }
      }
      if (printUploadOk && printUploadFile) {
        size_t written = printUploadFile.write(data, len);
        printUploadWritten += written;
        if (written != len) printUploadOk = false;
      }
      if (index + len == total && printUploadFile) printUploadFile.close();
    });

  server.on("/api/printer/job-meta", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (!printJobReady || !SPIFFS.exists(PRINT_JOB_FILE)) {
      request->send(200, "application/json", "{\"ready\":false}"); return;
    }
    File job = SPIFFS.open(PRINT_JOB_FILE, "r");
    size_t bytes = job ? job.size() : 0; if (job) job.close();
    String json = "{\"ready\":true,\"id\":" + String(printJobId)
      + ",\"width\":" + String(printJobWidth) + ",\"height\":" + String(printJobHeight)
      + ",\"bytes\":" + String(bytes) + ",\"kind\":\"" + printJobKind + "\"}";
    request->send(200, "application/json", json);
  });

  server.on("/api/printer/job", HTTP_GET, [](AsyncWebServerRequest *request) {
    uint32_t id = request->hasParam("id") ? strtoul(request->getParam("id")->value().c_str(), nullptr, 10) : 0;
    if (!printJobReady || id != printJobId || !SPIFFS.exists(PRINT_JOB_FILE)) {
      request->send(404, "text/plain", "No print job"); return;
    }
    AsyncWebServerResponse *response = request->beginResponse(SPIFFS, PRINT_JOB_FILE, "application/octet-stream");
    response->addHeader("Cache-Control", "no-store"); request->send(response);
  });

  server.on("/api/printer/ack", HTTP_POST, [](AsyncWebServerRequest *request) {
    uint32_t id = request->hasParam("id") ? strtoul(request->getParam("id")->value().c_str(), nullptr, 10) : 0;
    bool ok = request->hasParam("ok") && request->getParam("ok")->value() == "1";
    if (!printJobReady || id != printJobId) {
      request->send(404, "application/json", "{\"ok\":false}"); return;
    }
    if (ok) {
      SPIFFS.remove(PRINT_JOB_FILE); printJobReady = false; savePrintJobMeta();
    }
    request->send(200, "application/json", "{\"ok\":true}");
  });

  // ── 日报导出 ──
  server.on("/api/admin/export-report", HTTP_GET, [](AsyncWebServerRequest *request) {
    String rpt = "══════════════════════════\n";
    rpt += "  " + userStoreCfgs["admin"].name + " 每日经营报告\n";
    rpt += "══════════════════════════\n\n";
    // 基本统计
    float totalRev = 0; int paidOrders = 0;
    String j = Product_OrderHistoryToJson();
    DynamicJsonDocument doc(4096);
    deserializeJson(doc, j);
    JsonArray arr = doc.as<JsonArray>();
    for (JsonVariant v : arr) {
      if (v["paid"].as<bool>()) { totalRev += v["total"].as<float>(); paidOrders++; }
    }
    rpt += "📊 今日营业额：¥" + String(totalRev, 2) + "\n";
    rpt += "📋 今日订单数：" + String(paidOrders) + "\n";
    rpt += "🛒 平均客单价：¥" + String(paidOrders > 0 ? totalRev / paidOrders : 0, 2) + "\n\n";
    // 商品销售排行
    rpt += "── 商品销售排行 ──\n";
    Product sorted[PRODUCT_COUNT];
    for (int i = 0; i < PRODUCT_COUNT; i++) sorted[i] = products[i];
    for (int i = 0; i < PRODUCT_COUNT - 1; i++)
      for (int k = i + 1; k < PRODUCT_COUNT; k++)
        if (sorted[k].todaySold > sorted[i].todaySold) { Product t = sorted[i]; sorted[i] = sorted[k]; sorted[k] = t; }
    for (int i = 0; i < 10; i++) {
      if (sorted[i].qrCode.length() == 0) continue;
      rpt += String(i + 1) + ". " + sorted[i].name + "  " + sorted[i].icon + "\n";
      rpt += "   销量：" + String(sorted[i].todaySold) + "件  销售额：¥" + String(sorted[i].price * sorted[i].todaySold, 2) + "  库存：" + String(sorted[i].stock) + "\n";
    }
    // 补货预警
    rpt += "\n── 补货预警 ──\n";
    bool hasAlert = false;
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].qrCode.length() == 0) continue;
      int rate = products[i].todaySold < 1 ? 1 : products[i].todaySold;
      int days = products[i].stock / rate;
      if (days < 3 && products[i].stock > 0) {
        rpt += "⚠ " + products[i].name + " 仅剩" + String(days) + "天库存（库存" + String(products[i].stock) + "件）\n";
        hasAlert = true;
      }
    }
    if (!hasAlert) rpt += "✅ 库存充足\n";
    // 7天趋势
    rpt += "\n── 近7天趋势 ──\n";
    for (int i = 0; i < 7; i++)
      rpt += "D" + String(i + 1) + ": ¥" + String(dailyStats[i].revenue, 2) + " / " + String(dailyStats[i].itemsSold) + "件\n";
    rpt += "\n报告生成时间：" + String(millis() / 3600000) + "h\n";
    AsyncWebServerResponse *resp = request->beginResponse(200, "text/plain; charset=utf-8", rpt);
    resp->addHeader("Content-Disposition", "attachment; filename=report.txt");
    request->send(resp);
  });

  // 重定向到树莓派
  server.on("/login", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->redirect(String(PI_URL) + "/login");
  });
  // ==================== 超市大屏端 ====================
  server.on("/bigscreen", HTTP_GET, [](AsyncWebServerRequest *request) {
    String html = R"bigraw(
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智慧超市促销大屏</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
/* B端暗黑数据看板主题 */
body{font-family:'Inter','PingFang SC','Microsoft YaHei',sans-serif;background:#0B1437;color:#FFFFFF;overflow:hidden;width:100vw;height:100vh;cursor:pointer}

.slide{position:absolute;top:0;left:0;width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;opacity:0;transition:opacity 0.8s;padding:60px;padding-bottom:120px;}
.slide.active{opacity:1}

/* 标题防遮挡设计：限制最大宽度并靠左 */
.slide-title{
  font-size:56px; font-weight:900; margin-bottom:40px; 
  width:100%; max-width:1400px; text-align:left;
  padding-right: 450px; /* 强制给右上角的时钟留出绝对安全空间 */
  background:linear-gradient(90deg, #7551FF, #39B8FF);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}

/* 热销卡片：深色微透视质感 */
.rec-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:30px;width:100%;max-width:1400px}
.rec-card{background:#111C44;border-radius:24px;padding:50px 30px;text-align:center;box-shadow:0 18px 40px rgba(0,0,0,0.2);border:1px solid rgba(255,255,255,0.05);position:relative}
.rec-card .icon{font-size:80px;margin-bottom:20px;filter:drop-shadow(0 10px 10px rgba(0,0,0,0.3))}
.rec-card .name{font-size:36px;font-weight:800;margin-bottom:12px;color:#FFFFFF}
.rec-card .price{font-size:50px;font-weight:900;color:#00E396}
.rec-card .tag{position:absolute;top:20px;right:20px;background:rgba(117,81,255,0.2);color:#7551FF;padding:6px 16px;border-radius:20px;font-size:18px;font-weight:700}

/* 临期列表：醒目的警告配色 */
.expiry-list{width:100%;max-width:1400px;display:flex;flex-direction:column;gap:16px}
.expiry-item{display:flex;align-items:center;justify-content:space-between;background:#111C44;border-radius:20px;padding:24px 40px;border-left:8px solid #FF5B5B;box-shadow:0 18px 40px rgba(0,0,0,0.2)}
.expiry-item .left{display:flex;align-items:center;gap:24px}
.expiry-item .icon{font-size:56px}
.expiry-item .name{font-size:36px;font-weight:800;color:#FFFFFF}
.expiry-item .days{font-size:24px;color:#FF9F43;font-weight:700;background:rgba(255,159,67,0.1);padding:6px 16px;border-radius:12px}
.expiry-item .price-row{text-align:right}
.expiry-item .old-price{font-size:24px;color:#A3AED0;text-decoration:line-through;margin-bottom:4px}
.expiry-item .new-price{font-size:48px;font-weight:900;color:#FF5B5B}
.expiry-item .discount{display:inline-block;background:#FF5B5B;color:white;padding:6px 16px;border-radius:12px;font-size:20px;font-weight:800;margin-left:16px;vertical-align:middle}

/* 数据统计区 */
.stats-row{display:flex;gap:30px;width:100%;max-width:1400px}
.stat-box{flex:1;text-align:center;padding:50px;background:#111C44;border-radius:24px;box-shadow:0 18px 40px rgba(0,0,0,0.2);border:1px solid rgba(255,255,255,0.05)}
.stat-box .num{font-size:80px;font-weight:900;margin-bottom:10px}
.stat-box .num.green{color:#00E396}
.stat-box .num.blue{color:#39B8FF}
.stat-box .num.orange{color:#7551FF}
.stat-box .lbl{font-size:28px;color:#A3AED0;font-weight:600}

/* 右上角高级悬浮组件 */
.top-widget {
  position:fixed; top:30px; right:40px; z-index:999;
  display:flex; align-items:center; gap:20px;
  background:rgba(17,28,68,0.85); padding:16px 28px;
  border-radius:20px; border:1px solid rgba(255,255,255,0.1);
  box-shadow:0 10px 30px rgba(0,0,0,0.4); backdrop-filter:blur(10px);
}
.top-widget .clock-text { font-size:32px; font-weight:800; color:#FFFFFF; letter-spacing:1px; }
.top-widget .divider { width:2px; height:28px; background:rgba(255,255,255,0.15); }
.top-widget .env-text { font-size:20px; font-weight:700; min-width:180px; text-align:center; transition:color 0.4s; }

.hint{position:fixed;bottom:90px;left:50%;transform:translateX(-50%);color:#A3AED0;font-size:20px;font-weight:600;animation:fade 2s infinite;z-index:10;background:rgba(17,28,68,0.8);padding:10px 30px;border-radius:20px;backdrop-filter:blur(5px)}
@keyframes fade{0%,100%{opacity:0.4}50%{opacity:1}}
</style>
</head>
<body>

<div class="top-widget">
  <div class="clock-text" id="clock">00:00:00</div>
  <div class="divider"></div>
  <div class="env-text" id="envDisplay">环境监测中...</div>
</div>

<div class="slide active" id="slide-recommend">
  <div class="slide-title">🔥 今日热销推荐</div>
  <div class="rec-grid" id="hotGrid"></div>
</div>

<div class="slide" id="slide-expiry">
  <div class="slide-title">⏰ 临期特价清仓</div>
  <div class="expiry-list" id="expiryList"></div>
</div>

<div class="slide" id="slide-stats">
  <div class="slide-title">📊 今日战报</div>
  <div class="stats-row" id="statsRow"></div>
</div>

<div class="hint">👆 点击屏幕进入智能收银与导购终端</div>

<script>
// 总共只保留3个轮播界面
var products=[], currentSlide=0, totalSlides=3; 
var envTemp = "--", envHum = "--";

// 拉取温湿度数据
async function fetchEnv() {
  try {
    let r = await fetch('/api/env');
    let d = await r.json();
    envTemp = d.t.toFixed(1);
    envHum = d.h.toFixed(1);
  } catch(e) {}
}

// 刷新右上角小组件（时间 + 轮播天气/温湿度）
function updateWidget() {
  var d = new Date();
  document.getElementById('clock').textContent = 
    String(d.getHours()).padStart(2,'0') + ':' + 
    String(d.getMinutes()).padStart(2,'0') + ':' + 
    String(d.getSeconds()).padStart(2,'0');

  // 每 10 秒切换一次显示内容
  let sec = d.getSeconds();
  let el = document.getElementById('envDisplay');
  if (sec % 10 < 5) {
    el.innerHTML = '🌡️ ' + envTemp + '℃ &nbsp; 💧 ' + envHum + '%';
    el.style.color = '#00E396'; 
  } else {
    el.innerHTML = '📍 智慧门店 &nbsp; 🌤️ 晴朗'; // 修改这里！安全又高级
    el.style.color = '#39B8FF'; 
  }
}

function nextSlide(){
  document.querySelectorAll('.slide').forEach(function(s){s.classList.remove('active')});
  document.querySelectorAll('.slide')[currentSlide].classList.add('active');
  currentSlide=(currentSlide+1)%totalSlides;
}

async function loadData(){
  try{var r=await fetch('/api/products');products=await r.json();}catch(e){}
  renderHot(); renderExpiry(); renderStats();
}

function renderHot(){
  var sorted=products.slice().sort(function(a,b){return (b.today_sold||0)-(a.today_sold||0);}).slice(0,3);
  var h='';
  sorted.forEach(function(p){
    if(p.name && p.qr_code){
      h+='<div class="rec-card"><div class="tag">已售 '+(p.today_sold||0)+' 件</div><div class="icon">'+p.icon+'</div><div class="name">'+p.name+'</div><div class="price">¥'+p.price.toFixed(2)+'</div></div>';
    }
  });
  if(!h)h='<div style="font-size:36px;color:#A3AED0;text-align:center;width:100%;grid-column:1/-1;">等待商品数据...</div>';
  document.getElementById('hotGrid').innerHTML=h;
}

function renderExpiry(){
  var now=new Date(),list=[];
  products.forEach(function(p){
    var parts=(p.mfg_date||'').split('-');
    if(parts.length<3 || !p.name)return;
    var mfg=new Date(parseInt(parts[0]),parseInt(parts[1])-1,parseInt(parts[2]));
    var exp=new Date(mfg);exp.setMonth(exp.getMonth()+(p.shelf_life||12));
    var days=Math.ceil((exp-now)/(86400000));
    if(days<=60&&days>=-30){
      var discount=days<=30?0.5:0.7;
      list.push({name:p.name,icon:p.icon,price:p.price,days:days,discount:discount});
    }
  });
  list.sort(function(a,b){return a.days-b.days;});
  var h='';
  list.forEach(function(p){
    var newPrice=(p.price*p.discount).toFixed(2);
    h+='<div class="expiry-item"><div class="left"><span class="icon">'+p.icon+'</span><span class="name">'+p.name+'</span><span class="days">'+(p.days>0?'剩'+p.days+'天':'已过期')+'</span></div><div class="price-row"><div class="old-price">¥'+p.price.toFixed(2)+'</div><div class="new-price">¥'+newPrice+'<span class="discount">'+Math.round(p.discount*10)+'折</span></div></div></div>';
  });
  if(!h)h='<div style="font-size:36px;color:#00E396;text-align:center;margin-top:40px;width:100%">✅ 暂无临期商品，库存健康</div>';
  document.getElementById('expiryList').innerHTML=h;
}

function renderStats(){
  var total=0,items=0,vCount=0;
  products.forEach(function(p){
    if(p.name && p.qr_code){
      total+=p.price*(p.today_sold||0);
      items+=(p.today_sold||0);
      vCount++;
    }
  });
  document.getElementById('statsRow').innerHTML=
    '<div class="stat-box"><div class="num green">¥'+total.toFixed(0)+'</div><div class="lbl">今日销售额</div></div>'+
    '<div class="stat-box"><div class="num blue">'+items+'</div><div class="lbl">售出件数</div></div>'+
    '<div class="stat-box"><div class="num orange">'+vCount+'</div><div class="lbl">商品品类</div></div>';
}

// 启动定时器与初始化
setInterval(updateWidget, 1000);
updateWidget();

fetchEnv();
setInterval(fetchEnv, 5000);

loadData();
setInterval(loadData, 30000);

nextSlide();
setInterval(nextSlide, 8000);

// 点击进入顾客页面
document.addEventListener('click',function(){window.location.href='/';});
document.addEventListener('touchstart',function(){window.location.href='/';});
</script>
</body>
</html>
    )bigraw";
    request->send(200, "text/html; charset=utf-8", html);
  });

  ElegantOTA.begin(&server);

  server.begin();
  Serial.println("Web服务器已启动！");
}

#endif  // !SLAVE_MODE
