#ifndef VOICE_INTERACTION_H
#define VOICE_INTERACTION_H

#include <Arduino.h>

// 功放
#define I2S_DOUT  8
#define I2S_BCLK  46
#define I2S_LRC   9

//麦克风
#define I2S_SD    47
#define I2S_SCK   48
#define I2S_WS    13



void Voice_Init();
void Voice_HandleLoop();
void Voice_TestSpeaker();
void Voice_StartRecording();
String Voice_URLEncode(const String& str);
void Voice_PlayTTS(String text);
void Voice_StopRecording();
void Voice_PlayRecording();    // 播放录下的音频（测试用）
void Voice_HandleKey();
void Voice_RecordLoop();

void Voice_WakeupLoop();

void Test_SpeakerHardware();

void Test_Pure_AI_Model();

// 【新增】：导出语音任务队列
extern String pendingVoiceTask; 

#endif
