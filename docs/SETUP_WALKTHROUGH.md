# 新手从零到一完整落地部署指南 (Step-by-Step Walkthrough)

本文档面向**第一次接触本项目的新用户**，即便你此前从未玩过 ESPHome 或 Home Assistant，跟随本向导操作，也能在 15~20 分钟内完成端到端的 100% 本地离线语音对讲系统搭建。

---

## 阶段一：服务端部署 (约 5 分钟)

推荐在你的局域网家庭服务器、软路由、NAS（群晖/威联通/飞牛/Unraid）或 miniPC 上运行。

### 步骤 1.1：克隆项目与启动容器
```bash
git clone https://github.com/seaskyx2/cardputer-voice-assistant.git
cd cardputer-voice-assistant/server

# 一键启动所有服务 (包含 Home Assistant, Whisper, Piper, Ollama, 动作网关)
./init_server.sh
```
> 若你的服务器没有 bash 环境，可手动执行：
> 1. `cp .env.example .env`
> 2. `docker compose up -d --build`
> 3. `docker exec -it ollama ollama pull qwen3:4b`

---

## 阶段二：Home Assistant 语音中枢配置 (约 5 分钟)

打开电脑浏览器，访问 `http://<服务器IP>:8123`。

### 步骤 2.1：初始化与生成访问令牌 (HA Token)
1. 首次打开按向导创建管理员账号并登录。
2. 点击页面左下角 **用户头像** $\rightarrow$ 切换到顶部的 **安全性 (Security)** 标签页。
3. 滑动到最下方 **长期访问令牌 (Long-Lived Access Tokens)** $\rightarrow$ 点击 **创建令牌** $\rightarrow$ 命名为 `cardputer`。
4. 复制生成的这一长串密钥，粘贴到 `server/.env` 文件中的 `HA_TOKEN=你的密钥`，然后重启网关容器：
   ```bash
   docker restart ai-legion
   ```

### 步骤 2.2：接入 STT 与 TTS (Wyoming 协议)
> 注意：Docker Compose 默认桥接模式下，主机名请填写容器服务名（`sensevoice`、`piper`、`ai-legion`）；若手动启用了 `network_mode: host` 或宿主机直接部署，则填 `127.0.0.1`。

1. 进入 **设置 (Settings)** -> **设备与服务 (Devices & Services)** -> 点击右下角 **添加集成 (Add Integration)**。
2. 搜索并选择 **Wyoming**：
   * **主机 (Host)**：输入 `sensevoice`（Host 模式填 `127.0.0.1`）
   * **端口 (Port)**：输入 `10300`
   * 点击提交，系统自动识别出 `sensevoice` 极速中文语音转文字服务。
3. 再次点击右下角 **添加集成** -> 搜索并选择 **Wyoming**：
   * **主机**：输入 `piper`（Host 模式填 `127.0.0.1`）
   * **端口**：输入 `10200`
   * 点击提交，系统自动识别出 `piper` 华严中文女声合成服务。

### 步骤 2.3：接入智能家居动作调度大模型 (Ollama 集成)
1. 在 **设备与服务** 页面，点击右下角 **添加集成** -> 搜索并选择 **Ollama**。
2. **服务器地址**：输入 `http://ai-legion:8096`（Host 模式填 `http://127.0.0.1:8096`，指向 AI Legion v2.6 动作调度网关）。
3. 模型选择 `qwen3-4b:latest` 并提交。

### 步骤 2.4：创建离线对讲语音流水线 (Voice Assistant Pipeline)
1. 进入 **设置** -> **语音助手 (Voice Assistants)** -> 点击 **添加流水线**。
2. 配置项填写如下：
   * **名称**：`Cardputer 离线流水线`
   * **语言**：`中文 (Chinese)`
   * **对话代理 (Conversation Agent)**：选择刚刚添加的 **Ollama (qwen3-4b)**
   * **语音转文字 (STT)**：选择 **sensevoice**
   * **文字转语音 (TTS)**：选择 **piper (zh_CN-huayan-medium)**
3. 点击完成，并将该流水线设为 **首选 (Preferred)**。

---

## 阶段三：Cardputer-Adv 固件烧录与配网 (约 5 分钟)

将 M5Stack Cardputer-Adv 通过 Type-C 数据线插入电脑（请确保使用具备数据传输能力的数据线）。

### 步骤 3.1：配置你的 Wi-Fi 账密
进入项目根目录下的 `esphome/` 文件夹：
```bash
cd cardputer-voice-assistant/esphome
cp secrets.yaml.example secrets.yaml
```
用文本编辑器打开 `secrets.yaml`，填入你的局域网 Wi-Fi 名称和密码：
```yaml
wifi_ssid: "你的家用WiFi名称"
wifi_password: "你的WiFi密码"
```

### 步骤 3.2：一键编译与烧录
我们提供两种烧录方式（任选其一）：

#### 方式 A：本地 Python 环境（推荐常用 ESPHome 用户）
```bash
pip install esphome
esphome run m5cardputer-adv.yaml
```
*终端会列出串口端口，选择类似 `/dev/tty.usbmodem...` 或 `COM...`，按下回车即可全自动编译并刷入。*

#### 方式 B：Docker 一行免安装编译烧录（无需配置 Python 环境）
```bash
# macOS / Linux (替换其中的 /dev/ttyACM0 为你的实际串口)
docker run --rm --privileged -v "${PWD}":/config -it ghcr.io/esphome/esphome run /config/m5cardputer-adv.yaml
```

---

## 阶段四：设备握手与开箱对讲 (约 1 分钟)

1. 烧录完成后，Cardputer 会自动重启，屏幕依次显示启动动画并连入 Wi-Fi。
2. 打开 Home Assistant 的 **设置 $\rightarrow$ 设备与服务**，系统会弹出通知：**“发现新设备：ESPHome - M5Cardputer Adv”**。
3. 点击 **配置** $\rightarrow$ 填入 `secrets.yaml` 里的 `api_key`（若未修改可直接提交），设备即刻在线！
4. 点击进入该设备卡片，确认语音助手流水线已关联刚刚创建的 `Cardputer 离线流水线`。

---

## 阶段五：实测你的专属赛博对讲机！

拿起 Cardputer，看着正面这块 135x240 彩色小屏幕：
1. **测试音量**：按键盘右上角 **`-`** 或 **`+`** 键，屏幕立刻弹出青色音量 HUD 浮层；
2. **测试对话**：在待命界面按 **空格键**（短按或按住说话松手即发），对着麦克风说：
   > *“把吸顶灯打开”*  
   > *“把灯打开过5秒再关上”*  
   > *“现在几点了”*
3. **测试波形与打断**：
   * 说话时观察屏幕上 18 根动态绿色/青色波形柱实时跳动；
   * 停顿后进入流光雷达扫描与秒表计时；
   * 在 AI 正在语音播报时，随时按下任意键，验证 **0ms 瞬间打断闭嘴**！
