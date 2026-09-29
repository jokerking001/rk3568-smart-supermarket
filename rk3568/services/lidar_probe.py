#!/usr/bin/env python3
import argparse
import json
import os
import select
import time

import serial


def read_for(ser, seconds):
    end = time.monotonic() + seconds
    data = bytearray()
    while time.monotonic() < end:
        ready, _, _ = select.select([ser.fileno()], [], [], 0.1)
        if ready:
            chunk = os.read(ser.fileno(), 65536)
            if chunk:
                data.extend(chunk)
    return bytes(data)


def packet_summary(data):
    packets = []
    pos = 0
    while True:
        start = data.find(b"\xaa\x55", pos)
        if start < 0 or start + 4 > len(data):
            break
        lsn = data[start + 3]
        size = 8 + 3 * lsn
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
        })
        pos = start + size
    return packets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/lidar")
    ap.add_argument("--baud", type=int, default=150000)
    ap.add_argument("--passive", type=float, default=2.0)
    ap.add_argument("--active", type=float, default=5.0)
    ap.add_argument("--output", default="/home/linaro/ai/lidar/probe_raw.bin")
    args = ap.parse_args()

    ser = serial.Serial(
        args.port,
        args.baud,
        timeout=0,
        write_timeout=0.5,
        exclusive=True,
    )
    ser.reset_input_buffer()
    passive = read_for(ser, args.passive)
    ser.write(b"\xa5\x60")
    ser.flush()
    active = read_for(ser, args.active)
    ser.close()

    data = passive + active
    with open(args.output, "wb") as f:
        f.write(data)
    packets = packet_summary(data)
    print(json.dumps({
        "port": os.path.realpath(args.port),
        "baud": args.baud,
        "passive_bytes": len(passive),
        "active_bytes": len(active),
        "total_bytes": len(data),
        "aa55_headers": data.count(b"\xaa\x55"),
        "parsed_packets": len(packets),
        "first_64_hex": " ".join("%02x" % b for b in data[:64]),
        "first_packets": packets[:12],
        "output": args.output,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
