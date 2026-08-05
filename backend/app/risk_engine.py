"""损伤风险匹配引擎（架构 ARCHITECTURE-v2.md §4）。

**无状态纯函数**：输入 = 已算出的 ``StageMetric`` 列表 + 机位；输出 = 每阶段
``List[RiskItem]``。不回头重算任何几何量，不做任何 I/O。

分层：
- :mod:`app.risk_rules` 持有全部 17 条规则**数据**（阈值/文案）；
- 本模块只有**逻辑**：机位门控 → 条件求值（or/and）→ 文案渲染（含条件分支）
  → 按等级排序。

硬约束（架构 §9.3）：
1. 单条规则求值失败只 ``logger.exception``，**绝不让引擎异常冒泡到 pipeline**；
2. 文案渲染**禁止 eval / exec / f-string 动态求值**，分支只走声明式 ``Branch``；
3. 导入期 ``self_check()`` 四类检查，任一失败即 :class:`RuntimeError`。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from . import config, reference
from .risk_rules import RISK_RULES, Condition, RiskRule, TextTemplate
from .schemas import CameraView, PhaseKey, RiskItem, RiskLevel, StageMetric

logger = logging.getLogger(__name__)

#: 风险等级排序权重（high > medium > low；同级按 rule_id 稳定排序）
_LEVEL_ORDER: Dict[RiskLevel, int] = {
    RiskLevel.HIGH: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.LOW: 2,
}


# ---------------------------------------------------------------------------
# 开关与匹配
# ---------------------------------------------------------------------------


def _rule_enabled(rule: RiskRule) -> bool:
    """三层开关（架构 §4.3）：全局止血阀在调用方检查；这里是单条 + 灰度强开。"""
    return rule.enabled or rule.rule_id in config.RISK_RULES_FORCE_ENABLE


def _match(value: float, rule: RiskRule) -> bool:
    """按 ``rule.logic`` 组合求值所有 ``conditions``。"""
    if rule.logic == "and":
        return all(cond.match(value) for cond in rule.conditions)
    return any(cond.match(value) for cond in rule.conditions)


# ---------------------------------------------------------------------------
# 文案渲染（架构 §4.4，无 eval）
# ---------------------------------------------------------------------------


class _SafeDict(dict):
    """未知占位符原样保留（``{key}``），绝不 KeyError。"""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def _fmt(value: float, unit: str) -> str:
    """按单位定小数位：° % → 1 位，s / 无量纲 → 2 位。"""
    if unit in (reference.UNIT_DEG, reference.UNIT_PCT):
        return f"{value:.1f}"
    return f"{value:.2f}"


def render_description(rule: RiskRule, metric: StageMetric) -> str:
    """渲染触发原因成品文案（含条件分支）。

    占位符白名单固定 6 个：``value / unit / ref_min / ref_max / threshold / branch``。
    """
    tmpl = rule.trigger_template
    if tmpl is None:
        return ""
    branch = ""
    for br in tmpl.branches:
        if br.condition.match(metric.value):
            branch = br.text
            break
    ctx = _SafeDict(
        value=_fmt(metric.value, metric.unit),
        unit=metric.unit,
        ref_min=_fmt(metric.ref_min, metric.unit),
        ref_max=_fmt(metric.ref_max, metric.unit),
        threshold=_fmt(rule.conditions[0].threshold, metric.unit),
        branch=branch,
    )
    return tmpl.base.format_map(ctx)


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------


def _build_item(rule: RiskRule, metric: StageMetric) -> RiskItem:
    """把命中规则 + 指标上下文封装成对外 :class:`RiskItem`。"""
    return RiskItem(
        rule_id=rule.rule_id,
        risk_name=rule.risk_name,
        risk_level=rule.risk_level,
        trigger_phase=rule.trigger_phase,
        metric_key=metric.key,
        metric_name=metric.name,
        value=metric.value,
        unit=metric.unit,
        ref_min=metric.ref_min,
        ref_max=metric.ref_max,
        trigger_description=render_description(rule, metric),
        suggestions=list(rule.suggestions),
        manual_excerpt=rule.manual_excerpt,
        manual_page=rule.manual_page,
    )


def active_rules(view: CameraView) -> Tuple[RiskRule, ...]:
    """当前机位下可参与匹配的规则（声明式门控 + enabled 开关）。

    注意：这只是「候选集」；实际是否评估还取决于该阶段是否真的算出了
    对应指标（实证式门控，见 :func:`evaluate_phase`）。
    """
    if not config.RISK_ENGINE_ENABLED:
        return ()
    return tuple(
        rule
        for rule in RISK_RULES
        if _rule_enabled(rule) and view in rule.views
    )


def evaluate_phase(
    phase: PhaseKey, metrics: List[StageMetric], view: CameraView
) -> List[RiskItem]:
    """对单个阶段做风险匹配，返回按 high→medium→low 排序的风险列表。

    两道机位门（架构 §4.2）：
    1. 声明式 —— ``rule.views``（产品意图）；
    2. 实证式 —— 指标在 ``metrics`` 里实际存在（运行时真相，含 allow_drop 剔除）。

    单条规则求值失败被吞掉并记日志，绝不让引擎异常冒泡到 pipeline。
    """
    if not config.RISK_ENGINE_ENABLED:
        return []

    by_key = {m.key: m for m in metrics}  # 只看"这一阶段实际下发了什么"
    out: List[RiskItem] = []
    for rule in RISK_RULES:
        if rule.trigger_phase is not phase:
            continue
        if not _rule_enabled(rule):
            continue
        if view not in rule.views:
            continue
        metric = by_key.get(rule.metric_key)
        if metric is None:
            continue
        try:
            if not _match(metric.value, rule):
                continue
            out.append(_build_item(rule, metric))
        except Exception:  # noqa: BLE001 - 单条失败不影响整份报告
            logger.exception("risk rule failed: %s", rule.rule_id)

    out.sort(key=lambda r: (_LEVEL_ORDER[r.risk_level], r.rule_id))
    return out


def evaluate_all(
    phase_metrics: Dict[PhaseKey, List[StageMetric]], view: CameraView
) -> Dict[PhaseKey, List[RiskItem]]:
    """对 8 个阶段全量求值（纯内存，实测量级 < 1ms，AC-P5 ≤50ms 无压力）。"""
    return {
        phase: evaluate_phase(phase, metrics, view)
        for phase, metrics in phase_metrics.items()
    }


# ---------------------------------------------------------------------------
# 导入期自检（架构 §4.5，四类检查）
# ---------------------------------------------------------------------------


def self_check() -> List[str]:
    """四类检查，任一失败即 :class:`RuntimeError`。

    1. ``enabled=True`` 必须有非空 ``trigger_template`` 与非空 ``suggestions``；
    2. 每条规则的 ``metric_key`` 必须在 ``METRIC_SPECS[trigger_phase]`` 中存在同名 spec；
    3. 规则的 ``views`` 必须 ⊆ 该 spec 的 ``views``（声明式与实证式两道门一致）；
    4. （rule_id 唯一 / 运算符 / logic / conditions 非空由 :mod:`risk_rules` 自检）
    """
    problems: List[str] = []
    for rule in RISK_RULES:
        if rule.enabled:
            if rule.trigger_template is None or not rule.trigger_template.base.strip():
                problems.append(f"{rule.rule_id}: enabled=True 但缺 trigger_template")
            if not rule.suggestions:
                problems.append(f"{rule.rule_id}: enabled=True 但缺 suggestions")

        specs = reference.METRIC_SPECS.get(rule.trigger_phase, [])
        by_key = {s.key: s for s in specs}
        if rule.metric_key not in by_key:
            problems.append(
                f"{rule.rule_id}: metric_key {rule.metric_key!r} 不在 "
                f"{rule.trigger_phase.value} 的 METRIC_SPECS"
            )
            continue
        spec = by_key[rule.metric_key]
        if not rule.views.issubset(spec.views):
            problems.append(
                f"{rule.rule_id}: views {sorted(v.value for v in rule.views)} "
                f"⊄ spec views {sorted(v.value for v in spec.views)}"
            )

    if problems:
        raise RuntimeError("risk rule self_check failed:\n" + "\n".join(problems))
    return problems


#: 模块加载即自检：配置错误在服务启动瞬间暴露，而不是在用户面前
self_check()
