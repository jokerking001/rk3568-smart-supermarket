// ============================================================
//  ⚠️ 本文件属于**原主控形态**，从机模式（SLAVE_MODE=1）下整段不编译。
// ============================================================
//  RK3568 接管主控后，这个模块的职责已经搬到板端：
//      本地商品目录（SPIFFS） → 8094 的 SQLite
//  保留代码是为了 SLAVE_MODE=0 时能原样回退，不是从机固件的一部分。
//
//  为什么用 #if 而不是把文件挪走：Arduino 编译 sketch 目录下所有 .cpp，
//  挪走会让 SLAVE_MODE=0 也编不过；加保护则两套形态共存、互不干扰。
// ============================================================
#include "Slave_Config.h"

#if !SLAVE_MODE

#include "Product_Data.h"
#include <SPIFFS.h>
#include <ArduinoJson.h>
#include <time.h>

Product products[PRODUCT_COUNT];

#define MAX_ORDERS 20
struct Order {
  String items;
  float total;
  String time;
  bool paid;
  String method;
};
static Order orders[MAX_ORDERS];
static int orderCount = 0;
static bool orderPending[MAX_ORDERS] = {false};

static bool saveJsonRecoverably(const char *path, const char *tempPath, const char *backupPath,
                                JsonDocument &doc) {
  SPIFFS.remove(tempPath);
  File temp = SPIFFS.open(tempPath, "w");
  if (!temp) return false;
  bool written = serializeJson(doc, temp) > 0;
  temp.flush();
  temp.close();
  if (!written) { SPIFFS.remove(tempPath); return false; }

  SPIFFS.remove(backupPath);
  if (SPIFFS.exists(path) && !SPIFFS.rename(path, backupPath)) {
    SPIFFS.remove(tempPath);
    return false;
  }
  if (!SPIFFS.rename(tempPath, path)) {
    if (SPIFFS.exists(backupPath)) SPIFFS.rename(backupPath, path);
    return false;
  }
  SPIFFS.remove(backupPath);
  return true;
}

static void recoverJsonFile(const char *path, const char *backupPath) {
  if (!SPIFFS.exists(path) && SPIFFS.exists(backupPath)) SPIFFS.rename(backupPath, path);
}

static void saveOrders() {
  DynamicJsonDocument doc(8192);
  JsonArray arr = doc.to<JsonArray>();
  for (int i = 0; i < orderCount; i++) {
    JsonObject obj = arr.createNestedObject();
    obj["it"] = orders[i].items; obj["t"] = orders[i].total;
    obj["tm"] = orders[i].time; obj["p"] = orders[i].paid;
    obj["m"] = orders[i].method;
    obj["pd"] = orderPending[i];
  }
  if (!saveJsonRecoverably("/orders.json", "/orders.tmp", "/orders.bak", doc)) {
    Serial.println("订单数据保存失败，已保留上一版本");
  }
}
static void loadOrders() {
  recoverJsonFile("/orders.json", "/orders.bak");
  if (!SPIFFS.exists("/orders.json")) return;
  File f = SPIFFS.open("/orders.json", "r");
  if (!f) return;
  DynamicJsonDocument doc(8192);
  if (deserializeJson(doc, f)) { f.close(); return; }
  f.close();
  JsonArray arr = doc.as<JsonArray>();
  orderCount = min((int)arr.size(), MAX_ORDERS);
  for (int i = 0; i < orderCount; i++) {
    orders[i].items = arr[i]["it"].as<String>();
    orders[i].total = arr[i]["t"].as<float>();
    orders[i].time  = arr[i]["tm"].as<String>();
    orders[i].paid   = arr[i]["p"].as<bool>();
    orders[i].method = arr[i]["m"].as<String>();
    orderPending[i] = arr[i]["pd"] | false;
  }
}

static void saveProducts() {
  DynamicJsonDocument doc(16384);
  JsonArray arr = doc.to<JsonArray>();
  for (int i = 0; i < PRODUCT_COUNT; i++) {
    JsonObject obj = arr.createNestedObject();
    obj["c"] = products[i].qrCode;
    obj["n"] = products[i].name;
    obj["p"] = products[i].price;
    obj["s"] = products[i].stock;
    obj["t"] = products[i].todaySold;
    obj["m"] = products[i].mfgDate;
    obj["l"] = products[i].shelfLife;
    obj["i"] = products[i].icon;
    obj["w"] = products[i].isWeigh;
  }
  if (!saveJsonRecoverably("/products.json", "/products.tmp", "/products.bak", doc)) {
    Serial.println("商品数据保存失败，已保留上一版本");
  }
}

static void loadProducts() {
  recoverJsonFile("/products.json", "/products.bak");
  if (!SPIFFS.exists("/products.json")) return;
  File f = SPIFFS.open("/products.json", "r");
  if (!f) return;
  DynamicJsonDocument doc(16384);
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) return;
  JsonArray arr = doc.as<JsonArray>();
  for (int i = 0; i < PRODUCT_COUNT && i < arr.size(); i++) {
    products[i].qrCode = arr[i]["c"].as<String>();
    products[i].name = arr[i]["n"].as<String>();
    products[i].price = arr[i]["p"].as<float>();
    products[i].stock = arr[i]["s"].as<int>();
    products[i].todaySold = arr[i]["t"].as<int>();
    products[i].mfgDate = arr[i]["m"].as<String>();
    products[i].shelfLife = arr[i]["l"].as<int>();
    products[i].icon = arr[i]["i"].as<String>();
    products[i].isWeigh = arr[i]["w"].as<bool>();
  }
}

void Product_Save() { saveProducts(); }

void Product_Data_Init() {
  SPIFFS.begin(true);
  // 先尝试从SPIFFS加载
  loadProducts();
  // 如果全部为空，加载默认数据
  bool allEmpty = true;
  for (int i = 0; i < 8; i++) {
    if (products[i].qrCode.length() > 0) { allEmpty = false; break; }
  }
  if (allEmpty) {
    products[0] = {"6901234567890", "可口可乐 330ml",  3.50,  48, 15, "2026-03-15", 12, "\xF0\x9F\xA5\xA4", false};
    products[1] = {"6901234567891", "德芙巧克力 80g",  15.90,  3,  8, "2026-01-20", 18, "\xF0\x9F\x8D\xAB", false};
    products[2] = {"6901234567892", "康师傅牛肉面",     4.50,  35, 22, "2026-02-20",  6, "\xF0\x9F\x8D\x9C", false};
    products[3] = {"6901234567893", "维达抽纸 3包装",  12.90, 18,  5, "2026-05-01", 36, "\xF0\x9F\xA7\xBB", false};
    products[4] = {"6901234567894", "农夫山泉 550ml",   2.00,  60, 25, "2026-04-10", 12, "\xF0\x9F\x92\xA7", false};
    products[5] = {"6901234567895", "乐事薯片 75g",     7.50,  22, 12, "2026-02-28",  9, "\xF0\x9F\x8D\x9F", false};
    products[6] = {"6901234567896", "蒙牛纯牛奶 250ml", 3.80,  40, 18, "2026-05-15",  6, "\xF0\x9F\xA5\x9B", false};
    products[7] = {"6901234567897", "奥利奥饼干 97g",   9.90,  15,  9, "2026-03-20", 12, "\xF0\x9F\x8D\xAA", false};
    saveProducts();
  }
  loadOrders();
}

const Product* Product_FindByQR(const String& qrCode) {
  for (int i = 0; i < PRODUCT_COUNT; i++)
    if (products[i].qrCode == qrCode) return &products[i];
  return NULL;
}

String Product_Data_ToText() {
  String t = "请严格根据以下数据逐条分析，不得编造任何数字：\n";
  for (int i = 0; i < PRODUCT_COUNT; i++) {
    t += products[i].name + "：单价" + String(products[i].price, 2)
      + "元，库存" + products[i].stock + "件，今日售出" + products[i].todaySold
      + "件，生产日期" + products[i].mfgDate + "，保质期" + products[i].shelfLife + "个月。\n";
  }
  t += "要求：1.指出需补货商品(引用库存)；2.指出快过期需促销商品(引用日期)；3.指出畅销商品(引用销量)。不得编造数字。";
  return t;
}

bool Product_DeductStock(const String& qrCode, int qty) {
  for (int i = 0; i < PRODUCT_COUNT; i++) {
    if (products[i].qrCode == qrCode) {
      if (products[i].stock < qty) return false;
      products[i].stock -= qty;
      return true;
    }
  }
  return false;
}

void Product_AddSold(const String& qrCode, int qty) {
  for (int i = 0; i < PRODUCT_COUNT; i++) {
    if (products[i].qrCode == qrCode) {
      products[i].todaySold += qty;
      return;
    }
  }
}

int Product_CreateOrder(const String& items, float total, const String& method) {
  if (orderCount >= MAX_ORDERS) {
    for (int i = 0; i < MAX_ORDERS - 1; i++) {
      orders[i] = orders[i + 1];
      orderPending[i] = orderPending[i + 1];
    }
    orderCount = MAX_ORDERS - 1;
  }
  orders[orderCount].items = items;
  orders[orderCount].total = total;
  struct tm ti;
  char tb[20];
  if (getLocalTime(&ti, 0)) snprintf(tb, sizeof(tb), "%02d:%02d:%02d", ti.tm_hour, ti.tm_min, ti.tm_sec);
  else snprintf(tb, sizeof(tb), "--:--:--");
  orders[orderCount].time = String(tb);
  orders[orderCount].paid = false;
  orders[orderCount].method = method;
  orderPending[orderCount] = false;
  orderCount++;
  saveOrders();
  return orderCount - 1;
}

bool Product_ConfirmOrder(int orderId) {
  if (orderId < 0 || orderId >= orderCount) return false;
  if (orders[orderId].paid) return false;
  orders[orderId].paid = true;
  orderPending[orderId] = false;
  saveOrders();
  return true;
}

void Product_SetPending(int orderId) {
  if (orderId >= 0 && orderId < orderCount) {
    orderPending[orderId] = true;
    saveOrders();
  }
}

bool Product_IsPending(int orderId) {
  if (orderId < 0 || orderId >= orderCount) return false;
  return orderPending[orderId];
}

bool Product_IsPaid(int orderId) {
  if (orderId < 0 || orderId >= orderCount) return false;
  return orders[orderId].paid;
}

void Product_RefundOrder(int orderId) {
  if (orderId < 0 || orderId >= orderCount || !orders[orderId].paid) return;
  // 解析items恢复库存：格式 "商品名x数量, 商品名x数量"
  String it = orders[orderId].items;
  int pos = 0;
  while (pos < it.length()) {
    int xPos = it.indexOf('x', pos);
    if (xPos < 0) break;
    String name = it.substring(pos, xPos); name.trim();
    int end = it.indexOf(',', xPos);
    if (end < 0) end = it.length();
    int qty = it.substring(xPos + 1, end).toInt();
    // 找到对应商品，恢复库存
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].name == name) { products[i].stock += qty; break; }
    }
    pos = end + 1;
    if (pos <= 0) break;
  }
  orders[orderId].paid = false;
  orderPending[orderId] = false;
  saveOrders();
  saveProducts();
}

String Product_OrderHistoryToJson() {
  String j = "[";
  for (int i = 0; i < orderCount; i++) {
    if (i > 0) j += ",";
    j += "{\"items\":\"" + orders[i].items + "\",\"total\":" + String(orders[i].total, 2)
       + ",\"time\":\"" + orders[i].time + "\",\"paid\":" + (orders[i].paid ? "true" : "false")
       + ",\"method\":\"" + orders[i].method + "\"}";
  }
  j += "]";
  return j;
}

// ─── 每日统计（7天趋势图 + 动态补货预警）────────────────
DailyStat dailyStats[7] = {{0,0}};
static unsigned long lastDayNumber = 0;

static void saveDailyStats() {
  DynamicJsonDocument doc(1024);
  doc["dn"] = lastDayNumber;
  JsonArray arr = doc.createNestedArray("ds");
  for (int i = 0; i < 7; i++) {
    JsonObject obj = arr.createNestedObject();
    obj["r"] = dailyStats[i].revenue;
    obj["s"] = dailyStats[i].itemsSold;
  }
  File f = SPIFFS.open("/dailystats.json", "w");
  if (f) { serializeJson(doc, f); f.close(); }
}

static void loadDailyStats() {
  if (!SPIFFS.exists("/dailystats.json")) return;
  File f = SPIFFS.open("/dailystats.json", "r");
  if (!f) return;
  DynamicJsonDocument doc(1024);
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) return;
  lastDayNumber = doc["dn"].as<unsigned long>();
  JsonArray arr = doc["ds"].as<JsonArray>();
  for (int i = 0; i < 7 && i < arr.size(); i++) {
    dailyStats[i].revenue = arr[i]["r"].as<float>();
    dailyStats[i].itemsSold = arr[i]["s"].as<int>();
  }
}

void DailyStats_Init() {
  loadDailyStats();
  // 只用NTP真实日期判断是否跨天，同一天内重启（看门狗/掉线等）不再清空当天数据
  struct tm ti;
  if (!getLocalTime(&ti, 0)) return; // NTP未就绪，先保留已加载的数据，什么都不做

  unsigned long currentDay = ti.tm_year * 366UL + ti.tm_yday;
  if (lastDayNumber == 0) {
    // 从未记录过天数（首次运行/旧数据），直接以今天为准，不移位、不清空
    lastDayNumber = currentDay;
    saveDailyStats();
  } else if (currentDay != lastDayNumber) {
    // 真正跨天了才移位清零
    for (int i = 6; i > 0; i--) dailyStats[i] = dailyStats[i-1];
    dailyStats[0] = {0, 0};
    lastDayNumber = currentDay;
    saveDailyStats();
  }
  // 否则：同一天内重启，dailyStats保持loadDailyStats()加载的原值，不做任何操作
}

void DailyStats_Reset() {
  struct tm ti;
  if (!getLocalTime(&ti, 0)) return; // NTP未就绪，不重置
  for (int i = 0; i < 7; i++) dailyStats[i] = {0, 0};
  lastDayNumber = ti.tm_year * 366UL + ti.tm_yday;
  saveDailyStats();
}

void DailyStats_AddSale(float amount, int qty) {
  struct tm ti;
  if (!getLocalTime(&ti, 0)) {
    // NTP未就绪，不切换天，直接累加到今天
    dailyStats[0].revenue += amount;
    dailyStats[0].itemsSold += qty;
    saveDailyStats();
    return;
  }
  unsigned long currentDay = ti.tm_year * 366UL + ti.tm_yday;
  if (currentDay != lastDayNumber) {
    for (int i = 6; i > 0; i--) dailyStats[i] = dailyStats[i-1];
    dailyStats[0] = {0, 0};
    // 所有商品今日销量归零
    for (int i = 0; i < PRODUCT_COUNT; i++) products[i].todaySold = 0;
    saveProducts();
    lastDayNumber = currentDay;
  }
  dailyStats[0].revenue += amount;
  dailyStats[0].itemsSold += qty;
  saveDailyStats();
  Serial.printf("[DailyStats] 累计: ¥%.2f (%d件)\n", dailyStats[0].revenue, dailyStats[0].itemsSold);
}

String DailyStats_ToJson() {
  // 基于NTP真实时间生成最近7天日期标签
  String labels[7];
  struct tm ti;
  if (getLocalTime(&ti, 0)) {
    time_t now = mktime(&ti);
    for (int d = 6; d >= 0; d--) {
      time_t day = now - d * 86400;
      struct tm* lt = localtime(&day);
      char buf[8];
      snprintf(buf, sizeof(buf), "%d/%d", lt->tm_mon+1, lt->tm_mday);
      labels[6-d] = String(buf);
    }
  } else {
    const char* fallback[] = {"6天前","5天前","4天前","3天前","2天前","昨天","今天"};
    for (int i = 0; i < 7; i++) labels[i] = fallback[i];
  }
  String j = "[";
  for (int i = 6; i >= 0; i--) {
    if (i < 6) j += ",";
    j += "{\"label\":\"" + labels[6-i] + "\",\"r\":" + String(dailyStats[i].revenue, 2) + ",\"s\":" + String(dailyStats[i].itemsSold) + "}";
  }
  j += "]";
  return j;
}

#endif  // !SLAVE_MODE
