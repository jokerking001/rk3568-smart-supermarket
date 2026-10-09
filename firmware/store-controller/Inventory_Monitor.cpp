// ============================================================
//  ⚠️ 本文件属于**原主控形态**，从机模式（SLAVE_MODE=1）下整段不编译。
// ============================================================
//  RK3568 接管主控后，这个模块的职责已经搬到板端：
//      库存监控 → 8094 的库存表
//  保留代码是为了 SLAVE_MODE=0 时能原样回退，不是从机固件的一部分。
//
//  为什么用 #if 而不是把文件挪走：Arduino 编译 sketch 目录下所有 .cpp，
//  挪走会让 SLAVE_MODE=0 也编不过；加保护则两套形态共存、互不干扰。
// ============================================================
#include "Slave_Config.h"

#if !SLAVE_MODE

#include "Inventory_Monitor.h"
#include "Product_Data.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <time.h>

#define MINICLAW_IP        "10.176.240.100"
#define MINICLAW_PORT      18791
#define CHECK_INTERVAL_MS  60000    // 每 60 秒检查一次
#define ALERT_COOLDOWN_MS  1800000  // 同一商品同一告警类型 30 分钟冷却

// 每个商品每种告警的最后发送时间
static unsigned long s_last_alert[PRODUCT_COUNT][4] = {0};
// 0: out_of_stock, 1: low_stock, 2: near_expiry, 3: anomaly

static unsigned long s_last_check = 0;

static void send_alert(const char *type, const char *product, int stock, const char *detail)
{
    if (WiFi.status() != WL_CONNECTED) return;

    WiFiClient client;
    HTTPClient http;

    String url = "http://" + String(MINICLAW_IP) + ":" + String(MINICLAW_PORT) + "/alert";
    http.begin(client, url);
    http.addHeader("Content-Type", "application/json");

    StaticJsonDocument<384> doc;
    doc["type"]    = type;
    doc["store"]   = "智慧超市";
    doc["product"] = product;
    doc["stock"]   = stock;
    doc["detail"]  = detail;

    char time_buf[32];
    time_t t = time(nullptr);
    strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", localtime(&t));
    doc["time"]    = time_buf;

    String body;
    serializeJson(doc, body);

    int code = http.POST(body);
    Serial.printf("[库存监控] %s → miniclaw, HTTP %d (%s)\n", type, code, product);
    http.end();
}

static int days_until_expiry(const Product &p)
{
    if (p.mfgDate.length() < 10 || p.shelfLife <= 0) return 999;
    struct tm tm_mfg = {};
    sscanf(p.mfgDate.c_str(), "%d-%d-%d",
           &tm_mfg.tm_year, &tm_mfg.tm_mon, &tm_mfg.tm_mday);
    tm_mfg.tm_year -= 1900;
    tm_mfg.tm_mon  -= 1;
    tm_mfg.tm_mon += p.shelfLife;
    time_t expiry_time = mktime(&tm_mfg);
    time_t now = time(nullptr);
    if (now < 1700000000 || expiry_time <= 0) return 999;
    return (int)((expiry_time - now) / 86400);
}

static bool cooldown_ok(int idx, int alert_kind)
{
    unsigned long now = millis();
    if (s_last_alert[idx][alert_kind] != 0 &&
        now - s_last_alert[idx][alert_kind] < ALERT_COOLDOWN_MS) return false;
    s_last_alert[idx][alert_kind] = now;
    return true;
}

void InventoryMonitor_Init()
{
    memset(s_last_alert, 0, sizeof(s_last_alert));
    s_last_check = millis();
    Serial.println("[库存监控] 已初始化，检查间隔 60s，告警冷却 30min");
}

void InventoryMonitor_HandleLoop()
{
    unsigned long now = millis();
    if (now - s_last_check < CHECK_INTERVAL_MS) return;
    s_last_check = now;

    if (WiFi.status() != WL_CONNECTED) return;

    int out_of_stock_count = 0;
    int low_stock_count = 0;
    int near_expiry_count = 0;

    for (int i = 0; i < PRODUCT_COUNT; i++) {
        Product &p = products[i];
        if (p.qrCode.length() == 0) continue;

        // ── 缺货 (stock == 0) ──
        if (p.stock == 0 && !p.isWeigh) {
            if (cooldown_ok(i, 0)) {
                String detail = "【" + p.name + "】已售罄，请及时补货";
                send_alert("out_of_stock", p.name.c_str(), 0, detail.c_str());
            }
            out_of_stock_count++;
            continue;
        }

        // ── 低库存 (stock < 5) ──
        if (p.stock > 0 && p.stock < 5 && !p.isWeigh) {
            if (cooldown_ok(i, 1)) {
                String detail = "【" + p.name + "】库存仅剩 " + String(p.stock) + " 件，请尽快补货";
                send_alert("low_stock", p.name.c_str(), p.stock, detail.c_str());
            }
            low_stock_count++;
        }

        // ── 临期 (剩余保质期 < 3 天) ──
        int remaining = days_until_expiry(p);
        if (remaining >= 0 && remaining < 3 && p.shelfLife > 0) {
            if (cooldown_ok(i, 2)) {
                String detail = "【" + p.name + "】保质期仅剩 " + String(remaining) + " 天，建议促销或下架\n生产日期：" + p.mfgDate + "，保质期：" + String(p.shelfLife) + " 天";
                send_alert("near_expiry", p.name.c_str(), p.stock, detail.c_str());
            }
            near_expiry_count++;
        }
    }

    if (out_of_stock_count > 0 || low_stock_count > 0 || near_expiry_count > 0) {
        Serial.printf("[库存监控] 检查完成 — 缺货:%d 低库存:%d 临期:%d\n",
                      out_of_stock_count, low_stock_count, near_expiry_count);
    }
}

#endif  // !SLAVE_MODE
