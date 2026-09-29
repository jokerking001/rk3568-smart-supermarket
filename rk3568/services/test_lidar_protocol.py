#!/usr/bin/env python3
import importlib.util
from pathlib import Path

module_path = Path(__file__).with_name("lidar_service_v2.py")
spec = importlib.util.spec_from_file_location("lidar_service_v2", str(module_path))
lidar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lidar)

SAMPLE = bytes.fromhex(
    "AA5500083D676B6A1D51"
    "117033123833121C3313F83213D03214A832105C32106C32"
)

assert lidar.packet_checksum(SAMPLE) == 0x511D
packet = lidar.parse_packet(SAMPLE)
assert packet["ct"] == 0
assert len(packet["points"]) == 8
assert [point[2] for point in packet["points"]] == [17, 18, 18, 19, 19, 20, 16, 16]
assert [round(point[1] * 1000) for point in packet["points"]] == [3292, 3278, 3271, 3262, 3252, 3242, 3223, 3227]

stream = lidar.StreamParser()
packets, errors = stream.feed(b"noise" + SAMPLE[:17])
assert not packets
packets, more_errors = stream.feed(SAMPLE[17:] + SAMPLE)
assert len(packets) == 2
assert errors + more_errors >= 1
print("PASS checksum=0x511d points=8 distances_mm=3292,3278,3271,3262,3252,3242,3223,3227")
