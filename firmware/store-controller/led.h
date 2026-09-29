#ifndef __LED_H
#define __LED_H

#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

// 🌟 只需要这 1 个 IO 口 (根据你的 PCB 原理图，WS2812 接在 IO18)
#define WS2812_PIN 18
#define NUM_LEDS   3    // 一共 3 个灯珠

// 物理按键引脚保持不变
#define key_pin 39

// ================= API 接口 =================
void led_init();

// 低功耗指示灯控制 (控制第 1 个灯，索引为 0)
void led_low_power_mode(bool isLowPower);

// 录音指示灯控制 (控制第 2、3 个灯，索引为 1、2)
void rec_led_set(bool isRecording);

// 按键函数
void key_init();
bool key_isPressed();

#endif