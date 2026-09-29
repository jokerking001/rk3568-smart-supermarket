#include "led.h"

// 实例化 WS2812 对象
Adafruit_NeoPixel pixels(NUM_LEDS, WS2812_PIN, NEO_GRB + NEO_KHZ800);

void led_init() {
  pixels.begin();           // 初始化 WS2812
  pixels.setBrightness(50); // 设置全局亮度 (0-255，别设太高刺眼)
  pixels.clear();           // 熄灭所有灯
  pixels.show();            // 将状态推送到灯带
}

// 🔋 低功耗状态指示灯 (LED 0)
void led_low_power_mode(bool isLowPower) {
  if (isLowPower) {
    // 假设低功耗时亮起幽幽的“冰川蓝” (R:0, G:100, B:255)
    pixels.setPixelColor(0, pixels.Color(0, 100, 255));
  } else {
    // 唤醒/正常工作时，比如亮起“翠绿” (R:0, G:255, B:0)
    pixels.setPixelColor(0, pixels.Color(0, 255, 0));
  }
  pixels.show();
}

// 🎤 录音状态指示灯 (LED 1 和 LED 2)
void rec_led_set(bool isRecording) {
  if (isRecording) {
    // 录音时，2个灯一起爆亮红色！
    pixels.setPixelColor(1, pixels.Color(255, 0, 0));
    pixels.setPixelColor(2, pixels.Color(255, 0, 0));
  } else {
    // 停止录音，熄灭这两个灯 (或者设成微弱的待机色)
    pixels.setPixelColor(1, pixels.Color(0, 0, 0));
    pixels.setPixelColor(2, pixels.Color(0, 0, 0));
  }
  pixels.show();
}

// ================= 按键部分 =================
void key_init() {
  pinMode(key_pin, INPUT_PULLUP); // 根据你的电路配置上拉或下拉
}

bool key_isPressed() {
  // 假设按下为低电平，加个简单的防抖
  if (digitalRead(key_pin) == LOW) {
    delay(15); 
    if (digitalRead(key_pin) == LOW) {
      return true;
    }
  }
  return false;
}