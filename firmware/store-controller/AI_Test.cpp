// ============================================================
//  ⚠️ 本文件属于**原主控形态**，从机模式（SLAVE_MODE=1）下整段不编译。
// ============================================================
//  RK3568 接管主控后，这个模块的职责已经搬到板端：
//      语音问答调 DashScope → 由 RK3568 侧处理
//  保留代码是为了 SLAVE_MODE=0 时能原样回退，不是从机固件的一部分。
//
//  为什么用 #if 而不是把文件挪走：Arduino 编译 sketch 目录下所有 .cpp，
//  挪走会让 SLAVE_MODE=0 也编不过；加保护则两套形态共存、互不干扰。
// ============================================================
#include "Slave_Config.h"

#if !SLAVE_MODE

#include "AI_Test.h"
#include "secrets.h"        // 私密配置（密钥/地址），不进版本库
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h> 
#include <ArduinoJson.h>
#include <SPIFFS.h>          // 🔑 新增：用于底层直读账号数据库
#include "Product_Data.h"
#include "Voice_Interaction.h"
#include "Plus.h"
#include "RFID_Reader.h"     // 🔑 新增：用于调用刷卡硬件

const char* ai_api_key = DASHSCOPE_API_KEY;
const char* ai_api_url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation";

// ========== 模块一：短期记忆变量 ==========
static String chat_history = ""; 

void AI_Init() {
  chat_history = "";
}

String AI_Ask(String question)
{
  delay(100); 

  if (ESP.getMaxAllocHeap() < 25000) 
  {
      Serial.printf("连续内存不足(仅剩 %d 字节)，AI请求拦截\n", ESP.getMaxAllocHeap());
      return "系统繁忙，请稍后再问。";
  }

  WiFiClientSecure client;
  client.setInsecure(); 
  client.setHandshakeTimeout(15); 

  HTTPClient http;
  http.setReuse(false);   
  http.setTimeout(15000); 

  http.begin(client, ai_api_url); 
  http.addHeader("Content-Type", "application/json");
  String authHeader = "Bearer " + String(ai_api_key);
  http.addHeader("Authorization", authHeader);

  String escaped = question;
  escaped.replace("\\", "\\\\");
  escaped.replace("\"", "\\\"");
  escaped.replace("\n", "\\n");
  escaped.replace("\r", "");

  String payload = "{\"model\":\"qwen-turbo\","
                   "\"input\":{\"messages\":["
                   "{\"role\":\"system\",\"content\":\"你是超市智能导购，回答必须极其简短（30字以内），口语化，适合直接转为语音播报。绝对不要说'好的'、'很高兴为您服务'等废话。\"},"
                   "{\"role\":\"user\",\"content\":\"" + escaped + "\"}"
                   "]}}";

  int httpCode = http.POST(payload);
  String result = "千问暂时无法回答";

  if (httpCode == 200) {
    String response = http.getString();
    DynamicJsonDocument doc(1024);
    deserializeJson(doc, response);
    result = doc["output"]["text"].as<String>();
  } else {
    result = "请求失败，状态码：" + String(httpCode);
    Serial.println("HTTPS 请求内部错误：" + http.errorToString(httpCode)); 
  }
  
  http.end();
  client.stop(); 
  return result;
}

String AI_Analyze(String question)
{
  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient http;
  http.begin(client, ai_api_url);
  http.addHeader("Content-Type", "application/json");
  String authHeader = "Bearer " + String(ai_api_key);
  http.addHeader("Authorization", authHeader);

  String escaped = question;
  escaped.replace("\\", "\\\\");
  escaped.replace("\"", "\\\"");
  escaped.replace("\n", "\\n");
  escaped.replace("\r", "");

  String payload = "{\"model\":\"qwen-turbo\","
                   "\"input\":{\"messages\":["
                   "{\"role\":\"system\",\"content\":\"你是一位拥有20年经验的超市运营总监。请根据提供的商品数据给出专业建议。每条建议引用具体数字，结构清晰。字数300-500字。\"},"
                   "{\"role\":\"user\",\"content\":\"" + escaped + "\"}"
                   "]}}";

  int httpCode = http.POST(payload);
  String result = "AI分析暂时无法完成";

  if (httpCode == 200) {
    String response = http.getString();
    DynamicJsonDocument doc(2048);
    deserializeJson(doc, response);
    result = doc["output"]["text"].as<String>();
  } else {
    result = "分析请求失败，状态码：" + String(httpCode);
  }
  http.end();
  return result;
}

// ========== 模块一点五：RFID 店长权限验证系统 ==========
static bool verifyManagerCard(String targetUid) {
  if (targetUid == "") return false;
  if (!SPIFFS.exists("/users.json")) return false;
  
  File f = SPIFFS.open("/users.json", "r");
  if (!f) return false;
  
  DynamicJsonDocument doc(4096);
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) return false;

  // 遍历所有账号，寻找 UID 匹配 且 角色(r)必须是 manager 的员工
  for (JsonPair kv : doc.as<JsonObject>()) {
    if (kv.value()["uid"].as<String>() == targetUid && kv.value()["r"].as<String>() == "manager") {
      return true; // 确认是店长！
    }
  }
  return false;
}

// ========== 模块二：Agent 工具核心执行器 ==========
static String Agent_ExecuteTool(String jsonStr) {
  DynamicJsonDocument actionDoc(512);
  DeserializationError err = deserializeJson(actionDoc, jsonStr);
  
  if (err) return "指令解析失败";

  if (actionDoc["action"] == "update_price") {
    String targetName = actionDoc["name"].as<String>();
    float newPrice = actionDoc["price"].as<float>();
    targetName.trim();

    if (targetName == "" || targetName == "null") {
      return "改价失败：未指明具体的商品名称。";
    }

    // =========================================================
    // 🚨 核心风控：RFID 物理活体拦截与权限验证
    // =========================================================
    Serial.println("【安全系统】检测到高风险指令，启动物理验证...");
    
    // 1. 让设备直接开口提示顾客/员工需要刷卡
    Voice_PlayTTS("已拦截调价指令，请在十秒内刷店长卡确认权限。");
    
    unsigned long startTime = millis();
    bool cardSwiped = false;
    String swipedUid = "";

    // 彻底清空可能残留的旧刷卡状态
    if (RFID_IsNewCard()) { RFID_GetLastUID(); } 

    // 2. 阻塞 10 秒，等待活体刷卡
    while (millis() - startTime < 10000) {
      if (RFID_IsNewCard()) {
        swipedUid = RFID_GetLastUID();
        cardSwiped = true;
        break;
      }
      delay(50); // 喂狗防死机
    }

    // 3. 验证结果分支
    if (!cardSwiped) {
      Serial.println("【安全系统】操作超时，已取消。");
      return "操作超时，未检测到授权卡片，价格未修改。";
    }

    if (!verifyManagerCard(swipedUid)) {
      Serial.println("【安全系统】越权访问拒绝！卡号：" + swipedUid);
      return "权限不足。该卡片不是店长卡，拒绝调价。";
    }
    
    Serial.println("【安全系统】店长活体认证通过！允许放行指令。");
    // =========================================================

    int matchIndex = -1;

    // 4. 执行原本的改价逻辑 (双向模糊包含算法)
    for (int i = 0; i < PRODUCT_COUNT; i++) {
      if (products[i].qrCode != "") {
        String dbName = products[i].name;
        dbName.trim();
        if (dbName == targetName || dbName.indexOf(targetName) >= 0 || targetName.indexOf(dbName) >= 0) {
          matchIndex = i;
          break;
        }
      }
    }

    if (matchIndex != -1) {
      products[matchIndex].price = newPrice;
      Product_Save(); // 持久化写入 N16R8 的 Flash
      Serial.printf("【Agent 成功】%s 价格成功变更为 %.2f 元\n", products[matchIndex].name.c_str(), newPrice);
      return "认证成功，" + products[matchIndex].name + "的价格已修改为" + String(newPrice, 1) + "元。";
    } else {
      return "认证成功，但我在店里没有找到与【" + targetName + "】相关的商品。";
    }
  }
  return "未知指令";
}

// ========== 模块三：主大脑思考中枢 ==========
String AI_ProcessQuestion(String question)
{
  float t = getTemperature();
  float h = getHumidity();

  String fullPrompt = "【当前超市环境】温度：" + String(t, 1) + "℃，湿度：" + String(h, 1) + "%\n";
  
  if (chat_history != "") {
    fullPrompt += "【前一轮对话历史（用于理解“它”等代词）】\n" + chat_history + "\n";
  }

  fullPrompt += "【商品数据】\n" + Product_Data_ToText();
  fullPrompt += "\n【顾客提问】：" + question;
  
  fullPrompt += "\n【回复严格规则】：\n"
                "1. 若问题与超市无关，必须且只能回答：'抱歉我没听懂你在说什么'。\n"
                "2. 正常咨询时，字数限制30字内，严禁废话。\n"
                "3. 【最高指令】：如果提问者是要求修改商品价格（如打折、降价、调价等），请你务必且只能回复一个严格的JSON字符串，绝对不要输出任何其他中文字符！格式必须为：{\"action\":\"update_price\",\"name\":\"商品名\",\"price\":新价格的浮点数}";

  Serial.println("正在向千问大模型思考回答...");
  String answer = AI_Ask(fullPrompt);
  answer.trim(); 

  // ========== AI Agent 动作拦截器 (Tool Calling) ==========
  if (answer.startsWith("{") && answer.endsWith("}")) {
    Serial.println("【Agent 触发】拦截到动作指令：" + answer);
    String agentResult = Agent_ExecuteTool(answer);
    return agentResult; 
  }

  chat_history = "Q:" + question + " A:" + answer;

  if (answer.indexOf("企业") >= 0 || answer.length() > 50 || answer.indexOf("抱歉") >= 0) {
      if (question.indexOf("保质期") < 0 && answer.indexOf("保质期") >= 0) {
          return "抱歉我没听懂你在说什么";
      }
  }

  return answer;
}

void AI_HandleLoop()
{
  if (Serial.available())
  {
    String question = Serial.readStringUntil('\n');
    question.trim();
    if (question.length() > 0)
    {
      Serial.println("顾客：" + question);
      String fullPrompt = "以下是超市当前数据：\n" + Product_Data_ToText() + "\n用户问题：" + question + "\n请根据上述数据回答，不得编造。";
      String answer = AI_Ask(fullPrompt);
      Serial.println("千问：" + answer);
    }
  }
}

#endif  // !SLAVE_MODE
