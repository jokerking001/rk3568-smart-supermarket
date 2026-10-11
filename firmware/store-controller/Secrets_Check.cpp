// ============================================================
//  密钥 / 现场配置 启动自检
// ============================================================
//
//  为什么需要这个文件
//  ------------------
//  `secrets.h` 不进版本库（见 .gitignore），别人 clone 下来只能照
//  `secrets.h.example` 抄一份 —— 里面全是 `your-xxx` / `sk-xxxx` /
//  `change-me-` 这类占位值。
//
//  不管它的话，运行期表现是：
//      「百度Token获取失败，状态码：401」
//  这行日志会把人往两个错误方向带：以为密钥填错了，或者以为网络不通。
//  **实际是根本没填。** 2026-10-11 就为这个 401 白查过一轮。
//
//  所以这里在 setup() 里打一次汇总，把「没配」和「配错」分开。
//
//  ⚠️ 只打状态，**绝不打印密钥值** —— 仓库有凭据残留守卫
//     （tools/check_literals.py），SSID 也算凭据。
//
//  哪些密钥值得查
//  --------------
//  只有**真正编译进从机固件**的才查。另外三个在 `#if !SLAVE_MODE`
//  分支里，从机形态下根本不参与编译，填不填都不影响：
//      DASHSCOPE_API_KEY  -> 只在 AI_Test.cpp（主控）
//      MIMICLAW_STORE_KEY -> 只在 WebServer.cpp（主控）
//      JUHE_WEATHER_KEY   -> WIFI_Test.cpp 的 WIFI_Requst() 全工程无调用者
//  详见 docs/RK3568-迁移总览.md §15.6。
#include <Arduino.h>
#include "Slave_Config.h"


// 打一行状态。返回 true 表示「这项已配好」。
static bool report(const char *label, const char *value, const char *impact)
{
    bool ok = !Secret_IsPlaceholder(value);
    Serial.printf("  %-20s %s", label, ok ? "[已配置]" : "[仍是占位值]");
    if (!ok) Serial.printf("  -> %s", impact);
    Serial.println();
    return ok;
}


void Secrets_SelfCheck()
{
    Serial.println();
    Serial.println("========== 密钥 / 配置自检 ==========");

#if SLAVE_MODE
    Serial.println("从机固件实际只用下面这几项（其余密钥在主控分支里，本固件不编译）");
    Serial.println("--- 语音（百度 ASR + TTS）---");
    int missing = 0;
    if (!report("BAIDU_API_KEY", BAIDU_API_KEY, "语音识别与播报不可用")) missing++;
    if (!report("BAIDU_SECRET_KEY", BAIDU_SECRET_KEY, "语音识别与播报不可用")) missing++;

    Serial.println("--- 联网 ---");
    if (!report("WIFI_STA_SSID", WIFI_STA_SSID, "连不上热点，全部上报都会失败")) missing++;
    if (!report("WIFI_STA_PASSWORD", WIFI_STA_PASSWORD, "连不上热点，全部上报都会失败")) missing++;

    Serial.println("-------------------------------------");
    if (missing == 0) {
        Serial.println("✅ 全部已配置");
    } else {
        Serial.printf("⚠️  有 %d 项未配置，文件：firmware/store-controller/secrets.h\n", missing);
        Serial.println("    填好后重烧：python build_slave.py --upload <COM口>");
        if (Secret_IsPlaceholder(BAIDU_API_KEY) || Secret_IsPlaceholder(BAIDU_SECRET_KEY)) {
            Serial.println("    百度语音密钥申请：https://console.bce.baidu.com/ai/#/ai/speech/overview/index");
            Serial.println("    （首次进控制台自动发免费额度，个人认证即可，永久有效）");
        }
    }
#else
    Serial.println("主控形态：语音走百度、AI 问答走 DASHSCOPE、天气走聚合数据。");
    int missing = 0;
    if (!report("BAIDU_API_KEY", BAIDU_API_KEY, "语音不可用")) missing++;
    if (!report("BAIDU_SECRET_KEY", BAIDU_SECRET_KEY, "语音不可用")) missing++;
    if (!report("DASHSCOPE_API_KEY", DASHSCOPE_API_KEY, "AI 问答不可用")) missing++;
    if (!report("WIFI_STA_SSID", WIFI_STA_SSID, "连不上热点")) missing++;
    if (!report("WIFI_STA_PASSWORD", WIFI_STA_PASSWORD, "连不上热点")) missing++;
    Serial.println("-------------------------------------");
    Serial.println(missing == 0 ? "✅ 全部已配置"
                                : "⚠️  有未配置项，见 secrets.h");
#endif

    Serial.println("=====================================");
    Serial.println();
}
