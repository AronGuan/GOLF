#!/usr/bin/env bash
#
# manage.sh — 高尔夫挥杆分析后端运维脚本
#
# 一行命令完成：启动 / 停止 / 重启 / 状态 / 日志 / 健康检查 / 环境自检。
# 不改应用源码，仅封装 systemctl / journalctl / curl / run.py check。
#
set -euo pipefail

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SERVICE="golf-backend"
WORKDIR="/root/golf/GOLF/backend"
RUN_PY="${WORKDIR}/run.py"
HOST="127.0.0.1"
PORT="8000"
HEALTH_URL="http://${HOST}:${PORT}/api/v1/health"
# 占位符：部署时把 <你的ECS公网IP> 换成真实公网 IP，脚本内不 hardcode。
ACCESS_URL="http://<你的ECS公网IP>:${PORT}/api/v1/health"
LOG_FILE="/tmp/golf-backend.log"

# 定位 conda env `golf` 内的 python
CONDA="${CONDA_EXE:-$(command -v conda || echo /root/anaconda3/bin/conda)}"
if [ -x "$CONDA" ]; then
  CONDA_BASE="$("$CONDA" info --base 2>/dev/null)"
  PYTHON="${CONDA_BASE}/envs/golf/bin/python"
fi
if [ ! -x "${PYTHON:-}" ]; then
  PYTHON=/root/anaconda3/envs/golf/bin/python
fi

# 是否处于 systemd 环境
HAS_SYSTEMCTL=0
if command -v systemctl >/dev/null 2>&1; then
  HAS_SYSTEMCTL=1
fi

# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------
usage() {
  cat <<'EOF'
高尔夫挥杆分析后端 · 运维脚本

用法:
  ./manage.sh <命令> [参数]

命令:
  start     启动服务      sudo systemctl start golf-backend
  stop      停止服务      sudo systemctl stop golf-backend
  restart   重启服务      sudo systemctl restart golf-backend
  status    查看服务状态  sudo systemctl status golf-backend
  logs      查看日志      sudo journalctl -u golf-backend -f（可加 -n N 看最近 N 行）
  check     环境自检      打印 python / mediapipe / numpy / cv2 版本
  health    健康检查      curl http://127.0.0.1:8000/api/v1/health
  help      显示本帮助

示例:
  ./manage.sh restart
  ./manage.sh logs -n 100
  ./manage.sh health
  ./manage.sh check

说明:
  - 公网访问地址占位为 http://<你的ECS公网IP>:8000/api/v1/health
  - 非 systemd 环境（无 systemctl）会自动提示用 nohup 手动启动
EOF
}

# ---------------------------------------------------------------------------
# systemctl 封装（含非 systemd fallback 提示）
# ---------------------------------------------------------------------------
run_systemctl() {
  local action="$1"
  if [ "$HAS_SYSTEMCTL" -ne 1 ]; then
    echo "[manage] 未检测到 systemctl（非 systemd 环境）。" >&2
    echo "[manage] 可手动启动（无需 sudo）:" >&2
    echo "    nohup \"$PYTHON\" -m uvicorn app.main:app \\" >&2
    echo "        --host 0.0.0.0 --port ${PORT} --workers 1 > ${LOG_FILE} 2>&1 &" >&2
    exit 1
  fi
  sudo systemctl "$action" "$SERVICE"
}

# ---------------------------------------------------------------------------
# 主分发
# ---------------------------------------------------------------------------
main() {
  local cmd="${1:-help}"
  shift || true

  case "$cmd" in
    start)
      run_systemctl start
      echo "[manage] 服务已启动。"
      echo "[manage] 内网健康检查: curl ${HEALTH_URL}"
      echo "[manage] 公网访问地址:   ${ACCESS_URL}"
      ;;
    stop)
      run_systemctl stop
      echo "[manage] 服务已停止。"
      ;;
    restart)
      run_systemctl restart
      echo "[manage] 服务已重启。"
      echo "[manage] 公网访问地址: ${ACCESS_URL}"
      ;;
    status)
      if [ "$HAS_SYSTEMCTL" -ne 1 ]; then
        echo "[manage] 未检测到 systemctl，请改查进程: pgrep -af uvicorn" >&2
        exit 1
      fi
      sudo systemctl status "$SERVICE" || true
      ;;
    logs)
      if [ "$HAS_SYSTEMCTL" -ne 1 ]; then
        echo "[manage] 未检测到 systemctl，请改用: tail -f ${LOG_FILE}" >&2
        exit 1
      fi
      if [ "${1:-}" = "-n" ]; then
        sudo journalctl -u "$SERVICE" -n "${2:-}"
      else
        sudo journalctl -u "$SERVICE" -f
      fi
      ;;
    check)
      if [ ! -x "${PYTHON:-}" ]; then
        echo "[manage] 找不到 conda env 'golf' 的 python，请确认已执行 deploy 创建环境。" >&2
        echo "[manage] 期望路径: ${PYTHON}" >&2
        exit 1
      fi
      "$PYTHON" "$RUN_PY" check
      ;;
    health)
      if curl -fsS "$HEALTH_URL"; then
        echo
        echo "[manage] 健康检查通过。"
      else
        echo "[manage] 服务未启动或 ${HOST}:${PORT} 端口不通。" >&2
        exit 1
      fi
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      echo "[manage] 未知命令: $cmd" >&2
      echo
      usage
      exit 1
      ;;
  esac
}

main "$@"
