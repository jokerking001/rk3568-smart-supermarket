#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_rom_sys.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "cJSON.h"
#include "secrets.h"    /* 私密配置（WiFi / 主控地址），不进版本库 */

#define PIN_STB1 1
#define PIN_STB2 2
#define PIN_STB3 3
#define PIN_STB4 4
#define PIN_STB5 5
#define PIN_STB6 6
#define PIN_PAPER 7
#define PIN_TEMP 8
#define PIN_MOTOR_1 9
#define PIN_MOTOR_2 10
#define PIN_MOTOR_3 11
#define PIN_MOTOR_4 12
#define PIN_LATCH 13
#define PIN_CLOCK 14
#define PIN_DATA 15
#define PIN_HEAD_POWER 16
#define PIN_BUTTON 17
#define PIN_BUZZER 18

#define PRINT_WIDTH 384
#define BYTES_PER_ROW (PRINT_WIDTH / 8)
#define MAX_JOB_HEIGHT 2400
#define HEAT_PULSE_US 2200
#define MOTOR_STEPS_PER_ROW 2
#define PAPER_PRESENT_LEVEL 0
#define REQUIRE_PAPER_SENSOR_FOR_PRINT 0
#define TEMP_ADC_MIN 5
#define TEMP_ADC_MAX 3800

static const char *TAG = "re_min";
static EventGroupHandle_t wifi_events;
static adc_oneshot_unit_handle_t temp_adc;
static char printer_state[32] = "starting";
static uint32_t last_job_id = 0;
static bool last_job_ok = false;
static char last_error[64] = "none";
static int last_temp_raw = -1;
static httpd_handle_t status_server;

static void motor_step(uint32_t step);
static void motor_release(void);
static void beep(unsigned duration_ms);
static esp_err_t local_print_test(httpd_req_t *req);
static esp_err_t strobe_print_test(httpd_req_t *req);
static esp_err_t strobe4_hold_test(httpd_req_t *req);
static const EventBits_t WIFI_CONNECTED = BIT0;
static const gpio_num_t strobes[] = {
    PIN_STB1, PIN_STB2, PIN_STB3, PIN_STB4, PIN_STB5, PIN_STB6
};

static void outputs_safe(void)
{
    gpio_set_level(PIN_HEAD_POWER, 0);
    for (size_t i = 0; i < sizeof(strobes) / sizeof(strobes[0]); ++i) {
        gpio_set_level(strobes[i], 0);
    }
    gpio_set_level(PIN_CLOCK, 0);
    gpio_set_level(PIN_LATCH, 0);
    gpio_set_level(PIN_DATA, 0);
    gpio_set_level(PIN_MOTOR_1, 0);
    gpio_set_level(PIN_MOTOR_2, 0);
    gpio_set_level(PIN_MOTOR_3, 0);
    gpio_set_level(PIN_MOTOR_4, 0);
}

static void hardware_init(void)
{
    const uint64_t output_mask =
        (1ULL << PIN_STB1) | (1ULL << PIN_STB2) | (1ULL << PIN_STB3) |
        (1ULL << PIN_STB4) | (1ULL << PIN_STB5) | (1ULL << PIN_STB6) |
        (1ULL << PIN_MOTOR_1) | (1ULL << PIN_MOTOR_2) |
        (1ULL << PIN_MOTOR_3) | (1ULL << PIN_MOTOR_4) |
        (1ULL << PIN_LATCH) | (1ULL << PIN_CLOCK) | (1ULL << PIN_DATA) |
        (1ULL << PIN_HEAD_POWER) | (1ULL << PIN_BUZZER);
    gpio_config_t outputs = {
        .pin_bit_mask = output_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&outputs));

    gpio_config_t inputs = {
        .pin_bit_mask = (1ULL << PIN_PAPER) | (1ULL << PIN_BUTTON),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&inputs));
    outputs_safe();
}

static bool paper_present(void)
{
    int present_samples = 0;
    for (int i = 0; i < 7; ++i) {
        if (gpio_get_level(PIN_PAPER) == PAPER_PRESENT_LEVEL) present_samples++;
        esp_rom_delay_us(120);
    }
    return present_samples >= 5;
}

static bool head_temperature_safe(void)
{
    static unsigned consecutive_unsafe = 0;
    int sum = 0;
    for (int i = 0; i < 5; ++i) {
        int raw = 0;
        if (!temp_adc || adc_oneshot_read(temp_adc, ADC_CHANNEL_7, &raw) != ESP_OK) {
            strlcpy(last_error, "temperature ADC read failed", sizeof(last_error));
            return false;
        }
        sum += raw;
        esp_rom_delay_us(80);
    }
    int average = sum / 5;
    last_temp_raw = average;
    if (average < TEMP_ADC_MIN || average > TEMP_ADC_MAX) {
        consecutive_unsafe++;
        ESP_LOGW(TAG, "Head temperature ADC suspect: avg=%d count=%u", average, consecutive_unsafe);
        if (consecutive_unsafe >= 3) {
            snprintf(last_error, sizeof(last_error), "temperature ADC unsafe: %d", average);
            return false;
        }
    } else {
        consecutive_unsafe = 0;
    }
    return true;
}

static esp_err_t status_page(httpd_req_t *req)
{
    static const char page[] =
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>热敏打印机状态</title><style>body{font-family:system-ui,Microsoft YaHei,sans-serif;max-width:560px;margin:32px auto;padding:0 18px;color:#18202a;background:#f5f7fa}h1{font-size:24px}.card{background:#fff;border:1px solid #dfe5ec;border-radius:10px;padding:18px;box-shadow:0 4px 14px #0000000d}.row{display:flex;justify-content:space-between;border-bottom:1px solid #edf0f3;padding:10px 0}.row:last-child{border:0}.ok{color:#07845a}.bad{color:#c62828}</style>"
        "<body><h1>热敏打印机</h1><div class='card'><div class='row'><span>状态</span><b id='state'>读取中</b></div><div class='row'><span>IP 地址</span><b id='ip'>-</b></div><div class='row'><span>Wi-Fi</span><b id='wifi'>-</b></div><div class='row'><span>纸张</span><b id='paper'>-</b></div><div class='row'><span>温度 ADC</span><b id='temp'>-</b></div><div class='row'><span>最近任务</span><b id='job'>-</b></div><div class='row'><span>失败原因</span><b id='error'>-</b></div></div><script>async function pull(){try{let d=await(await fetch('/api/status?t='+Date.now())).json();for(let k of ['state','ip','wifi','paper','temp','job','error'])document.getElementById(k).textContent=d[k]||'-';document.getElementById('state').className=d.state==='waiting'?'ok':'bad'}catch(e){document.getElementById('state').textContent='连接失败'}}pull();setInterval(pull,2000)</script></body></html>";
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    return httpd_resp_send(req, page, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t status_json(httpd_req_t *req)
{
    esp_netif_ip_info_t ip = {};
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif) esp_netif_get_ip_info(netif, &ip);
    char ip_text[16];
    snprintf(ip_text, sizeof(ip_text), IPSTR, IP2STR(&ip.ip));
    char json[384];
    snprintf(json, sizeof(json), "{\"state\":\"%s\",\"ip\":\"%s\",\"wifi\":\"%s\",\"paper\":\"%s\",\"button\":%d,\"temp\":%d,\"job\":\"%s #%lu\",\"error\":\"%s\"}",
             printer_state, ip_text, (ip.ip.addr ? "connected" : "disconnected"),
             paper_present() ? "present" : "missing", gpio_get_level(PIN_BUTTON), last_temp_raw,
             last_job_id ? (last_job_ok ? "ok" : "failed") : "none", (unsigned long)last_job_id, last_error);
    httpd_resp_set_type(req, "application/json; charset=utf-8");
    return httpd_resp_send(req, json, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t motor_test(httpd_req_t *req)
{
    static const uint8_t maps[][4] = {
        {0, 1, 2, 3}, {0, 2, 1, 3}, {0, 1, 3, 2},
        {0, 2, 3, 1}, {0, 3, 1, 2}, {0, 3, 2, 1}
    };
    static const gpio_num_t pins[] = {PIN_MOTOR_1, PIN_MOTOR_2, PIN_MOTOR_3, PIN_MOTOR_4};
    static const uint8_t phases[8][4] = {
        {1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0}, {0, 1, 1, 0},
        {0, 0, 1, 0}, {0, 0, 1, 1}, {0, 0, 0, 1}, {1, 0, 0, 1}
    };
    int selected = -1;
    char query[32] = {};
    char pattern_value[8] = {};
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
        httpd_query_key_value(query, "p", pattern_value, sizeof(pattern_value)) == ESP_OK) {
        selected = atoi(pattern_value) - 1;
    }
    if (selected >= (int)(sizeof(maps) / sizeof(maps[0]))) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "p must be 1 through 6");
    }
    const size_t first = selected >= 0 ? (size_t)selected : 0;
    const size_t last = selected >= 0 ? (size_t)selected + 1 : sizeof(maps) / sizeof(maps[0]);
    strlcpy(printer_state, "motor-test", sizeof(printer_state));
    ESP_LOGI(TAG, "Motor phase scan: patterns %u through %u", (unsigned)(first + 1), (unsigned)last);
    for (size_t pattern = first; pattern < last; ++pattern) {
        ESP_LOGI(TAG, "Motor phase pattern %u", (unsigned)(pattern + 1));
        for (size_t count = 0; count <= pattern; ++count) {
            beep(55);
            vTaskDelay(pdMS_TO_TICKS(90));
        }
        for (uint32_t step = 0; step < 1200; ++step) {
            const uint8_t *phase = phases[step & 7U];
            for (size_t output = 0; output < 4; ++output) {
                gpio_set_level(pins[output], phase[maps[pattern][output]]);
            }
            uint32_t delay_us = step < 240 ? 12000U - step * 33U : 4000U;
            esp_rom_delay_us(delay_us);
        }
        motor_release();
        vTaskDelay(pdMS_TO_TICKS(700));
    }
    motor_release();
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return httpd_resp_sendstr(req, "motor phase scan complete");
}

static esp_err_t head_power_test(httpd_req_t *req)
{
    if (strcmp(printer_state, "waiting") != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "printer busy");
    }
    strlcpy(printer_state, "head-power-test", sizeof(printer_state));
    for (size_t i = 0; i < sizeof(strobes) / sizeof(strobes[0]); ++i) {
        gpio_set_level(strobes[i], 0);
    }
    gpio_set_level(PIN_HEAD_POWER, 1);
    ESP_LOGI(TAG, "GPIO16 head power enabled for 2 seconds; strobes remain off");
    vTaskDelay(pdMS_TO_TICKS(2000));
    gpio_set_level(PIN_HEAD_POWER, 0);
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return httpd_resp_sendstr(req, "head power test complete");
}

static esp_err_t motor_hold_test(httpd_req_t *req)
{
    if (strcmp(printer_state, "waiting") != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "printer busy");
    }
    strlcpy(printer_state, "motor-hold", sizeof(printer_state));
    // Lock both coils at a valid TC1508S full-step phase for 10 seconds.
    gpio_set_level(PIN_MOTOR_1, 1);
    gpio_set_level(PIN_MOTOR_2, 0);
    gpio_set_level(PIN_MOTOR_3, 1);
    gpio_set_level(PIN_MOTOR_4, 0);
    beep(80);
    vTaskDelay(pdMS_TO_TICKS(10000));
    motor_release();
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return httpd_resp_sendstr(req, "motor hold test complete");
}

static esp_err_t motor_channel_test(httpd_req_t *req)
{
    if (strcmp(printer_state, "waiting") != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "printer busy");
    }
    strlcpy(printer_state, "channel-a-test", sizeof(printer_state));
    beep(100);
    for (int i = 0; i < 12; ++i) {
        gpio_set_level(PIN_MOTOR_1, 0);
        gpio_set_level(PIN_MOTOR_2, 0);
        gpio_set_level(PIN_MOTOR_3, (i & 1) == 0);
        gpio_set_level(PIN_MOTOR_4, (i & 1) != 0);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    motor_release();
    vTaskDelay(pdMS_TO_TICKS(1000));

    strlcpy(printer_state, "channel-b-test", sizeof(printer_state));
    beep(100);
    vTaskDelay(pdMS_TO_TICKS(120));
    beep(100);
    for (int i = 0; i < 12; ++i) {
        gpio_set_level(PIN_MOTOR_1, (i & 1) == 0);
        gpio_set_level(PIN_MOTOR_2, (i & 1) != 0);
        gpio_set_level(PIN_MOTOR_3, 0);
        gpio_set_level(PIN_MOTOR_4, 0);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    motor_release();
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return httpd_resp_sendstr(req, "motor channel comparison complete");
}

static void status_server_init(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;
    config.max_uri_handlers = 12;
    if (httpd_start(&status_server, &config) != ESP_OK) return;
    httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = status_page, .user_ctx = NULL};
    httpd_uri_t api = {.uri = "/api/status", .method = HTTP_GET, .handler = status_json, .user_ctx = NULL};
    httpd_uri_t motor = {.uri = "/api/motor-test", .method = HTTP_GET, .handler = motor_test, .user_ctx = NULL};
    httpd_uri_t motor_hold = {.uri = "/api/motor-hold", .method = HTTP_GET, .handler = motor_hold_test, .user_ctx = NULL};
    httpd_uri_t motor_channels = {.uri = "/api/motor-channels", .method = HTTP_GET, .handler = motor_channel_test, .user_ctx = NULL};
    httpd_uri_t head = {.uri = "/api/head-power-test", .method = HTTP_GET, .handler = head_power_test, .user_ctx = NULL};
    httpd_uri_t print_test = {.uri = "/api/print-test", .method = HTTP_GET, .handler = local_print_test, .user_ctx = NULL};
    httpd_uri_t strobe_test = {.uri = "/api/strobe-test", .method = HTTP_GET, .handler = strobe_print_test, .user_ctx = NULL};
    httpd_uri_t strobe4_hold = {.uri = "/api/strobe4-hold", .method = HTTP_GET, .handler = strobe4_hold_test, .user_ctx = NULL};
    httpd_register_uri_handler(status_server, &root);
    httpd_register_uri_handler(status_server, &api);
    httpd_register_uri_handler(status_server, &motor);
    httpd_register_uri_handler(status_server, &motor_hold);
    httpd_register_uri_handler(status_server, &motor_channels);
    httpd_register_uri_handler(status_server, &head);
    httpd_register_uri_handler(status_server, &print_test);
    httpd_register_uri_handler(status_server, &strobe_test);
    httpd_register_uri_handler(status_server, &strobe4_hold);
}

static void beep(unsigned duration_ms)
{
    gpio_set_level(PIN_BUZZER, 1);
    vTaskDelay(pdMS_TO_TICKS(duration_ms));
    gpio_set_level(PIN_BUZZER, 0);
}

static void motor_step(uint32_t step)
{
    // GPIO order is IND, INC, INB, INA. Each adjacent pair is one
    // TC1508S H-bridge; both coils are energized for full-step torque.
    static const uint8_t phases[4][4] = {
        {1, 0, 1, 0},
        {0, 1, 1, 0},
        {0, 1, 0, 1},
        {1, 0, 0, 1},
    };
    const uint8_t *phase = phases[step & 3U];
    gpio_set_level(PIN_MOTOR_1, phase[0]);
    gpio_set_level(PIN_MOTOR_2, phase[1]);
    gpio_set_level(PIN_MOTOR_3, phase[2]);
    gpio_set_level(PIN_MOTOR_4, phase[3]);
    esp_rom_delay_us(6000);
}

static void motor_release(void)
{
    gpio_set_level(PIN_MOTOR_1, 0);
    gpio_set_level(PIN_MOTOR_2, 0);
    gpio_set_level(PIN_MOTOR_3, 0);
    gpio_set_level(PIN_MOTOR_4, 0);
}

static void feed_step(uint32_t step)
{
    // Match the proven SW2 manual-feed timing exactly.
    motor_step(step);
    vTaskDelay(pdMS_TO_TICKS(3));
}

static void shift_row(const uint8_t row[BYTES_PER_ROW])
{
    for (int byte = 0; byte < BYTES_PER_ROW; ++byte) {
        uint8_t value = row[byte];
        for (int bit = 7; bit >= 0; --bit) {
            gpio_set_level(PIN_DATA, (value >> bit) & 1U);
            gpio_set_level(PIN_CLOCK, 1);
            esp_rom_delay_us(1);
            gpio_set_level(PIN_CLOCK, 0);
        }
    }
    gpio_set_level(PIN_LATCH, 1);
    esp_rom_delay_us(2);
    gpio_set_level(PIN_LATCH, 0);
}

static bool row_has_black(const uint8_t row[BYTES_PER_ROW])
{
    for (int i = 0; i < BYTES_PER_ROW; ++i) {
        if (row[i]) return true;
    }
    return false;
}

static bool print_row(const uint8_t row[BYTES_PER_ROW], uint32_t row_number)
{
    if ((row_number & 7U) == 0 && !head_temperature_safe()) {
        outputs_safe();
        ESP_LOGE(TAG, "Temperature protection stopped the print job");
        return false;
    }

    shift_row(row);
    if (row_has_black(row)) {
        for (size_t group = 0; group < sizeof(strobes) / sizeof(strobes[0]); ++group) {
            gpio_set_level(strobes[group], 1);
            esp_rom_delay_us(HEAT_PULSE_US);
            gpio_set_level(strobes[group], 0);
        }
    }
    uint32_t first_step = row_number * MOTOR_STEPS_PER_ROW;
    for (uint32_t step = 0; step < MOTOR_STEPS_PER_ROW; ++step) {
        feed_step(first_step + step);
    }
    return true;
}

static void feed_rows(uint32_t *step, int rows)
{
    for (int i = 0; i < rows; ++i) feed_step((*step)++);
    motor_release();
}

static esp_err_t local_print_test(httpd_req_t *req)
{
    if (strcmp(printer_state, "waiting") != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "printer busy");
    }
    strlcpy(printer_state, "test-print", sizeof(printer_state));
    strlcpy(last_error, "none", sizeof(last_error));
    uint8_t row[BYTES_PER_ROW];
    bool ok = true;
    gpio_set_level(PIN_HEAD_POWER, 1);
    vTaskDelay(pdMS_TO_TICKS(2));
    for (uint32_t y = 0; y < 96 && ok; ++y) {
        memset(row, 0, sizeof(row));
        if ((y >= 8 && y < 24) || (y >= 40 && y < 56) || (y >= 72 && y < 88)) {
            for (int x = 4; x < BYTES_PER_ROW - 4; ++x) row[x] = 0xFF;
        }
        ok = print_row(row, y);
    }
    outputs_safe();
    uint32_t step = 96U * MOTOR_STEPS_PER_ROW;
    if (ok) feed_rows(&step, 20);
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    if (!ok) return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, last_error);
    return httpd_resp_sendstr(req, "local print test complete");
}

static esp_err_t strobe_print_test(httpd_req_t *req)
{
    if (strcmp(printer_state, "waiting") != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "printer busy");
    }
    strlcpy(printer_state, "strobe-test", sizeof(printer_state));
    uint8_t row[BYTES_PER_ROW];
    memset(row, 0xFF, sizeof(row));
    uint32_t motor_position = 0;
    gpio_set_level(PIN_HEAD_POWER, 1);
    vTaskDelay(pdMS_TO_TICKS(2));
    for (size_t group = 0; group < sizeof(strobes) / sizeof(strobes[0]); ++group) {
        for (int y = 0; y < 14; ++y) {
            shift_row(row);
            gpio_set_level(strobes[group], 1);
            esp_rom_delay_us(HEAT_PULSE_US);
            gpio_set_level(strobes[group], 0);
            feed_step(motor_position++);
        }
        for (int gap = 0; gap < 8; ++gap) feed_step(motor_position++);
    }
    outputs_safe();
    for (int tail = 0; tail < 20; ++tail) feed_step(motor_position++);
    motor_release();
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return httpd_resp_sendstr(req, "strobe test complete");
}

static esp_err_t strobe4_hold_test(httpd_req_t *req)
{
    if (strcmp(printer_state, "waiting") != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "printer busy");
    }
    strlcpy(printer_state, "strobe4-hold", sizeof(printer_state));
    outputs_safe();
    // Keep head power disabled; only expose a safe logic level for probing.
    // group=1..6 selects a strobe; the default remains group 4 for compatibility.
    int group = 4;
    char query[24] = {0};
    char value[8] = {0};
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
        httpd_query_key_value(query, "group", value, sizeof(value)) == ESP_OK) {
        int requested = atoi(value);
        if (requested >= 1 && requested <= 6) group = requested;
    }
    gpio_set_level(strobes[group - 1], 1);
    vTaskDelay(pdMS_TO_TICKS(15000));
    gpio_set_level(strobes[group - 1], 0);
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return httpd_resp_sendstr(req, "strobe4 hold complete");
}

static esp_err_t http_get_json(const char *url, char *buffer, size_t capacity)
{
    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 4000,
        .buffer_size = 1024,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return ESP_ERR_NO_MEM;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        esp_http_client_fetch_headers(client);
        if (esp_http_client_get_status_code(client) != 200) err = ESP_FAIL;
        else {
            int read = esp_http_client_read_response(client, buffer, capacity - 1);
            if (read < 0 || (size_t)read >= capacity) err = ESP_ERR_INVALID_SIZE;
            else buffer[read] = '\0';
        }
        esp_http_client_close(client);
    }
    esp_http_client_cleanup(client);
    return err;
}

static void acknowledge_job(uint32_t id, bool success)
{
    char url[192];
    snprintf(url, sizeof(url), CONTROLLER_BASE_URL "/api/printer/ack?id=%lu&ok=%d",
             (unsigned long)id, success ? 1 : 0);
    esp_http_client_config_t config = {.url = url, .method = HTTP_METHOD_POST, .timeout_ms = 3000};
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client) {
        esp_http_client_perform(client);
        esp_http_client_cleanup(client);
    }
}

static bool download_and_print(uint32_t id, int width, int height, int expected_bytes)
{
    strlcpy(last_error, "none", sizeof(last_error));
    if (width != PRINT_WIDTH || height <= 0 || height > MAX_JOB_HEIGHT ||
        expected_bytes != height * BYTES_PER_ROW) {
        strlcpy(last_error, "invalid job dimensions", sizeof(last_error));
        ESP_LOGE(TAG, "Rejected invalid job dimensions: %dx%d bytes=%d", width, height, expected_bytes);
        return false;
    }
    if (REQUIRE_PAPER_SENSOR_FOR_PRINT && !paper_present()) {
        strlcpy(last_error, "paper missing", sizeof(last_error));
        strlcpy(printer_state, "waiting-paper", sizeof(printer_state));
        ESP_LOGE(TAG, "Job %lu waiting: paper sensor is not active", (unsigned long)id);
        beep(150);
        return false;
    }

    char url[192];
    /* 取**裸光栅字节**用 /printjob.bin（application/octet-stream）。
     * 不要用 /api/printer/job —— 那条返回的是 JSON + base64，
     * 直接当字节流读会打印出一堆乱码。（2026-10-09 联调时对齐的契约） */
    snprintf(url, sizeof(url), CONTROLLER_BASE_URL "/printjob.bin?id=%lu", (unsigned long)id);
    esp_http_client_config_t config = {.url = url, .timeout_ms = 10000, .buffer_size = 1024};
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) { strlcpy(last_error, "HTTP client init failed", sizeof(last_error)); return false; }
    esp_http_client_set_method(client, HTTP_METHOD_GET);
    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        strlcpy(last_error, "job download open failed", sizeof(last_error));
        esp_http_client_cleanup(client);
        return false;
    }
    esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client) != 200) {
        strlcpy(last_error, "job download HTTP error", sizeof(last_error));
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return false;
    }

    ESP_LOGI(TAG, "Printing job %lu, %dx%d", (unsigned long)id, width, height);
    strlcpy(printer_state, "printing", sizeof(printer_state));
    gpio_set_level(PIN_HEAD_POWER, 1);
    vTaskDelay(pdMS_TO_TICKS(2));
    uint8_t row[BYTES_PER_ROW];
    uint32_t motor_position = 0;
    bool ok = true;
    for (int y = 0; y < height && ok; ++y) {
        int offset = 0;
        while (offset < BYTES_PER_ROW) {
            int got = esp_http_client_read(client, (char *)row + offset, BYTES_PER_ROW - offset);
            if (got <= 0) { strlcpy(last_error, "job data ended early", sizeof(last_error)); ok = false; break; }
            offset += got;
        }
        if (ok) ok = print_row(row, motor_position++);
        if ((y & 31) == 31) vTaskDelay(1);
    }
    outputs_safe();
    motor_position *= MOTOR_STEPS_PER_ROW;
    if (ok) feed_rows(&motor_position, 20);
    else motor_release();
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    beep(ok ? 80 : 300);
    ESP_LOGI(TAG, "Job %lu %s", (unsigned long)id, ok ? "complete" : "failed");
    if (ok) strlcpy(last_error, "none", sizeof(last_error));
    last_job_id = id; last_job_ok = ok;
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    return ok;
}

static void printer_task(void *arg)
{
    (void)arg;
    uint32_t last_failed_id = 0;
    for (;;) {
        bool failed_this_loop = false;
        xEventGroupWaitBits(wifi_events, WIFI_CONNECTED, pdFALSE, pdTRUE, portMAX_DELAY);
        char json[512];
        if (http_get_json(CONTROLLER_BASE_URL "/api/printer/job-meta", json, sizeof(json)) == ESP_OK) {
            cJSON *root = cJSON_Parse(json);
            cJSON *ready = root ? cJSON_GetObjectItem(root, "ready") : NULL;
            if (cJSON_IsTrue(ready)) {
                uint32_t id = (uint32_t)cJSON_GetObjectItem(root, "id")->valuedouble;
                int width = cJSON_GetObjectItem(root, "width")->valueint;
                int height = cJSON_GetObjectItem(root, "height")->valueint;
                int bytes = cJSON_GetObjectItem(root, "bytes")->valueint;
                bool ok = download_and_print(id, width, height, bytes);
                if (ok) {
                    acknowledge_job(id, true);
                    last_failed_id = 0;
                } else if (last_failed_id != id) {
                    acknowledge_job(id, false);
                    last_failed_id = id;
                    failed_this_loop = true;
                } else {
                    failed_this_loop = true;
                }
            }
            cJSON_Delete(root);
        }
        vTaskDelay(pdMS_TO_TICKS(failed_this_loop ? 30000 : 2000));
    }
}

static void button_task(void *arg)
{
    (void)arg;
    uint32_t motor_position = 0;
    for (;;) {
        if (gpio_get_level(PIN_BUTTON) == 0) {
            vTaskDelay(pdMS_TO_TICKS(40));
            while (gpio_get_level(PIN_BUTTON) == 0) {
                motor_step(motor_position++);
                vTaskDelay(pdMS_TO_TICKS(3));
            }
            motor_release();
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "WiFi ready, IP=" IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED);
    }
}

static void wifi_init(void)
{
    wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t config = {0};
    strlcpy((char *)config.sta.ssid, WIFI_SSID, sizeof(config.sta.ssid));
    strlcpy((char *)config.sta.password, WIFI_PASSWORD, sizeof(config.sta.password));
    config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(40));
    ESP_LOGI(TAG, "WiFi max TX power limited to 10 dBm for supply test");
}

void app_main(void)
{
    esp_err_t nvs = nvs_flash_init();
    if (nvs == ESP_ERR_NVS_NO_FREE_PAGES || nvs == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    hardware_init();
    adc_oneshot_unit_init_cfg_t adc_unit = {.unit_id = ADC_UNIT_1};
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&adc_unit, &temp_adc));
    adc_oneshot_chan_cfg_t adc_channel = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    ESP_ERROR_CHECK(adc_oneshot_config_channel(temp_adc, ADC_CHANNEL_7, &adc_channel));
    int temp_raw = 0; adc_oneshot_read(temp_adc, ADC_CHANNEL_7, &temp_raw);
    last_temp_raw = temp_raw;
    ESP_LOGI(TAG, "Thermal printer safe boot; paper=%d expected=%d temp_adc=%d",
             gpio_get_level(PIN_PAPER), PAPER_PRESENT_LEVEL, temp_raw);
    wifi_init();
    strlcpy(printer_state, "waiting", sizeof(printer_state));
    status_server_init();
    xTaskCreate(printer_task, "printer", 8192, NULL, 5, NULL);
    xTaskCreate(button_task, "feed_button", 2048, NULL, 3, NULL);
}
