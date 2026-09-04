# ADR-003｜SwingNet 事件定位改用「锚点约束 + 区间重定位」

- **状态**：已接受（Accepted）
- **日期**：2026-09-04
- **决策人**：主理人 / 架构师（高见远）
- **执行人**：工程师（寇豆码）
- **相关文档**：`docs/ADR-002-impact-frame-no-club.md`、`docs/PRD.md` §P1-09
- **相关代码**：`app/ai/swingnet_detector.py:71 detect` /
  `:139 _constrained_events`、`app/pipeline.py:101 _detect_dtl_events_swingnet`

---

## 1. 背景

DTL 机位切分走 SwingNet（GolfDB 8 事件检测）。历史实现对每个事件类**独立全视频
argmax**（无时序约束），即：

```python
events = np.argmax(probs, axis=0)[:-1]   # 每类全视频取概率最大帧
```

这条实现有两个隐患：

1. **过渡事件（Toe-up / Mid-backswing / Mid-downswing / Mid-follow-through）**
   会被视频首尾的静止段或重复姿态假峰抢走。实测 `11.mp4`（竖屏 DTL）：
   Toe-up 取到帧 17（视频开头静止段）、Mid-backswing 取到帧 100（与 Address 同帧）。
2. 乱序结果触发 `_detect_dtl_events_swingnet` 的**时序守卫 #3（严格递增）** →
   **整体回退规则引擎**。而规则引擎是 face-on 设计的启发式，在竖屏 DTL 上把
   整段挥杆挤成 1 秒（backswing→impact 仅 3 帧 0.1s，物理不可能），
   **所有阶段指标建立在错误相位上**。

## 2. 关键证据

`11.mp4` 上 SwingNet 的 4 个**主锚点其实极准**：

| 事件 | 帧 | conf |
|---|---|---|
| Address | 100 | 0.01 |
| Top | 109 | 0.28 |
| **Impact** | **116** | **0.92** |
| Finish | 157 | 0.14 |

问题**只出在 4 个过渡事件**被全局 argmax 取错位置，导致 8 事件乱序，进而
触发守卫整体回退。核心教训：

> **守卫过度严格 = 把「部分可用」降级成「完全不可用」。**
> 正确的降级是「用可靠的部分 + 重定位不可靠的部分」，而非二值化全弃。

## 3. 决策

在 `SwingNetDetector._constrained_events` 内落地**锚点约束 + 区间重定位**：

1. 先取 4 个主锚点（Address / Top / Impact / Finish）的全局 argmax——GolfDB 模型
   对主事件的区分度远高于过渡事件，锚点可信；
2. 若锚点严格递增，把 4 个过渡事件**限制在锚点区间内**重定位：
   - Toe-up ∈ (Address, Top)
   - Mid-backswing ∈ (Toe-up, Top)
   - Mid-downswing ∈ (Top, Impact)
   - Mid-follow-through ∈ (Impact, Finish)
3. 对最终帧号做**严格递增强制**（相邻过渡事件 argmax 到同一帧时后推 1 帧，
   竖屏 DTL 上 Toe-up 与 Mid-backswing 区分度低、可能同帧）；
4. 若锚点本身乱序（多段挥杆 / 非单次挥杆），保持全视频 argmax 原结果，
   **交由 pipeline 单调守卫回退规则引擎**（不改守卫契约）。

## 4. 效果

`11.mp4`（竖屏 DTL）修复前后对比：

| | 修复前 | 修复后 |
|---|---|---|
| 8 事件时序 | `[100, 17, 100, 109, 113, 116, 118, 157]`（乱序） | `[100, 102, 103, 109, 113, 116, 118, 157]`（严格递增） |
| 下杆 top→impact | 规则引擎回退后 0.10s（物理不可能） | **0.23s** ✅ |
| 送杆 impact→finish | 规则引擎回退后 0.43s | **1.37s** ✅ |
| 切分路径 | 规则引擎（回退） | **SwingNet** ✅ |

横屏 `1446d1b9`（impact conf 0.028 < 0.30）仍走规则引擎回退，结果逐字节不变，
零回归。

## 5. 边界与代价

1. **重定位后 confidence 会下降**：区间内 argmax 的帧概率 ≤ 全局 argmax 的帧
   概率。这是刻意取舍——优先保证物理时序正确，confidence 反映「该事件在
   该区间的置信度」，不再参与单调守卫（守卫只看 frame_index）。
2. **主锚点乱序时不做任何修复**：此时视频非单次挥杆（多段 / 残缺），诚实
   回退规则引擎，不强行迁就。
3. **过渡事件在竖屏 DTL 上天然区分度低**：Toe-up 与 Mid-backswing 可能
   argmax 到相邻帧，靠严格递增强制兜底（±1 帧误差可接受）。

## 6. 重开此议题的触发条件

1. 发现 SwingNet 在**主锚点也乱序**的素材（说明模型域外失效，非本 ADR 能治）；
2. 引入更强的事件检测模型（如带时序 CRF 的变体），可天然消解区间约束 hack；
3. 采集到 ≥20 段高质量 DTL 素材，可用 ground truth 标定区间约束的精度上限。

---

**一句话**：SwingNet 的主锚点本就准，错的是「过渡事件独立 argmax 无时序约束」
+「守卫过度严格整体回退」；用区间约束 + 严格递增重排修好后，`11.mp4` 切分从
「压缩成 1 秒的错相位」恢复为物理合理的 8 阶段。
