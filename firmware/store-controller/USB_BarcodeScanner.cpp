#include "USB_BarcodeScanner.h"

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <usb/usb_host.h>

namespace {

constexpr size_t MAX_BARCODE_LENGTH = 64;
constexpr int CODE_QUEUE_LENGTH = 4;

struct BarcodeMessage {
  char value[MAX_BARCODE_LENGTH + 1];
};

QueueHandle_t codeQueue = nullptr;
TaskHandle_t clientTaskHandle = nullptr;
usb_host_client_handle_t clientHandle = nullptr;
usb_device_handle_t deviceHandle = nullptr;
usb_transfer_t* inputTransfer = nullptr;

volatile bool hostInstalled = false;
volatile bool scannerConnected = false;
volatile bool deviceGone = false;
volatile bool transferInFlight = false;
volatile bool transferError = false;
volatile uint8_t pendingDeviceAddress = 0;

bool interfaceClaimed = false;
uint8_t interfaceNumber = 0;
uint8_t endpointAddress = 0;
String barcodeBuffer;
uint8_t previousKeys[6] = {0};

bool containsKey(const uint8_t* keys, uint8_t key) {
  for (int i = 0; i < 6; ++i) {
    if (keys[i] == key) return true;
  }
  return false;
}

char hidKeyToAscii(uint8_t key, bool shift) {
  if (key >= 0x04 && key <= 0x1d) {
    char value = 'a' + (key - 0x04);
    return shift ? value - ('a' - 'A') : value;
  }

  static const char normalDigits[] = "1234567890";
  static const char shiftedDigits[] = "!@#$%^&*()";
  if (key >= 0x1e && key <= 0x27) {
    int index = key - 0x1e;
    return shift ? shiftedDigits[index] : normalDigits[index];
  }

  if (key >= 0x59 && key <= 0x61) return '1' + (key - 0x59);
  if (key == 0x62) return '0';
  if (key == 0x63) return '.';
  if (key == 0x2c) return ' ';

  switch (key) {
    case 0x2d: return shift ? '_' : '-';
    case 0x2e: return shift ? '+' : '=';
    case 0x2f: return shift ? '{' : '[';
    case 0x30: return shift ? '}' : ']';
    case 0x31: return shift ? '|' : '\\';
    case 0x33: return shift ? ':' : ';';
    case 0x34: return shift ? '"' : '\'';
    case 0x35: return shift ? '~' : '`';
    case 0x36: return shift ? '<' : ',';
    case 0x37: return shift ? '>' : '.';
    case 0x38: return shift ? '?' : '/';
    default: return 0;
  }
}

void enqueueBarcode() {
  barcodeBuffer.trim();
  if (barcodeBuffer.length() == 0) return;

  BarcodeMessage message = {};
  barcodeBuffer.substring(0, MAX_BARCODE_LENGTH).toCharArray(message.value, sizeof(message.value));
  if (xQueueSend(codeQueue, &message, 0) != pdTRUE) {
    BarcodeMessage discarded;
    xQueueReceive(codeQueue, &discarded, 0);
    xQueueSend(codeQueue, &message, 0);
  }
  Serial.printf("[USB Scanner] barcode: %s\n", message.value);
  barcodeBuffer = "";
}

void processKeyboardReport(const uint8_t* report, int length) {
  if (length < 8) return;

  // Boot keyboards use eight bytes. Some report-mode scanners prepend one report ID.
  const uint8_t* keyboard = report + (length == 9 ? 1 : 0);
  const uint8_t modifier = keyboard[0];
  const uint8_t* keys = keyboard + 2;
  const bool shift = (modifier & 0x22) != 0;

  for (int i = 0; i < 6; ++i) {
    const uint8_t key = keys[i];
    if (key == 0 || containsKey(previousKeys, key)) continue;

    if (key == 0x28 || key == 0x58) {
      enqueueBarcode();
    } else if (key == 0x2a) {
      if (barcodeBuffer.length() > 0) barcodeBuffer.remove(barcodeBuffer.length() - 1);
    } else {
      const char value = hidKeyToAscii(key, shift);
      if (value != 0 && barcodeBuffer.length() < MAX_BARCODE_LENGTH) barcodeBuffer += value;
    }
  }
  memcpy(previousKeys, keys, sizeof(previousKeys));
}

void submitInputTransfer() {
  if (!inputTransfer || deviceGone || !deviceHandle) return;
  inputTransfer->device_handle = deviceHandle;
  inputTransfer->bEndpointAddress = endpointAddress;
  inputTransfer->callback = [](usb_transfer_t* transfer) {
    transferInFlight = false;
    if (transfer->status == USB_TRANSFER_STATUS_COMPLETED) {
      processKeyboardReport(transfer->data_buffer, transfer->actual_num_bytes);
      if (!deviceGone) submitInputTransfer();
    } else if (transfer->status == USB_TRANSFER_STATUS_NO_DEVICE ||
               transfer->status == USB_TRANSFER_STATUS_CANCELED) {
      deviceGone = true;
    } else {
      transferError = true;
    }
  };
  inputTransfer->context = nullptr;
  inputTransfer->num_bytes = inputTransfer->data_buffer_size;
  inputTransfer->timeout_ms = 0;
  esp_err_t err = usb_host_transfer_submit(inputTransfer);
  if (err == ESP_OK) {
    transferInFlight = true;
  } else {
    transferError = true;
    Serial.printf("[USB Scanner] submit failed: %s\n", esp_err_to_name(err));
  }
}

void cleanupDevice() {
  scannerConnected = false;
  barcodeBuffer = "";
  memset(previousKeys, 0, sizeof(previousKeys));

  if (inputTransfer && !transferInFlight) {
    usb_host_transfer_free(inputTransfer);
    inputTransfer = nullptr;
  }
  if (interfaceClaimed && deviceHandle) {
    usb_host_interface_release(clientHandle, deviceHandle, interfaceNumber);
    interfaceClaimed = false;
  }
  if (deviceHandle) {
    usb_host_device_close(clientHandle, deviceHandle);
    deviceHandle = nullptr;
  }

  endpointAddress = 0;
  deviceGone = false;
  transferError = false;
  Serial.println("[USB Scanner] disconnected");
}

bool findHidInputEndpoint(const usb_config_desc_t* config, uint8_t& foundInterface,
                          uint8_t& foundAlternate, uint8_t& foundEndpoint, int& foundMps) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(config);
  int offset = config->bLength;
  bool inHidInterface = false;
  uint8_t currentInterface = 0;
  uint8_t currentAlternate = 0;

  while (offset + 2 <= config->wTotalLength) {
    const usb_standard_desc_t* descriptor =
        reinterpret_cast<const usb_standard_desc_t*>(bytes + offset);
    if (descriptor->bLength < 2 || offset + descriptor->bLength > config->wTotalLength) break;

    if (descriptor->bDescriptorType == USB_B_DESCRIPTOR_TYPE_INTERFACE) {
      const usb_intf_desc_t* intf = reinterpret_cast<const usb_intf_desc_t*>(descriptor);
      inHidInterface = intf->bInterfaceClass == USB_CLASS_HID;
      currentInterface = intf->bInterfaceNumber;
      currentAlternate = intf->bAlternateSetting;
    } else if (inHidInterface && descriptor->bDescriptorType == USB_B_DESCRIPTOR_TYPE_ENDPOINT) {
      const usb_ep_desc_t* endpoint = reinterpret_cast<const usb_ep_desc_t*>(descriptor);
      const bool isInput = (endpoint->bEndpointAddress & USB_B_ENDPOINT_ADDRESS_EP_DIR_MASK) != 0;
      const bool isInterrupt =
          (endpoint->bmAttributes & USB_BM_ATTRIBUTES_XFERTYPE_MASK) == USB_BM_ATTRIBUTES_XFER_INT;
      if (isInput && isInterrupt) {
        foundInterface = currentInterface;
        foundAlternate = currentAlternate;
        foundEndpoint = endpoint->bEndpointAddress;
        foundMps = endpoint->wMaxPacketSize & 0x07FF;
        return foundMps > 0;
      }
    }
    offset += descriptor->bLength;
  }
  return false;
}

void openScanner(uint8_t address) {
  if (deviceHandle) return;

  esp_err_t err = usb_host_device_open(clientHandle, address, &deviceHandle);
  if (err != ESP_OK) {
    Serial.printf("[USB Scanner] device open failed: %s\n", esp_err_to_name(err));
    deviceHandle = nullptr;
    return;
  }

  const usb_device_desc_t* deviceDescriptor = nullptr;
  usb_host_get_device_descriptor(deviceHandle, &deviceDescriptor);

  const usb_config_desc_t* config = nullptr;
  err = usb_host_get_active_config_descriptor(deviceHandle, &config);
  if (err != ESP_OK || !config) {
    Serial.println("[USB Scanner] configuration descriptor unavailable");
    cleanupDevice();
    return;
  }

  uint8_t alternate = 0;
  int maxPacketSize = 0;
  if (!findHidInputEndpoint(config, interfaceNumber, alternate, endpointAddress, maxPacketSize)) {
    Serial.println("[USB Scanner] connected device is not a HID keyboard scanner");
    cleanupDevice();
    return;
  }

  err = usb_host_interface_claim(clientHandle, deviceHandle, interfaceNumber, alternate);
  if (err != ESP_OK) {
    Serial.printf("[USB Scanner] interface claim failed: %s\n", esp_err_to_name(err));
    cleanupDevice();
    return;
  }
  interfaceClaimed = true;

  const int transferSize = usb_round_up_to_mps(8, maxPacketSize);
  err = usb_host_transfer_alloc(transferSize, 0, &inputTransfer);
  if (err != ESP_OK) {
    Serial.printf("[USB Scanner] transfer allocation failed: %s\n", esp_err_to_name(err));
    cleanupDevice();
    return;
  }

  scannerConnected = true;
  if (deviceDescriptor) {
    Serial.printf("[USB Scanner] connected VID=%04X PID=%04X interface=%u endpoint=0x%02X\n",
                  deviceDescriptor->idVendor, deviceDescriptor->idProduct,
                  interfaceNumber, endpointAddress);
  } else {
    Serial.println("[USB Scanner] HID keyboard connected");
  }
  submitInputTransfer();
}

void clientEventCallback(const usb_host_client_event_msg_t* event, void*) {
  if (event->event == USB_HOST_CLIENT_EVENT_NEW_DEV) {
    if (!deviceHandle) pendingDeviceAddress = event->new_dev.address;
  } else if (event->event == USB_HOST_CLIENT_EVENT_DEV_GONE &&
             event->dev_gone.dev_hdl == deviceHandle) {
    scannerConnected = false;
    deviceGone = true;
  }
}

void hostLibraryTask(void*) {
  usb_host_config_t config = {};
  config.skip_phy_setup = false;
  config.intr_flags = ESP_INTR_FLAG_LEVEL1;
  //config.enum_filter_cb = nullptr;

  esp_err_t err = usb_host_install(&config);
  if (err != ESP_OK) {
    Serial.printf("[USB Scanner] host install failed: %s\n", esp_err_to_name(err));
    vTaskDelete(nullptr);
    return;
  }

  hostInstalled = true;
  xTaskNotifyGive(clientTaskHandle);
  Serial.println("[USB Scanner] USB Host ready on GPIO20(D+) / GPIO19(D-)");

  while (true) {
    uint32_t eventFlags = 0;
    usb_host_lib_handle_events(portMAX_DELAY, &eventFlags);
  }
}

void scannerClientTask(void*) {
  ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
  if (!hostInstalled) {
    vTaskDelete(nullptr);
    return;
  }

  usb_host_client_config_t config = {};
  config.is_synchronous = false;
  config.max_num_event_msg = 5;
  config.async.client_event_callback = clientEventCallback;
  config.async.callback_arg = nullptr;

  esp_err_t err = usb_host_client_register(&config, &clientHandle);
  if (err != ESP_OK) {
    Serial.printf("[USB Scanner] client register failed: %s\n", esp_err_to_name(err));
    vTaskDelete(nullptr);
    return;
  }

  while (true) {
    usb_host_client_handle_events(clientHandle, pdMS_TO_TICKS(50));

    if (deviceGone && !transferInFlight) {
      cleanupDevice();
    }

    if (transferError && !deviceGone && deviceHandle && inputTransfer && !transferInFlight) {
      transferError = false;
      usb_host_endpoint_clear(deviceHandle, endpointAddress);
      submitInputTransfer();
    }

    const uint8_t address = pendingDeviceAddress;
    if (address != 0 && !deviceHandle) {
      pendingDeviceAddress = 0;
      openScanner(address);
    }
  }
}

}  // namespace

void USB_BarcodeScanner_Init() {
#if !CONFIG_IDF_TARGET_ESP32S3
  Serial.println("[USB Scanner] unsupported target; ESP32-S3 is required");
  return;
#else
  if (codeQueue || clientTaskHandle) return;
  codeQueue = xQueueCreate(CODE_QUEUE_LENGTH, sizeof(BarcodeMessage));
  if (!codeQueue) {
    Serial.println("[USB Scanner] queue allocation failed");
    return;
  }

  if (xTaskCreate(scannerClientTask, "usb_scanner", 4096, nullptr, 3, &clientTaskHandle) != pdPASS ||
      xTaskCreate(hostLibraryTask, "usb_host", 4096, nullptr, 2, nullptr) != pdPASS) {
    Serial.println("[USB Scanner] task creation failed");
  }
#endif
}

bool USB_BarcodeScanner_Read(String& code) {
  if (!codeQueue) return false;
  BarcodeMessage message;
  if (xQueueReceive(codeQueue, &message, 0) != pdTRUE) return false;
  code = message.value;
  return true;
}

bool USB_BarcodeScanner_IsConnected() {
  return scannerConnected;
}

const char* USB_BarcodeScanner_State() {
  if (!hostInstalled) return "starting";
  if (scannerConnected) return "connected";
  return "waiting";
}
