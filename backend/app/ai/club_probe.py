"""阶段级球杆观测器（方案 A：静态阶段真值化）。

职责：在**慢速阶段**的事件帧上跑 GolfPose ONNX（检测 + 5 关键点），
把「真实球杆几何」交给 :mod:`app.metrics` 使用，从而把 ``swing_plane``
从 L1 代理（左肩→左腕连线）升级为 L0 实测（shaft→hosel 杆身连线）。

为什么只在慢速阶段跑（性能铁律）
---------------------------------
GolfPose 的训练数据偏向静态姿态。实测（横屏 DTL ``1446d1b9…mp4``）：

==========  ==================================================
阶段        检测结果
==========  ==================================================
Address     ✅ 0.788
Takeaway    ✅ 0.867
Backswing   ✅ 0.563
Top         ✅ 0.898
Downswing   ❌ 漏检（运动模糊）
Impact      ❌ 漏检
Follow-thr. ❌ 漏检
Finish      ❌ 漏检
==========  ==================================================

全帧跑的成本：35ms × 238 帧 ≈ 8.3s（冷机），而该机器存在 CPU 功耗墙
（持续负载下降频到 1/7，实测 63s/视频），完全不可接受。
因此由 :data:`app.config.CLUB_ONNX_PHASES` 控制白名单，常态只跑 3 帧。

设计约束（与 :mod:`app.ai.swingnet_detector` 同构）
--------------------------------------------------
1. **零异常外抛**：任何失败（权重缺失 / onnxruntime 未装 / 推理异常 /
   预算耗尽）都返回 ``available=False`` 的观测，调用方自然回退代理指标。
2. **机位门控**：``swing_plane`` 是 DTL 专属指标，face-on 下不跑。
3. **预算守卫**：:data:`app.config.CLUB_ONNX_BUDGET_SEC` 兜住异常机器。

依赖方向：``club_probe -> club_onnx / config / schemas / geometry``。
不 import ``metrics``，避免循环导入。

解锁说明
--------
代码侧已完全就绪（506 passed），但**当前素材下门控放行率仅 5%**：
GolfPose 训练数据偏向静态 close-up 教学片段，真实挥杆中段模型域差异巨大。
要让真值化在生产中真正生效，需要补 20+ 段高质量 DTL 素材（含 5 点 + 8 阶段真值帧
标注）。详细规范见 ``docs/CLUB_DATA_REQUIREMENTS.md``。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from app import config, geometry
from app.ai.club_onnx import ClubOnnxDetector
from app.schemas import CameraView, PhaseKey, SwingEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 观测结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClubObservation:
    """单个阶段帧上的球杆观测结果。

    Attributes:
        available: 是否拿到了 5 个关键点 + 杆身角度。``False`` 时其余字段无意义。
            详见 ``accept_reason``。
        accepted: ⭐ **真值采信** 判定 —— ``available=True`` 不等于 ``accepted=True``：
            满足全部质量门控（所有关键点 ≥ :data:`config.CLUB_ONNX_MIN_KP_SCORE`、
            骨架长度 ≥ :data:`config.CLUB_ONNX_MIN_SKELETON_PX`、
            shaft→hosel 基线长度 ≥ :data:`config.CLUB_ONNX_MIN_BASELINE_PX`）才为 ``True``。
            调用方（指标计算）**只采信 accepted=True 的真值**，其余一律回退代理。
        accept_reason: ``accepted=False`` 时记录哪条门控不过，便于日志诊断。
        frame_index: 观测帧的原视频帧号（便于日志/可视化追溯）。
        bbox: ``[x1, y1, x2, y2]`` 原图坐标的球杆外接框。
        bbox_score: 检测器置信度（``cls × objectness``）。
        keypoints: ``{名称: (x, y, score)}``，名称见
            :data:`app.ai.club_onnx.KP_NAMES`（shaft/hosel/heel/toe_down/toe_up）。
        shaft_angle_deg: 杆身（shaft→hosel）与图像水平线的夹角，取锐角侧
            ``[0, 90]``。与 ``m_swing_plane`` 现有口径一致（``>90`` 时取
            ``180 − value``），保证真值与代理值可直接比较。
        club_len_px: 由 5 点骨架（shaft→…→toe_up）累加的杆长（像素），
            用于合理性校验（过短说明是误检的小目标）。
        min_kp_score: 5 个关键点中的最低得分（用于前端展示 + 日志）。
        baseline_px: shaft→hosel 距离（角度计算的实际基线长度）。
    """

    available: bool = False
    accepted: bool = False
    accept_reason: str = ""
    #: 真值来源：``"onnx"``（GolfPose 关键点）/ ``"rule"``（Hough 规则法）/ ``""``（无）
    source: str = ""
    #: 统一置信度（ONNX = 最低关键点得分；rule = 单帧检测 confidence）
    confidence: float = 0.0
    frame_index: int = -1
    bbox: Tuple[float, float, float, float] = ()
    bbox_score: float = 0.0
    keypoints: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    shaft_angle_deg: float = float("nan")
    club_len_px: float = 0.0
    min_kp_score: float = 0.0
    baseline_px: float = 0.0

    def kp(self, name: str) -> Optional[Tuple[float, float, float]]:
        """按名称取关键点 ``(x, y, score)``，不存在返回 ``None``。"""
        return self.keypoints.get(name)


#: 全阶段都不可用的空观测（ONNX 关闭 / 机位不符 / 模型不可用时的返回值）
def _empty_map(phase_keys: Sequence[PhaseKey]) -> Dict[PhaseKey, ClubObservation]:
    return {key: ClubObservation() for key in phase_keys}


# ---------------------------------------------------------------------------
# 观测器
# ---------------------------------------------------------------------------


class ClubProbe:
    """在白名单阶段帧上采集真实球杆几何。

    Typical:
        >>> probe = ClubProbe()
        >>> obs = probe.observe(frames_bgr, events, view)     # doctest: +SKIP
        >>> obs[PhaseKey.TOP].available                        # doctest: +SKIP
        True
    """

    def __init__(
        self,
        detector: Optional[ClubOnnxDetector] = None,
        score_thr: Optional[float] = None,
        kp_thr: Optional[float] = None,
        budget_sec: Optional[float] = None,
    ) -> None:
        self._detector = detector
        self.score_thr = (
            config.CLUB_ONNX_SCORE_THR if score_thr is None else float(score_thr)
        )
        self.kp_thr = config.CLUB_ONNX_KP_THR if kp_thr is None else float(kp_thr)
        self.budget_sec = (
            config.CLUB_ONNX_BUDGET_SEC if budget_sec is None else float(budget_sec)
        )
        self._phases: Tuple[PhaseKey, ...] = self._parse_phases()
        #: 上一次 ``observe`` 的耗时统计（秒），供日志/诊断使用
        self.last_elapsed_sec: float = 0.0
        #: 当前 ``observe`` 的计时起点（预算守卫基线，与 ``last_elapsed_sec`` 区分开：
        #: 后者在 ``finally`` 才写回，不能在预算检查里读）
        self._t0: float = 0.0

    # ---------- 配置解析 ----------

    @staticmethod
    def _parse_phases() -> Tuple[PhaseKey, ...]:
        """把 :data:`config.CLUB_ONNX_PHASES` 解析成 :class:`PhaseKey` 元组。

        配置里写字符串（避免 config 反向依赖 schemas 的枚举），这里做一次
        宽松解析：非法阶段名直接跳过并告警，绝不让配置错误拖垮主链路。
        """
        valid = {p.value: p for p in PhaseKey}
        out: List[PhaseKey] = []
        for raw in getattr(config, "CLUB_ONNX_PHASES", ()) or ():
            key = valid.get(str(raw).strip().lower())
            if key is None:
                logger.warning("CLUB_ONNX_PHASES 含非法阶段名，已跳过: %r", raw)
                continue
            if key not in out:
                out.append(key)
        return tuple(out)

    # ---------- 主入口 ----------

    def observe(
        self,
        frames_bgr: Dict[int, np.ndarray],
        events: Sequence[SwingEvent],
        view: CameraView,
        landmarks: Optional[Sequence["FrameLandmarks"]] = None,
        meta: Optional["VideoMeta"] = None,
    ) -> Dict[PhaseKey, ClubObservation]:
        """在白名单阶段帧上采集球杆几何。

        Args:
            frames_bgr: ``{原视频帧号: BGR uint8 图像}``（pipeline step 4a 产物）。
            events: 8 个阶段事件（取 ``frame_index`` 去 ``frames_bgr`` 取图）。
            view: 解析后的机位。
            landmarks: 全片关键点序列（**规则法必需**：用于握把锚点 + 杆长先验）。
            meta: 视频元信息（**规则法必需**：width/height）。

        Returns:
            ``{PhaseKey: ClubObservation}``，**键集合恒等于全部 8 个阶段**
            （未观测的阶段给 ``available=False``），调用方无需做键缺失判断。
        """
        all_keys = tuple(PhaseKey)
        self._t0 = time.time()
        try:
            return self._observe_inner(frames_bgr, events, view, all_keys,
                                        landmarks, meta)
        except Exception:  # noqa: BLE001 - 观测失败绝不能中断分析主链路
            logger.exception("ClubProbe.observe 异常，全部阶段回退代理指标")
            return _empty_map(all_keys)
        finally:
            self.last_elapsed_sec = time.time() - self._t0

    # ---------- 内部实现 ----------

    def _observe_inner(
        self,
        frames_bgr: Dict[int, np.ndarray],
        events: Sequence[SwingEvent],
        view: CameraView,
        all_keys: Tuple[PhaseKey, ...],
        landmarks: Optional[Sequence["FrameLandmarks"]] = None,
        meta: Optional["VideoMeta"] = None,
    ) -> Dict[PhaseKey, ClubObservation]:
        result = _empty_map(all_keys)

        if not getattr(config, "CLUB_ONNX_ENABLED", False):
            logger.info("ClubProbe: CLUB_ONNX_ENABLED=False，跳过")
            return result
        if not self._phases:
            logger.info("ClubProbe: 阶段白名单为空，跳过")
            return result
        # swing_plane 是 DTL 专属指标（reference.METRIC_SPECS 的 views 门控），
        # face-on 下跑球杆检测纯属浪费。
        if view is not CameraView.DOWN_THE_LINE:
            logger.info("ClubProbe: 机位 %s 非 DTL，跳过球杆观测", view.value)
            return result
        if not frames_bgr:
            logger.info("ClubProbe: 无可用事件帧，跳过")
            return result

        detector = self._detector or ClubOnnxDetector(
            score_thr=self.score_thr, kp_thr=self.kp_thr
        )

        # ---- 规则法（零标注兜底，anchors=8 个事件帧升序） ---------------------
        # ⚠️ 关键（2026-09-04 实测）：规则法是时序跟踪算法，密集窗口帧 + 速度门控
        # 切换 framediff 会导致**连锁失败**（debug_rule_per_anchor.py 已证）。
        # 这里直接调底层：anchors 仅 8 个事件帧、强制 hough、无速度门控、无窗口扩展
        # —— 已知有球杆的 7 帧上 7/7 命中（probe_rule_on_known_frames.py）。
        rule_track = self._observe_rule(frames_bgr, events, view, landmarks, meta)

        by_phase = {e.key: e for e in events}
        for phase in self._phases:
            event = by_phase.get(phase)
            if event is None:
                continue
            image = frames_bgr.get(event.frame_index)
            if image is None:
                logger.debug(
                    "ClubProbe: 阶段 %s 帧 %d 不在解码集内，跳过",
                    phase.value, event.frame_index,
                )
                continue

            # 预算守卫：已超支则放弃剩余阶段（回退代理，不阻断分析）
            elapsed = time.time() - self._t0
            if elapsed > self.budget_sec:
                logger.warning(
                    "ClubProbe: 推理预算 %.1fs 已耗尽（当前 %.1fs），放弃 %s 及之后阶段",
                    self.budget_sec, elapsed, phase.value,
                )
                break

            # 优先级：ONNX(accepted) > 规则法(conf≥阈值) > 代理
            onnx_obs = self._observe_one(detector, image, event.frame_index)
            rule_obs = rule_track.get(phase)
            chosen = self._pick_better(onnx_obs, rule_obs)

            result[phase] = chosen
            logger.info(
                "ClubProbe: %s(frame=%d) src=%-4s accepted=%s "
                "angle=%s conf=%.2f reason=%s",
                phase.value, event.frame_index, chosen.source, chosen.accepted,
                "nan" if math.isnan(chosen.shaft_angle_deg)
                else f"{chosen.shaft_angle_deg:.1f}°",
                chosen.confidence, chosen.accept_reason or "-",
            )
        return result

    @staticmethod
    def _pick_better(
        onnx_obs: "ClubObservation",
        rule_obs: Optional["ClubObservation"],
    ) -> "ClubObservation":
        """优先级：ONNX(accepted) > 规则法(conf≥阈值) > 都没观测到。

        两个都没过质量门控时：优先返回 rule（available=True）让上层感知"观测到了
        但质量不够"，否则返回 onnx（available=False 表明模型不可用/没检出）。
        """
        if onnx_obs.accepted:
            return onnx_obs
        if rule_obs is not None and rule_obs.accepted:
            return rule_obs
        if rule_obs is not None and rule_obs.available:
            return rule_obs
        return onnx_obs

    # ---------- 规则法真值源（零标注 Hough，2026-09-04）----------

    def _observe_rule(
        self,
        frames_bgr: Dict[int, np.ndarray],
        events: Sequence[SwingEvent],
        view: CameraView,
        landmarks: Optional[Sequence["FrameLandmarks"]],
        meta: Optional["VideoMeta"],
    ) -> Dict[PhaseKey, ClubObservation]:
        """规则法 Hough 真值源：grip → head 方向即杆身方向（与 ONNX shaft→hosel 语义等价）。

        **关键设计**（实测驱动）：
        1. anchors 只用 8 个事件帧升序，**不**走 plan_frames 的窗口扩展（避免连锁失败）
        2. 不传 signals，强制 ``_detect_hough`` 分支（速度门控切 framediff 会引发连锁失败）
        3. 扇形放宽到 35°（比默认 track=25° 更宽容一帧预测误差）
        4. 单帧 conf ≥ :data:`config.CLUB_RULE_MIN_CONF` 才接受

        与 :func:`app.club_detector.detect` 不复用，因为后者是为 ``shaft_plane_dev``
        指标设计（含窗口模式 + 速度门控），不适合单帧真值采集。
        """
        if not getattr(config, "CLUB_RULE_ENABLED", True):
            return {}
        if landmarks is None or meta is None or not landmarks or not events:
            logger.debug("ClubProbe: 规则法缺 landmarks/meta，跳过")
            return {}

        try:
            from app import club_detector as cd
            from app import geometry as _geom
        except Exception:  # noqa: BLE001 - 缺依赖直接跳过
            return {}

        width, height = int(meta.width), int(meta.height)
        lm_by_frame = {f.frame_index: f for f in landmarks}
        addr_event = next((e for e in events if e.key.value == "address"), events[0])
        addr_lm = lm_by_frame.get(int(addr_event.frame_index), landmarks[0])
        club_len = cd.club_length_prior(cd._landmark_px(addr_lm, width, height), view)
        if not (math.isfinite(club_len) and club_len >= 10.0):
            logger.info("ClubProbe: 规则法杆长先验无效（%.1f），跳过", club_len)
            return {}

        # anchors: 8 个事件帧升序（**仅**事件帧，不含窗口）
        anchors = [(e.key, e.frame_index) for e in events if e.frame_index is not None]
        anchors.sort(key=lambda x: x[1])

        # 同时解码 targets（含前一帧，给 framediff 兜底用 —— 但我们强制 hough，
        # 所以 targets == anchors）
        decoded: Dict[int, np.ndarray] = {
            f: img for f, img in frames_bgr.items()
            if any(fa == f for _, fa in anchors)
        }
        if not decoded:
            return {}

        fan_deg = float(config.CLUB_RULE_FAN_DEG)
        dir_tol = float(config.CLUB_RULE_FAN_DEG)
        result: Dict[PhaseKey, ClubObservation] = {}
        last_dir: Optional[np.ndarray] = None
        last_grip: Optional[np.ndarray] = None

        for phase, f_idx in anchors:
            bgr = decoded.get(f_idx)
            frame_lm = lm_by_frame.get(f_idx)
            if bgr is None or frame_lm is None:
                continue

            landmark_px = cd._landmark_px(frame_lm, width, height)
            grip = cd._grip_px(landmark_px)
            if grip is None:
                continue

            # 时序预测：上一帧方向（无历史时给默认 [0,1]）
            if last_dir is not None:
                predicted = np.asarray(last_dir, dtype=np.float64).copy()
                if last_grip is not None:
                    vel = cd._unit(grip - last_grip)
                    if vel is not None:
                        bl = cd._unit(predicted + cd._VELOCITY_BLEND * vel)
                        if bl is not None:
                            predicted = bl
            else:
                predicted = np.array([0.0, 1.0], dtype=np.float64)

            body_mask = _geom.skeleton_polygon_mask(landmark_px, (height, width))
            skeleton = cd._skeleton_segments(landmark_px)

            try:
                outcome = cd._detect_hough(
                    bgr, grip, club_len, predicted, fan_deg, dir_tol,
                    body_mask, skeleton,
                )
            except Exception:  # noqa: BLE001
                outcome = None

            if outcome is None:
                continue
            head, shaft_dir, conf = outcome
            accepted = conf >= float(config.CLUB_RULE_MIN_CONF)
            angle = self._shaft_angle_from_pixels(
                grip.astype(np.float64), head.astype(np.float64)
            )
            if not math.isfinite(angle):
                continue

            obs = ClubObservation(
                available=accepted,
                accepted=accepted,
                accept_reason=(
                    "" if accepted
                    else f"rule_conf={conf:.2f}<{config.CLUB_RULE_MIN_CONF:.2f}"
                ),
                source="rule",
                confidence=float(conf),
                frame_index=int(f_idx),
                shaft_angle_deg=float(angle),
                club_len_px=float(club_len),
            )
            result[phase] = obs
            last_dir = shaft_dir
            last_grip = grip
        return result

    @staticmethod
    def _shaft_angle_from_pixels(grip: np.ndarray, head: np.ndarray) -> float:
        """grip→head 向量与水平线夹角，取锐角侧 [0, 90]（与 ``_shaft_angle`` 同口径）。"""
        shaft = head - grip
        ang = math.degrees(math.atan2(float(shaft[1]), float(shaft[0])))
        ang = (ang + 360.0) % 180.0
        return 180.0 - ang if ang > 90.0 else ang

    def _observe_one(
        self, detector: ClubOnnxDetector, image: np.ndarray, frame_index: int
    ) -> ClubObservation:
        """单帧观测：检测 → 关键点 → 杆身角度。（异常一律降级为 unavailable）"""
        try:
            found = detector.detect_full(image, self.score_thr, self.kp_thr)
        except Exception:  # noqa: BLE001
            logger.exception("ClubProbe: 帧 %d 球杆检测异常", frame_index)
            return ClubObservation(frame_index=frame_index)
        if not found:
            return ClubObservation(frame_index=frame_index)

        top = found[0]
        kps = {
            str(k["name"]): (float(k["x"]), float(k["y"]), float(k["score"]))
            for k in top.get("keypoints", [])
        }
        bbox = tuple(float(v) for v in top.get("bbox", ()))
        bbox_score = float(top.get("score", 0.0))

        angle = self._shaft_angle(kps)
        length = self._skeleton_length(kps)
        baseline = self._baseline_length(kps)
        min_kp = min((v[2] for v in kps.values()), default=0.0)

        # ---- 三闸门：只采信"模型确有信心"的真值 ----
        accepted, reason = self._quality_gate(length, baseline, min_kp)
        if not accepted:
            logger.info(
                "ClubProbe: 帧 %d 真值未采信 (%s, len=%.1f baseline=%.1f min_kp=%.2f)",
                frame_index, reason, length, baseline, min_kp,
            )

        return ClubObservation(
            available=accepted,
            accepted=accepted,
            accept_reason=reason if not accepted else "",
            source="onnx",
            confidence=float(min_kp),
            frame_index=frame_index,
            bbox=bbox,
            bbox_score=bbox_score,
            keypoints=kps,
            shaft_angle_deg=angle,
            club_len_px=length,
            min_kp_score=min_kp,
            baseline_px=baseline,
        )

    # ---------- 质量门控 ----------

    @staticmethod
    def _quality_gate(length: float, baseline: float, min_kp: float
                      ) -> Tuple[bool, str]:
        """三闸门，全部满足才采信真值。返回 ``(accepted, reason)``。

        闸门（实测标定，详见 :data:`config.CLUB_ONNX_MIN_*` 注释）：

          1. 5 个关键点得分**全部** ≥ ``MIN_KP_SCORE`` (0.50)
          2. 骨架总长 ≥ ``MIN_SKELETON_PX`` (120.0)
          3. shaft→hosel 基线长度 ≥ ``MIN_BASELINE_PX`` (40.0)
        """
        if not math.isfinite(min_kp) or min_kp < config.CLUB_ONNX_MIN_KP_SCORE:
            return False, f"min_kp={min_kp:.2f}<{config.CLUB_ONNX_MIN_KP_SCORE:.2f}"
        if length < config.CLUB_ONNX_MIN_SKELETON_PX:
            return False, f"skeleton_len={length:.1f}<{config.CLUB_ONNX_MIN_SKELETON_PX:.1f}"
        if baseline < config.CLUB_ONNX_MIN_BASELINE_PX:
            return False, f"baseline_len={baseline:.1f}<{config.CLUB_ONNX_MIN_BASELINE_PX:.1f}"
        return True, ""

    @staticmethod
    def _baseline_length(kps: Dict[str, Tuple[float, float, float]]) -> float:
        """shaft→hosel 距离（角度计算的实际基线长度）。

        基线太短（如 address 帧 5 点挤成一团时仅 3.8px）则方向纯属噪声，
        角度必为 0° / 90° / 180° 之一。这是为什么 quality_gate 必须有第三闸门。
        """
        shaft = kps.get("shaft")
        hosel = kps.get("hosel")
        if shaft is None or hosel is None:
            return 0.0
        return math.hypot(hosel[0] - shaft[0], hosel[1] - shaft[1])

    # ---------- 几何量 ----------

    @staticmethod
    def _shaft_angle(kps: Dict[str, Tuple[float, float, float]]) -> float:
        """杆身（shaft→hosel）与图像水平线的夹角，取锐角侧 ``[0, 90]``。

        口径与 ``m_swing_plane`` 现有实现完全一致（``line_angle_from_horizontal``
        返回 ``[0, 180)``，``>90`` 时取 ``180 − value``），保证真值与代理值
        可直接比较、可安全互换。
        """
        shaft = kps.get("shaft")
        hosel = kps.get("hosel")
        if shaft is None or hosel is None:
            return float("nan")
        ang = geometry.line_angle_from_horizontal(
            np.array([shaft[0], shaft[1]], dtype=np.float64),
            np.array([hosel[0], hosel[1]], dtype=np.float64),
        )
        if not math.isfinite(ang):
            return float("nan")
        return 180.0 - ang if ang > 90.0 else ang

    @staticmethod
    def _skeleton_length(kps: Dict[str, Tuple[float, float, float]]) -> float:
        """沿骨架 shaft→hosel→heel→toe_down→toe_up 累加长度（像素）。

        既作为「杆长」合理性校验（误检的小目标会被挡掉），也是关键点
        是否齐全的间接判据（缺一段则长度明显偏短）。
        """
        chain = ("shaft", "hosel", "heel", "toe_down", "toe_up")
        total = 0.0
        for a, b in zip(chain, chain[1:]):
            pa, pb = kps.get(a), kps.get(b)
            if pa is None or pb is None:
                return 0.0
            total += math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        return total


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def observe_club(
    frames_bgr: Dict[int, np.ndarray],
    events: Sequence[SwingEvent],
    view: CameraView,
    probe: Optional[ClubProbe] = None,
) -> Dict[PhaseKey, ClubObservation]:
    """一次性的便捷入口（每次新建 :class:`ClubProbe`，无状态复用）。"""
    return (probe or ClubProbe()).observe(frames_bgr, events, view)
