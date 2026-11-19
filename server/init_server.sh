#!/bin/bash
set -e

echo "======================================================="
echo "   Cardputer Voice Assistant - 服务端一键部署向导      "
echo "======================================================="

# 1. 检查 Docker 环境
if ! command -v docker &> /dev/null; then
    echo "❌ 错误: 未检测到 Docker，请先安装 Docker 及 Docker Compose。"
    exit 1
fi

# 2. 检查并生成 .env
if [ ! -f .env ]; then
    echo "📄 正在从 .env.example 生成 .env 配置文件..."
    cp .env.example .env
    echo "⚠️ 请注意: 待 Home Assistant 启动后，需在 .env 中填入你的 HA_TOKEN。"
fi

# 3. 构建并启动容器群
echo "🚀 正在启动服务群 (Home Assistant, Whisper, Piper, Ollama, AI-Legion)..."
docker compose up -d --build

# 4. 等待 Ollama 启动并拉取大模型
echo "⏳ 等待 Ollama 服务就绪..."
sleep 5
echo "📥 正在拉取本地大模型 (phi4-mini: 微软 3.8B 高智商全屋决策中枢)..."
docker exec -i ollama ollama pull phi4-mini || echo "⚠️ 模型下载中或已存在，可稍后手动执行: docker exec -it ollama ollama pull phi4-mini"

echo ""
echo "======================================================="
echo "🎉 服务端容器启动成功！"
echo ""
echo "👉 后续仅需简单配置 Home Assistant (耗时约 3 分钟):"
echo "  1. 打开浏览器访问: http://<你的服务器IP>:8123 完成初始管理员账号创建"
echo "  2. 生成长期令牌: 点击左下角头像 -> 安全 -> 长期访问令牌 -> 创建令牌 -> 填入 server/.env 中的 HA_TOKEN"
echo "  3. 添加集成 (设置 -> 设备与服务 -> 添加集成):"
echo "     - 搜索 'Wyoming'，主机填 127.0.0.1，端口填 10300 (添加 Whisper STT)"
echo "     - 再次搜索 'Wyoming'，主机填 127.0.0.1，端口填 10200 (添加 Piper TTS)"
echo "     - 搜索 'Ollama'，主机填 http://127.0.0.1:8096，模型选 phi4-mini (绑定智能动作网关)"
echo "  4. 创建语音流水线 (设置 -> 语音助手 -> 添加流水线):"
echo "     - 对话代理选 Ollama，STT 选 whisper，TTS 选 piper"
echo "======================================================="
