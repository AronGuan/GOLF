"""QA 独立验证脚本（严过关，2026-08）——不依赖工程师写的测试，独立审查。

覆盖清单（对照 team-lead 下发的验证清单）：
A. 机位过滤后每阶段指标数（face_on 3/4/4/4/4/3/4/4、dtl 2/2/2/2/1/1/1/1）
B. 17 条规则机位门控：正面机位不触发 DTL 专属规则、反之亦然
C. 指标缺失/None 时空状态；低置信度降级路径
D. 风险引擎对空 phases / 异常输入容错
E. _SafeDict 无 eval 审查（静态 + 行为）
F. RISK-016 符号陷阱（⑦ shoulder_turn -> shoulder_open）真实数据验证
G. m_swing_plane 只用 11→15 两点 + 可见度守卫
H. 空转：swing_plane 数值合理范围、RISK-014 触发（端到端在 probe 里做）
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

sys.path.insert(0, os.path.join(BASE_DIR, "tests"))
from conftest import make_swing_frames  # noqa: E402

from app import club_detector, config, geometry, metrics, risk_engine, segmenter, view_detector  # noqa: E402
from app.risk_rules import RISK_RULES  # noqa: E402
from app.schemas import (  # noqa: E402
    CameraView,
    ClubTrack,
    FrameLandmarks,
    MetricSource,
    PhaseKey,
    StageMetric,
    SwingEvent,
    VideoMeta,
)

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"  [FAIL] {name} :: {detail}")


def make_metric(key, value, ref_min=0.0, ref_max=100.0, unit="°") -> StageMetric:
    return StageMetric(key=key, name=key, value=value, unit=unit,
                       ref_min=ref_min, ref_max=ref_max, status="normal")


def metric_counts():
    """A. 机位过滤后每阶段指标数。"""
    print("\n== A. 机位过滤后每阶段指标数 ==")
    from app import reference
    face = {k: sum(1 for s in specs if s.supports(CameraView.FACE_ON))
            for k, specs in reference.METRIC_SPECS.items()}
    dtl = {k: sum(1 for s in specs if s.supports(CameraView.DOWN_THE_LINE))
           for k, specs in reference.METRIC_SPECS.items()}
    exp_face = [3, 4, 4, 4, 4, 3, 4, 4]
    exp_dtl = [2, 2, 2, 2, 1, 1, 1, 1]
    for i, key in enumerate(PhaseKey):
        check(f"A face_on {key.value} count", face[key] == exp_face[i],
              f"got {face[key]} exp {exp_face[i]}")
        check(f"A dtl {key.value} count", dtl[key] == exp_dtl[i],
              f"got {dtl[key]} exp {exp_dtl[i]}")


def view_gating():
    """B. 机位门控：正面不触发 DTL 专属（009/014），反之亦然。"""
    print("\n== B. 机位门控 ==")
    ALL_IDS = frozenset(r.rule_id for r in RISK_RULES)
    face_ids = {r.rule_id for r in risk_engine.active_rules(CameraView.FACE_ON)}
    dtl_ids = {r.rule_id for r in risk_engine.active_rules(CameraView.DOWN_THE_LINE)}
    check("B face excludes RISK-009", "RISK-009" not in face_ids)
    check("B face excludes RISK-014", "RISK-014" not in face_ids)
    # RISK-014 enabled=True -> 默认就在 DTL 候选集；RISK-009 enabled=False（缺文案）
    # 需强开才进候选集（设计如此，不是 bug）
    check("B dtl includes RISK-014", "RISK-014" in dtl_ids)
    check("B dtl default excludes RISK-009(disabled)", "RISK-009" not in dtl_ids)
    # 强开后 RISK-009 进入 DTL 候选集
    import app.risk_engine as re_mod
    old = config.RISK_RULES_FORCE_ENABLE
    config.RISK_RULES_FORCE_ENABLE = frozenset(ALL_IDS)
    dtl_forced = {r.rule_id for r in risk_engine.active_rules(CameraView.DOWN_THE_LINE)}
    config.RISK_RULES_FORCE_ENABLE = old
    check("B dtl forced includes RISK-009", "RISK-009" in dtl_forced)
    for rid in ("RISK-001", "RISK-002", "RISK-005", "RISK-007",
                "RISK-010", "RISK-012", "RISK-013", "RISK-015", "RISK-016"):
        check(f"B dtl excludes {rid}", rid not in dtl_ids, f"in dtl_ids")

    # 实证式：正面 TOP 就算有 swing_plane 也不产 RISK-009
    items = risk_engine.evaluate_phase(
        PhaseKey.TOP,
        [make_metric("hip_turn", 70.0, 45.0, 60.0),
         make_metric("swing_plane", 40.0, 55.0, 65.0)],
        CameraView.FACE_ON,
    )
    ids = {r.rule_id for r in items}
    check("B empirical face TOP no RISK-009", "RISK-009" not in ids and "RISK-001" in ids)


def empty_and_missing():
    """C. 指标缺失 / None 空状态。"""
    print("\n== C. 空状态与缺失 ==")
    # 阶段无任何指标
    items = risk_engine.evaluate_phase(PhaseKey.TOP, [], CameraView.FACE_ON)
    check("C empty metrics -> []", items == [])
    # 缺某指标 -> 该规则跳过
    items = risk_engine.evaluate_phase(PhaseKey.TOP,
                                       [make_metric("hip_turn", 10.0, 45.0, 60.0)],
                                       CameraView.FACE_ON)
    check("C missing x_factor skips RISK-005", not any(r.rule_id == "RISK-005" for r in items))
    # 异常输入：None view 不应炸
    try:
        risk_engine.evaluate_all({}, CameraView.FACE_ON)
        check("C evaluate_all empty dict ok", True)
    except Exception as exc:
        check("C evaluate_all empty dict ok", False, str(exc))
    # NaN 指标值 -> 不触发（NaN 比较恒 False）
    nan_items = risk_engine.evaluate_phase(
        PhaseKey.TOP, [make_metric("hip_turn", float("nan"), 45.0, 60.0)],
        CameraView.FACE_ON)
    check("C NaN metric value no trigger", not any(r.rule_id == "RISK-001" for r in nan_items))


def safe_dict_no_eval():
    """E. _SafeDict 无 eval。"""
    print("\n== E. _SafeDict 无 eval ==")
    import inspect
    import app.risk_engine as re_mod
    src = inspect.getsource(re_mod)
    for banned in ("eval(", "exec(", "compile("):
        check(f"E no {banned} in risk_engine", banned not in src)
    # 行为：未知占位符原样保留
    from app.risk_rules import RiskRule, Condition, TextTemplate
    from app.schemas import RiskLevel
    weird = RiskRule(rule_id="RISK-X", risk_name="x", risk_level=RiskLevel.HIGH,
                     trigger_phase=PhaseKey.TOP, metric_key="hip_turn",
                     conditions=(Condition(">", 10.0),),
                     trigger_template=TextTemplate(base="值 {value} {nope}"))
    text = re_mod.render_description(weird, make_metric("hip_turn", 20.0, 45.0, 60.0))
    check("E unknown placeholder preserved", "{nope}" in text and "20.0" in text, text)
    # 恶意占位符：确认不会执行代码（format_map 只做字面替换，原样返回）
    evil = RiskRule(rule_id="RISK-E", risk_name="x", risk_level=RiskLevel.HIGH,
                    trigger_phase=PhaseKey.TOP, metric_key="hip_turn",
                    conditions=(Condition(">", 10.0),),
                    trigger_template=TextTemplate(base="__import__('os').system('echo pwned')"))
    t2 = re_mod.render_description(evil, make_metric("hip_turn", 20.0, 45.0, 60.0))
    check("E evil template returned verbatim",
          t2 == "__import__('os').system('echo pwned')", t2)


def engine_zero_specialcase():
    """引擎是否真的零特判（RISK-016 符号陷阱应在数据层拆除）。"""
    print("\n== F. 引擎零特判 / RISK-016 符号 ==")
    import inspect
    import app.risk_engine as re_mod
    src = inspect.getsource(re_mod)
    # 引擎内不应出现 "shoulder_open" 特判
    check("F engine no shoulder_open specialcase", "shoulder_open" not in src)
    # 数据层：⑦ spec fn_key == shoulder_open
    from app import reference
    ft_specs = {s.key: s for s in reference.METRIC_SPECS[PhaseKey.FOLLOW_THROUGH]}
    s = ft_specs["shoulder_turn"]
    check("F ⑦ shoulder_turn fn_key=shoulder_open", s.fn_key == "shoulder_open",
          f"got fn_key={s.fn_key!r}")
    # 行为：开放角 45 不触发，20 触发
    items = risk_engine.evaluate_phase(
        PhaseKey.FOLLOW_THROUGH,
        [make_metric("shoulder_turn", 45.0, 35.0, 60.0)],
        CameraView.FACE_ON)
    check("F ⑦ open 45 not trigger 016", not any(r.rule_id == "RISK-016" for r in items))
    items = risk_engine.evaluate_phase(
        PhaseKey.FOLLOW_THROUGH,
        [make_metric("shoulder_turn", 20.0, 35.0, 60.0)],
        CameraView.FACE_ON)
    check("F ⑦ open 20 triggers 016", any(r.rule_id == "RISK-016" for r in items))


def swing_plane_only_11_15():
    """G. m_swing_plane 只用 11→15 + 可见度守卫。"""
    print("\n== G. m_swing_plane ==")
    import inspect
    import app.metrics as mm
    src = inspect.getsource(mm.m_swing_plane)
    check("G uses L_SHOULDER(11)", "L_SHOULDER" in src)
    check("G uses L_WRIST(15)", "L_WRIST" in src)
    check("G visibility guard", "visibility" in src and "0.5" in src)
    # 用合成帧验证：构造 ctx，确认数值有限且 allow_drop 行为
    frames = make_swing_frames()
    meta = VideoMeta(fps=30.0, duration=4.0, width=480, height=854,
                     frame_count=len(frames), sample_step=1, low_fps=False)
    sig = segmenter.build_signals(frames, 30.0)
    events = segmenter.segment_swing(frames, 30.0, sig=sig)
    ctx = metrics.build_context(frames, events, sig, meta, view=CameraView.DOWN_THE_LINE)
    ctx.phase = PhaseKey.TOP
    items = {m.key: m for m in metrics.compute_phase_metrics(ctx)}
    check("G swing_plane present in DTL TOP", "swing_plane" in items)
    if "swing_plane" in items:
        v = items["swing_plane"].value
        check("G swing_plane finite", math.isfinite(v), f"got {v}")
    # allow_drop: 低可见度 -> 剔除
    ctx2 = metrics.build_context(frames, events, sig, meta, view=CameraView.DOWN_THE_LINE)
    ctx2.phase = PhaseKey.TOP
    # 把 Top 帧可见度打低
    top_ev = next(e for e in events if e.key is PhaseKey.TOP)
    top_frame = frames[top_ev.array_index]
    saved = top_frame.visibility.copy()
    top_frame.visibility[geometry.L_SHOULDER] = 0.1
    items2 = {m.key: m for m in metrics.compute_phase_metrics(ctx2)}
    check("G low visibility drops swing_plane", "swing_plane" not in items2)
    top_frame.visibility[:] = saved


def shaft_plane_dev_deg():
    """球杆三级降级（合成数据构造低置信度）。"""
    print("\n== H. shaft_plane_dev 降级 ==")
    from app.schemas import ClubDetection
    frames = make_swing_frames()
    meta = VideoMeta(fps=30.0, duration=4.0, width=480, height=854,
                     frame_count=len(frames), sample_step=1, low_fps=False)
    sig = segmenter.build_signals(frames, 30.0)
    events = segmenter.segment_swing(frames, 30.0, sig=sig)

    def build_club(conf):
        dets = []
        lm = {f.frame_index: f for f in frames}
        top_i = next(e.array_index for e in events if e.key is PhaseKey.TOP)
        impact_i = next(e.array_index for e in events if e.key is PhaseKey.IMPACT)
        # 关键帧 ①④⑤⑥ + Top→Impact 窗口内每一帧（L0 需 Address base + ≥4 轨迹点）
        wanted = {ev.frame_index for ev in events
                  if ev.key in (PhaseKey.ADDRESS, PhaseKey.TOP,
                                PhaseKey.DOWNSWING, PhaseKey.IMPACT)}
        for i in range(top_i, impact_i + 1):
            wanted.add(frames[i].frame_index)
        for fi in sorted(wanted):
            norm = lm[fi].norm
            grip = np.array([norm[geometry.L_WRIST, 0] * 480,
                             norm[geometry.L_WRIST, 1] * 854])
            dets.append(ClubDetection(frame_index=fi, grip=grip,
                                      head=grip + np.array([-40.0, -120.0]),
                                      confidence=conf, method="hough"))
        return ClubTrack(detections=dets, club_len_px=180.0,
                         overall_confidence=conf, available=True,
                         view=CameraView.DOWN_THE_LINE,
                         swing_plane_measurable=True)

    # L0 high conf
    ctx = metrics.build_context(frames, events, sig, meta,
                                view=CameraView.DOWN_THE_LINE, club=build_club(0.8))
    ctx.phase = PhaseKey.DOWNSWING
    items = {m.key: m for m in metrics.compute_phase_metrics(ctx)}
    if "shaft_plane_dev" in items:
        it = items["shaft_plane_dev"]
        check("H L0 source measured", it.source is MetricSource.MEASURED)
        check("H L0 not estimated", it.estimated is False)
    else:
        check("H L0 present", False, "shaft_plane_dev missing at conf 0.8")

    # L1 proxy
    ctx = metrics.build_context(frames, events, sig, meta,
                                view=CameraView.DOWN_THE_LINE, club=build_club(0.4))
    ctx.phase = PhaseKey.DOWNSWING
    items = {m.key: m for m in metrics.compute_phase_metrics(ctx)}
    if "shaft_plane_dev" in items:
        it = items["shaft_plane_dev"]
        check("H L1 source proxy", it.source is MetricSource.PROXY)
        check("H L1 estimated", it.estimated is True)
        check("H L1 ref padded", abs(it.ref_min - (-10.0)) < 1e-6 and
              abs(it.ref_max - 15.0) < 1e-6, f"ref {it.ref_min}~{it.ref_max}")
    else:
        check("H L1 present", False, "shaft_plane_dev missing at conf 0.4")

    # L2 low conf
    ctx = metrics.build_context(frames, events, sig, meta,
                                view=CameraView.DOWN_THE_LINE, club=build_club(0.1))
    ctx.phase = PhaseKey.DOWNSWING
    items = metrics.compute_phase_metrics(ctx)
    check("H L2 dropped", all(m.key != "shaft_plane_dev" for m in items))
    check("H L2 warning", any("球杆" in w for w in ctx.warnings))


def rule_data_integrity():
    """规则数据完整性：17 条、enabled 分布、metric_key 命中、文案空缺真实性。"""
    print("\n== I. 规则数据完整性 ==")
    check("I 17 rules", len(RISK_RULES) == 17, f"got {len(RISK_RULES)}")
    enabled = {r.rule_id for r in RISK_RULES if r.enabled}
    check("I 10 enabled", enabled == {"RISK-001", "RISK-002", "RISK-005", "RISK-006",
                                      "RISK-007", "RISK-010", "RISK-011", "RISK-014",
                                      "RISK-016", "RISK-017"}, f"got {sorted(enabled)}")
    # 缺文案 7 条：trigger_template / suggestions / manual_excerpt 均为空/None
    disabled = [r for r in RISK_RULES if not r.enabled]
    check("I 7 disabled", len(disabled) == 7, f"got {len(disabled)}")
    for r in disabled:
        check(f"I {r.rule_id} no trigger_template",
              r.trigger_template is None, f"template={r.trigger_template!r}")
        check(f"I {r.rule_id} no suggestions", r.suggestions == (), f"{r.suggestions}")
        check(f"I {r.rule_id} no manual_excerpt", r.manual_excerpt is None,
              f"{r.manual_excerpt!r}")
    # metric_key 命中 METRIC_SPECS
    from app import reference
    for r in RISK_RULES:
        specs = reference.METRIC_SPECS.get(r.trigger_phase, [])
        keys = {s.key for s in specs}
        check(f"I {r.rule_id} metric_key in specs", r.metric_key in keys,
              f"{r.metric_key} not in {sorted(keys)}")
    # 每条规则 views ⊆ spec views
    for r in RISK_RULES:
        specs = reference.METRIC_SPECS.get(r.trigger_phase, [])
        spec = next((s for s in specs if s.key == r.metric_key), None)
        if spec is not None:
            check(f"I {r.rule_id} views subset spec", r.views.issubset(spec.views),
                  f"{r.views} ⊄ {spec.views}")


def plan_frames_budget():
    """字节预算护栏。"""
    print("\n== J. plan_frames 字节预算 ==")
    frames = make_swing_frames()
    meta = VideoMeta(fps=30.0, duration=4.0, width=3840, height=2160,
                     frame_count=120, sample_step=1, low_fps=False)
    sig = segmenter.build_signals(frames, 30.0)
    events = segmenter.segment_swing(frames, 30.0, sig=sig)
    anchors, targets = club_detector.plan_frames(
        frames, events, meta=meta, budget_bytes=config.DECODE_BYTES_BUDGET)
    per_frame = 3840 * 2160 * 3
    check("J byte budget respected", len(targets) * per_frame <= config.DECODE_BYTES_BUDGET,
          f"{len(targets)} frames x {per_frame} bytes")
    event_frames = {e.frame_index for e in events}
    check("J 8 event frames kept", event_frames.issubset(set(anchors)))


def view_detector_threshold():
    """K. view_detector 阈值 vs 实测肩宽比。"""
    print("\n== K. view_detector 阈值 ==")
    # 阈值 0.13；正面实测 0.2486~0.2706，DTL 实测 0.07~0.1265
    check("K threshold 0.13 in config", abs(config.VIEW_SHOULDER_RATIO_DTL - 0.13) < 1e-9)
    # 行为：0.25 -> face-on, 0.10 -> dtl
    from app.schemas import FrameLandmarks
    def frames_with_ratio(ratio, w=480, h=854):
        height_norm = 0.7
        sw_px = ratio * height_norm * h
        sw_norm = sw_px / w
        norm = np.zeros((geometry.NUM_LANDMARKS, 3))
        norm[geometry.L_SHOULDER] = [0.5 - sw_norm / 2.0, 0.4, 0.0]
        norm[geometry.R_SHOULDER] = [0.5 + sw_norm / 2.0, 0.4, 0.0]
        norm[geometry.NOSE] = [0.5, 0.2, 0.0]
        norm[geometry.L_ANKLE] = [0.4, 0.9, 0.0]
        norm[geometry.R_ANKLE] = [0.6, 0.9, 0.0]
        return [FrameLandmarks(frame_index=0, timestamp=0.0, detected=True,
                               norm=norm, world=np.zeros((33, 3)),
                               visibility=np.full(33, 0.95))]
    w, h = 480, 854
    v = view_detector.detect_view(frames_with_ratio(0.25),
                                  VideoMeta(fps=30, duration=1, width=w, height=h,
                                            frame_count=30, sample_step=1), 0)
    check("K ratio 0.25 -> FACE_ON", v is CameraView.FACE_ON)
    v = view_detector.detect_view(frames_with_ratio(0.10),
                                  VideoMeta(fps=30, duration=1, width=w, height=h,
                                            frame_count=30, sample_step=1), 0)
    check("K ratio 0.10 -> DTL", v is CameraView.DOWN_THE_LINE)


def disabled_rule_copy_honest():
    """L. disabled 7 条文案为空是真实存在（不编造）。"""
    print("\n== L. disabled 文案真实性 ==")
    # 已经由 I 覆盖；此处补一个断言：所有 enabled=True 规则都有 template
    enabled_rules = [r for r in RISK_RULES if r.enabled]
    for r in enabled_rules:
        check(f"L {r.rule_id} enabled has template",
              r.trigger_template is not None and r.trigger_template.base.strip() != "")
        check(f"L {r.rule_id} enabled has suggestions", len(r.suggestions) >= 1)


def main():
    metric_counts()
    view_gating()
    empty_and_missing()
    safe_dict_no_eval()
    engine_zero_specialcase()
    swing_plane_only_11_15()
    shaft_plane_dev_deg()
    rule_data_integrity()
    plan_frames_budget()
    view_detector_threshold()
    disabled_rule_copy_honest()
    print(f"\n{'=' * 60}\nQA 独立验证: PASS={PASS} FAIL={FAIL}")
    for name, detail in FAILURES:
        print(f"  FAILED: {name} :: {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
