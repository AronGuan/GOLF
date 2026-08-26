# PLAN.md — SwingNet POC 验证方案（详细）

> 目标：用**最小成本**验证 SwingNet 在用户真实 DTL 视频上的 8 事件定位精度，是否优于主 backend 的规则引擎（当前 DTL ±5 帧）。

## 背景数据

| 项 | 规则引擎（现状）| SwingNet（待验证）|
|---|---|---|
| 信号源 | MediaPipe 33 关键点 | 原始视频帧（MobileNetV2 特征）|
| 方法 | 启发式规则（手腕高度穿越等）| CNN + BiLSTM 时序 |
| DTL 精度 | ±5 帧（方向不固定，M8 已记录）| 待测（GolfDB 上 PCE 76.1%）|
| 成本 | 0（已部署）| 需 PyTorch + 63MB 权重 |

## M0：环境 + 数据准备

1. **建独立环境**（不污染主 backend 的 Python 3.12 + mediapipe 环境）：
   ```
   # 用便携 Python 建 venv，装 PyTorch（CPU 版即可）
   E:\project\golf\.tools\python312\python.exe -m venv backend_ai\.venv
   backend_ai\.venv\Scripts\pip install torch torchvision --index-url https://pypi.tuna.tsinghua.edu.cn/simple
   ```

2. **下载 SwingNet 预训练权重**（63MB）：
   - 官方：Google Drive（`swingnet_1800.pth.tar`）——国内可能失败
   - 兜底：让用户手动下载，或查 OpenDataLab 是否有镜像

3. **下载 GolfDB 标注**（692KB annotation pickle）+ 可选 `videos_160.zip`（699MB，仅训练需要；POC 只推理可先不下载）

4. **准备用户真实样本**：从 `E:\project\golf\.tools\_probe\samples\侧面\` 复制 4e8d0d7e / 11a6594b / 11 / c6f67f38 / 470057ac / f470c599 到 `backend_ai/data/samples/`

## M1：SwingNet 推理跑通

1. 克隆/参考 `wmcnally/golfdb` 的 `test_video.py`（单视频推理入口）
2. 跑通一段用户样本，输出 8 事件帧号

## M2：精度对比（核心）

对每段用户 DTL 样本，产出三组数据：

| 事件 | 规则引擎帧 | SwingNet 帧 | 用户视觉真值帧 |
|---|---|---|---|
| Address | — | — | — |
| ... | | | |
| Impact | | | |

**对比指标**：
- PCE（正确事件百分比，GolfDB 同款口径）
- 各事件帧差分布（规则引擎 vs 真值、SwingNet vs 真值）
- 重点看 **Impact/Downswing/Follow-through**（DTL 痛点）

**结论判据**：
- SwingNet 帧差 ≤ ±2 帧 且优于规则引擎 → **AI 路线可行**，进入微调/商用阶段
- 否则 → 维持规则引擎 + 手动微调兜底

## M3：决策报告

输出 `reports/poc_decision.md`：
- 三组数据对比表
- SwingNet 是否值得投入的明确结论
- 若可行：商用数据方案（CaddieSet / 自采标注 / 授权）+ 集成路径
- 若不可行：明确记录"AI 路线验证失败"，回归规则引擎

## 关键前置风险

1. **下载可达性**：Google Drive 国内可能超时（主项目 memory 已记录"Google 存储不可达"）→ 优先 OpenDataLab / 用户手动下载
2. **SwingNet 输入要求**：需"裁剪到单次挥杆"的视频 → 复用主 backend 的挥杆窗口定位（可先手动裁）
3. **命名映射**：GolfDB 8 事件 vs 主项目 8 阶段（见 README）

## 执行顺序建议

先做 M0（环境+权重），立刻跑 M1（单段样本推理）——**如果权重下载都成问题，整个 POC 的成本就要重新评估**。
