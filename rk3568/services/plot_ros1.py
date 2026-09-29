import collections
import serial
import sys
import struct
import math
import threading
import queue
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

PORT = '/dev/ttyUSB0'
BAUDRATE = 150000


class LidarPlotter:
    def __init__(self):
        self.ser = None
        self.read_thread = None
        self.data_queue = queue.Queue()

        self.MAX_POINTS = 2000

        self.current_scan_data = collections.deque(maxlen=self.MAX_POINTS)

        self.fig, self.ax = plt.subplots(subplot_kw={'projection': 'polar'})

        self.line, = self.ax.plot([], [], 'o', markersize=1)

        # --- 添加下面这行 ---
        # 预设R轴（距离）的范围。假设雷达最大距离为4000mm。
        self.ax.set_rlim(0, 4000)
        # ---------------------

        # (可选) 调整极坐标以匹配雷达习惯
        self.ax.set_theta_zero_location('N')  # 0度角设置在正上方
        self.ax.set_theta_direction(-1)     # 角度顺时针增加

    @staticmethod
    def parse_packet(packet_data):
        ct_header = packet_data[2]
        lsn = packet_data[3]
        fsangle_raw = struct.unpack('<H', packet_data[4:6])[0]
        lsangle_raw = struct.unpack('<H', packet_data[6:8])[0]
        angle_start_deg = (fsangle_raw >> 1)/64.0
        angle_end_deg = (lsangle_raw >> 1)/64.0
        if lsn > 1:
            diff_angle_deg = (angle_end_deg-angle_start_deg) % 360.0
        else:
            diff_angle_deg = 0.0
        points = []
        for i in range(lsn):
            offset = 8+i*3
            dist_raw = struct.unpack('<H', packet_data[offset:offset+2])[0]
            distance_mm = dist_raw/4.0
            if lsn > 1:
                angle_deg = (diff_angle_deg/(lsn-1))*i+angle_start_deg
            else:
                angle_deg = angle_start_deg
            angle_rad = math.radians(angle_deg % 360)
            if distance_mm > 0:
                points.append((angle_rad, distance_mm))
        return points

    def _read_serial_loop(self):

        try:
            sync_state = 0

            while True:
                if sync_state == 0:
                    byte1 = self.ser.read(1)
                    if not byte1:
                        continue

                    if byte1 == b'\xaa':
                        sync_state = 1

                elif sync_state == 1:
                    byte2 = self.ser.read(1)
                    if not byte2:
                        sync_state = 0
                        continue
                    if byte2 == b'\x55':
                        header_rest = self.ser.read(2)

                        if not header_rest or len(header_rest) < 2:
                            sync_state = 0
                            continue
                        _ct_data = header_rest[0]
                        lsn = header_rest[1]

                        remaining_bytes = 4+((lsn)*3)
                        packet_payload = self.ser.read(remaining_bytes)

                        if len(packet_payload) == remaining_bytes:
                            full_packet = b'\xaa\x55'+header_rest+packet_payload

                            points = self.parse_packet(full_packet)
                            print(points)

                            if points:
                                self.data_queue.put(points)

                        sync_state = 0

                    elif byte2 == b'\xaa':
                        sync_state = 1
                    else:
                        sync_state = 0
        except serial.SerialException as e:
            print(f"[Thread Error]: {e}")
        except Exception as e:
            print(f"[Thread Error], {e}")

        print("TT exit")

    def update_plot(self, frame):

        angles, distances = [], []
        try:
            while True:

                points_list = self.data_queue.get_nowait()

                self.current_scan_data.extend(points_list)

        except queue.Empty:
            pass

        if self.current_scan_data:
            angles, distances = zip(*self.current_scan_data)
        else:
            angles, distances = [], []

        self.line.set_data(angles, distances)

        return self.line,

    def start(self):
        try:
            print(f"open TT {PORT}")
            self.ser = serial.Serial(PORT, baudrate=BAUDRATE, timeout=1)

            print("read TTL")
            self.read_thread = threading.Thread(
                target=self._read_serial_loop, daemon=True)
            self.read_thread.start()

            print("plot begin")
            self.ani = FuncAnimation(
                self.fig,
                self.update_plot,
                interval=100,
                blit=True,
                cache_frame_data=False
            )

            plt.show()

        except serial.SerialException as e:
            print(f"can't upen TTL")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n exit")

        finally:
            if self.ser and self.ser.is_open:
                self.ser.close()
                print("port closed")


if __name__ == "__main__":
    plotter = LidarPlotter()
    plotter.start()
