#!/usr/bin/env python3
import json
import struct
import time

import usb.core
import usb.util


VID = 0x34BF
PID = 0xFF0A


def configure_cdc(dev, control_interface, baud=150000, control_lines=0):
    coding = struct.pack("<IBBB", baud, 0, 0, 8)
    dev.ctrl_transfer(0x21, 0x20, 0, control_interface, coding, timeout=1000)
    # The official YDLidar SDK starts common single-channel units with DTR low.
    dev.ctrl_transfer(0x21, 0x22, control_lines, control_interface, None, timeout=1000)


def set_control_lines(dev, control_interface, value):
    dev.ctrl_transfer(0x21, 0x22, value, control_interface, None, timeout=1000)


def official_start(dev, control_interface, out_endpoint):
    # stop() -> stopMotor() -> scan -> startMotor(), matching YDlidarDriver.
    set_control_lines(dev, control_interface, 1)
    dev.write(out_endpoint, b"\xa5\x00", timeout=1000)
    time.sleep(0.005)
    dev.write(out_endpoint, b"\xa5\x65", timeout=1000)
    time.sleep(0.050)
    dev.write(out_endpoint, b"\xa5\x60", timeout=1000)
    set_control_lines(dev, control_interface, 0)
    time.sleep(0.5)


def read_both(dev, seconds):
    result = {0x84: bytearray(), 0x85: bytearray()}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for endpoint in (0x84, 0x85):
            try:
                result[endpoint].extend(bytes(dev.read(endpoint, 16384, timeout=30)))
            except usb.core.USBError as exc:
                if getattr(exc, "errno", None) != 110:
                    raise
    return {k: bytes(v) for k, v in result.items()}


def brief(data):
    return {
        "bytes": len(data),
        "aa55": data.count(b"\xaa\x55"),
        "first_hex": " ".join("%02x" % b for b in data[:64]),
    }


def main():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise SystemExit("STC USB device not found")

    detached = []
    claimed = []
    try:
        for interface in (0, 1, 2, 3):
            try:
                if dev.is_kernel_driver_active(interface):
                    dev.detach_kernel_driver(interface)
                    detached.append(interface)
            except (NotImplementedError, usb.core.USBError):
                pass
        for interface in (1, 3):
            usb.util.claim_interface(dev, interface)
            claimed.append(interface)

        configure_cdc(dev, 0)
        try:
            configure_cdc(dev, 2)
        except usb.core.USBError:
            # The malformed second CDC union may reject class requests on IF2.
            pass

        time.sleep(0.7)
        passive = read_both(dev, 2.0)
        official_start(dev, 0, 0x04)
        after_out04 = read_both(dev, 3.0)
        try:
            official_start(dev, 2, 0x05)
        except usb.core.USBError:
            # Malformed descriptors sometimes route both CDC control requests to IF0.
            official_start(dev, 0, 0x05)
        after_out05 = read_both(dev, 3.0)

        all_data = {}
        for endpoint in (0x84, 0x85):
            data = passive[endpoint] + after_out04[endpoint] + after_out05[endpoint]
            path = "/home/linaro/ai/lidar/probe_ep%02x.bin" % endpoint
            with open(path, "wb") as f:
                f.write(data)
            all_data[hex(endpoint)] = {
                "passive": brief(passive[endpoint]),
                "after_out04": brief(after_out04[endpoint]),
                "after_out05": brief(after_out05[endpoint]),
                "total": brief(data),
                "path": path,
            }
        print(json.dumps(all_data, ensure_ascii=False, indent=2))
    finally:
        for interface in reversed(claimed):
            try:
                usb.util.release_interface(dev, interface)
            except usb.core.USBError:
                pass
        for interface in detached:
            try:
                dev.attach_kernel_driver(interface)
            except usb.core.USBError:
                pass


if __name__ == "__main__":
    main()
