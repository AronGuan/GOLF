#!/usr/bin/env bash
# 高尔夫挥杆分析后端 —— 阿里云 ECS (Alibaba Cloud Linux 3) 一键部署
# 用法:  bash deploy-aliyun.sh
# 前置:  本项目已放到 $GOLF_PROJECT_DIR (默认 /root/golf)
set -euo pipefail

PROJECT_DIR="${GOLF_PROJECT_DIR:-/root/golf}"
BACKEND_DIR="$PROJECT_DIR/backend"
PORT=8000

echo ">>> [1/5] 安装 Docker ..."
if ! command -v docker >/dev/null 2>&1; then
  sudo dnf -y install docker
  sudo systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
  sudo dnf -y install docker-compose-plugin
fi

echo ">>> [2/5] 进入后端目录 $BACKEND_DIR"
cd "$BACKEND_DIR"

echo ">>> [3/5] 构建镜像并启动容器 ..."
sudo docker compose build
sudo docker compose up -d

echo ">>> [4/5] 开放防火墙端口 $PORT (若 firewalld 启用) ..."
if systemctl is-active --quiet firewalld 2>/dev/null; then
  sudo firewall-cmd --permanent --add-port=${PORT}/tcp
  sudo firewall-cmd --reload
fi

echo ">>> [5/5] 健康检查 ..."
sleep 8
curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" && echo "  <- health OK" \
  || echo "health 未就绪，请查: sudo docker compose logs"

echo
echo "部署完成。公网访问地址: http://<你的ECS公网IP>:${PORT}"
echo "重要: 到阿里云控制台【安全组】放行 入方向 ${PORT}/tcp (这是 ECS 真正的防火墙)"
