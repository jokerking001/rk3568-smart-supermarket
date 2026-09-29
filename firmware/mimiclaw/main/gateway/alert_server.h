#pragma once

#include "esp_err.h"

/**
 * Initialize and start the Alert HTTP server.
 * Listens on MIMI_ALERT_PORT for POST /alert requests from external devices
 * (e.g., smart-store controller) and dispatches notifications via Feishu.
 */
esp_err_t alert_server_start(void);

/**
 * Stop the alert server.
 */
esp_err_t alert_server_stop(void);
