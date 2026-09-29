#include "ros/init.h"
#include "ros/node_handle.h"
#include "ros/time.h"
#include <bits/stdint-uintn.h>
#include <cmath>
#include <cstddef>
#include <exception>
#include <iomanip>
#include <ros/ros.h>
#include <sensor_msgs/LaserScan.h>
#include <sstream>
#include <std_msgs/UInt8MultiArray.h>
#include <string.h>
#include <string>
#include <vector>

/**
 * @brief 步骤 B: C++ 解析器节点
 * * 订阅: /lidar/raw_packet (std_msgs/UInt8MultiArray)
 * * 发布: /scan (sensor_msgs/LaserScan)
 */
class LidarParser
{
  public:
    LidarParser (ros::NodeHandle &nh)
    {
        // 从参数服务器获取 frame_id, 如果未设置则默认为 "laser_frame"
        nh.param ("frame_id", frame_id_, std::string ("laser_frame"));

        // 初始化发布器: 发布到 /scan
        pub_ = nh.advertise<sensor_msgs::LaserScan> ("scan", 10);

        // 初始化订阅者: 订阅来自 Python 节点的 /lidar/raw_packet
        sub_ = nh.subscribe ("/lidar/raw_packet", 10,
                             &LidarParser::rawPacketCallback, this);

        ROS_INFO ("parser started");
        ROS_INFO_STREAM ("sub /lidear/raw_packet");
        ROS_INFO_STREAM ("pub /scan (frame_id:" << frame_id_ << ")");
    }

    ~LidarParser () { ROS_INFO ("parser closed"); }

  private:
    /**
     * @brief 存储单个雷达点的数据结构
     */
    struct LidarPoint
    {
        double angle_red;
        double distance_m;
    };

    /**
     * @brief 字节转 uint16_t (小端)
     */
    inline uint16_t
    bytesToUint16 (const std::vector<uint8_t> &data, size_t index)
    {
        return (static_cast<uint16_t> (data[index + 1]) << 8) | data[index];
    }

    std::vector<LidarPoint>
    parsePacket (const std::vector<uint8_t> &packet_data)
    {
        if (packet_data.size () < 8)
            {
                ROS_WARN_THROTTLE (1.0, "data too short");
                return {};
            }

        uint8_t lsn = packet_data[3];

        size_t expected_size = 8 + (static_cast<size_t> (lsn) * 3);
        if (packet_data.size () != expected_size)
            {
                ROS_WARN_STREAM_THROTTLE (
                    1.0, "size not paired LSN=" << (int)lsn << "expected size"
                                                << expected_size << "real size"
                                                << packet_data.size ());
                return {};
            }
        uint16_t fsangle_raw = bytesToUint16 (packet_data, 4);
        uint16_t lsangle_raw = bytesToUint16 (packet_data, 6);

        double angle_start_deg = static_cast<double> (fsangle_raw >> 1) / 64.0;
        double angle_end_deg = static_cast<double> (lsangle_raw >> 1) / 64.0;

        double diff_angle_deg = 0.0;
        if (lsn > 1)
            {
                diff_angle_deg = angle_end_deg - angle_start_deg;
                if (diff_angle_deg < 0)
                    {
                        diff_angle_deg += 360.0;
                    }
            }
        std::vector<LidarPoint> points;
        points.reserve (lsn);
        for (int i = 0; i < lsn; ++i)
            {
                size_t offset = 8 + i * 3;

                uint16_t dist_raw = bytesToUint16 (packet_data, offset);
                double distance_m
                    = static_cast<double> (dist_raw) / 4.0 / 1000.0;

                double angle_deg = angle_start_deg;
                if (lsn > 1)
                    {
                        angle_deg = (diff_angle_deg / (lsn - 1)) * i
                                    + angle_start_deg;
                    }
                angle_deg = std::fmod (angle_deg, 360.0);
                if (angle_deg < 0)
                    {
                        angle_deg += 360.0;
                    }
                if (distance_m > 0.01)
                    {
                        LidarPoint p;
                        p.angle_red = M_PI * angle_deg / 180.0;
                        p.distance_m = distance_m;
                        points.push_back (p);
                    }
            }
        return points;
    }

    void
    publishScan (const std::vector<LidarPoint> &points)
    {
        if (points.empty ())
            return;
        sensor_msgs::LaserScan scan;
        scan.header.stamp = ros::Time::now ();
        scan.header.frame_id = frame_id_;

        scan.angle_min = points.front ().angle_red;
        scan.angle_max = points.back ().angle_red;

        if (points.size () > 1)
            {
                scan.angle_increment
                    = (scan.angle_max - scan.angle_min) / (points.size () - 1);
            }
        else
            {
                scan.angle_increment = 0.0;
            }

        scan.time_increment = 0.0001;
        scan.scan_time = 0.1;

        scan.range_min = 0.05;
        scan.range_max = 8.0;

        scan.ranges.resize (points.size ());
        for (size_t i = 0; i < points.size (); ++i)
            {
                scan.ranges[i] = static_cast<float> (points[i].distance_m);
            }
        pub_.publish (scan);
    }

    void
    rawPacketCallback (const std_msgs::UInt8MultiArrayConstPtr &msg)
    {
        std::vector<LidarPoint> points = parsePacket (msg->data);

        if (!points.empty ())
            {
                publishScan (points);

                ROS_INFO_STREAM_THROTTLE (1.0, "parse successed and pubed"
                                                   << points.size ()
                                                   << "points");
            }
        else
            {
                ROS_WARN_DELAYED_THROTTLE (1.0,
                                           "recived a pkg,parse not data");
            }
    }

    ros::Publisher pub_;
    ros::Subscriber sub_;
    std::string frame_id_;
};

int
main (int argc, char **argv)
{
    ros::init (argc, argv, "lidar_parser_node");
    ros::NodeHandle nh ("~");

    try
        {
            LidarParser parser (nh);

            ros::spin ();
        }
    catch (const std::exception &e)
        {
            ROS_ERROR_STREAM ("parse failed" << e.what ());
            return 1;
        }

    return 0;
}
