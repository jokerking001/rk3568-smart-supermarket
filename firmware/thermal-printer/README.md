# re_min ESP-IDF thermal printer

ESP32-S3 firmware for the modified `thermal printer 1.5` board.

The printer polls Work7_20 for 384-dot monochrome raster jobs. Work7_20's web
pages rasterize Chinese receipts and daily reports with the browser's fonts,
so the printer firmware does not need a large CJK font table.

Safety behavior:

- All six strobes and the 8.1 V head switch are low at boot.
- Heating is blocked when the paper sensor is inactive.
- Heat power is enabled only around each 220 us strobe pulse.
- No test page is printed at boot.

The 40 x 60 mm layout uses a 320 x 480 dot content area centered on the
384-dot print head (8 dots/mm). Longer daily reports may use more than one
60 mm section on continuous paper.
