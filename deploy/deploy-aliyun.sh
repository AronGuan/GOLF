#!/usr/bin/env bash
# 高尔夫挥杆分析后端 —— 阿里云 ECS (Alibaba Cloud Linux 3) 一键部署
# 用法:  bash deploy-aliyun.sh
# 前置:  本项目已放到 $GOLF_PROJECT_DIR (默认 /root/golf/GOLF)
# 镜像加速器默认 https://1mtp2h46.mirror.aliyuncs.com；可用 DOCKER_MIRROR 环境变量覆盖
set -euo pipefail

PROJECT_DIR="${GOLF_PROJECT_DIR:-/root/golf/GOLF}"
BACKEND_DIR="$PROJECT_DIR/backend"
PORT=8000

echo ">>> [1/5] 安装 Docker (docker-ce) ..."
# 注: Alibaba Cloud Linux 3 兼容 RHEL 8 / CentOS 8 系。
#     默认仓库的 `dnf install docker` 装的是 podman 兼容层, 没有 docker.service,
#     会导致 `systemctl enable docker` 失败。这里改用 Docker 官方仓库的 docker-ce。
if ! systemctl list-unit-files 2>/dev/null | grep -q '^docker.service'; then
  # 清掉可能已存在的错误仓库配置(el9 会依赖 GLIBC 2.34)
  sudo rm -f /etc/yum.repos.d/docker-ce.repo
  # 移除 podman 兼容层, 避免与 docker-ce 的 runc/containerd 冲突
  sudo dnf remove -y podman podman-docker runc 2>/dev/null || true
  # 安装仓库工具并添加阿里云 docker-ce 镜像源 (ALinux3 对应 el8)
  sudo dnf -y install dnf-plugins-core
  sudo dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
  sudo sed -i 's/\$releasever/8/g' /etc/yum.repos.d/docker-ce.repo
  sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo systemctl enable --now docker
fi

echo ">>> 配置 Docker 镜像加速器（阿里云 ECS 直连 Docker Hub 易超时）..."
configure_docker_mirror() {
  # 候选镜像加速器列表：用户指定的 DOCKER_MIRROR 放最前（保留覆盖能力），
  # 之后追加常用公共 fallback（按可能的可用性排序）。
  local candidates=()
  if [ -n "${DOCKER_MIRROR:-}" ]; then
    candidates+=("${DOCKER_MIRROR}")
  fi
  local fallbacks=(
    "https://hub-mirror.c.163.com"
    "https://mirror.baidubce.com"
    "https://docker.m.daocloud.io"
    "https://docker.nju.edu.cn"
  )

  # 合并候选并去重，避免对同一地址重复探测
  local seen="" m
  local merged=()
  for m in "${candidates[@]:-}" "${fallbacks[@]}"; do
    [ -z "$m" ] && continue
    if printf '%s\n' "$seen" | grep -qxF "$m"; then
      continue
    fi
    seen+="$m"$'\n'
    merged+=("$m")
  done
  candidates=("${merged[@]:-}")

  # 逐个探测候选镜像，只收集可用的
  local working=()
  local mirror http_code
  for mirror in "${candidates[@]}"; do
    http_code=$(curl -gsS -o /dev/null -w "%{http_code}" --connect-timeout 8 --max-time 15 \
      -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
      "$mirror/v2/library/hello-world/manifests/latest" 2>/dev/null || echo "000")
    case "$http_code" in
      200|401)
        echo "  ✓ 可用: $mirror (HTTP $http_code)"
        working+=("$mirror")
        ;;
      *)
        echo "  ✗ 跳过不可用: $mirror (HTTP $http_code)"
        ;;
    esac
    # 收集到 2 个可用镜像即可停止，避免不必要的网络等待
    if [ "${#working[@]}" -ge 2 ]; then
      break
    fi
  done

  # 没有任何可用镜像：给出指引并退出（不尝试直连 Docker Hub）
  if [ "${#working[@]}" -eq 0 ]; then
    echo "✗ 所有候选镜像加速器均不可用（探测 /v2 均非 200/401）。"
    echo "  请检查本服务器出站网络（安全组/防火墙是否放行 443），或手动指定可用加速器："
    echo "  DOCKER_MIRROR=https://<你的加速器>.mirror.aliyuncs.com bash deploy-aliyun.sh"
    exit 1
  fi

  # 生成 daemon.json（只写入可用的镜像）
  sudo mkdir -p /etc/docker
  local json="["
  local first=1
  for mirror in "${working[@]}"; do
    if [ "$first" -eq 1 ]; then
      first=0
    else
      json+=","
    fi
    json+="\"$mirror\""
  done
  json+="]"
  sudo tee /etc/docker/daemon.json >/dev/null <<EOF
{
  "registry-mirrors": ${json}
}
EOF
  sudo systemctl restart docker
  echo "已配置 registry-mirrors:"
  for mirror in "${working[@]}"; do
    echo "  - $mirror"
  done
}
DOCKER_MIRROR="${DOCKER_MIRROR:-https://1mtp2h46.mirror.aliyuncs.com}"
configure_docker_mirror

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
