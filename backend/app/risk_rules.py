"""损伤风险规则库（PRD-v2-risk-engine.md §3.2 / §3.3 的 17 条规则静态数据）。

**纯数据模块**：这里没有任何控制流（无 ``if`` 求值、无渲染），只有
数据结构定义与规则数据。匹配逻辑在 :mod:`app.risk_engine`，改文案只改本文件。

依赖方向：``risk_rules -> schemas``（仅枚举）。**不** import ``reference`` /
``metrics``，避免污染数据层。

三条设计纪律（架构 §4.3 / §4.4）：
1. ``enabled=False`` 的 7 条规则：结构照写、条件照填，但 ``trigger_template`` /
   ``suggestions`` / ``manual_excerpt`` 一律留空，**严禁研发自行编造文案**。
2. RISK-011 的内嵌 JS 三元表达式由 ``Branch`` 声明式表达，引擎侧零 eval。
3. ``metric_key`` 一律填 PDD 的【对外 key】（与 ``reference.METRIC_SPECS`` 的
   ``MetricSpec.key`` 对齐），引擎只按对外 key 查表——RISK-016 的符号陷阱在
   数据层被 ``fn_key`` 映射拆除，引擎侧零特判。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple

from .schemas import CameraView, PhaseKey, RiskLevel

#: 双机位全集（与 reference.ALL_VIEWS 同值；此处不 import reference 保持纯数据）
ALL_VIEWS: FrozenSet[CameraView] = frozenset(
    {CameraView.FACE_ON, CameraView.DOWN_THE_LINE}
)


@dataclass(frozen=True)
class Condition:
    """单个布尔条件。``operator`` ∈ {'>', '<', '>=', '<=', '=='}。

    ``==`` 用 ``math.isclose(value, threshold, abs_tol=1e-6)`` 判定，禁止裸 ``==``
    （架构 §9.4）。
    """

    operator: str
    threshold: float

    def match(self, value: float) -> bool:
        """对 ``value`` 求值；未知运算符抛 :class:`ValueError`（导入期自检兜底）。"""
        op = self.operator
        if op == ">":
            return value > self.threshold
        if op == "<":
            return value < self.threshold
        if op == ">=":
            return value >= self.threshold
        if op == "<=":
            return value <= self.threshold
        if op == "==":
            return math.isclose(value, self.threshold, abs_tol=1e-6)
        raise ValueError(f"unknown operator: {op!r}")


@dataclass(frozen=True)
class Branch:
    """文案条件分支：命中 ``condition`` 时把 ``text`` 填进模板的 ``{branch}`` 占位符。"""

    condition: Condition
    text: str


@dataclass(frozen=True)
class TextTemplate:
    """触发原因模板。

    ``base`` 支持的占位符（白名单 6 个）：``{value} {unit} {ref_min} {ref_max}
    {threshold} {branch}``。未知占位符原样保留、不抛异常（``_SafeDict``）。
    ``branches`` 按声明顺序求值，取第一个命中的 ``text``；全不命中则
    ``{branch}`` -> ``""``。
    """

    base: str
    branches: Tuple[Branch, ...] = ()


@dataclass(frozen=True)
class RiskRule:
    """一条风险规则（PDD §5.1 数据化）。"""

    rule_id: str
    risk_name: str
    risk_level: RiskLevel
    trigger_phase: PhaseKey
    #: 必须是 §3.3 映射表的【对外 key】
    metric_key: str
    conditions: Tuple[Condition, ...]
    #: "or" | "and"，解决 C1 双区间
    logic: str = "or"
    #: 机位门控（声明式；实证式门控在引擎里按指标存在性判断）
    views: FrozenSet[CameraView] = ALL_VIEWS
    trigger_template: Optional[TextTemplate] = None
    suggestions: Tuple[str, ...] = ()
    manual_excerpt: Optional[str] = None
    manual_page: Optional[str] = None
    #: 🔑 决策 3 的开关：False = 逻辑照常实现但不对用户可见
    enabled: bool = True
    #: 缺文案的占位说明，仅供研发/PM 阅读，不出网
    copy_note: str = ""


#: 缺文案 7 条的统一占位说明（严禁研发自行编造）
_MISSING_COPY_NOTE = "⛔ PDD 未提供 trigger_description / suggestions / manual_excerpt"

#: 双机位简写
_F = frozenset({CameraView.FACE_ON})
_D = frozenset({CameraView.DOWN_THE_LINE})
_FD = ALL_VIEWS


# ---------------------------------------------------------------------------
# 17 条规则（顺序 = RISK-001 → RISK-017；文案逐字抄录 PRD §3.3）
# ---------------------------------------------------------------------------

RISK_RULES: Tuple[RiskRule, ...] = (
    # ---- RISK-001 髋部转动过度风险（high · ④ top · 正面）-----------------
    RiskRule(
        rule_id="RISK-001",
        risk_name="髋部转动过度风险",
        risk_level=RiskLevel.HIGH,
        trigger_phase=PhaseKey.TOP,
        metric_key="hip_turn",
        conditions=(Condition(">", 62.0),),
        logic="or",
        views=_F,
        trigger_template=TextTemplate(
            base=(
                "你的顶点阶段髋部转动角为 {value}°，高于参考范围(45°~60°)。"
                "髋部转动过度会削弱肩髋分离（X-Factor），降低蓄力效果，"
                "同时增加腰部及髋部的损伤风险。"
            )
        ),
        suggestions=(
            "技术动作调整：顶点时感受\"上半身扭转而下半身稳定\"的分离感，限制髋部过度转动。",
            "专项体能训练：调整骨盆额状面平衡，纠正下交叉综合征体态。",
            "运动姿势改善：准备姿势时增加脚尖打开幅度。",
        ),
        manual_excerpt=(
            "研究显示，6成以上的腰部不适最终可归因于髋部损伤...髋部损伤会诱发"
            "腹股沟区域的不适感，造成挥杆动作异常。"
        ),
        manual_page="6",
    ),
    # ---- RISK-002 髋部过早转动风险（medium · ② takeaway · 正面）----------
    RiskRule(
        rule_id="RISK-002",
        risk_name="髋部过早转动风险",
        risk_level=RiskLevel.MEDIUM,
        trigger_phase=PhaseKey.TAKEAWAY,
        metric_key="hip_turn",
        conditions=(Condition(">", 20.0),),
        logic="or",
        views=_F,
        trigger_template=TextTemplate(
            base=(
                "你的起杆阶段髋部转动角为 {value}°，高于参考范围(8°~18°)。"
                "起杆阶段髋部应保持相对稳定，过早转动可能增加腰部代偿压力。"
            )
        ),
        suggestions=(
            "技术动作调整：起杆应由肩部带动，而非髋部主动旋转。",
            "专项体能训练：加强核心稳定性训练，提升髋关节控制力。",
        ),
        manual_excerpt=(
            "胸椎与髋部的灵活性及稳定性受限，是导致背痛重要的功能性因素。"
            "此外高尔夫异常挥杆动作也与腰部不适密切相关。"
        ),
        manual_page="6",
    ),
    # ---- RISK-003 髋部灵活性不足风险（medium · ④ top · 正面 · ❌缺文案）---
    RiskRule(
        rule_id="RISK-003",
        risk_name="髋部灵活性不足风险",
        risk_level=RiskLevel.MEDIUM,
        trigger_phase=PhaseKey.TOP,
        metric_key="hip_turn",
        conditions=(Condition("<", 40.0),),
        logic="or",
        views=_F,
        manual_page="6",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-004 头部晃动风险（low · ② takeaway · 全部 · ❌缺文案）-------
    RiskRule(
        rule_id="RISK-004",
        risk_name="头部晃动风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.TAKEAWAY,
        metric_key="head_drift",
        conditions=(Condition(">=", 5.0),),
        logic="or",
        views=_FD,
        manual_page="-",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-005 X-Factor 过低风险（high · ④ top · 正面）-----------------
    RiskRule(
        rule_id="RISK-005",
        risk_name="X-Factor 过低风险",
        risk_level=RiskLevel.HIGH,
        trigger_phase=PhaseKey.TOP,
        metric_key="x_factor",
        conditions=(Condition("<", 18.0),),
        logic="or",
        views=_F,
        trigger_template=TextTemplate(
            base=(
                "你的顶点阶段X-Factor为 {value}°，低于参考范围(20°~35°)。"
                "X-Factor（肩髋分离度）是挥杆力量的核心来源，数值过低说明"
                "'上弦'不紧，力量无法有效蓄积。"
            )
        ),
        suggestions=(
            "技术动作调整：顶点时感受肩部继续转动而髋部保持稳定，建立分离感。",
            "专项体能训练：提升胸椎旋转灵活性，同时加强核心抗旋转能力。",
        ),
        manual_excerpt=(
            "胸椎与髋部的灵活性及稳定性受限，是导致背痛重要的功能性因素。"
        ),
        manual_page="P6/P11",
    ),
    # ---- RISK-006 鸡翅风险(肘部)（high · ④ top · 全部）--------------------
    RiskRule(
        rule_id="RISK-006",
        risk_name="鸡翅风险(肘部)",
        risk_level=RiskLevel.HIGH,
        trigger_phase=PhaseKey.TOP,
        metric_key="lead_arm_straightness",
        conditions=(Condition("<", 145.0),),
        logic="or",
        views=_FD,
        trigger_template=TextTemplate(
            base=(
                "你的顶点阶段引导臂伸直度为 {value}°，低于参考范围(150°~172°)。"
                "左臂过度弯曲（\"鸡翅\"）会导致挥杆力量泄漏，"
                "同时增加肘部和腕部的损伤风险。"
            )
        ),
        suggestions=(
            "技术动作调整：顶点时保持左臂伸展，避免\"鸡翅\"动作。",
            "专项体能训练：加强肩背肌肉力量与柔韧性。",
        ),
        manual_excerpt=(
            "手腕过度屈曲或伸展的击球状态会分别增加前臂屈肌和前臂伸肌的张力，"
            "从而引起高尔夫球肘或网球肘。"
        ),
        manual_page="8",
    ),
    # ---- RISK-007 肩部转动不足风险（low · ③ backswing · 正面）-------------
    RiskRule(
        rule_id="RISK-007",
        risk_name="肩部转动不足风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.BACKSWING,
        metric_key="shoulder_turn",
        conditions=(Condition("<", 50.0),),
        logic="or",
        views=_F,
        trigger_template=TextTemplate(
            base=(
                "你的上杆阶段肩部转动角为 {value}°，低于参考范围(55°~72°)。"
                "肩部转动不足可能导致上杆不充分，影响击球距离。"
            )
        ),
        suggestions=(
            "技术动作调整：增加上杆时肩部的旋转幅度。",
            "专项体能训练：加强胸椎灵活性训练。",
        ),
        manual_excerpt="在挥杆击球过程中，肩部主要承担力量传递工作。",
        manual_page="11",
    ),
    # ---- RISK-008 后臂过直风险（low · ③ backswing · 全部 · ❌缺文案）------
    RiskRule(
        rule_id="RISK-008",
        risk_name="后臂过直风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.BACKSWING,
        metric_key="trail_arm_flexion",
        conditions=(Condition(">", 130.0),),
        logic="or",
        views=_FD,
        manual_page="8",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-009 挥杆平面过平/过陡风险（medium · ④ top · 侧面 · ❌缺文案）
    RiskRule(
        rule_id="RISK-009",
        risk_name="挥杆平面过平/过陡风险",
        risk_level=RiskLevel.MEDIUM,
        trigger_phase=PhaseKey.TOP,
        metric_key="swing_plane",
        conditions=(Condition("<", 50.0), Condition(">", 70.0)),
        logic="or",
        views=_D,
        manual_page="-",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-010 X-Factor 过早释放风险（medium · ⑤ downswing · 正面）-----
    RiskRule(
        rule_id="RISK-010",
        risk_name="X-Factor 过早释放风险",
        risk_level=RiskLevel.MEDIUM,
        trigger_phase=PhaseKey.DOWNSWING,
        metric_key="x_factor_retention",
        conditions=(Condition("<", 80.0),),
        logic="or",
        views=_F,
        trigger_template=TextTemplate(
            base=(
                "你的X-Factor保持率为 {value}%，低于参考范围(≥85%)。"
                "下杆初期X-Factor过早释放意味着髋部和肩部同时打开，"
                "损失了本应传导至球杆的能量。"
            )
        ),
        suggestions=(
            "技术动作调整：下杆初期保持上半身的扭转，由髋部率先启动带动下杆。",
            "专项体能训练：提升核心力量与协调性。",
        ),
        manual_excerpt=(
            "髋部损伤会诱发腹股沟区域的不适感，造成挥杆动作异常，"
            "进而引发其他关节的运动损伤。"
        ),
        manual_page="P6/P11",
    ),
    # ---- RISK-011 膝部过屈/过直风险（low · ① address · 全部 · 双区间）-----
    RiskRule(
        rule_id="RISK-011",
        risk_name="膝部过屈/过直风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.ADDRESS,
        metric_key="knee_flexion",
        conditions=(Condition("<", 156.0), Condition(">", 174.0)),
        logic="or",
        views=_FD,
        trigger_template=TextTemplate(
            base=(
                "你的膝部弯曲角为 {value}°，参考范围为 160°~172°。{branch}。"
            ),
            branches=(
                Branch(Condition("<", 156.0), "膝部弯曲过度，可能增加膝关节压力"),
                Branch(Condition(">=", 156.0), "膝部过于伸直，可能导致挥杆时重心不稳"),
            ),
        ),
        suggestions=(
            "调整准备姿势时膝部微屈，保持弹性。",
            "专项体能训练：加强下肢肌肉力量与柔韧性。",
        ),
        manual_excerpt="过度屈膝或下蹲也会增加膝关节的压力，增加膝关节损伤风险。",
        manual_page="10",
    ),
    # ---- RISK-012 站姿过宽/过窄风险（low · ① address · 正面 · ❌缺文案）---
    RiskRule(
        rule_id="RISK-012",
        risk_name="站姿过宽/过窄风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.ADDRESS,
        metric_key="stance_width_ratio",
        conditions=(Condition("<", 0.9), Condition(">", 1.4)),
        logic="or",
        views=_F,
        manual_page="-",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-013 髋部开放不足风险（medium · ⑥ impact · 正面 · ❌缺文案）--
    RiskRule(
        rule_id="RISK-013",
        risk_name="髋部开放不足风险",
        risk_level=RiskLevel.MEDIUM,
        trigger_phase=PhaseKey.IMPACT,
        metric_key="hip_open_angle",
        conditions=(Condition("<", 12.0),),
        logic="or",
        views=_F,
        manual_page="8",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-014 过早起身(Early Extension)风险（high · ⑥ impact · 侧面）--
    RiskRule(
        rule_id="RISK-014",
        risk_name="过早起身(Early Extension)风险",
        risk_level=RiskLevel.HIGH,
        trigger_phase=PhaseKey.IMPACT,
        metric_key="spine_tilt_change",
        conditions=(Condition(">=", 10.0),),
        logic="or",
        views=_D,
        trigger_template=TextTemplate(
            base=(
                "你的脊柱前倾变化量为 {value}°，远高于参考范围(<8°)。"
                "这表明你在击球时'起身'(Early Extension)明显，"
                "是打薄、打厚、剃头球的主要原因之一。"
            )
        ),
        suggestions=(
            "技术动作调整：击球时保持脊柱角度，避免身体向上直起。",
            "专项体能训练：加强核心力量与髋部灵活性，减少身体代偿。",
        ),
        manual_excerpt=(
            "脊柱过度侧屈引起的胸腔压缩或腹外斜肌快速发力均可能导致肋部骨折。"
        ),
        manual_page="11",
    ),
    # ---- RISK-015 重心转移不足风险（medium · ⑥ impact · 正面 · ❌缺文案）--
    RiskRule(
        rule_id="RISK-015",
        risk_name="重心转移不足风险",
        risk_level=RiskLevel.MEDIUM,
        trigger_phase=PhaseKey.IMPACT,
        metric_key="pelvis_shift",
        conditions=(Condition("<", 8.0),),
        logic="or",
        views=_F,
        manual_page="10",
        enabled=False,
        copy_note=_MISSING_COPY_NOTE,
    ),
    # ---- RISK-016 释放不完整风险（low · ⑦ follow_through · 正面）----------
    # ⚠️ 符号陷阱：metric_key 是 PDD 的 shoulder_turn，但 ⑦ spec 的 fn_key 是
    #    shoulder_open（= −肩转，正值），引擎按对外 key 查表即拿到正确正开放角。
    RiskRule(
        rule_id="RISK-016",
        risk_name="释放不完整风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.FOLLOW_THROUGH,
        metric_key="shoulder_turn",
        conditions=(Condition("<", 30.0),),
        logic="or",
        views=_F,
        trigger_template=TextTemplate(
            base=(
                "你的送杆阶段肩部转动角为 {value}°，低于参考范围(35°~60°)。"
                "肩部释放不完整可能导致能量泄漏，影响击球质量。"
            )
        ),
        suggestions=(
            "技术动作调整：送杆时充分释放肩部，跟随挥杆完成完整动作。",
        ),
        manual_excerpt=None,
        manual_page="8",
        #: 缺 manual_excerpt（有页码），trigger_description / suggestions 齐全 → 开启
        enabled=True,
        copy_note="⚠️ PDD 缺 manual_excerpt（页码 P8），前端隐藏「查看手册原文」入口",
    ),
    # ---- RISK-017 收杆不稳定风险（low · ⑧ finish · 全部）-------------------
    RiskRule(
        rule_id="RISK-017",
        risk_name="收杆不稳定风险",
        risk_level=RiskLevel.LOW,
        trigger_phase=PhaseKey.FINISH,
        metric_key="balance_hold",
        conditions=(Condition("<", 0.6),),
        logic="or",
        views=_FD,
        trigger_template=TextTemplate(
            base=(
                "你的收杆平衡保持时间为 {value}s，低于参考范围(≥0.8s)。"
                "收杆不稳说明挥杆过程中重心转移存在缺陷。"
            )
        ),
        suggestions=(
            "技术动作调整：收杆时保持重心完全转移至前脚，维持3秒平衡。",
            "专项体能训练：加强下肢力量与平衡能力。",
        ),
        manual_excerpt=None,
        manual_page="10",
        #: 缺 manual_excerpt（有页码），trigger_description / suggestions 齐全 → 开启
        enabled=True,
        copy_note="⚠️ PDD 缺 manual_excerpt（页码 P10），前端隐藏「查看手册原文」入口",
    ),
)


# ---------------------------------------------------------------------------
# 导入期自身一致性检查（架构 §4.5 第 4 项；第 1~3 项在 risk_engine.self_check）
# ---------------------------------------------------------------------------


def _self_check_rules() -> None:
    """规则库自身一致性：ID 唯一 / 运算符白名单 / logic 白名单 / conditions 非空。

    失败立即 :class:`RuntimeError`，让配置错误在服务启动瞬间暴露。
    """
    ids = [rule.rule_id for rule in RISK_RULES]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"risk_rules: duplicate rule_id: {ids}")

    allowed_ops = {">", "<", ">=", "<=", "=="}
    for rule in RISK_RULES:
        if not rule.conditions:
            raise RuntimeError(f"{rule.rule_id}: conditions 为空")
        if rule.logic not in ("and", "or"):
            raise RuntimeError(f"{rule.rule_id}: 非法 logic {rule.logic!r}")
        for cond in rule.conditions:
            if cond.operator not in allowed_ops:
                raise RuntimeError(
                    f"{rule.rule_id}: 非法 operator {cond.operator!r}"
                )
        if rule.trigger_template is not None:
            for branch in rule.trigger_template.branches:
                if branch.condition.operator not in allowed_ops:
                    raise RuntimeError(
                        f"{rule.rule_id}: 分支非法 operator {branch.condition.operator!r}"
                    )
        if not rule.risk_name:
            raise RuntimeError(f"{rule.rule_id}: 缺 risk_name")


_self_check_rules()
