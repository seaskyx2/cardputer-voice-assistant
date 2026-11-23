#!/bin/bash
set -e

echo "======================================================="
echo "   Cardputer Voice Assistant v2.6 - 服务端部署向导     "
echo "======================================================="

# 1. 检查 Docker 环境
if ! command -v docker &> /dev/null; then
    echo "错误: 未检测到 Docker，请先安装 Docker 及 Docker Compose。"
    exit 1
fi

# 2. 检查并生成 .env
if [ ! -f .env ]; then
    echo "正在从 .env.example 生成 .env 配置文件..."
    cp .env.example .env
    echo "提示: 待 Home Assistant 启动后，请在 server/.env 中填入你的 HA_TOKEN。"
fi

# 3. 构建并启动容器群
echo "正在启动服务群 (Home Assistant, SenseVoice, Piper, Ollama, AI-Legion v2.6)..."
docker compose up -d --build

# 4. 等待 Ollama 启动并拉取大模型
echo "等待 Ollama 服务就绪..."
sleep 5
echo "正在拉取主力端侧小模型 (qwen3:4b - 支持 /no_think 极速控家 + /think 深度推理)..."
docker exec -i ollama ollama pull qwen3:4b || echo "模型下载中或已存在，可稍后手动执行: docker exec -it ollama ollama pull qwen3:4b"

echo ""
echo "======================================================="
echo "服务端容器启动成功！"
echo ""
echo "服务入口："
echo "  - Home Assistant 中枢: http://<服务器IP>:8123"
echo "  - AI Legion 算力控制塔: http://<服务器IP>:8096"
echo ""
echo "后续 Home Assistant 配置 (约 3 分钟)："
echo "  1. 登录 http://<服务器IP>:8123 创建管理员账号"
echo "  2. 点击左下角头像 -> 安全 -> 长期访问令牌 -> 创建令牌 -> 填入 server/.env 的 HA_TOKEN 后执行 docker restart ai-legion"
echo "  3. 添加集成 (设置 -> 设备与服务 -> 添加集成)："
echo "     * 注意：Docker 默认桥接模式下请填容器服务名；若启用了 network_mode: host 则填 127.0.0.1"
echo "     - 搜索 'Wyoming'，主机填 sensevoice (或 127.0.0.1)，端口填 10300 (SenseVoiceSmall INT8 极速 STT)"
echo "     - 再次搜索 'Wyoming'，主机填 piper (或 127.0.0.1)，端口填 10200 (Piper TTS)"
echo "     - 搜索 'Ollama'，地址填 http://ai-legion:8096 (或 http://127.0.0.1:8096)，模型选 qwen3-4b"
echo "  4. 创建语音流水线 (设置 -> 语音助手 -> 添加流水线)："
echo "     - 对话代理选 Ollama，STT 选 sensevoice，TTS 选 piper"
echo "======================================================="
