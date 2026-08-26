# backend_ai — AI 路线探索（独立模块）

> 本目录是「规则引擎 → AI 模型」迁移的**独立实验区**，与主 `backend/`（规则引擎）完全隔离，互不影响。

## 定位

主 `backend/` 的 8 阶段切分用启发式规则（MediaPipe 33 关键点 + 手工阈值），DTL 侧面视角下识别率已到天花板（详见 `docs/STATUS.md` §四 J 任务）。

本目录验证：**GolfDB + SwingNet 深度学习方案能否把 8 事件定位精度（尤其 DTL）提升到优于规则引擎**。

## 核心结论（前置，避免重复调研）

- **现成数据集**：GolfDB（1400 段 720p、含 face-on + down-the-line、8 事件标注）
- **现成模型**：SwingNet（MobileNetV2 + BiLSTM，PCE 76.1%/8事件、91.8%/6事件），预训练权重可直接下载
- **关键限制**：
  1. **许可证 CC BY-NC 4.0（非商业）** —— POC 验证可用，商用需另寻数据/授权
  2. **8 事件命名与主项目不同**（Toe-up/Mid-backswing/Mid-downswing/Mid-follow-through），需映射
  3. 官方下载走 Google Drive（国内可能不可达），需走 **OpenDataLab 国内镜像**

## POC 目标（一个可证伪的问题）

> **SwingNet 在用户真实 DTL 视频上的 8 事件定位误差，是否显著小于规则引擎（当前 ±5 帧）？**

- 若 SwingNet 误差 ≤ ±2 帧 → AI 路线值得继续投（进入微调/商用数据阶段）
- 若 SwingNet 误差 ≈ 规则引擎 → GolfDB 分布与真实用户视频差异大，需另想
- 若 SwingNet 更差 → 放弃，维持规则引擎 + 手动微调兜底

## 目录结构（规划）

```
backend_ai/
├── README.md            # 本文件：定位 + 规划
├── PLAN.md              # POC 验证方案（详细步骤）
├── data/                # 数据（gitignore）
│   ├── golfdb/          #   GolfDB videos_160 + 标注
│   └── samples/         #   用户真实 DTL 样本（从主 backend 复制）
├── models/              # 预训练权重（gitignore）
│   └── swingnet_1800.pth.tar
├── scripts/             # 下载、预处理、推理、对比脚本
├── reports/             # 对比结果（PCE 表、帧差分布）
└── requirements.txt     # PyTorch 等依赖（独立于主 backend）
```

## 里程碑

| 阶段 | 内容 | 产出 |
|---|---|---|
| **M0 环境** | 建独立 PyTorch 环境 + 下载 SwingNet 权重 + GolfDB 标注 | 可跑 test_video.py |
| **M1 推理跑通** | 拿主 backend 的真实 DTL 样本（4e8d0d7e/11a6594b/11 等）跑 SwingNet | 每段的 8 事件帧号 |
| **M2 精度对比** | SwingNet vs 规则引擎 vs 用户视觉真值 | PCE 对比表 + 帧差分布 |
| **M3 结论** | 判断 AI 路线是否值得投 | 决策报告 |

## 风险与依赖

| 风险 | 应对 |
|---|---|
| Google Drive 下载失败（国内）| 走 OpenDataLab 镜像 / 用户手动下载 |
| GolfDB 非商业许可 | POC 阶段无碍；商用阶段换 CaddieSet（2025，含关节点+球）或自采标注 |
| 用户真实视频与 GolfDB 分布差异大 | M2 对比会暴露，届时决策 |
| SwingNet 输入要求"裁剪到单次挥杆" | 需预处理：主 backend 已能定位挥杆窗口，可复用 |

## 与主 backend 的关系

- **完全隔离**：独立依赖、独立脚本、不 import 主 backend
- **唯一共享**：用户真实 DTL 样本（从 `samples/侧面/` 复制过来）
- **将来若 AI 验证成功**：把 SwingNet 封装成推理服务，主 backend 通过接口调用（渐进替换，而非推倒重来）
