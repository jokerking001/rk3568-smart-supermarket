# MimiClaw Smart Store Handoff

Updated: 2026-07-24 (Asia/Shanghai)

## Read First

Read `PROJECT_HANDOFF_2026-07-23.md` for the customer app, merchant web UI, Cloudflare Tunnel, MimiClaw, and Work7_20 history. This document records today's vision and USB-camera work.

## Current Hardware Work

The target vision setup is a XIAO ESP32S3 Sense / ESP32-S3 visual node plus the existing store controller:

- Existing store controller project: `firmware/store-controller`
- The HX711 load cell remains in Work7_20. Do not move HX711 code to the XIAO vision node.
- HX711 public API in Work7_20: `float Scale_GetWeight();`
- XIAO fruit-classification project: `firmware/vision-node`
- Model archive: `<本地下载目录>/xiao-esp32s3-fruits-classify_inferencing.zip`
- Model labels: `apple`, `banana`, `grapes`

The intended architecture is: the XIAO sends visual recognition/confidence to Work7_20, and Work7_20 combines it with the locally connected HX711 weight. Work7_20 already has the visual fusion endpoint work from earlier:

- `POST /api/vision-fusion`
- `GET /api/vision-fusion/latest`

## Arduino USB Webcam Probe

The user connected a USB webcam to the ESP32-S3 native USB Host/OTG interface and asked for Arduino code.

Created and compiled successfully:

- Arduino sketch: `<本地工程目录>\usb_host_uvc_probe_arduino\usb_host_uvc_probe_arduino.ino`
- Notes: `<本地工程目录>\usb_host_uvc_probe_arduino\README.md`
- Compile command used:

```powershell
& '<本地工程目录>\.tools\arduino-cli\arduino-cli.exe' compile `
  --config-file '<本地工程目录>\.arduino-build\arduino-cli.yaml' `
  --fqbn esp32:esp32:esp32s3 `
  '<本地工程目录>\usb_host_uvc_probe_arduino'
```

Compilation result: success, 312789 bytes (23%) flash and 17992 bytes (5%) RAM.

### Sketch Behaviour

The sketch starts ESP32-S3 USB Host, then prints USB VID/PID, interfaces, and endpoints. It identifies UVC camera interfaces:

- Video Control: class `0x0E`, subclass `0x01`
- Video Streaming: class `0x0E`, subclass `0x02`

It does not provide a camera web page yet. It is intentionally an enumeration/compatibility probe first.

### Current Observed Blocker

The serial output after connecting the USB webcam was:

```text
[USB] Host ready. Plug in the USB camera.
E (...) HUB: Configuration descriptor larger than control transfer max length
E (...) HUB: Stage failed: CHECK_SHORT_CONFIG_DESC
```

This means the USB Host hardware started correctly, but the webcam's configuration descriptor is larger than the maximum compiled into the installed Arduino ESP32 core.

Verified installed core setting:

```text
CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE=256
```

Location:

`%LOCALAPPDATA%\Arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.1-632e0c2a\esp32s3\sdkconfig`

Important: this value is embedded in the Arduino core's precompiled USB Host library. Adding a `#define` or changing only the `.ino` sketch cannot fix it. A compatible camera with a shorter descriptor may enumerate, or the ESP32 Arduino core/IDF USB Host library must be rebuilt with this value raised (use 1024 or 2048).

Do not claim that a video preview page works until UVC enumeration succeeds. Even then, use a powered USB OTG hub if the camera cannot receive enough stable 5V supply from the board.

## Flash/Boot Recovery Status

After uploading, the board later printed continuously:

```text
E (...) boot: Failed to verify partition table
E (...) boot: load partition table error!
```

This is a corrupted/mismatched flash partition table, unrelated to the UVC descriptor error. The recovery procedure is:

1. In Arduino IDE select the actual board. For the XIAO, select `XIAO_ESP32S3`, not generic `ESP32S3 Dev Module`.
2. Select the correct serial port.
3. Set `Tools -> Erase All Flash Before Sketch Upload -> Enabled`.
4. Upload again.
5. Set erase back to Disabled after recovery if desired.

This erases flash-resident programs/settings on that board, including prior Wi-Fi settings, but does not affect any PC project files. A successful boot of the probe should show:

```text
[USB] Host ready. Plug in the USB camera.
```

## Next Actions

1. First restore the target board using the flash recovery steps above.
2. Confirm the exact board used for the USB webcam test. If it is the XIAO, compile/upload with its XIAO board definition.
3. Decide whether to rebuild the USB Host core with a larger descriptor limit or test another UVC webcam.
4. Only after `RESULT: UVC camera detected` appears, implement a constrained UVC stream receiver and local monitoring page.
5. Keep the original XIAO OV3660 visual model project separate from the USB webcam experiment; do not overwrite it.

## Safety / Project Rules

- Do not remove or rewrite existing Work7_20 HX711 logic.
- Do not copy Wi-Fi passwords, API keys, or Cloudflare credentials into handoff files.
- Work7_20 source updates should be compiled/flashed by the user unless explicitly requested otherwise.
- Preserve the existing customer and merchant UI work; this USB webcam test is an independent hardware experiment.
