#!/usr/bin/env bash
# ============================================================================
# 高尔夫挥杆分析后端 —— 阿里云 ECS (Alibaba Cloud Linux 3) conda 一键部署
#
# 本脚本用 Anaconda/conda 虚拟环境 + uvicorn 直接运行后端，**替代 Docker 方案**。
# 原因：ECS 上安装 Docker 慢、直连 Docker Hub 拉镜像易超时；conda + 清华 pip 源更快更稳。
#
# 关键约束（不可违背，改动前请回看 docs/ARCHITECTURE.md §10.1）：
#   * conda 环境必须是 Python 3.12  —— 3.13 的 mediapipe wheel 是精简包，没有 mp.solutions
#   * mediapipe==0.10.14 + legacy mp.solutions.pose —— 1.0.0 已移除 legacy API
#   * numpy==1.26.4 (<2)            —— 0.10.14 不兼容 numpy 2.x
#   * uvicorn 固定 --workers 1      —— 任务状态存在内存 dict，多 worker 会读不到任务
#
# 用法:  cd /root/golf/GOLF/deploy && bash deploy-aliyun-conda.sh
# 前置:  ECS 已安装 Anaconda；项目已放到 $GOLF_PROJECT_DIR (默认 /root/golf/GOLF)
# ============================================================================
set -euo pipefail

PROJECT_DIR="${GOLF_PROJECT_DIR:-/root/golf/GOLF}"
BACKEND_DIR="$PROJECT_DIR/backend"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="golf"
PORT=8000
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
SERVICE_NAME="golf-backend"
SERVICE_TEMPLATE="$DEPLOY_DIR/${SERVICE_NAME}.service"
SERVICE_TARGET="/etc/systemd/system/${SERVICE_NAME}.service"

# ---------------------------------------------------------------------------
# [1/7] 定位 conda 与目标环境的 python
# ---------------------------------------------------------------------------
echo ">>> [1/7] 定位 conda ..."
CONDA="$(command -v conda || echo /root/anaconda3/bin/conda)"
if [ ! -x "$CONDA" ]; then
  echo "✗ 找不到 conda 可执行文件: $CONDA"
  echo "  请确认已安装 Anaconda，或先执行: source /root/anaconda3/etc/profile.d/conda.sh"
  exit 1
fi
CONDA_BASE="$("$CONDA" info --base)"
PYTHON="$CONDA_BASE/envs/$ENV_NAME/bin/python"
echo "    conda      = $CONDA"
echo "    conda base = $CONDA_BASE"
echo "    env python = $PYTHON (环境创建后生成)"

# ---------------------------------------------------------------------------
# [2/7] 安装 opencv-python-headless 运行所需的系统库
# ---------------------------------------------------------------------------
echo ">>> [2/7] 安装系统依赖 (mesa-libGL, opencv 需要) ..."
sudo dnf -y install mesa-libGL || true

# ---------------------------------------------------------------------------
# [3/7] 创建 / 更新 conda 环境
# ---------------------------------------------------------------------------
echo ">>> [3/7] 创建或更新 conda 环境 '$ENV_NAME' (python 3.12) ..."
cd "$DEPLOY_DIR"
"$CONDA" env create -f environment.yml || "$CONDA" env update -f environment.yml

if [ ! -x "$PYTHON" ]; then
  echo "✗ conda 环境创建后仍找不到 python: $PYTHON"
  echo "  请手动检查: $CONDA env list"
  exit 1
fi
echo "    python 版本: $("$PYTHON" --version)"

# ---------------------------------------------------------------------------
# [4/7] 用环境内的 pip 补装 requirements.txt（走清华镜像）
# ---------------------------------------------------------------------------
echo ">>> [4/7] 安装后端依赖 (清华 PyPI 镜像) ..."
if [ -f "$BACKEND_DIR/requirements.txt" ]; then
  "$PYTHON" -m pip install -r "$BACKEND_DIR/requirements.txt" -i "$PIP_INDEX"
else
  echo "    ! 未找到 $BACKEND_DIR/requirements.txt，跳过（environment.yml 已装齐核心依赖）"
fi

# ---------------------------------------------------------------------------
# [4.5/7] 安装 PyTorch (CPU) —— SwingNet AI 事件检测需要
#   注意：torch 不能走清华 PyPI 源（Linux 下会装成几 GB 的 CUDA 版，含 NVIDIA 依赖），
#   必须从官方 CPU wheel 源安装。国内暂无稳定 CPU wheel 镜像（清华 pytorch-wheels 已下线）。
#   download.pytorch.org 走 CloudFront CDN，阿里云 ECS 一般可访问（速度中等）。
#   版本锁 2.13.0（与本地开发环境一致，避免未来大版本引入不兼容）。
# ---------------------------------------------------------------------------
echo ">>> [4.5/7] 安装 PyTorch (CPU) — SwingNet AI 事件检测 ..."
"$PYTHON" -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu --default-timeout=300 --retries 3

# ---------------------------------------------------------------------------
# [5/7] 放行 8000/tcp（firewalld 是第二道防火墙）
# ---------------------------------------------------------------------------
echo ">>> [5/7] 放行防火墙端口 $PORT/tcp ..."
if systemctl is-active --quiet firewalld 2>/dev/null; then
  sudo firewall-cmd --add-port=${PORT}/tcp --permanent || true
  sudo firewall-cmd --reload || true
else
  echo "    firewalld 未运行，跳过（仍需在阿里云控制台安全组放行 ${PORT}/tcp）"
fi

# ---------------------------------------------------------------------------
# [6/7] 注册 systemd 服务
#      systemd 不展开环境变量/shell 变量，必须把占位符替换成绝对路径
# ---------------------------------------------------------------------------
# 探测本机公网 IP（阿里云元数据接口），失败则回退到已知地址
PUBLIC_IP="$(curl -fsS --max-time 5 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || echo 39.102.63.30)"
echo ">>> [6/7] 安装 systemd 服务 $SERVICE_NAME ..."
if [ ! -f "$SERVICE_TEMPLATE" ]; then
  echo "✗ 缺少 systemd 模板: $SERVICE_TEMPLATE"
  exit 1
fi
sed -e "s#__PYTHON_PATH__#${PYTHON}#g" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=${BACKEND_DIR}#" \
    -e "s#__PUBLIC_BASE_URL__#${PUBLIC_IP}#g" \
    "$SERVICE_TEMPLATE" | sudo tee "$SERVICE_TARGET" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# ---------------------------------------------------------------------------
# [6.5/7] 检查 SwingNet 权重（63MB 不入 git，需手动上传）
# ---------------------------------------------------------------------------
WEIGHTS="$BACKEND_DIR/models/swingnet_1800.pth.tar"
echo ">>> [6.5/7] 检查 SwingNet 权重 ..."
if [ -f "$WEIGHTS" ]; then
  echo "    ✓ SwingNet 权重存在: $WEIGHTS"
else
  echo "    ⚠ SwingNet 权重缺失: $WEIGHTS"
  echo "      DTL 视频会自动回退规则引擎（AI 事件检测不生效，但不影响使用）。"
  echo "      手动上传权重（在本地项目根目录执行）："
  echo "        scp backend/models/swingnet_1800.pth.tar root@<ECS公网IP>:/root/golf/GOLF/backend/models/"
fi

# ---------------------------------------------------------------------------
# [7/7] 健康检查
# ---------------------------------------------------------------------------
echo ">>> [7/7] 健康检查 ..."
sleep 5
if curl -fsS "http://127.0.0.1:${PORT}/api/v1/health"; then
  echo
  echo "    <- health OK"
else
  echo
  echo "    ! health 未就绪。排查命令："
  echo "      sudo systemctl status ${SERVICE_NAME}"
  echo "      sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
fi

PUBLIC_IP="$(curl -fsS --max-time 5 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || true)"
echo
echo "============================================================"
echo "部署完成（conda 方式，未使用 Docker）"
echo "  环境:     $ENV_NAME  ($PYTHON)"
echo "  服务:     $SERVICE_NAME  (uvicorn app.main:app, --workers 1)"
echo "  本机访问: http://127.0.0.1:${PORT}/api/v1/health"
if [ -n "$PUBLIC_IP" ]; then
  echo "  公网访问: http://${PUBLIC_IP}:${PORT}"
else
  echo "  公网访问: http://<你的ECS公网IP>:${PORT}"
fi
echo
echo "重要: 到阿里云控制台【安全组】放行 入方向 ${PORT}/tcp (ECS 真正的防火墙)"
echo "图片地址: GOLF_PUBLIC_BASE_URL=http://${PUBLIC_IP}:${PORT} (结果页截图将用此公网地址)"
echo "运维: sudo systemctl status|restart ${SERVICE_NAME}   /   sudo journalctl -u ${SERVICE_NAME} -f"
echo "============================================================"
