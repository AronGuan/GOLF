#!/usr/bin/env bash
# 高尔夫挥杆分析后端 —— 阿里云 ECS (Alibaba Cloud Linux 3) 一键部署
# 用法:  bash deploy-aliyun.sh
# 前置:  本项目已放到 $GOLF_PROJECT_DIR (默认 /root/golf)
set -euo pipefail

PROJECT_DIR="${GOLF_PROJECT_DIR:-/root/golf}"
BACKEND_DIR="$PROJECT_DIR/backend"
PORT=8000

echo ">>> [1/5] 安装 Docker (docker-ce) ..."
# 注: Alibaba Cloud Linux 3 (RHEL9 系) 默认仓库的 `dnf install docker` 装的是
#     podman 兼容层, 没有 docker.service, 会导致 `systemctl enable docker` 失败。
#     这里改用 Docker 官方仓库的 docker-ce, 它提供真正的 docker.service 与 daemon。
if ! systemctl list-unit-files 2>/dev/null | grep -q '^docker.service'; then
  sudo dnf -y install dnf-plugins-core
  sudo dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
  # ALinux3 的 $releasever 是 3, 而 docker-ce 仓库目录只有 7/8/9, 强制按 9 解析
  sudo sed -i 's/\$releasever/9/g' /etc/yum.repos.d/docker-ce.repo
  sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo systemctl enable --now docker
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
