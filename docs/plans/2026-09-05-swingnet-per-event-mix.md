# DTL SwingNet per-event 混合策略 plan

**日期**：2026-09-05
**目标**：DTL 视频上 SwingNet 与规则引擎 per-event 混合 — 每个阶段独立判断 conf，conf ≥ 阈值用 SwingNet（高精度），conf < 阈值用规则引擎（用户肉眼看下来准）
**触发问题**：9660113a 等 DTL 视频上 SwingNet 8 阶段里 7 阶段 conf < 0.15（仅 Impact=0.71 可信），全段用 SwingNet 会让前 4 阶段识别完全失效；用户视觉判断规则引擎输出更稳定，故采用 per-event 混合
**前置**：SwingNet 方案 A（局部乱序锚点修复）已落地（commit `5e26996` 之前未提交），M3 杆头最低点方案默认开（仅 DTL 生效）

---

## 任务清单

### 任务 1：写测试 — per-event 混合在 DTL 上选择阶段

**文件**：`backend/tests/test_swingnet_pipeline.py`

**新增 3 个测试**：

1. `test_segment_events_dtl_per_event_mix_all_high_uses_swingnet`
   - 全部 conf=0.9 → 输出 = SwingNet 8 阶段，used_swingnet=True

2. `test_segment_events_dtl_per_event_mix_low_conf_falls_back_to_rule_per_phase`
   - Address conf=0.1（低）、Impact conf=0.9（高）
   - 期望：Address 帧号 = 规则引擎 Address 帧号，其他 = SwingNet 帧号
   - 单调守卫保证顺序

3. `test_segment_events_dtl_per_event_mix_impact_low_uses_rule_for_impact`
   - Impact conf=0.1（低），其他 conf=0.9（高）
   - 期望：Impact 帧号 = 规则引擎 Impact 帧号，其他 = SwingNet 帧号
   - used_swingnet=False（M3 不会被启用，因为 Impact 不是 SwingNet 出的）

**修改 2 个测试**（保留逻辑验证）：

4. `test_segment_events_dtl_fallback_on_low_impact_confidence` —— 改为 per-event 混合语义：Impact conf=0.1 → Impact 用规则引擎帧号，其他 conf 高 → 用 SwingNet 帧号

5. `test_segment_events_dtl_fallback_on_non_monotonic` —— 改为 per-event 混合语义：SwingNet 输出非单调（Finish=20 早于 Top=43）→ 单调守卫在合并阶段处理，每个阶段 conf 高仍按 SwingNet 给的位置、单调守卫把它们按挥杆顺序排好

**验收**：3 个新测试 + 2 个修改测试在代码改动前全部失败（RED）

---

### 任务 2：实现 per-event 混合 — `_try_swingnet_raw` + `_merge_dtl_events_per_event`

**文件**：`backend/app/pipeline.py`

**步骤 2.1**：新增 `_try_swingnet_raw(video_path, meta)` helper

```python
def _try_swingnet_raw(video_path: str, meta: VideoMeta) -> Optional[Dict[str, Dict]]:
    """跑 SwingNet 返回 raw dict（{事件名: {frame_index, confidence}}），不做任何守卫。
    
    Returns:
        ``None`` 表示 SwingNet 不可用（异常 / 返回空 / 事件不全）；非 None 为 raw dict。
    """
    from .ai.swingnet_detector import SwingNetDetector
    try:
        raw = SwingNetDetector().detect(video_path)
    except Exception as exc:
        logger.warning("SwingNet detect failed (raw pass): %s", exc)
        return None
    if not raw or len(raw) != len(_SWINGNET_PHASE_MAP):
        return None
    return raw
```

**步骤 2.2**：新增 `_merge_dtl_events_per_event(sw_raw, re_events, meta, frames, threshold)`

```python
def _merge_dtl_events_per_event(
    sw_raw: Dict[str, Dict],
    re_events: Sequence[SwingEvent],
    meta: VideoMeta,
    frames: List[FrameLandmarks],
    threshold: float,
) -> Tuple[List[SwingEvent], bool]:
    """DTL per-event 混合：每个阶段 conf ≥ threshold 用 SwingNet，否则用规则引擎。
    
    Args:
        sw_raw: SwingNetDetector.detect 的输出（{事件名: {frame_index, confidence}}）。
        re_events: 规则引擎 segment_swing 的 8 个事件（已单调）。
        meta: 视频元信息（fps / sample_step / total_frames）。
        frames: pose_extractor.extract 的采样序列（用于 array_index 转换）。
        threshold: conf 阈值（默认 :data:`config.SWINGNET_MIX_THRESHOLD`）。
    
    Returns:
        ``(events, impact_from_swingnet)``——events 严格单调，impact_from_swingnet
        表示 Impact 阶段是否来自 SwingNet（用于 pipeline 主流程控制 M3 击球校正开关）。
    """
    # SwingNet 事件名 -> PhaseKey 顺序：与 PHASE_ORDER 完全一致
    sw_names = list(_SWINGNET_PHASE_MAP.keys())
    phase_keys = list(_SWINGNET_PHASE_MAP.values())
    
    re_by_key = {e.key: e for e in re_events}
    fps = float(meta.fps)
    step = max(1, int(meta.sample_step))
    n = len(frames)
    
    # 按 phase 顺序判断来源
    picks: List[Tuple[PhaseKey, int, bool, bool]] = []  # (key, frame_index, from_swingnet, estimated)
    for sw_name, key in zip(sw_names, phase_keys):
        sw_fi = int(sw_raw[sw_name]["frame_index"])
        sw_conf = float(sw_raw[sw_name]["confidence"])
        re_e = re_by_key.get(key)
        if re_e is None:
            # 防御：理论上不会发生（re_events 是 segment_swing 输出，恒 8 个）
            picks.append((key, sw_fi, True, False))
            continue
        if sw_conf >= threshold:
            picks.append((key, sw_fi, True, False))
        else:
            picks.append((key, int(re_e.frame_index), False, bool(re_e.estimated)))
    
    # 单调守卫：相邻阶段 fi 非递增时后推 1 帧（保留物理时序）
    for i in range(1, len(picks)):
        prev_fi = picks[i - 1][1]
        if picks[i][1] <= prev_fi:
            picks[i] = (picks[i][0], prev_fi + 1, picks[i][2], picks[i][3])
    # 防越界
    picks = [(k, min(fi, max(0, n - 1)), fs, est) for (k, fi, fs, est) in picks]
    
    # 构造 SwingEvent
    events: List[SwingEvent] = []
    impact_from_swingnet = False
    for idx, (key, fi, from_sw, est) in enumerate(picks):
        array_index = max(0, min(n - 1, fi // step))
        events.append(SwingEvent(
            index=PHASE_META[key].index,
            key=key,
            frame_index=fi,
            timestamp=round(fi / fps, 3),
            estimated=est,
            array_index=array_index,
        ))
        if key is PhaseKey.IMPACT and from_sw:
            impact_from_swingnet = True
    return events, impact_from_swingnet
```

**验收**：3 个新测试 + 2 个修改测试全部通过（GREEN）

---

### 任务 3：配置 + `_segment_events` 接入 per-event 混合

**文件 1**：`backend/app/config.py`

新增 2 个开关：

```python
#: per-event 混合阈值：DTL 上 SwingNet 阶段 conf ≥ 此值用 SwingNet（高精度），
#: 否则用规则引擎（用户视觉验证稳定）。0.30 与 SWINGNET_MIN_IMPACT_CONF 同档位，
#: 适合 SwingNet 8 阶段 conf 普遍偏低的 DTL 场景（实测 9660113a 7/8 conf < 0.15）。
SWINGNET_MIX_THRESHOLD: Final[float] = 0.30

#: per-event 混合总开关。False 时维持原 DTL→SwingNet 失败回退规则引擎的二元语义。
SWINGNET_MIX_ENABLED: Final[bool] = True
```

**文件 2**：`backend/app/pipeline.py:_segment_events`（line 199 附近）

改写 DTL 分叉：

```python
def _segment_events(
    video_path: str,
    meta: VideoMeta,
    frames: List[FrameLandmarks],
    signals: SwingSignals,
    aspect: float,
    view: CameraView,
) -> Tuple[List[SwingEvent], bool]:
    if view is CameraView.DOWN_THE_LINE:
        # 总是先跑规则引擎（per-event 混合的兜底数据源）
        re_events = segmenter.segment_swing(
            frames, meta.fps, sig=signals, aspect=aspect, view=view
        )
        if config.SWINGNET_ENABLED:
            sw_raw = _try_swingnet_raw(video_path, meta)
            if sw_raw is not None:
                if config.SWINGNET_MIX_ENABLED:
                    events, impact_from_sw = _merge_dtl_events_per_event(
                        sw_raw, re_events, meta, frames, config.SWINGNET_MIX_THRESHOLD
                    )
                    return events, impact_from_sw
                # 关闭 per-event 混合：保留原二元语义（SWINGNET_MIN_IMPACT_CONF 守卫 + 单调守卫）
                events = _detect_dtl_events_swingnet(video_path, meta, frames, signals)
                if events is not None:
                    return events, True
            logger.warning("DTL SwingNet 不可用，全用规则引擎")
        else:
            logger.info("SwingNet 已关闭（SWINGNET_ENABLED=False），全用规则引擎")
        return re_events, False
    # face-on：保持规则引擎（逐字节不变）
    events = segmenter.segment_swing(
        frames, meta.fps, sig=signals, aspect=aspect, view=view
    )
    return events, False
```

**验收**：所有 SwingNet pipeline 测试通过（532 + 3 新增 = 535）；9660113a 实际跑出预期 per-event 混合结果

---

### 任务 4：实际视频回归测试

**文件**：`backend/.workbuddy/diag_per_event_mix.py`（一次性诊断脚本，不入 git）

跑 3 个 DTL 视频 × per-event 混合（SWINGNET_MIX_THRESHOLD=0.3），输出：

| 视频 | Address | Takeaway | Backswing | Top | Downswing | Impact | Follow | Finish |
|------|---------|----------|-----------|-----|-----------|--------|--------|--------|
| 9660113a | 96 (rule) | 110 (rule) | 113 (rule) | 119 (rule) | 124 (rule) | **130 (sw)** | 132 (rule) | 137 (rule) |
| 11.mp4 | 99 (rule) | 103 (rule) | 109 (rule) | 109 (sw) | 113 (sw) | **116 (sw)** | 118 (sw) | 131 (rule) |
| 470057ac | 78 (rule) | 85 (rule) | 87 (rule) | 92 (rule) | 96 (rule) | **97 (rule)** | 102 (rule) | 111 (rule) |

**验收**：手动对照结果，确认 per-event 混合逻辑生效

---

## 风险与权衡

1. **规则引擎在 DTL 上"挤到中段"**（9660113a Address=96 应在 30 帧）：用户已明确认可规则引擎输出"肉眼看下来是准的"，不质疑此点
2. **470057ac 上 Impact 用规则引擎（conf=0.13 < 0.3）**：used_swingnet=False → M3 杆头最低点不会启用，击球精度可能比之前低。但用户接受 per-event 规则
3. **阈值 0.3 硬编码**：可后续按更多真值样本标定调整

---

## 提交顺序

1. **任务 1**：写测试（RED）
2. **任务 2**：实现 helper 函数（GREEN）
3. **任务 3**：配置 + 接入 `_segment_events`（GREEN）
4. **任务 4**：实际视频回归（不提交）
5. 全套测试通过后 git commit

---

## 不做的事（YAGNI）

- 不改 SwingEvent schema（保持 frozen dataclass）
- 不改 SwingNetDetector（保持方案 A 已落地的版本）
- 不动规则引擎（接受它在 DTL 上的行为）
- 不引入新 DTL 信号源（仅做 per-event 混合这个最小改动）