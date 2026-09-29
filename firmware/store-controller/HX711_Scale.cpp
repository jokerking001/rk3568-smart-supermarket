#include "HX711_Scale.h"

static int32_t tareOffset = 0;
static float   lastStableWeight = 0;
static bool    initOk = false;

// 补齐缺失的轮询时间控制和缓存变量
static unsigned long lastScaleTime = 0;
static const unsigned long scaleInterval = 500; // 每 500ms 后台轮询一次
static float currentWeight = 0.0;
static const int SNAPSHOT_SAMPLES = 8;
static float weightSamples[SNAPSHOT_SAMPLES] = {0};
static int weightSampleCount = 0;
static int weightSampleIndex = 0;
static unsigned long lastValidWeightTime = 0;

// 读取 HX711 24位有符号原始值，返回 INT32_MIN 表示超时
static int32_t hx711_readRaw()
{
  // 等待 DT 变低（数据就绪），超时 100ms
  unsigned long start = millis();
  while (digitalRead(HX711_DT_PIN) == HIGH)
  {
    if (millis() - start > 100) return INT32_MIN;
    delayMicroseconds(1);
  }

  int32_t val = 0;
  for (int i = 0; i < 24; i++)
  {
    digitalWrite(HX711_SCK_PIN, HIGH);
    delayMicroseconds(1);
    val = (val << 1) | digitalRead(HX711_DT_PIN);
    digitalWrite(HX711_SCK_PIN, LOW);
    delayMicroseconds(1);
  }

  // 第 25 个脉冲：设置增益为 A通道 128倍
  digitalWrite(HX711_SCK_PIN, HIGH);
  delayMicroseconds(1);
  digitalWrite(HX711_SCK_PIN, LOW);

  // 24位有符号 → 32位有符号
  if (val & 0x800000) val |= 0xFF000000;

  return val;
}

void Scale_Init()
{
  pinMode(HX711_DT_PIN, INPUT);
  pinMode(HX711_SCK_PIN, OUTPUT);
  digitalWrite(HX711_SCK_PIN, LOW);

  Serial.println("HX711 称重模块初始化，去皮中...");

  // 丢弃前几次不稳定读数
  for (int i = 0; i < 5; i++)
  {
    hx711_readRaw();
    delay(50);
  }

  // 采 10 次取平均值作为零点
  int64_t sum = 0;
  int count = 0;
  for (int i = 0; i < 10; i++)
  {
    int32_t raw = hx711_readRaw();
    if (raw != INT32_MIN)
    {
      sum += raw;
      count++;
    }
    delay(50);
  }

  if (count > 0)
  {
    tareOffset = (int32_t)(sum / count);
    Serial.printf("去皮完成，零点值：%d\n", tareOffset);
    initOk = true;
  }
  else
  {
    tareOffset = 0;
    Serial.println("HX711 初始化失败！请检查接线。");
    initOk = false;
    return;
  }

  lastStableWeight = 0;
  Serial.println("HX711 称重模块就绪。");
}

float Scale_GetWeight()
{
  if (!initOk) return -1;

  int32_t raw = hx711_readRaw();
  if (raw == INT32_MIN) return -1;  // HX711 未就绪

  float weight = (float)((raw - tareOffset) / SCALE_CAL_FACTOR * 100);
  if (weight < 0) weight = 0;
  return weight;
}

void Scale_Tare()
{
  if (!initOk) return;

  int64_t sum = 0;
  int count = 0;
  for (int i = 0; i < 10; i++)
  {
    int32_t raw = hx711_readRaw();
    if (raw != INT32_MIN)
    {
      sum += raw;
      count++;
    }
    delay(20);
  }

  if (count > 0)
  {
    tareOffset = (int32_t)(sum / count);
  }
  lastStableWeight = 0;
  weightSampleCount = 0;
  weightSampleIndex = 0;
  Serial.printf("手动去皮完成，新零点：%d\n", tareOffset);
}

bool Scale_GetSnapshot(float *weight, bool *stable, unsigned long *ageMs)
{
  if (!weight || !stable || !ageMs || !initOk || weightSampleCount < 5 || lastValidWeightTime == 0) return false;
  float minWeight = weightSamples[0];
  float maxWeight = weightSamples[0];
  float sum = 0;
  for (int i = 0; i < weightSampleCount; i++) {
    minWeight = min(minWeight, weightSamples[i]);
    maxWeight = max(maxWeight, weightSamples[i]);
    sum += weightSamples[i];
  }
  *weight = sum / weightSampleCount;
  *stable = (maxWeight - minWeight) <= 4.0f;
  *ageMs = millis() - lastValidWeightTime;
  return *ageMs <= 1500;
}

bool Scale_IsProductRemoved()
{
  if (!initOk) return false;

  float current = Scale_GetWeight();
  if (current < 0) return false;

  float drop = lastStableWeight - current;
  if (drop > 30)
  {
    Serial.printf("检测到商品取走！重量从 %.1fg 降至 %.1fg\n", lastStableWeight, current);
    lastStableWeight = current;
    return true;
  }

  if (current > lastStableWeight)
  {
    lastStableWeight = current;
  }

  return false;
}

void Scale_HandleLoop()
{
  unsigned long currentMillis = millis();
  
  // 非阻塞定时器：每隔 scaleInterval (500ms) 执行一次
  if (currentMillis - lastScaleTime >= scaleInterval)
  {
    lastScaleTime = currentMillis;
    
    // 调用你自己写的重量获取函数
    float tempWeight = Scale_GetWeight();
    
    // 如果返回的不是 -1 (错误码)，就更新缓存
    if (tempWeight >= 0)
    {
      currentWeight = tempWeight;
      weightSamples[weightSampleIndex] = tempWeight;
      weightSampleIndex = (weightSampleIndex + 1) % SNAPSHOT_SAMPLES;
      if (weightSampleCount < SNAPSHOT_SAMPLES) weightSampleCount++;
      lastValidWeightTime = currentMillis;
      
      // 检测商品是否被取走（后台悄悄运行，只在发生骤降时打印）
      Scale_IsProductRemoved();
      
      // ==========================================
      // 把下面这两行注释掉，彻底关掉串口刷屏！
      // Serial.print("当前重量: ");
      // Serial.printf("%.1f g\n", currentWeight);
      // ==========================================
    }
  }
}
