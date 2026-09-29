#!/usr/bin/env python3
import argparse
import json
import struct
import time

import usb.core
import usb.util


VID = 0x34BF
PID = 0xFF0A


def read_bulk(dev, endpoint, seconds):
    data = bytearray()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            chunk = dev.read(endpoint, 16384, timeout=100)
            data.extend(bytes(chunk))
        except usb.core.USBError as exc:
            if getattr(exc, "errno", None) != 110:
                raise
    return bytes(data)


def summarize(data):
    packets = []
    pos = 0
    while True:
        start = data.find(b"\xaa\x55", pos)
        if start < 0 or start + 10 > len(data):
            break
        lsn = data[start + 3]
        size = 10 + 3 * lsn
        if lsn == 0 or start + size > len(data):
            pos = start + 1
            continue
        packet = data[start:start + size]
        fsa = int.from_bytes(packet[4:6], "little")
        lsa = int.from_bytes(packet[6:8], "little")
        packets.append({
            "offset": start,
            "ct": packet[2],
            "lsn": lsn,
            "size": size,
            "start_deg": round((fsa >> 1) / 64.0, 3),
            "end_deg": round((lsa >> 1) / 64.0, 3),
            "checksum": packet[8:10].hex(),
        })
        pos = start + size
    return packets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--control-interface", type=int, default=2)
    ap.add_argument("--data-interface", type=int, default=3)
    ap.add_argument("--in-endpoint", type=lambda x: int(x, 0), default=0x85)
    ap.add_argument("--out-endpoint", type=lambda x: int(x, 0), default=0x05)
    ap.add_argument("--baud", type=int, default=150000)
    ap.add_argument("--passive", type=float, default=3.0)
    ap.add_argument("--active", type=float, default=6.0)
    ap.add_argument("--output", default="/home/linaro/ai/lidar/probe_usb_if3.bin")
    args = ap.parse_args()

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise SystemExit("STC USB device not found")
    if dev.is_kernel_driver_active(args.data_interface):
        dev.detach_kernel_driver(args.data_interface)
    usb.util.claim_interface(dev, args.data_interface)
    try:
        # CDC ACM: SET_LINE_CODING and assert DTR+RTS on the second function.
        line_coding = struct.pack("<IBBB", args.baud, 0, 0, 8)
        dev.ctrl_transfer(0x21, 0x20, 0, args.control_interface, line_coding, timeout=1000)
        dev.ctrl_transfer(0x21, 0x22, 3, args.control_interface, None, timeout=1000)
        passive = read_bulk(dev, args.in_endpoint, args.passive)
        dev.write(args.out_endpoint, b"\xa5\x60", timeout=1000)
        active = read_bulk(dev, args.in_endpoint, args.active)
    finally:
        try:
            dev.ctrl_transfer(0x21, 0x22, 0, args.control_interface, None, timeout=1000)
        except Exception:
            pass
        usb.util.release_interface(dev, args.data_interface)

    data = passive + active
    with open(args.output, "wb") as f:
        f.write(data)
    packets = summarize(data)
    print(json.dumps({
        "interfaces": [args.control_interface, args.data_interface],
        "endpoints": [hex(args.in_endpoint), hex(args.out_endpoint)],
        "baud": args.baud,
        "passive_bytes": len(passive),
        "active_bytes": len(active),
        "aa55_headers": data.count(b"\xaa\x55"),
        "parsed_checksum_packets": len(packets),
        "first_64_hex": " ".join("%02x" % b for b in data[:64]),
        "first_packets": packets[:12],
        "output": args.output,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
