"""风险引擎测试（架构 ARCHITECTURE-v2.md §4）。

覆盖：
- 17 条规则逐条边界（刚好触发 / 刚好不触发，阈值 ±0.1；禁用规则验证纯条件逻辑）；
- RISK-016 符号回归（⑦ 开放角 45 不触发 / 20 触发）；
- RISK-011 三分支文案逐字断言（§4.4 表）；
- 机位门控（声明式 + 实证式）；
- ``enabled`` 开关 + 灰度强开；
- 单条规则异常不冒泡；
- 同阶段多风险排序 high→medium→low；
- 性能（8 阶段全量 < 50ms）；
- 导入期自检：enabled=True 缺文案 -> 导入即 RuntimeError。
"""

from __future__ import annotations

import importlib
import time

import pytest

from app import config, risk_engine
from app.risk_rules import RISK_RULES, Condition, RiskRule
from app.schemas import CameraView, MetricStatus, PhaseKey, StageMetric

ALL_RULE_IDS = [rule.rule_id for rule in RISK_RULES]


def make_metric(
    key: str, value: float, ref_min: float = 0.0, ref_max: float = 100.0,
    unit: str = "°",
) -> StageMetric:
    """构造一个可被风险引擎消费的指标。"""
    return StageMetric(
        key=key,
        name=key,
        value=value,
        unit=unit,
        ref_min=ref_min,
        ref_max=ref_max,
        status=MetricStatus.NORMAL,
    )


def rule_by_id(rule_id: str) -> RiskRule:
    return next(r for r in RISK_RULES if r.rule_id == rule_id)


#: 每条规则的边界用例（阈值 ±0.1）。disabled 规则用 `_match` 验证条件本身。
#: 元组 = (rule_id, phase, metric_key, trigger_value, non_trigger_value, ref_min, ref_max)
RULE_BOUNDARIES = [
    ("RISK-001", PhaseKey.TOP, "hip_turn", 62.1, 61.9, 45.0, 60.0),
    ("RISK-002", PhaseKey.TAKEAWAY, "hip_turn", 20.1, 19.9, 8.0, 18.0),
    ("RISK-003", PhaseKey.TOP, "hip_turn", 39.9, 40.1, 45.0, 60.0),
    ("RISK-004", PhaseKey.TAKEAWAY, "head_drift", 5.0, 4.9, 0.0, 4.0),
    ("RISK-005", PhaseKey.TOP, "x_factor", 17.9, 18.1, 20.0, 35.0),
    ("RISK-006", PhaseKey.TOP, "lead_arm_straightness", 144.9, 145.1, 150.0, 172.0),
    ("RISK-007", PhaseKey.BACKSWING, "shoulder_turn", 49.9, 50.1, 55.0, 72.0),
    ("RISK-008", PhaseKey.BACKSWING, "trail_arm_flexion", 130.1, 129.9, 95.0, 125.0),
    ("RISK-009", PhaseKey.TOP, "swing_plane", 49.9, 60.0, 55.0, 65.0),
    ("RISK-010", PhaseKey.DOWNSWING, "x_factor_retention", 79.9, 80.1, 85.0, 130.0),
    ("RISK-011", PhaseKey.ADDRESS, "knee_flexion", 155.9, 165.0, 160.0, 172.0),
    ("RISK-012", PhaseKey.ADDRESS, "stance_width_ratio", 0.89, 1.15, 1.0, 1.3),
    ("RISK-013", PhaseKey.IMPACT, "hip_open_angle", 11.9, 12.1, 15.0, 30.0),
    ("RISK-014", PhaseKey.IMPACT, "spine_tilt_change", 10.0, 9.9, 0.0, 8.0),
    ("RISK-015", PhaseKey.IMPACT, "pelvis_shift", 7.9, 8.1, 10.0, 20.0),
    ("RISK-016", PhaseKey.FOLLOW_THROUGH, "shoulder_turn", 29.9, 30.1, 35.0, 60.0),
    ("RISK-017", PhaseKey.FINISH, "balance_hold", 0.59, 0.61, 0.8, 3.0),
]


def _view_for_rule(rule: RiskRule) -> CameraView:
    """取规则适用的任一机位（用于评估）。"""
    return next(iter(rule.views))


class TestRuleBoundaries:
    """17 条规则逐条边界。"""

    @pytest.mark.parametrize(
        "rule_id,phase,metric_key,trigger_value,non_value,ref_min,ref_max",
        RULE_BOUNDARIES,
    )
    def test_boundary(self, rule_id, phase, metric_key, trigger_value, non_value,
                      ref_min, ref_max):
        rule = rule_by_id(rule_id)
        view = _view_for_rule(rule)

        if rule.enabled:
            # 触发侧：刚好越界 -> 产出 RiskItem
            items = risk_engine.evaluate_phase(
                phase, [make_metric(metric_key, trigger_value, ref_min, ref_max)], view
            )
            assert any(r.rule_id == rule_id for r in items), (
                f"{rule_id} 在 value={trigger_value} 应触发"
            )
            # 不触发侧：刚好在界内
            items2 = risk_engine.evaluate_phase(
                phase, [make_metric(metric_key, non_value, ref_min, ref_max)], view
            )
            assert not any(r.rule_id == rule_id for r in items2), (
                f"{rule_id} 在 value={non_value} 不应触发"
            )
        else:
            # 禁用规则：验证纯条件逻辑（引擎侧零产出）
            assert risk_engine._match(trigger_value, rule) is True
            assert risk_engine._match(non_value, rule) is False

    @pytest.mark.parametrize(
        "rule_id,phase,metric_key,value,ref_min,ref_max",
        [
            # 双区间规则的另一侧
            ("RISK-009", PhaseKey.TOP, "swing_plane", 70.1, 55.0, 65.0),
            ("RISK-011", PhaseKey.ADDRESS, "knee_flexion", 174.1, 160.0, 172.0),
            ("RISK-012", PhaseKey.ADDRESS, "stance_width_ratio", 1.41, 1.0, 1.3),
        ],
    )
    def test_double_interval_other_side(self, rule_id, phase, metric_key, value,
                                        ref_min, ref_max):
        """双区间（A or B）的 B 侧也要触发。"""
        rule = rule_by_id(rule_id)
        assert risk_engine._match(value, rule) is True


class TestRisk016SymbolRegression:
    """🚨 RISK-016 符号回归（架构 §4.6 三层防线之测试层）。"""

    def test_normal_open_angle_does_not_trigger(self):
        """正常挥杆：⑦ 开放角 45°（在 35~60 参考内）不触发。"""
        items = risk_engine.evaluate_phase(
            PhaseKey.FOLLOW_THROUGH,
            [make_metric("shoulder_turn", 45.0, 35.0, 60.0)],
            CameraView.FACE_ON,
        )
        assert not any(r.rule_id == "RISK-016" for r in items)

    def test_low_open_angle_triggers(self):
        """释放不完整：⑦ 开放角 20° < 30 触发。"""
        items = risk_engine.evaluate_phase(
            PhaseKey.FOLLOW_THROUGH,
            [make_metric("shoulder_turn", 20.0, 35.0, 60.0)],
            CameraView.FACE_ON,
        )
        assert any(r.rule_id == "RISK-016" for r in items)


class TestRisk011BranchText:
    """RISK-011 三分支文案逐字断言（架构 §4.4 表）。"""

    def _eval(self, value):
        return risk_engine.evaluate_phase(
            PhaseKey.ADDRESS,
            [make_metric("knee_flexion", value, 160.0, 172.0)],
            CameraView.FACE_ON,
        )

    def test_value_150_branch_overbend(self):
        items = self._eval(150.0)
        item = next(r for r in items if r.rule_id == "RISK-011")
        assert item.trigger_description == (
            "你的膝部弯曲角为 150.0°，参考范围为 160°~172°。"
            "膝部弯曲过度，可能增加膝关节压力。"
        )

    def test_value_178_branch_overextend(self):
        items = self._eval(178.0)
        item = next(r for r in items if r.rule_id == "RISK-011")
        assert item.trigger_description == (
            "你的膝部弯曲角为 178.0°，参考范围为 160°~172°。"
            "膝部过于伸直，可能导致挥杆时重心不稳。"
        )

    def test_value_165_no_trigger(self):
        items = self._eval(165.0)
        assert not any(r.rule_id == "RISK-011" for r in items)

    def test_unknown_placeholder_preserved(self):
        """未知占位符原样保留（_SafeDict），不抛异常。"""
        rule = rule_by_id("RISK-016")
        metric = make_metric("shoulder_turn", 20.0, 35.0, 60.0)
        # 直接渲染一份含未知占位符的模板
        from app.risk_rules import TextTemplate

        weird = RiskRule(
            rule_id="RISK-T", risk_name="x", risk_level=rule.risk_level,
            trigger_phase=PhaseKey.TOP, metric_key="hip_turn",
            conditions=(Condition(">", 10.0),),
            trigger_template=TextTemplate(base="值 {value} 未知 {nope}"),
        )
        text = risk_engine.render_description(weird, metric)
        assert "{nope}" in text
        assert "20.0" in text


class TestViewGating:
    """机位门控（架构 §4.2 声明式 + 实证式双保险）。"""

    def test_force_enabled_all_view_gates(self, monkeypatch):
        """强开全部规则后，门控纯由机位决定。"""
        monkeypatch.setattr(
            config, "RISK_RULES_FORCE_ENABLE", frozenset(ALL_RULE_IDS)
        )
        face_ids = {r.rule_id for r in risk_engine.active_rules(CameraView.FACE_ON)}
        dtl_ids = {r.rule_id for r in risk_engine.active_rules(CameraView.DOWN_THE_LINE)}

        # 侧面专属：RISK-009 / 014 在正面不参与
        assert "RISK-009" not in face_ids
        assert "RISK-014" not in face_ids
        # 正面专属 11 条（除 009/014 外全部）
        for rid in ("RISK-001", "RISK-002", "RISK-003", "RISK-004", "RISK-005",
                    "RISK-006", "RISK-007", "RISK-008", "RISK-010", "RISK-011",
                    "RISK-012", "RISK-013", "RISK-015", "RISK-016", "RISK-017"):
            assert rid in face_ids, f"{rid} 应在正面候选集"
        # 正面专属规则在侧面不参与
        assert "RISK-009" in dtl_ids and "RISK-014" in dtl_ids
        for rid in ("RISK-001", "RISK-002", "RISK-003", "RISK-005", "RISK-007",
                    "RISK-010", "RISK-012", "RISK-013", "RISK-015", "RISK-016"):
            assert rid not in dtl_ids, f"{rid} 不应在侧面候选集"

    def test_evaluate_face_on_skips_dtl_only_rule(self, monkeypatch):
        """实证式门控：正面 TOP 即使 swing_plane 满足条件也不产 RISK-009。"""
        monkeypatch.setattr(
            config, "RISK_RULES_FORCE_ENABLE", frozenset(ALL_RULE_IDS)
        )
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [
                make_metric("hip_turn", 70.0, 45.0, 60.0),   # RISK-001 会触发
                make_metric("swing_plane", 40.0, 55.0, 65.0),  # RISK-009 条件满足
            ],
            CameraView.FACE_ON,
        )
        ids = {r.rule_id for r in items}
        assert "RISK-001" in ids
        assert "RISK-009" not in ids

    def test_missing_metric_skips_rule(self):
        """指标缺失（allow_drop 剔除 / 机位不适用）→ 规则跳过、不抛异常。"""
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [make_metric("hip_turn", 10.0, 45.0, 60.0)],  # 没有 x_factor
            CameraView.FACE_ON,
        )
        assert not any(r.rule_id == "RISK-005" for r in items)


class TestEnabledSwitch:
    """``enabled`` 开关 + 灰度强开（架构 §4.3）。"""

    def test_default_only_ten_enabled(self):
        enabled_ids = {r.rule_id for r in RISK_RULES if r.enabled}
        assert enabled_ids == {
            "RISK-001", "RISK-002", "RISK-005", "RISK-006", "RISK-007",
            "RISK-010", "RISK-011", "RISK-014", "RISK-016", "RISK-017",
        }

    def test_disabled_rule_never_emits(self):
        """RISK-003 条件满足但 enabled=False → 不产出。"""
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [make_metric("hip_turn", 35.0, 45.0, 60.0)],
            CameraView.FACE_ON,
        )
        assert not any(r.rule_id == "RISK-003" for r in items)

    def test_force_enable_turns_on_disabled_rule(self, monkeypatch):
        monkeypatch.setattr(config, "RISK_RULES_FORCE_ENABLE", frozenset({"RISK-003"}))
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [make_metric("hip_turn", 35.0, 45.0, 60.0)],
            CameraView.FACE_ON,
        )
        assert any(r.rule_id == "RISK-003" for r in items)

    def test_global_kill_switch(self, monkeypatch):
        """RISK_ENGINE_ENABLED=False 一键关停整个风险区。"""
        monkeypatch.setattr(config, "RISK_ENGINE_ENABLED", False)
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [make_metric("hip_turn", 70.0, 45.0, 60.0)],
            CameraView.FACE_ON,
        )
        assert items == []
        assert risk_engine.active_rules(CameraView.FACE_ON) == ()


class TestRobustness:
    """单条规则异常不冒泡；排序；性能。"""

    def test_single_rule_exception_swallowed(self, monkeypatch):
        orig_match = risk_engine._match

        def _boom(value, rule):
            if rule.rule_id == "RISK-001":
                raise RuntimeError("boom")
            return orig_match(value, rule)

        monkeypatch.setattr(risk_engine, "_match", _boom)
        # 不应抛异常；RISK-001 失败被吞掉
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [make_metric("hip_turn", 70.0, 45.0, 60.0)],
            CameraView.FACE_ON,
        )
        assert not any(r.rule_id == "RISK-001" for r in items)

    def test_sorting_high_before_medium(self, monkeypatch):
        """同阶段多风险按 high→medium→low，同级按 rule_id 稳定。

        DTL TOP 可参与（强开）的规则：RISK-006（high，全机位）、RISK-009（medium，
        侧面）——验证 high 排在 medium 前。face-on TAKEAWAY 另验 medium>low。"""
        monkeypatch.setattr(
            config, "RISK_RULES_FORCE_ENABLE", frozenset(ALL_RULE_IDS)
        )
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [
                make_metric("hip_turn", 70.0, 45.0, 60.0),           # RISK-001(face-on 被门控)
                make_metric("x_factor", 10.0, 20.0, 35.0),           # RISK-005(face-on 被门控)
                make_metric("lead_arm_straightness", 100.0, 150.0, 172.0),  # RISK-006 high
                make_metric("swing_plane", 40.0, 55.0, 65.0),        # RISK-009 medium
            ],
            CameraView.DOWN_THE_LINE,
        )
        ids = [r.rule_id for r in items]
        assert ids == ["RISK-006", "RISK-009"], ids

        # medium > low：face-on TAKEAWAY（RISK-002 medium + RISK-004 low）
        items2 = risk_engine.evaluate_phase(
            PhaseKey.TAKEAWAY,
            [
                make_metric("hip_turn", 25.0, 8.0, 18.0),   # RISK-002 medium
                make_metric("head_drift", 6.0, 0.0, 4.0),   # RISK-004 low
            ],
            CameraView.FACE_ON,
        )
        ids2 = [r.rule_id for r in items2]
        assert ids2 == ["RISK-002", "RISK-004"], ids2

    def test_risk_item_fields(self):
        items = risk_engine.evaluate_phase(
            PhaseKey.TOP,
            [make_metric("hip_turn", 70.0, 45.0, 60.0)],
            CameraView.FACE_ON,
        )
        item = next(r for r in items if r.rule_id == "RISK-001")
        assert item.metric_key == "hip_turn"
        assert item.trigger_phase is PhaseKey.TOP
        assert item.value == pytest.approx(70.0)
        assert item.ref_min == pytest.approx(45.0)
        assert item.ref_max == pytest.approx(60.0)
        assert item.manual_page == "6"
        assert len(item.suggestions) == 3
        assert item.trigger_description.startswith("你的顶点阶段髋部转动角")

    def test_performance_under_50ms(self):
        phase_metrics = {
            phase: [
                make_metric("hip_turn", 30.0),
                make_metric("shoulder_turn", 40.0),
                make_metric("knee_flexion", 165.0, 160.0, 172.0),
            ]
            for phase in PhaseKey
        }
        start = time.perf_counter()
        for _ in range(200):
            risk_engine.evaluate_all(phase_metrics, CameraView.FACE_ON)
        elapsed_ms = (time.perf_counter() - start) / 200 * 1000
        assert elapsed_ms < 50, f"风险引擎单次全量 {elapsed_ms:.2f}ms 超标 (AC-P5)"

    def test_evaluate_all_returns_eight_phases(self):
        phase_metrics = {phase: [] for phase in PhaseKey}
        result = risk_engine.evaluate_all(phase_metrics, CameraView.FACE_ON)
        assert set(result) == set(PhaseKey)
        assert all(isinstance(v, list) for v in result.values())


class TestImportSelfCheck:
    """导入期自检：enabled=True 缺文案 -> 导入即 RuntimeError（架构 §4.5）。"""

    def test_import_raises_when_enabled_without_copy(self, monkeypatch):
        import app.risk_rules as rr

        clean = rr.RISK_RULES
        bad = RiskRule(
            rule_id="RISK-003",
            risk_name="髋部灵活性不足风险",
            risk_level=rr.RiskLevel.MEDIUM,
            trigger_phase=PhaseKey.TOP,
            metric_key="hip_turn",
            conditions=(Condition("<", 40.0),),
            views=frozenset({CameraView.FACE_ON}),
            enabled=True,  # ← 翻了开关但没填文案
        )
        rules = list(clean)
        rules[2] = bad  # RISK-003 位置
        monkeypatch.setattr(rr, "RISK_RULES", tuple(rules))

        with pytest.raises(RuntimeError, match="enabled=True 但缺 trigger_template"):
            importlib.reload(risk_engine)

        # 恢复现场：用干净规则重新加载，避免污染后续用例
        monkeypatch.setattr(rr, "RISK_RULES", clean)
        importlib.reload(risk_engine)
