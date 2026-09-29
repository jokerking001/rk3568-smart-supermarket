#include "tool_store.h"
#include "mimi_config.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"

static const char *TAG = "tool_store";

#define STORE_RESPONSE_SIZE (20 * 1024)
#define STORE_TIMEOUT_MS    5000

typedef struct {
    char *data;
    size_t len;
    size_t cap;
    bool overflow;
} store_response_t;

static char s_store_url[160] = {0};

static esp_err_t store_http_event_handler(esp_http_client_event_t *evt)
{
    store_response_t *response = (store_response_t *)evt->user_data;
    if (evt->event_id != HTTP_EVENT_ON_DATA || !response || evt->data_len <= 0) {
        return ESP_OK;
    }

    size_t available = response->cap - response->len - 1;
    size_t copy_len = (size_t)evt->data_len;
    if (copy_len > available) {
        copy_len = available;
        response->overflow = true;
    }
    if (copy_len > 0) {
        memcpy(response->data + response->len, evt->data, copy_len);
        response->len += copy_len;
        response->data[response->len] = '\0';
    }
    return ESP_OK;
}

static esp_err_t store_get(const char *path, char **body_out)
{
    *body_out = NULL;
    if (s_store_url[0] == '\0') return ESP_ERR_INVALID_STATE;

    char url[224];
    int written = snprintf(url, sizeof(url), "%s%s", s_store_url, path);
    if (written < 0 || written >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;

    store_response_t response = {
        .data = heap_caps_calloc(1, STORE_RESPONSE_SIZE, MALLOC_CAP_SPIRAM),
        .cap = STORE_RESPONSE_SIZE,
    };
    if (!response.data) return ESP_ERR_NO_MEM;

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = STORE_TIMEOUT_MS,
        .buffer_size = 2048,
        .event_handler = store_http_event_handler,
        .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        free(response.data);
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Accept", "application/json");
    if (MIMI_SECRET_STORE_API_KEY[0] != '\0') {
        esp_http_client_set_header(client, "X-MimiClaw-Key", MIMI_SECRET_STORE_API_KEY);
    }
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK) {
        free(response.data);
        return err;
    }
    if (status != 200 || response.overflow) {
        ESP_LOGE(TAG, "GET %s failed: status=%d overflow=%d", path, status, response.overflow);
        free(response.data);
        return response.overflow ? ESP_ERR_INVALID_SIZE : ESP_FAIL;
    }

    *body_out = response.data;
    return ESP_OK;
}

static esp_err_t store_update_price(const char *code, double price, char **body_out)
{
    *body_out = NULL;
    if (s_store_url[0] == '\0' || MIMI_SECRET_STORE_API_KEY[0] == '\0') {
        return ESP_ERR_INVALID_STATE;
    }

    char url[224];
    int written = snprintf(url, sizeof(url), "%s/api/admin/update-price", s_store_url);
    if (written < 0 || written >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;

    char form[192];
    written = snprintf(form, sizeof(form), "code=%s&price=%.2f", code, price);
    if (written < 0 || written >= (int)sizeof(form)) return ESP_ERR_INVALID_SIZE;

    store_response_t response = {
        .data = heap_caps_calloc(1, STORE_RESPONSE_SIZE, MALLOC_CAP_SPIRAM),
        .cap = STORE_RESPONSE_SIZE,
    };
    if (!response.data) return ESP_ERR_NO_MEM;

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = STORE_TIMEOUT_MS,
        .buffer_size = 2048,
        .event_handler = store_http_event_handler,
        .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        free(response.data);
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "application/x-www-form-urlencoded");
    esp_http_client_set_header(client, "X-MimiClaw-Key", MIMI_SECRET_STORE_API_KEY);
    esp_http_client_set_post_field(client, form, strlen(form));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || status != 200 || response.overflow) {
        ESP_LOGE(TAG, "Price update failed: err=%s status=%d", esp_err_to_name(err), status);
        free(response.data);
        return err != ESP_OK ? err : ESP_FAIL;
    }

    *body_out = response.data;
    return ESP_OK;
}

static esp_err_t store_update_service(const char *request_id, const char *status, char **body_out)
{
    *body_out = NULL;
    char url[224];
    int written = snprintf(url, sizeof(url), "%s/api/service-request/update", s_store_url);
    if (written < 0 || written >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;

    char form[192];
    written = snprintf(form, sizeof(form), "request_id=%s&status=%s", request_id, status);
    if (written < 0 || written >= (int)sizeof(form)) return ESP_ERR_INVALID_SIZE;

    store_response_t response = {
        .data = heap_caps_calloc(1, STORE_RESPONSE_SIZE, MALLOC_CAP_SPIRAM),
        .cap = STORE_RESPONSE_SIZE,
    };
    if (!response.data) return ESP_ERR_NO_MEM;
    esp_http_client_config_t config = {
        .url = url, .method = HTTP_METHOD_POST, .timeout_ms = STORE_TIMEOUT_MS,
        .buffer_size = 2048, .event_handler = store_http_event_handler, .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) { free(response.data); return ESP_FAIL; }
    esp_http_client_set_header(client, "Content-Type", "application/x-www-form-urlencoded");
    esp_http_client_set_header(client, "X-MimiClaw-Key", MIMI_SECRET_STORE_API_KEY);
    esp_http_client_set_post_field(client, form, strlen(form));
    esp_err_t err = esp_http_client_perform(client);
    int http_status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || http_status != 200 || response.overflow) {
        ESP_LOGE(TAG, "Service update failed: err=%s status=%d body=%s",
                 esp_err_to_name(err), http_status,
                 response.data && response.data[0] ? response.data : "(empty)");
        if (response.data && response.data[0] && !response.overflow) {
            *body_out = response.data;
        } else {
            free(response.data);
        }
        return err != ESP_OK ? err : ESP_FAIL;
    }
    *body_out = response.data;
    return ESP_OK;
}

static esp_err_t store_apply_pricing(const char *proposal_id, char **body_out)
{
    *body_out = NULL;
    char url[224];
    int written = snprintf(url, sizeof(url), "%s/api/admin/pricing-apply", s_store_url);
    if (written < 0 || written >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;
    char form[128];
    written = snprintf(form, sizeof(form), "proposal_id=%s", proposal_id);
    if (written < 0 || written >= (int)sizeof(form)) return ESP_ERR_INVALID_SIZE;
    store_response_t response = {
        .data = heap_caps_calloc(1, STORE_RESPONSE_SIZE, MALLOC_CAP_SPIRAM),
        .cap = STORE_RESPONSE_SIZE,
    };
    if (!response.data) return ESP_ERR_NO_MEM;
    esp_http_client_config_t config = {
        .url = url, .method = HTTP_METHOD_POST, .timeout_ms = STORE_TIMEOUT_MS,
        .buffer_size = 2048, .event_handler = store_http_event_handler, .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) { free(response.data); return ESP_FAIL; }
    esp_http_client_set_header(client, "Content-Type", "application/x-www-form-urlencoded");
    esp_http_client_set_header(client, "X-MimiClaw-Key", MIMI_SECRET_STORE_API_KEY);
    esp_http_client_set_post_field(client, form, strlen(form));
    esp_err_t err = esp_http_client_perform(client);
    int http_status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || http_status != 200 || response.overflow) {
        free(response.data);
        return err != ESP_OK ? err : ESP_FAIL;
    }
    *body_out = response.data;
    return ESP_OK;
}

static esp_err_t store_apply_restock(const char *proposal_id, char **body_out)
{
    *body_out = NULL;
    char url[224];
    int written = snprintf(url, sizeof(url), "%s/api/admin/restock-apply", s_store_url);
    if (written < 0 || written >= (int)sizeof(url)) return ESP_ERR_INVALID_SIZE;
    char form[128];
    written = snprintf(form, sizeof(form), "proposal_id=%s", proposal_id);
    if (written < 0 || written >= (int)sizeof(form)) return ESP_ERR_INVALID_SIZE;
    store_response_t response = {
        .data = heap_caps_calloc(1, STORE_RESPONSE_SIZE, MALLOC_CAP_SPIRAM),
        .cap = STORE_RESPONSE_SIZE,
    };
    if (!response.data) return ESP_ERR_NO_MEM;
    esp_http_client_config_t config = {
        .url = url, .method = HTTP_METHOD_POST, .timeout_ms = STORE_TIMEOUT_MS,
        .buffer_size = 2048, .event_handler = store_http_event_handler, .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) { free(response.data); return ESP_FAIL; }
    esp_http_client_set_header(client, "Content-Type", "application/x-www-form-urlencoded");
    esp_http_client_set_header(client, "X-MimiClaw-Key", MIMI_SECRET_STORE_API_KEY);
    esp_http_client_set_post_field(client, form, strlen(form));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || status != 200 || response.overflow) {
        free(response.data);
        return err != ESP_OK ? err : ESP_FAIL;
    }
    *body_out = response.data;
    return ESP_OK;
}

static bool contains_query(const char *value, const char *query)
{
    return !query || query[0] == '\0' || (value && strstr(value, query));
}

static void format_products(const char *body, const char *query, bool alerts_only,
                            char *output, size_t output_size)
{
    cJSON *root = cJSON_Parse(body);
    if (!root || !cJSON_IsArray(root)) {
        cJSON_Delete(root);
        snprintf(output, output_size, "Error: Work7_5 returned invalid product JSON");
        return;
    }

    size_t offset = 0;
    int matched = 0;
    bool truncated = false;
    cJSON *item;
    cJSON_ArrayForEach(item, root) {
        cJSON *code = cJSON_GetObjectItem(item, "qr_code");
        cJSON *name = cJSON_GetObjectItem(item, "name");
        const char *code_value = cJSON_IsString(code) ? code->valuestring : "";
        const char *name_value = cJSON_IsString(name) ? name->valuestring : "";
        if (code_value[0] == '\0' || name_value[0] == '\0') continue;
        if (!contains_query(code_value, query) && !contains_query(name_value, query)) continue;

        cJSON *price = cJSON_GetObjectItem(item, "price");
        cJSON *stock = cJSON_GetObjectItem(item, "stock");
        cJSON *sold = cJSON_GetObjectItem(item, "today_sold");
        cJSON *weigh = cJSON_GetObjectItem(item, "isWeigh");
        int stock_value = cJSON_IsNumber(stock) ? stock->valueint : 0;
        int sold_value = cJSON_IsNumber(sold) ? sold->valueint : 0;
        bool low_stock = stock_value < 10;
        bool less_than_three_days = sold_value > 0 && stock_value / sold_value < 3;
        if (alerts_only && !low_stock && !less_than_three_days) continue;
        int n;
        if (alerts_only) {
            const char *alert = stock_value <= 0 ? "out_of_stock" :
                                (less_than_three_days ? "less_than_3_days" : "low_stock");
            n = snprintf(output + offset, output_size - offset,
                         "%s | code=%s | stock=%d | sold_today=%d | alert=%s\n",
                         name_value, code_value, stock_value, sold_value, alert);
        } else {
            n = snprintf(output + offset, output_size - offset,
                         "%s | code=%s | price=%.2f | stock=%d | sold_today=%d | weigh=%s\n",
                         name_value, code_value,
                         cJSON_IsNumber(price) ? price->valuedouble : 0.0,
                         stock_value, sold_value, cJSON_IsTrue(weigh) ? "true" : "false");
        }
        if (n < 0 || (size_t)n >= output_size - offset) {
            truncated = true;
            break;
        }
        offset += (size_t)n;
        matched++;
    }
    cJSON_Delete(root);

    if (matched == 0) {
        snprintf(output, output_size, alerts_only ? "No current inventory alerts."
                                                   : "No matching products found.");
    } else if (truncated && output_size - offset > 24) {
        snprintf(output + offset, output_size - offset, "[result truncated]\n");
    }
}

esp_err_t tool_store_init(void)
{
    if (MIMI_SECRET_STORE_URL[0] != '\0') {
        strncpy(s_store_url, MIMI_SECRET_STORE_URL, sizeof(s_store_url) - 1);
        size_t len = strlen(s_store_url);
        while (len > 0 && s_store_url[len - 1] == '/') s_store_url[--len] = '\0';
    }

    if (s_store_url[0] == '\0') {
        ESP_LOGW(TAG, "Store URL is not configured; set MIMI_SECRET_STORE_URL");
    } else {
        ESP_LOGI(TAG, "Store tool initialized: %s", s_store_url);
    }
    return ESP_OK;
}

esp_err_t tool_store_execute(const char *input_json, char *output, size_t output_size)
{
    if (s_store_url[0] == '\0') {
        snprintf(output, output_size,
                 "Error: Work7_5 address is not configured. Set MIMI_SECRET_STORE_URL.");
        return ESP_ERR_INVALID_STATE;
    }

    cJSON *input = cJSON_Parse(input_json);
    cJSON *action = input ? cJSON_GetObjectItem(input, "action") : NULL;
    cJSON *query_item = input ? cJSON_GetObjectItem(input, "query") : NULL;
    if (!cJSON_IsString(action)) {
        cJSON_Delete(input);
        snprintf(output, output_size, "Error: action must be summary, products, trend, or weight");
        return ESP_ERR_INVALID_ARG;
    }

    const char *path = NULL;
    if (strcmp(action->valuestring, "summary") == 0) path = "/api/admin/summary";
    else if (strcmp(action->valuestring, "products") == 0) path = "/api/products";
    else if (strcmp(action->valuestring, "alerts") == 0) path = "/api/products";
    else if (strcmp(action->valuestring, "trend") == 0) path = "/api/admin/trend";
    else if (strcmp(action->valuestring, "weight") == 0) path = "/api/weight";
    else if (strcmp(action->valuestring, "customer_analytics") == 0) path = "/api/admin/customer-analytics";
    else if (strcmp(action->valuestring, "service_status") == 0) path = "/api/service-request";
    else if (strcmp(action->valuestring, "pricing_review") == 0) path = "/api/admin/pricing-review";
    else if (strcmp(action->valuestring, "pricing_status") == 0) path = "/api/admin/pricing-proposal";
    else if (strcmp(action->valuestring, "restock_review") == 0) path = "/api/admin/restock-review";
    else if (strcmp(action->valuestring, "restock_status") == 0) path = "/api/admin/restock-proposal";
    else if (strcmp(action->valuestring, "restock_apply") == 0) {
        cJSON *proposal_id = cJSON_GetObjectItem(input, "proposal_id");
        if (!cJSON_IsString(proposal_id) || proposal_id->valuestring[0] == '\0') {
            cJSON_Delete(input);
            snprintf(output, output_size, "Error: restock_apply requires proposal_id");
            return ESP_ERR_INVALID_ARG;
        }
        char *body = NULL;
        esp_err_t err = store_apply_restock(proposal_id->valuestring, &body);
        cJSON_Delete(input);
        if (err != ESP_OK) {
            snprintf(output, output_size, "Error: restock proposal apply failed (%s)", esp_err_to_name(err));
            return err;
        }
        snprintf(output, output_size, "%s", body);
        free(body);
        return ESP_OK;
    }
    else if (strcmp(action->valuestring, "pricing_apply") == 0) {
        cJSON *proposal_id = cJSON_GetObjectItem(input, "proposal_id");
        if (!cJSON_IsString(proposal_id) || proposal_id->valuestring[0] == '\0') {
            cJSON_Delete(input);
            snprintf(output, output_size, "Error: pricing_apply requires proposal_id");
            return ESP_ERR_INVALID_ARG;
        }
        char *body = NULL;
        esp_err_t err = store_apply_pricing(proposal_id->valuestring, &body);
        cJSON_Delete(input);
        if (err != ESP_OK) {
            snprintf(output, output_size, "Error: pricing proposal apply failed (%s)", esp_err_to_name(err));
            return err;
        }
        snprintf(output, output_size, "%s", body);
        free(body);
        return ESP_OK;
    }
    else if (strcmp(action->valuestring, "service_update") == 0) {
        cJSON *request_id = cJSON_GetObjectItem(input, "request_id");
        cJSON *status = cJSON_GetObjectItem(input, "status");
        bool valid_status = cJSON_IsString(status) &&
            (strcmp(status->valuestring, "accepted") == 0 ||
             strcmp(status->valuestring, "completed") == 0 ||
             strcmp(status->valuestring, "rejected") == 0);
        if (!cJSON_IsString(request_id) || request_id->valuestring[0] == '\0' || !valid_status) {
            cJSON_Delete(input);
            snprintf(output, output_size, "Error: service_update requires request_id and accepted, completed, or rejected status");
            return ESP_ERR_INVALID_ARG;
        }
        char request_id_copy[64];
        char status_copy[16];
        strlcpy(request_id_copy, request_id->valuestring, sizeof(request_id_copy));
        strlcpy(status_copy, status->valuestring, sizeof(status_copy));
        cJSON_Delete(input);

        /* Work7 enforces waiting -> accepted -> completed. Complete both
         * transitions when the merchant resolves a still-waiting ticket. */
        if (strcmp(status_copy, "completed") == 0) {
            char *current_body = NULL;
            esp_err_t current_err = store_get("/api/service-request", &current_body);
            if (current_err == ESP_OK && current_body) {
                cJSON *current = cJSON_Parse(current_body);
                cJSON *current_id = current ? cJSON_GetObjectItem(current, "request_id") : NULL;
                cJSON *current_status = current ? cJSON_GetObjectItem(current, "status") : NULL;
                if (cJSON_IsString(current_id) && cJSON_IsString(current_status) &&
                    strcmp(current_id->valuestring, request_id_copy) == 0 &&
                    strcmp(current_status->valuestring, "waiting") == 0) {
                    char *accepted_body = NULL;
                    esp_err_t accepted_err = store_update_service(request_id_copy, "accepted", &accepted_body);
                    free(accepted_body);
                    if (accepted_err != ESP_OK) {
                        cJSON_Delete(current);
                        free(current_body);
                        snprintf(output, output_size,
                                 "Error: could not accept waiting ticket before completion (%s)",
                                 esp_err_to_name(accepted_err));
                        return accepted_err;
                    }
                }
                cJSON_Delete(current);
                free(current_body);
            }
        }

        char *body = NULL;
        esp_err_t err = store_update_service(request_id_copy, status_copy, &body);
        if (err != ESP_OK) {
            snprintf(output, output_size, "Error: service request update failed (%s)%s%s",
                     esp_err_to_name(err), body && body[0] ? ": " : "", body && body[0] ? body : "");
            free(body);
            return err;
        }
        snprintf(output, output_size, "%s", body);
        free(body);
        return ESP_OK;
    }
    else if (strcmp(action->valuestring, "update_price") == 0) {
        cJSON *code = cJSON_GetObjectItem(input, "code");
        cJSON *price = cJSON_GetObjectItem(input, "price");
        if (!cJSON_IsString(code) || !cJSON_IsNumber(price) ||
            code->valuestring[0] == '\0' || price->valuedouble <= 0 || price->valuedouble > 99999) {
            cJSON_Delete(input);
            snprintf(output, output_size, "Error: update_price requires a product code and a price from 0.01 to 99999");
            return ESP_ERR_INVALID_ARG;
        }
        char *body = NULL;
        esp_err_t err = store_update_price(code->valuestring, price->valuedouble, &body);
        cJSON_Delete(input);
        if (err != ESP_OK) {
            snprintf(output, output_size, "Error: price update failed (%s)", esp_err_to_name(err));
            return err;
        }
        snprintf(output, output_size, "%s", body);
        free(body);
        return ESP_OK;
    }
    else {
        cJSON_Delete(input);
        snprintf(output, output_size, "Error: unsupported store action");
        return ESP_ERR_INVALID_ARG;
    }

    char query[128] = {0};
    if (cJSON_IsString(query_item)) {
        strncpy(query, query_item->valuestring, sizeof(query) - 1);
    }
    bool is_products = strcmp(action->valuestring, "products") == 0;
    bool is_alerts = strcmp(action->valuestring, "alerts") == 0;
    cJSON_Delete(input);

    char *body = NULL;
    esp_err_t err = store_get(path, &body);
    if (err != ESP_OK) {
        snprintf(output, output_size, "Error: cannot reach Work7_5 (%s)", esp_err_to_name(err));
        return err;
    }

    if (is_products || is_alerts) format_products(body, query, is_alerts, output, output_size);
    else snprintf(output, output_size, "%s", body);
    free(body);
    return ESP_OK;
}
