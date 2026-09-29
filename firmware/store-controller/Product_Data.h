#ifndef PRODUCT_DATA_H
#define PRODUCT_DATA_H

#include <Arduino.h>

struct Product {
  String qrCode;
  String name;
  float price;
  int stock;
  int todaySold;
  String mfgDate;
  int shelfLife;
  String icon;
  bool isWeigh;
};

#define PRODUCT_COUNT 50
extern Product products[PRODUCT_COUNT];

// 每日销售统计（7天趋势图用）
struct DailyStat {
  float revenue;
  int itemsSold;
};
extern DailyStat dailyStats[7];

void Product_Data_Init();
void Product_Save();
void DailyStats_Init();
void DailyStats_Reset();
void DailyStats_AddSale(float amount, int qty);
String DailyStats_ToJson();
String Product_Data_ToText();
const Product* Product_FindByQR(const String& qrCode);
bool Product_DeductStock(const String& qrCode, int qty);
void Product_AddSold(const String& qrCode, int qty);
int Product_CreateOrder(const String& items, float total, const String& method = "cash");
bool Product_ConfirmOrder(int orderId);
void Product_SetPending(int orderId);
bool Product_IsPending(int orderId);
bool Product_IsPaid(int orderId);
void Product_RefundOrder(int orderId);
String Product_OrderHistoryToJson();

#endif
