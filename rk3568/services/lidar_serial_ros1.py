#!/usr/bin/env python3
"""
步骤 A: Lidar 驱动节点 (Python)

本节点执行以下操作:
1. 使用 pyserial (已验证可行) 在 150000 波特率下打开 /dev/ttyUSB0。
2. 使用您原始 plot.py 中的状态机逻辑查找 0xAA 0x55 帧头。
3. 读取完整的原始数据包 (Header + Payload)。
4. 将此原始数据包 (bytes) 作为 std_msgs/UInt8MultiArray 发布到 /lidar/raw_packet 话题。
5. C++ 节点 (步骤 B) 将订阅此话题。
"""

import rospy
from std_msgs.msg import UInt8MultiArray
import serial
import sys
import struct # 我们需要 struct 来获取 LSN

# --- ROS 和 Lidar 配置 ---
NODE_NAME = 'lidar_raw_driver'
RAW_PACKET_TOPIC = '/lidar/raw_packet'
PORT = '/dev/ttyUSB0'
BAUDRATE = 150000

def run_driver():
    """
    主驱动函数，包含状态机和 ROS 发布器。
    """
    # 1. 初始化 ROS 节点和发布器
    rospy.init_node(NODE_NAME)
    pub = rospy.Publisher(RAW_PACKET_TOPIC, UInt8MultiArray, queue_size=10)
    
    rospy.loginfo(f"节点 {NODE_NAME} 已启动。")
    rospy.loginfo(f"正在发布原始数据包到 {RAW_PACKET_TOPIC}")

    ser = None
    try:
        # 2. 打开串口 (使用 Python pyserial)
        ser = serial.Serial(PORT, BAUDRATE, timeout=1)
        rospy.loginfo(f"成功打开端口 {PORT} @ {BAUDRATE} 波特率。")
        
        # 3. 移植自 plot.py 的状态机
        sync_state = 0
        
        while not rospy.is_shutdown():
            if sync_state == 0:
                # 状态 0: 查找 0xAA
                byte1 = ser.read(1)
                if not byte1:
                    continue  # 超时
                if byte1 == b'\xaa':
                    sync_state = 1

            elif sync_state == 1:
                # 状态 1: 查找 0x55
                byte2 = ser.read(1)
                if not byte2:
                    sync_state = 0
                    continue # 超时
                
                if byte2 == b'\x55':
                    # 4. 成功找到帧头 (AA 55)!
                    # 读取 CT (1 byte) + LSN (1 byte)
                    header_rest = ser.read(2)
                    if not header_rest or len(header_rest) < 2:
                        sync_state = 0
                        continue # 数据包不完整
                    
                    # 我们必须解包 LSN (第2个字节) 才能知道包有多长
                    # (CT 是 header_rest[0], LSN 是 header_rest[1])
                    lsn = header_rest[1] # 在 Python 3.x 中，这会得到一个 int
                    
                    # 计算剩余负载长度: 4 bytes (Fs, Ls) + LSN * 3 bytes (points)
                    remaining_bytes = 4 + (lsn * 3)
                    
                    # 读取剩余的包
                    packet_payload = ser.read(remaining_bytes)

                    if len(packet_payload) == remaining_bytes:
                        # 5. 成功读取完整数据包!
                        full_packet = b'\xaa\x55' + header_rest + packet_payload
                        
                        # 6. 将其发布为 ROS 消息
                        msg = UInt8MultiArray()
                        msg.data = full_packet # ROS Python 自动处理 bytes -> list[int]
                        pub.publish(msg)
                        
                        if lsn > 0:
                             # 确认我们收到了您的 Python 日志中看到的非零 LSN
                             rospy.loginfo_throttle(1.0, f"已发布原始数据包 (LSN={lsn}, 总大小={len(full_packet)})")
                        
                    else:
                        rospy.logwarn("读取 payload 超时，数据包可能已损坏。")

                    sync_state = 0 # 重置状态机

                elif byte2 == b'\xaa':
                    sync_state = 1 # 保持在状态 1 (处理 0xAA 0xAA 0x55)
                else:
                    sync_state = 0 # 同步失败

    except serial.SerialException as e:
        rospy.logfatal(f"[致命错误] 无法打开或读取端口 {PORT}: {e}")
        rospy.logfatal("请检查： 1. 雷达是否连接? 2. 您是否有权限 (例如 'dialout' 组)?")
        sys.exit(1)
    
    except rospy.ROSInterruptException:
        rospy.loginfo("节点被关闭 (Ctrl+C)。")

    finally:
        if ser and ser.is_open:
            ser.close()
            rospy.loginfo(f"端口 {PORT} 已关闭。")


if __name__ == '__main__':
    run_driver()
