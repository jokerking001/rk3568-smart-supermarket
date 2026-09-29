#!/usr/bin/env python3
"""Unit tests for the scanner decode path.

The scanner hardware is not attached to the board yet, so the parts that can still
be proven are proven here:

  * the native ``struct input_event`` layout unpacks correctly on this architecture
  * Linux keycodes decode to the right ASCII, including the shifted layer
  * a code is committed on Enter, and also when a burst simply stops (gap timeout)
  * the lidar's serial device is never mistaken for a scanner (handoff section 6)

Run:  python3 test_scanner_decode.py
"""

import struct
import sys

import scanner_service as sc

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s %s" % (name, detail))


class Recorder(sc.Scanner):
    """Scanner with emit() intercepted so no network call is attempted."""

    def __init__(self):
        sc.Scanner.__init__(self, "http://127.0.0.1:1")
        self.emitted = []

    def emit(self, code, source="scanner", add_to_cart=False, session="default"):
        self.emitted.append({"code": code, "source": source})
        return {"ok": True, "code": code, "message": ""}


# keycode -> expected character, as it would arrive from the kernel
DIGITS = {7: "6", 10: "9", 11: "0", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5",
          8: "7", 9: "8"}


def main():
    print("== scanner decode tests ==")

    # --- 1. struct layout -------------------------------------------------
    check("input_event struct size matches the kernel ABI",
          sc.INPUT_EVENT.size in (16, 24), "size=%d" % sc.INPUT_EVENT.size)
    check("struct size equals sizeof(long)*2 + 8",
          sc.INPUT_EVENT.size == struct.calcsize("l") * 2 + 8,
          "size=%d long=%d" % (sc.INPUT_EVENT.size, struct.calcsize("l")))

    packed = sc.INPUT_EVENT.pack(1, 2, sc.EV_KEY, 2, 1)
    sec, usec, etype, code, value = sc.INPUT_EVENT.unpack_from(packed, 0)
    check("round-trip unpack yields the same fields",
          (sec, usec, etype, code, value) == (1, 2, sc.EV_KEY, 2, 1),
          "%s" % ((sec, usec, etype, code, value),))

    # --- 2. digit decode via the real _on_key path ------------------------
    scanner = Recorder()
    barcode = "6901234567890"
    for char in barcode:
        code = [k for k, v in DIGITS.items() if v == char][0]
        scanner._on_key(code, False)
    check("buffer accumulates the scanned digits", scanner.buffer == barcode,
          "buffer=%r" % scanner.buffer)

    scanner._on_key(28, False)  # KEY_ENTER
    check("Enter commits exactly one code",
          len(scanner.emitted) == 1 and scanner.emitted[0]["code"] == barcode,
          "%s" % scanner.emitted)
    check("buffer is cleared after commit", scanner.buffer == "", repr(scanner.buffer))

    # --- 3. letters and shift --------------------------------------------
    scanner = Recorder()
    for code in (30, 31, 32):            # a, s, d
        scanner._on_key(code, False)
    check("lowercase letters decode", scanner.buffer == "asd", repr(scanner.buffer))

    scanner = Recorder()
    for code in (30, 31, 32):            # A, S, D with shift held
        scanner._on_key(code, True)
    check("shift produces uppercase", scanner.buffer == "ASD", repr(scanner.buffer))

    # --- 4. shifted symbol layer -----------------------------------------
    scanner = Recorder()
    scanner._on_key(2, True)             # shift+1 -> !
    scanner._on_key(12, True)            # shift+- -> _
    check("shifted symbols decode", scanner.buffer == "!_", repr(scanner.buffer))

    # --- 5. backspace -----------------------------------------------------
    scanner = Recorder()
    for code in (2, 3, 4):               # 1, 2, 3
        scanner._on_key(code, False)
    scanner._on_key(14, False)           # KEY_BACKSPACE
    check("backspace removes one character", scanner.buffer == "12", repr(scanner.buffer))

    # --- 6. gap timeout commits without Enter ----------------------------
    scanner = Recorder()
    for code in (2, 3, 4):               # 1, 2, 3
        scanner._on_key(code, False)
    scanner.last_char_at = scanner.last_char_at - (sc.GAP_TIMEOUT + 0.05)
    scanner._flush_if_stale()
    check("gap timeout commits a code with no trailing Enter",
          len(scanner.emitted) == 1 and scanner.emitted[0]["code"] == "123",
          "%s" % scanner.emitted)

    # --- 7. no spurious commit -------------------------------------------
    scanner = Recorder()
    scanner._flush_if_stale()
    check("empty buffer never emits", scanner.emitted == [], "%s" % scanner.emitted)

    # --- 8. overlong input is dropped, not truncated silently -------------
    scanner = Recorder()
    for _ in range(sc.MAX_CODE_LEN + 10):
        scanner._push("9")
    check("overlong scan is dropped, buffer stays empty",
          scanner.stats["dropped"] == 1 and scanner.buffer == "",
          "dropped=%s buffer_len=%d" % (scanner.stats["dropped"], len(scanner.buffer)))
    scanner._commit()
    check("a dropped overlong burst never emits a truncated barcode",
          scanner.emitted == [], "%s" % scanner.emitted)
    scanner._push("1")
    scanner._on_key(28, False)
    check("decoder recovers after a dropped burst",
          scanner.emitted and scanner.emitted[-1]["code"] == "1", "%s" % scanner.emitted)

    # --- 9. serial line decode -------------------------------------------
    scanner = Recorder()
    for char in "6901234567891\r\n":
        if char in ("\r", "\n"):
            scanner._commit()
        else:
            scanner._push(char)
    check("serial CR/LF terminator commits one code",
          len(scanner.emitted) == 1 and scanner.emitted[0]["code"] == "6901234567891",
          "%s" % scanner.emitted)

    # --- 10. never grab the lidar as a serial scanner ---------------------
    scanner = Recorder()
    scanner._push("abc")
    check("buffer sanity before serial checks", scanner.buffer == "abc", repr(scanner.buffer))

    real_exists, real_realpath = sc.os.path.exists, sc.os.path.realpath
    try:
        sc.os.path.exists = lambda p: p in ("/dev/ttyACM0", "/dev/ttyACM1")
        sc.os.path.realpath = lambda p: "/dev/ttyACM0" if p in ("/dev/lidar", "/dev/ttyACM0") else "/dev/ttyACM1"
        picked = sc.detect_serial_scanner()
        check("serial auto-detect skips the lidar device", picked == "/dev/ttyACM1",
              "picked=%s" % picked)
    finally:
        sc.os.path.exists, sc.os.path.realpath = real_exists, real_realpath

    # --- 11. input device classification ---------------------------------
    # os.path.join uses the host separator, so normalise before keying the fake map.
    def fake_names(mapping):
        normalised = {k.replace("\\", "/"): v for k, v in mapping.items()}
        return lambda p: normalised[p.replace("\\", "/")]

    real_listdir, real_name = sc.os.listdir, sc.device_name
    try:
        sc.os.listdir = lambda p: ["event0", "event1", "event2"]
        sc.device_name = fake_names({
            "/dev/input/event0": "rk8xx-keypad",
            "/dev/input/event1": "USB Barcode Scanner",
            "/dev/input/event2": "Goodix Capacitive TouchScreen",
        })
        found = sc.detect_scanner()
        check("scanner is found by name",
              found and found["path"].replace("\\", "/") == "/dev/input/event1", "%s" % found)
    finally:
        sc.os.listdir, sc.device_name = real_listdir, real_name

    try:
        sc.os.listdir = lambda p: ["event0", "event1"]
        sc.device_name = fake_names({
            "/dev/input/event0": "rk8xx-keypad",
            "/dev/input/event1": "Goodix Capacitive TouchScreen",
        })
        found = sc.detect_scanner()
        check("touchscreen is never claimed as a scanner", found is None, "%s" % found)
    finally:
        sc.os.listdir, sc.device_name = real_listdir, real_name

    print("\n== %d passed, %d failed ==" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
