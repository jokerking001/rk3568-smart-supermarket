#include "alert_server.h"
#include "mimi_config.h"
#include "bus/message_bus.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "esp_http_server.h"
#include "esp_log.h"

static const char *TAG = "alert";
static httpd_handle_t s_server;

#ifndef MIMI_ALERT_FEISHU_CHAT_ID
#define MIMI_ALERT_FEISHU_CHAT_ID ""
#endif

static const char *json_string(cJSON *root, const char *key, const char *fallback)
{
    cJSON *item = cJSON_GetObjectItem(root, key);
    return cJSON_IsString(item) ? item->valuestring : fallback;
}

static esp_err_t alert_post_handler(httpd_req_t *req)
{
    if (req->content_len <= 0 || req->content_len > 2048) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid body size");
        return ESP_FAIL;
    }

    char *body = calloc(1, req->content_len + 1);
    if (!body) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "OOM");
        return ESP_ERR_NO_MEM;
    }

    size_t offset = 0;
    while (offset < req->content_len) {
        int received = httpd_req_recv(req, body + offset, req->content_len - offset);
        if (received <= 0) {
            free(body);
            httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Incomplete body");
            return ESP_FAIL;
        }
        offset += (size_t)received;
    }

    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }

    const char *type = json_string(root, "type", "unknown");
    const char *store = json_string(root, "store", "Unknown store");
    const char *time = json_string(root, "time", "Unknown time");
    const char *product = json_string(root, "product", "");
    const char *detail = json_string(root, "detail", "");
    const char *request_id = json_string(root, "request_id", "");
    const char *terminal = json_string(root, "terminal", "Terminal 1");
    cJSON *stock_item = cJSON_GetObjectItem(root, "stock");
    int stock = cJSON_IsNumber(stock_item) ? stock_item->valueint : -1;

    char message[768];
    if (strcmp(type, "human_service") == 0) {
        snprintf(message, sizeof(message),
                 "[Human service]\nStore: %s\nTerminal: %s\nTicket: %s\nTime: %s\n\n"
                 "Customer request: %s\n\nReply with 接单, 处理完成, or 无法处理 %s.",
                 store, terminal, request_id[0] ? request_id : "Unknown", time,
                 detail[0] ? detail : "Not provided",
                 request_id[0] ? request_id : "Unknown");
    } else {
        snprintf(message, sizeof(message),
                 "[Store alert: %s]\nStore: %s\nProduct: %s\nStock: %d\nTime: %s\n%s",
                 type, store, product[0] ? product : "-", stock, time, detail);
    }
    cJSON_Delete(root);

    esp_err_t err = ESP_OK;
    if (MIMI_ALERT_FEISHU_CHAT_ID[0] == '\0') {
        ESP_LOGW(TAG, "MIMI_ALERT_FEISHU_CHAT_ID is not configured");
        err = ESP_ERR_INVALID_STATE;
    } else {
        mimi_msg_t outbound = {0};
        strlcpy(outbound.channel, MIMI_CHAN_FEISHU, sizeof(outbound.channel));
        strlcpy(outbound.chat_id, MIMI_ALERT_FEISHU_CHAT_ID, sizeof(outbound.chat_id));
        outbound.content = strdup(message);
        err = outbound.content ? message_bus_push_outbound(&outbound) : ESP_ERR_NO_MEM;
        if (err != ESP_OK) free(outbound.content);
    }

    httpd_resp_set_type(req, "application/json");
    if (err != ESP_OK) httpd_resp_set_status(req, "503 Service Unavailable");
    httpd_resp_sendstr(req, err == ESP_OK ? "{\"status\":\"queued\"}" : "{\"status\":\"error\"}");
    return ESP_OK;
}

esp_err_t alert_server_start(void)
{
    if (s_server) return ESP_OK;

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = MIMI_ALERT_PORT;
    config.ctrl_port = MIMI_ALERT_PORT + 1;
    config.max_open_sockets = 4;

    esp_err_t err = httpd_start(&s_server, &config);
    if (err != ESP_OK) return err;

    httpd_uri_t uri = {
        .uri = "/alert",
        .method = HTTP_POST,
        .handler = alert_post_handler,
    };
    err = httpd_register_uri_handler(s_server, &uri);
    if (err != ESP_OK) {
        httpd_stop(s_server);
        s_server = NULL;
        return err;
    }

    ESP_LOGI(TAG, "Alert server started on port %d", MIMI_ALERT_PORT);
    return ESP_OK;
}

esp_err_t alert_server_stop(void)
{
    if (!s_server) return ESP_OK;
    esp_err_t err = httpd_stop(s_server);
    s_server = NULL;
    return err;
}
