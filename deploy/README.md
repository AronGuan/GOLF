# 部署指南：阿里云 ECS (Alibaba Cloud Linux 3) + Docker

> 适用场景：已购阿里云 ECS，想把高尔夫挥杆分析后端跑在云上，小程序**仅用开发者工具预览**（暂不做域名/HTTPS/真机）。
> 算法层零改动：`mediapipe==0.10.14` + legacy API 在 Linux 容器里同样自带模型、零外网依赖。

---

## 架构

```
阿里云 ECS (Alibaba Cloud Linux 3)
  └─ Docker 容器 (python:3.12-slim)
        ├─ mediapipe==0.10.14 / numpy<2 / opencv-headless / fastapi / uvicorn
        ├─ uvicorn 单 worker，监听 0.0.0.0:8000
        └─ 任务产物挂载到宿主机 ./data
  └─ 安全组放行 8000/tcp
        ↑ HTTP
  微信开发者工具 (urlCheck=false, BASE_URL=http://<公网IP>:8000)
```

---

## 步骤

### 1. 准备 ECS
- 系统：Alibaba Cloud Linux 3（已选）
- 到阿里云控制台 **【安全组】→ 入方向规则**，放行 `8000/tcp`（这是 ECS 真正的防火墙，firewalld 是第二道）
- 建议规格：2 vCPU / 4 GB 起（MediaPipe CPU 推理够用）

### 2. 上传代码到服务器
两种方式任选：

```bash
# 方式 A：git（推荐，便于后续更新）
git clone <你的仓库> /root/golf/GOLF

# 方式 B：本地 scp（项目根目录 E:\project\golf）
scp -r E:\project\golf root@<公网IP>:/root/golf/GOLF
```
> 只需 `backend/` 和 `deploy/` 两个目录即可跑后端；小程序目录不上传也行。

### 3. 一键部署
```bash
ssh root@<公网IP>
cd /root/golf/GOLF/deploy
bash deploy-aliyun.sh
```
脚本会自动：装 Docker → 构建镜像 → 起容器 → 开防火墙 → 探活。

### 4. 验证
```bash
# 在服务器上
curl http://127.0.0.1:8000/api/v1/health
# 期望: {"code":0,"data":{"status":"ok","mediapipe":"0.10.14"}}

# 在你本地电脑（验证公网可达）
curl http://<公网IP>:8000/api/v1/health
```

### 5. 小程序连云端
1. 微信开发者工具打开 `E:\project\golf\miniprogram`
2. 改 `miniprogram/utils/api.js` 的 `BASE_URL` 为：`http://<公网IP>:8000`
3. 确认 `project.config.json` 里 `setting.urlCheck=false`（已设）
4. 进 index 页 → 拍/选视频 → 上传 → analyzing 轮询 → result 看 8 阶段

---

## 部署产物

| 文件 | 作用 |
|---|---|
| `backend/Dockerfile` | python:3.12-slim 基础镜像，装 mediapipe 等依赖，单 worker 启动 |
| `backend/.dockerignore` | 排除 data/tests 等，缩小镜像 |
| `backend/docker-compose.yml` | 端口映射 + 数据卷持久化 |
| `deploy/deploy-aliyun.sh` | ECS 一键部署脚本 |
| `deploy/README.md` | 本文件 |

---

## 重要注意事项

1. **单 worker 限制**：任务状态是内存字典 + `BackgroundTasks`，故 `docker-compose.yml` / Dockerfile 固定 **1 个 worker**。多 worker 会让轮询请求落到没有该任务的进程。并发上来后再考虑换 Redis/数据库。
2. **公网暴露无鉴权**：当前 8000 端口直接对公网开放、无任何认证。仅适合开发预览。正式上线前务必加：反向代理 + 鉴权 + 限速，或至少限制来源 IP。
3. **数据卷**：`backend/data` 挂载到容器 `/app/data`，容器重建任务产物不丢；但不要拿它当长期存储，定期清理。
4. **MediaPipe 锁定**：镜像里 `mediapipe==0.10.14` 是硬约束，**不要**升到 1.0.0（移除 legacy API）或 3.13 的精简 wheel（只有 tasks 子模块）。改 `requirements.txt` 前务必回看 `docs/ARCHITECTURE.md` §10.1。

---

## Docker Hub 拉取超时（阿里云 ECS 常见）

现象：`docker compose build` 卡在 `pulling python:3.12-slim` 并最终报 `TLS handshake timeout` / `context deadline exceeded`（连不上 `registry-1.docker.io`）。

原因：阿里云 ECS 出站访问 Docker Hub 常被限速或不可达；且专属加速器（如阿里云 ACR 镜像加速器）也可能已停止代理 Docker Hub，导致直连超时。

解决：脚本现在会**自动测试多个镜像加速器**，只把探测可用的写入 `/etc/docker/daemon.json`，无需手动找地址：

- 脚本内置常用 fallback 列表（按可用性排序）：
  - `https://hub-mirror.c.163.com`
  - `https://mirror.baidubce.com`
  - `https://docker.m.daocloud.io`
  - `https://docker.nju.edu.cn`
- 探测逻辑：对每个候选访问 `<mirror>/v2/library/hello-world/manifests/latest`，HTTP `200`/`401` 视为可用，收集到 **2 个可用**即停止（减少网络等待）；全部不可用才报错退出（不再尝试直连 Docker Hub，中国大陆 ECS 直连几乎必超时）。
- 用户指定的 `DOCKER_MIRROR` 会被放在候选**最前**优先测试，保留覆盖能力。直接运行即可：
  ```bash
  bash deploy-aliyun.sh
  ```
- 如需换用其它加速器，仍可用环境变量覆盖（它会被当成第一候选优先探测）：
  `DOCKER_MIRROR=https://其它地址.mirror.aliyuncs.com bash deploy-aliyun.sh`
- 也可在服务器上手动 `sudo vim /etc/docker/daemon.json` 写入可用镜像后 `sudo systemctl restart docker`。
- 若自动挑选后构建仍卡住，可尝试关闭 BuildKit 重试：
  `DOCKER_BUILDKIT=0 bash deploy-aliyun.sh`

---

## 常见问题

### Q1: `Failed to enable unit: Unit file docker.service does not exist`
原因：Alibaba Cloud Linux 3 默认 `dnf install docker` 装的是 podman 兼容层，没有 `docker.service`。
修复：脚本已改用 Docker 官方仓库的 `docker-ce`，如果仍遇到此错误，手动执行：
```bash
sudo rm -f /etc/yum.repos.d/docker-ce.repo
sudo dnf remove -y podman podman-docker runc 2>/dev/null || true
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
sudo sed -i 's/$releasever/8/g' /etc/yum.repos.d/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
```

### Q2: `nothing provides libc.so.6(GLIBC_2.34) needed by docker-ce`
原因：误把 docker-ce 仓库按 **el9** 解析（RHEL 9 需要 GLIBC 2.34），但 Alibaba Cloud Linux 3 实际兼容 **el8**。
修复：把 `/etc/yum.repos.d/docker-ce.repo` 里的 `$releasever` 改成 `8`（即上面 Q1 的 `sed -i 's/$releasever/8/g'`），然后重新安装。

### Q3: `podman ... requires runc >= 1.0.0-57, but none of the providers can be installed`
原因：系统中已有的 podman 与 docker-ce 的 runc/containerd 冲突。
修复：先 `sudo dnf remove -y podman podman-docker runc`，再装 docker-ce。

## 后续升级（不在本次范围）

- **真机访问**：需要 ① 已备案域名 ② SSL 证书（阿里云免费或 Let's Encrypt）③ 在微信公众平台把该域名加入「request 合法域名」。届时在容器前加 nginx 反代 + HTTPS，小程序 `BASE_URL` 改 `https://域名`。
- **多实例 / 高可用**：把内存任务存储换成 Redis，worker 数可上调。
- **GPU 加速**：MediaPipe CPU 推理在 6s/60fps 视频约数秒，够 MVP；量大再考虑 GPU 机型。
