# M5Stack Cardputer-Adv 离线语音智能助手

一个专为 **M5Stack Cardputer-Adv** 设计的 100% 局域网离线语音对讲客户端，深度集成 Home Assistant，实现全屋物理设备控制、实时麦克风波形可视化与丝滑的按键交互体验。

---

## 🌟 核心特性

- **100% 局域网离线自治**：语音流与指令绝不出家门，完全依靠本地家庭服务器运行。
- **真·声画同步波形**：录音时通过 I2S 获取物理麦克风真实振幅，在 135x240 彩屏上渲染对称频谱能量柱。
- **动态交互流光**：本地模型推理时秒表毫秒级走字 + 雷达流光扫描；TTS 播报时呈现双色对话气泡。
- **全场景音量控制**：
  - 硬件快捷键：随时按 `-` / `+`（或 `[` / `]`）调节扬声器音量，支持实时青色 HUD 能量条悬浮窗反馈。
  - 双向同步：注册 Home Assistant `number` 音量滑块实体，具备断电记忆特性。
- **0ms 即时打断 (Barge-in)**：AI 说话过程中按任意非音量键，瞬间掐断扬声器 DMA 缓冲并静音，秒回待命。
- **5s 自动平滑重置**：对话结束后停留 5 秒供查看结果（带进度微条指示），超时自动清屏回到待命主页。
- **全屋物理设备联动**：智能调度网关对接 Home Assistant REST API，杜绝模型输出代码，支持“开灯关灯、延时执行”等多步复合指令。
- **1296 个中文字库**：精编常用字、全屋家电词汇与 Whisper 常见繁体字容错，消灭缺字报警与渲染掉帧。

---

## 📁 目录结构

```
.
├── esphome/
│   ├── m5cardputer-adv.yaml       # 通用版 ESPHome 固件配置
│   ├── secrets.yaml.example       # Wi-Fi 与 API 秘钥模板
│   ├── visualizer.h               # 麦克风能量计算与扫描动画算法
│   └── custom_components/         # TCA8418 键盘矩阵驱动组件
├── server/
│   ├── docker-compose.yml         # 服务端一键部署 (HA + Whisper + Piper + Ollama)
│   ├── .env.example               # 服务端环境变量配置模板
│   └── ai-legion/                 # 智能家居大模型动作调度网关
└── docs/
    ├── HARDWARE_GUIDE.md          # Cardputer-Adv 硬件避坑与 DAC / 按键真值详解
    └── ARCHITECTURE.md            # 系统架构拓扑与 5 阶段状态机图解
```

---

## 🚀 快速上手

### 1. 服务端部署 (Docker)

在家庭局域网服务器（Linux / NAS / miniPC）上：

```bash
cd server
cp .env.example .env
# 编辑 .env，填入你的 Home Assistant Token
docker compose up -d
```

### 2. 烧录 Cardputer-Adv

将 Cardputer 通过 USB 连接电脑：

```bash
cd esphome
cp secrets.yaml.example secrets.yaml
# 编辑 secrets.yaml，填入你的 Wi-Fi 名称和密码

# 使用 ESPHome 编译并烧录
esphome run m5cardputer-adv.yaml
```

---

## 🎮 按键操作指南

- **开始对讲**：在就绪界面按 **任意键**（或键盘空格、侧边 G0 键）。
- **发送语音**：说完话静音自动检测发送，或在录音中按任意键提前发送。
- **主动打断**：AI 播报或思考时，按任意非音量键立即静音并重置待命。
- **调节音量**：任何时刻按键盘右上角 **`-`**（降低）或 **`+` / `=`**（增大）。
