# M5Stack Cardputer-Adv 离线语音智能助手

<div align="center">

![Hardware](https://img.shields.io/badge/Hardware-M5Stack%20Cardputer--Adv-blue?style=flat-square)
![Framework](https://img.shields.io/badge/Firmware-ESPHome%202026.x-green?style=flat-square)
![Platform](https://img.shields.io/badge/Hub-Home%20Assistant-orange?style=flat-square)
![Privacy](https://img.shields.io/badge/Privacy-100%25%20Local%20Autonomy-purple?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-lightgrey?style=flat-square)

**专为 M5Stack Cardputer-Adv 打造的 100% 局域网离线自治语音对讲终端**  
*真·物理声画同步频谱波形 ｜ 0ms 瞬间按键打断 ｜ 全局最高优先级音量 HUD ｜ 全屋家电多步动作调度*

</div>

---

## 📸 核心特性亮点

* 🔒 **100% 局域网离线自治**：语音流与指令绝不出家门，完全依靠家庭局域网服务器运行，零公网依赖。
* 🌊 **真实麦克风声画同步波形**：录音时通过 I2S 获取物理麦克风真实 RMS 振幅，在 135x240 彩屏上渲染 18 根对称动态能量柱。
* ⏱️ **秒表流光扫描动画**：本地大模型思考时毫秒级秒表走字 + 激光雷达扫描动画，告别死等焦虑。
* 🔊 **全场景全局音量控制**：
  * **按键直调**：无论在任何画面（哪怕 AI 正在说话播报时），按键盘右上角 **`-`** / **`+`**（或第二行 **`[`** / **`]`**）即时调节，弹出青色 HUD 能量条悬浮窗，不误打断声音，不误触发对讲！
  * **双向同步**：自动向 Home Assistant 注册 `Cardputer Speaker Volume` 滑块实体，具备断电记忆功能。
* ⚡ **0ms 瞬间按键打断 (Barge-in)**：AI 回复播报过长？按任意非音量按键，底层在 0 毫秒内掐断 I2S DMA 扬声器缓冲并静音，瞬间回归待命。
* ⏳ **5 秒平滑倒计时回归**：播报完毕后对话卡片保留 5 秒供查阅，下方青色微进度条缩完后自动清屏回归主界面；无需硬等，任意时刻按键均可秒开下一轮对讲。
* 🏠 **解决大模型乱写代码痛点**：内置智能动作网关，将自然语言意图无缝转译为 Home Assistant 物理服务调用，完美支持复合指令（如*“把灯打开过5秒再关上”*）。
* 🔤 **1296 个中文字库精编**：包含现代汉语高频字、全屋家电词汇与繁体字兜底，消灭缺字告警，确保 60FPS 丝滑渲染。

---

## 🛠️ 新手从零到一极速部署

本项目已经为你打包了所有服务端与固件配置，**新手请直接参阅图文详尽的操作指南**：

👉 **[📖 5 阶段手把手新手落地部署指南 (SETUP_WALKTHROUGH.md)](docs/SETUP_WALKTHROUGH.md)**

### 极简 3 步总览：

1. **启动本地服务器后端**：
   ```bash
   cd server
   ./init_server.sh
   ```
2. **在 Home Assistant 中配置语音中枢**（详见手册，3 分钟搞定）：
   * 添加 Wyoming 集成：`127.0.0.1:10300` (Whisper STT) 与 `127.0.0.1:10200` (Piper TTS)；
   * 添加 Ollama 集成：`http://127.0.0.1:8096` (AI Legion 动作网关)；
   * 创建并绑定语音流水线。
3. **烧录 Cardputer-Adv**：
   ```bash
   cd esphome
   cp secrets.yaml.example secrets.yaml   # 填入你的 Wi-Fi 账号密码
   esphome run m5cardputer-adv.yaml
   ```

---

## 🎮 按键交互指南

| 场景 | 推荐按键 | 行为与效果 |
| :--- | :--- | :--- |
| **就绪待命** | `空格` / `任意键` / `侧边 G0` | 开启录音对讲（屏幕显示麦克风对称波形） |
| **录音说话** | `任意键` 或 `静音停顿` | 结束录音并发送至服务端进行 STT 转写 |
| **正在播报** | `任意非音量按键` | **0ms 瞬间打断 (Barge-in)**：扬声器立刻闭嘴并重置待命 |
| **任何时刻** | 键盘右上角 **`-`** | **降低音量 5%**，中央弹出青色音量 HUD 浮层 |
| **任何时刻** | 键盘右上角 **`+` / `=`** | **增大音量 5%**，中央弹出青色音量 HUD 浮层 |

---

## 📚 深度硬核文档

* 🔧 **[Cardputer-Adv 硬件避坑与 DAC / 按键真值详解 (HARDWARE_GUIDE.md)](docs/HARDWARE_GUIDE.md)**：深入剖析 ES8311 DAC 寄存器黄金 0dB 设置、NS4150B 模拟功放防削波策略、TCA8418 键盘矩阵映射真值反推公式。
* 📐 **[系统拓扑与 5 阶段状态机图解 (ARCHITECTURE.md)](docs/ARCHITECTURE.md)**：Mermaid 架构图与状态流转生命周期详解。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
