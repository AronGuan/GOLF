# 高尔夫挥杆分析小程序 —— v2.0 增量 PRD：损伤风险筛查引擎 + 侧面机位

| 项 | 内容 |
|---|---|
| 文档版本 | v2.0-delta（增量 PRD） |
| 撰写人 | 许清楚（本项目 PM） |
| 需求来源 | 用户方 PM 郑天虹《高尔夫挥杆分析小程序 —— 产品开发文档 (PDD) V2.0》 |
| 基线文档 | `docs/PRD.md`（MVP v1.0）、`docs/ARCHITECTURE.md` §8.3、`backend/app/reference.py`（23 个指标函数 / 35 条 MetricSpec） |
| 关联文档 | `docs/club-detection-design.md`、`docs/ADR-001-club-detection.md` |
| 文档性质 | **增量**：只描述相对 MVP 的变更/新增，不重复 MVP 已有内容 |
| 文档状态 | 待架构师评审（含 21 项待澄清问题，其中 6 项为**阻塞级**） |

> **阅读须知（给架构师和工程师）**
> 1. 本文所有阈值、文案均**逐字来自 PDD v2.0**，未做任何自行编造。凡 PDD 未写明的，一律显式标注 `⛔ 文档未明确`。
> 2. §3 的风险规则表已精确到「阶段 + 指标 key + 运算符 + 数值 + 机位门控」，可直接照着写 `if` 判断。
> 3. §7 是**与现状的差异对照**，§8 是**必须先拍板才能动工的问题清单**。**建议先读 §8 再读正文。**

---

## 1. 产品目标（v2.0 增量）

| # | 目标 | 说明 | 可度量成功信号 |
|---|---|---|---|
| **G4** | **从"给数据"升级为"给诊断"** | 在 8 阶段指标之上，叠加一层基于《高尔夫运动保障手册》的损伤风险规则引擎，输出「风险名称 + 触发原因 + 改进建议 + 手册原文」 | PDD AC-18：每条风险含等级/名称/触发原因/建议/手册原文/页码，缺一不可；PDD 交付判定：≥4/5 名真实球手能自主说出一条与自己动作相关的损伤风险建议 |
| **G5** | **打通侧面机位（down-the-line）** | 支持用户在正面 / 侧面二选一，侧面机位解锁矢状面指标（脊柱前倾角、挥杆平面角、脊柱侧弯角、起身量） | PDD AC-09/AC-10：正面结果页不出现侧面专属指标；侧面结果页出现 `swing_plane`、`spine_tilt_change` |
| **G6** | **降低专业术语理解门槛** | 每张指标卡下方常驻一行术语解释；风险等级用红/橙/蓝三色 + 图标区分；指标状态从 3 值扩到 5 值，把"重度偏离"从"偏低/偏高"里单独拆出来 | PDD AC-16：每个指标卡片下方均有解释文案 |

> **本期仍不做**：登录、历史记录、视频回放、手动微调阶段点、左手球手、AI 文字点评、训练推荐、分享（PDD §2.2 / §2.3，与 MVP PRD 的 P1/P2 一致）。

---

## 2. v2.0 用户旅程变更点

```
打开小程序
  → 【新增】选择机位（正面 / 侧面，互斥二选一，无 auto）
  → 查看该机位对应的拍摄要求（图文随机位动态切换）
  → 拍摄 / 相册选择 → 本地校验(2~15s, ≤20MB, mp4/mov)
  → 【变更】上传时携带 camera_view 参数
  → 等待分析（步骤④文案改为「计算姿态指标与风险」）
  → 结果页：
      区域1 8阶段缩略图（不变）
      区域2 骨架叠加大图（不变）
      区域3 指标卡片 【新增 description 术语解释行】【新增 critical_low/critical_high 两态】
      区域4 【全新】本阶段风险与改进建议区
      区域5 全程指标条（常驻，不变）
      区域6 【新增】[查看完整报告] 占位按钮（P1 预留，本期不实现功能）
```

---

## 3. 损伤风险筛查引擎 ⭐（本次核心）

### 3.1 风险规则数据结构（PDD §5.1 原文）

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `rule_id` | string | 唯一标识 | `RISK-001` |
| `risk_name` | string | 风险名称 | `"腰部损伤风险"` |
| `risk_level` | string | 风险等级 | `high` / `medium` / `low` |
| `trigger_phase` | string | 触发阶段 | `top` / `impact` 等 |
| `trigger_condition` | object | 触发条件 | `{metric_key: "hip_turn", operator: ">", threshold: 60}` |
| `trigger_description` | string | 触发原因模板 | `"你的髋部转动角({value}°)高于参考范围({ref_min}°~{ref_max}°)，这可能导致..."` |
| `suggestions` | array | 改进建议列表 | `["建议1", "建议2"]` |
| `manual_excerpt` | string | 手册原文摘录 | `"研究显示，6成以上的腰部不适..."` |
| `manual_page` | number | 手册页码 | `6` |

**运算符集合**（PDD §5.3）：`>` `<` `>=` `<=` `==`。
⚠️ PDD 中 RISK-009 / RISK-011 / RISK-012 的条件是「A 或 B」双区间形式（`<x 或 >y`），**单个 `trigger_condition` object 无法表达**——见 §8 待澄清 #C1。

### 3.2 风险规则完整总表（17 条，可直接编码）

> **列说明**
> - `PDD指标key` = PDD v2.0 §4.1 使用的 key；`现有实现key` = 当前 `reference.py` / `metrics.py` 中的 key（**两者不一致时需做映射或改名，见 §7**）。
> - `机位门控` = PDD §5.4 规定的所需机位；若当前分析机位不满足，该规则**不参与匹配**（不是"不触发"，而是"不评估"）。
> - `触发条件` 已展开成可直接写的布尔表达式。

| 规则ID | 风险名称 | 等级 | 触发阶段 | PDD指标key | 现有实现key | 触发条件（布尔表达式） | 机位门控 | 手册页 | 文案完整度 |
|---|---|---|---|---|---|---|---|---|---|
| **RISK-001** | 髋部转动过度风险 | 🔴 high | ④ top | `hip_turn` | `hip_turn` | `value > 62` | 正面 | P6 | ✅ 完整 |
| **RISK-002** | 髋部过早转动风险 | 🟡 medium | ② takeaway | `hip_turn` | `hip_turn` | `value > 20` | 正面 | P6 ⚠️ | ✅ 完整 |
| **RISK-003** | 髋部灵活性不足风险 | 🟡 medium | ④ top | `hip_turn` | `hip_turn` | `value < 40` | 正面 | P6 | ❌ 缺原因/建议/原文 |
| **RISK-004** | 头部晃动风险 | 🔵 low | ② takeaway | `head_drift` | `head_drift_pct` | `value >= 5` | 全部 | ⛔ `-` | ❌ 缺原因/建议/原文 |
| **RISK-005** | X-Factor 过低风险 | 🔴 high | ④ top | `x_factor` | `x_factor` | `value < 18` | 正面 | P6/P11 ⚠️ | ✅ 完整 |
| **RISK-006** | 鸡翅风险(肘部) | 🔴 high | ④ top | `lead_arm_straightness` | `lead_arm_straight` | `value < 145` | 全部 | P8 | ✅ 完整 |
| **RISK-007** | 肩部转动不足风险 | 🔵 low | ③ backswing | `shoulder_turn` | `shoulder_turn` | `value < 50` | 正面 | P11 | ✅ 完整 |
| **RISK-008** | 后臂过直风险 | 🔵 low | ③ backswing | `trail_arm_flexion` | `trail_elbow_flex` | `value > 130` | 全部 | P8 | ❌ 缺原因/建议/原文 |
| **RISK-009** | 挥杆平面过平/过陡风险 | 🟡 medium | ④ top | `swing_plane` | **（全新）** | `value < 50 or value > 70` | **侧面** | ⛔ `-` | ❌ 缺原因/建议/原文 |
| **RISK-010** | X-Factor 过早释放风险 | 🟡 medium | ⑤ downswing | `x_factor_retention` | `x_factor_retention` | `value < 80` | 正面 | P6/P11 ⚠️ | ✅ 完整 |
| **RISK-011** | 膝部过屈/过直风险 | 🔵 low | ① address | `knee_flexion` | `knee_flex` | `value < 156 or value > 174` | 全部 | P10 | ✅ 完整 |
| **RISK-012** | 站姿过宽/过窄风险 | 🔵 low | ① address | `stance_width_ratio` | `stance_width_ratio` | `value < 0.9 or value > 1.4` | 正面 | ⛔ `-` | ❌ 缺原因/建议/原文 |
| **RISK-013** | 髋部开放不足风险 | 🟡 medium | ⑥ impact | `hip_open_angle` | `hip_open` | `value < 12` | 正面 | P8 | ❌ 缺原因/建议/原文 |
| **RISK-014** | 过早起身(Early Extension)风险 | 🔴 high | ⑥ impact | `spine_tilt_change` | `spine_tilt_delta` | `value >= 10` | **侧面** | P11 | ✅ 完整 |
| **RISK-015** | 重心转移不足风险 | 🟡 medium | ⑥ impact | `pelvis_shift` | `pelvis_shift_pct` | `value < 8` | 正面 | P10 | ❌ 缺原因/建议/原文 |
| **RISK-016** | 释放不完整风险 | 🔵 low | ⑦ follow_through | `shoulder_turn` | `shoulder_open` ⚠️ | `value < 30` | 正面 | P8 | ⚠️ 缺手册原文 |
| **RISK-017** | 收杆不稳定风险 | 🔵 low | ⑧ finish | `balance_hold` | `balance_hold_sec` | `value < 0.6` | 全部 | P10 | ⚠️ 缺手册原文 |

**统计**：17 条 = high 4 条 / medium 6 条 / low 7 条；按阶段 = ①2 ②2 ③2 ④5 ⑤1 ⑥3 ⑦1 ⑧1。

> 🚨 **两个必须警示工程师的坑**
>
> **坑 1 — RISK-016 的符号陷阱。** PDD 在 ⑦送杆 把指标 key 写作 `shoulder_turn`（参考 35°~60°），语义是**开放角（正值）**；而现有实现中 `shoulder_turn` 在 ⑦ 是**带符号的转动角（此时为负值）**，⑦ 的开放角对应现有 key 是 `shoulder_open = -shoulder_turn`。若直接把 RISK-016 接到现有 `m_shoulder_turn`，条件 `< 30` 会在**每一次挥杆上恒真**，产生 100% 误报。**必须接 `shoulder_open`。**
>
> **坑 2 — RISK-014 与 `spine_tilt_change` 的符号矛盾。** PDD §4.1 把 `spine_tilt_change` 定义为「击球时前倾角 − Address时前倾角」；但"起身"意味着前倾角**减小**，按此公式结果为**负值**，与 §5.2.6 的阈值 `≥ 10°`（正值）自相矛盾。现有实现 `m_spine_tilt_delta = max(0, addr_tilt − impact_tilt)` 语义正确。**建议以现有实现为准，PDD 公式视为笔误**，但需用户确认（§8 待澄清 #A2）。

### 3.3 逐条详述（含全部原始文案）

> 以下文案**逐字抄录自 PDD v2.0**。标注「⛔ 文档未提供」的字段，PDD 中确实没有对应内容，需用户补齐后才能满足 AC-18。

---

#### RISK-001 髋部转动过度风险

| 字段 | 值 |
|---|---|
| `risk_level` | `high` |
| `trigger_phase` | `top`（④ 顶点） |
| `trigger_condition` | `{"metric_key": "hip_turn", "operator": ">", "threshold": 62}` |
| 参考范围（该阶段） | 45°~60° |
| `manual_page` | 6 |
| 机位门控 | 正面（face_on） |

**`trigger_description`（原文）**
> "你的顶点阶段髋部转动角为 {value}°，高于参考范围(45°~60°)。髋部转动过度会削弱肩髋分离（X-Factor），降低蓄力效果，同时增加腰部及髋部的损伤风险。"

**`suggestions`（原文，3 条）**
1. 技术动作调整：顶点时感受"上半身扭转而下半身稳定"的分离感，限制髋部过度转动。
2. 专项体能训练：调整骨盆额状面平衡，纠正下交叉综合征体态。
3. 运动姿势改善：准备姿势时增加脚尖打开幅度。

**`manual_excerpt`（原文）**
> "研究显示，6成以上的腰部不适最终可归因于髋部损伤...髋部损伤会诱发腹股沟区域的不适感，造成挥杆动作异常。"

---

#### RISK-002 髋部过早转动风险

| 字段 | 值 |
|---|---|
| `risk_level` | `medium` |
| `trigger_phase` | `takeaway`（② 起杆） |
| `trigger_condition` | `{"metric_key": "hip_turn", "operator": ">", "threshold": 20}` |
| 参考范围（该阶段） | 8°~18° |
| `manual_page` | **⚠️ 冲突**：§5.2.2 表格写 `P6`，§7.3 JSON 示例写 `5` |
| 机位门控 | 正面（face_on） |

**`trigger_description`（原文）**
> "你的起杆阶段髋部转动角为 {value}°，高于参考范围(8°~18°)。起杆阶段髋部应保持相对稳定，过早转动可能增加腰部代偿压力。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：起杆应由肩部带动，而非髋部主动旋转。
2. 专项体能训练：加强核心稳定性训练，提升髋关节控制力。

**`manual_excerpt`（原文，⚠️ 两处版本不一致）**
- §5.2.2 完整版：> "胸椎与髋部的灵活性及稳定性受限，是导致背痛重要的功能性因素。此外高尔夫异常挥杆动作也与腰部不适密切相关。"
- §7.3 JSON 示例截断版：> "胸椎与髋部的灵活性及稳定性受限，是导致背痛重要的功能性因素。"
- **建议采用 §5.2.2 完整版**（§8 待澄清 #B3）。

---

#### RISK-003 髋部灵活性不足风险

| 字段 | 值 |
|---|---|
| `risk_level` | `medium` |
| `trigger_phase` | `top`（④ 顶点） |
| `trigger_condition` | `{"metric_key": "hip_turn", "operator": "<", "threshold": 40}` |
| 参考范围（该阶段） | 45°~60° |
| `manual_page` | 6 |
| 机位门控 | 正面（face_on） |
| `trigger_description` | ⛔ **文档未提供** |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供**（页码 P6 与 RISK-001 同页，可推测但不应由研发编造） |

> 备注：RISK-001（`> 62`）与 RISK-003（`< 40`）在同一阶段同一指标上互斥，不会同时触发。

---

#### RISK-004 头部晃动风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `takeaway`（② 起杆） |
| `trigger_condition` | `{"metric_key": "head_drift", "operator": ">=", "threshold": 5}` |
| 单位 | `%` 肩宽 |
| 参考范围（该阶段） | < 4% |
| `manual_page` | ⛔ **文档写 `-`**（与 §5.1 `manual_page: number` 类型冲突，与 AC-18"缺一不可"冲突） |
| 机位门控 | 全部（face_on + down_the_line） |
| `trigger_description` | ⛔ **文档未提供** |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供** |

---

#### RISK-005 X-Factor 过低风险 ⭐

| 字段 | 值 |
|---|---|
| `risk_level` | `high` |
| `trigger_phase` | `top`（④ 顶点） |
| `trigger_condition` | `{"metric_key": "x_factor", "operator": "<", "threshold": 18}` |
| 参考范围（该阶段） | 20°~35° |
| `manual_page` | ⚠️ 文档写 `P6/P11`（**两页**，与 `number` 类型冲突） |
| 机位门控 | 正面（face_on） |

**`trigger_description`（原文）**
> "你的顶点阶段X-Factor为 {value}°，低于参考范围(20°~35°)。X-Factor（肩髋分离度）是挥杆力量的核心来源，数值过低说明'上弦'不紧，力量无法有效蓄积。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：顶点时感受肩部继续转动而髋部保持稳定，建立分离感。
2. 专项体能训练：提升胸椎旋转灵活性，同时加强核心抗旋转能力。

**`manual_excerpt`（原文）**
> "胸椎与髋部的灵活性及稳定性受限，是导致背痛重要的功能性因素。"

---

#### RISK-006 鸡翅风险(肘部)

| 字段 | 值 |
|---|---|
| `risk_level` | `high` |
| `trigger_phase` | `top`（④ 顶点） |
| `trigger_condition` | `{"metric_key": "lead_arm_straightness", "operator": "<", "threshold": 145}` |
| 参考范围（该阶段） | 150°~172° |
| `manual_page` | 8 |
| 机位门控 | 全部（face_on + down_the_line） |

**`trigger_description`（原文）**
> "你的顶点阶段引导臂伸直度为 {value}°，低于参考范围(150°~172°)。左臂过度弯曲（"鸡翅"）会导致挥杆力量泄漏，同时增加肘部和腕部的损伤风险。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：顶点时保持左臂伸展，避免"鸡翅"动作。
2. 专项体能训练：加强肩背肌肉力量与柔韧性。

**`manual_excerpt`（原文）**
> "手腕过度屈曲或伸展的击球状态会分别增加前臂屈肌和前臂伸肌的张力，从而引起高尔夫球肘或网球肘。"

---

#### RISK-007 肩部转动不足风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `backswing`（③ 上杆） |
| `trigger_condition` | `{"metric_key": "shoulder_turn", "operator": "<", "threshold": 50}` |
| 参考范围（该阶段） | 55°~72° |
| `manual_page` | 11 |
| 机位门控 | 正面（face_on） |

**`trigger_description`（原文）**
> "你的上杆阶段肩部转动角为 {value}°，低于参考范围(55°~72°)。肩部转动不足可能导致上杆不充分，影响击球距离。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：增加上杆时肩部的旋转幅度。
2. 专项体能训练：加强胸椎灵活性训练。

**`manual_excerpt`（原文）**
> "在挥杆击球过程中，肩部主要承担力量传递工作。"

---

#### RISK-008 后臂过直风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `backswing`（③ 上杆） |
| `trigger_condition` | `{"metric_key": "trail_arm_flexion", "operator": ">", "threshold": 130}` |
| 参考范围（该阶段） | 95°~125° |
| `manual_page` | 8 |
| 机位门控 | 全部（face_on + down_the_line） |
| `trigger_description` | ⛔ **文档未提供** |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供** |

---

#### RISK-009 挥杆平面过平/过陡风险（**侧面专属**）

| 字段 | 值 |
|---|---|
| `risk_level` | `medium` |
| `trigger_phase` | `top`（④ 顶点） |
| `trigger_condition` | **双区间**：`value < 50 or value > 70` |
| 参考范围（该阶段） | 55°~65°（`<55` 偏平 / `>65` 偏陡） |
| `manual_page` | ⛔ **文档写 `-`** |
| 机位门控 | **仅侧面（down_the_line）** |
| `trigger_description` | ⛔ **文档未提供**（需区分"过平"与"过陡"两套文案） |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供** |

---

#### RISK-010 X-Factor 过早释放风险

| 字段 | 值 |
|---|---|
| `risk_level` | `medium` |
| `trigger_phase` | `downswing`（⑤ 下杆） |
| `trigger_condition` | `{"metric_key": "x_factor_retention", "operator": "<", "threshold": 80}` |
| 单位 | `%` |
| 参考范围（该阶段） | ≥ 85% |
| `manual_page` | ⚠️ 文档写 `P6/P11` |
| 机位门控 | 正面（face_on） |

**`trigger_description`（原文）**
> "你的X-Factor保持率为 {value}%，低于参考范围(≥85%)。下杆初期X-Factor过早释放意味着髋部和肩部同时打开，损失了本应传导至球杆的能量。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：下杆初期保持上半身的扭转，由髋部率先启动带动下杆。
2. 专项体能训练：提升核心力量与协调性。

**`manual_excerpt`（原文）**
> "髋部损伤会诱发腹股沟区域的不适感，造成挥杆动作异常，进而引发其他关节的运动损伤。"

---

#### RISK-011 膝部过屈/过直风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `address`（① 准备） |
| `trigger_condition` | **双区间**：`value < 156 or value > 174` |
| 参考范围（该阶段） | 160°~172° |
| `manual_page` | 10 |
| 机位门控 | 全部（face_on + down_the_line） |

**`trigger_description`（原文，含内嵌三元表达式）**
> "你的膝部弯曲角为 {value}°，参考范围为 160°~172°。{value<156 ? '膝部弯曲过度，可能增加膝关节压力' : '膝部过于伸直，可能导致挥杆时重心不稳'}。"

> 💡 工程实现提示：该模板内嵌了一个 JS 风格三元表达式，**不能直接做字符串格式化**。建议后端渲染时按分支拼装，等价于：
> ```python
> tail = "膝部弯曲过度，可能增加膝关节压力" if value < 156 else "膝部过于伸直，可能导致挥杆时重心不稳"
> text = f"你的膝部弯曲角为 {value}°，参考范围为 160°~172°。{tail}。"
> ```

**`suggestions`（原文，2 条）**
1. 调整准备姿势时膝部微屈，保持弹性。
2. 专项体能训练：加强下肢肌肉力量与柔韧性。

**`manual_excerpt`（原文）**
> "过度屈膝或下蹲也会增加膝关节的压力，增加膝关节损伤风险。"

---

#### RISK-012 站姿过宽/过窄风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `address`（① 准备） |
| `trigger_condition` | **双区间**：`value < 0.9 or value > 1.4` |
| 单位 | 无量纲 |
| 参考范围（该阶段） | 1.0~1.3 |
| `manual_page` | ⛔ **文档写 `-`** |
| 机位门控 | 正面（face_on） |
| `trigger_description` | ⛔ **文档未提供**（需区分"过窄"与"过宽"两套文案） |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供** |

---

#### RISK-013 髋部开放不足风险

| 字段 | 值 |
|---|---|
| `risk_level` | `medium` |
| `trigger_phase` | `impact`（⑥ 击球） |
| `trigger_condition` | `{"metric_key": "hip_open_angle", "operator": "<", "threshold": 12}` |
| 参考范围（该阶段） | 15°~30° |
| `manual_page` | 8 |
| 机位门控 | 正面（face_on） |
| `trigger_description` | ⛔ **文档未提供** |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供** |

---

#### RISK-014 过早起身(Early Extension)风险 ⭐（**侧面专属**）

| 字段 | 值 |
|---|---|
| `risk_level` | `high` |
| `trigger_phase` | `impact`（⑥ 击球） |
| `trigger_condition` | `{"metric_key": "spine_tilt_change", "operator": ">=", "threshold": 10}` |
| 参考范围（该阶段） | < +8° |
| `manual_page` | 11 |
| 机位门控 | **仅侧面（down_the_line）** |

**`trigger_description`（原文）**
> "你的脊柱前倾变化量为 {value}°，远高于参考范围(<8°)。这表明你在击球时'起身'(Early Extension)明显，是打薄、打厚、剃头球的主要原因之一。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：击球时保持脊柱角度，避免身体向上直起。
2. 专项体能训练：加强核心力量与髋部灵活性，减少身体代偿。

**`manual_excerpt`（原文）**
> "脊柱过度侧屈引起的胸腔压缩或腹外斜肌快速发力均可能导致肋部骨折。"

> ⚠️ **产品侧提醒**：该原文（肋部骨折 / 脊柱侧屈）与风险名称（起身 / Early Extension）**语义关联度较弱**，疑似 PDD 引错原文。建议向用户确认（§8 待澄清 #B4）。
> ⚠️ **能力回退提醒**：PDD 把该指标限定为侧面专属，意味着**正面机位用户拿不到本产品最高价值的 high 级风险之一**（现有实现在正面机位是能算出 `spine_tilt_delta` 的）。见 §8 待澄清 #A3。

---

#### RISK-015 重心转移不足风险

| 字段 | 值 |
|---|---|
| `risk_level` | `medium` |
| `trigger_phase` | `impact`（⑥ 击球） |
| `trigger_condition` | `{"metric_key": "pelvis_shift", "operator": "<", "threshold": 8}` |
| 单位 | `%` 肩宽 |
| 参考范围（该阶段） | +10%~+20% |
| `manual_page` | 10 |
| 机位门控 | 正面（face_on） |
| `trigger_description` | ⛔ **文档未提供** |
| `suggestions` | ⛔ **文档未提供** |
| `manual_excerpt` | ⛔ **文档未提供** |

---

#### RISK-016 释放不完整风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `follow_through`（⑦ 送杆） |
| `trigger_condition` | `{"metric_key": "shoulder_turn", "operator": "<", "threshold": 30}` |
| 参考范围（该阶段） | 35°~60° |
| `manual_page` | 8（表格有页码，但正文未给原文） |
| 机位门控 | 正面（face_on） |
| ⚠️ 现有 key 映射 | **`shoulder_open`（= −shoulder_turn），不是 `shoulder_turn`** —— 见 §3.2 坑 1 |

**`trigger_description`（原文）**
> "你的送杆阶段肩部转动角为 {value}°，低于参考范围(35°~60°)。肩部释放不完整可能导致能量泄漏，影响击球质量。"

**`suggestions`（原文，仅 1 条）**
1. 技术动作调整：送杆时充分释放肩部，跟随挥杆完成完整动作。

**`manual_excerpt`** ⛔ **文档未提供**（与 AC-18 冲突）

---

#### RISK-017 收杆不稳定风险

| 字段 | 值 |
|---|---|
| `risk_level` | `low` |
| `trigger_phase` | `finish`（⑧ 收杆） |
| `trigger_condition` | `{"metric_key": "balance_hold", "operator": "<", "threshold": 0.6}` |
| 单位 | 秒 |
| 参考范围（该阶段） | ≥ 0.8s |
| `manual_page` | 10（表格有页码，但正文未给原文） |
| 机位门控 | 全部（face_on + down_the_line） |

**`trigger_description`（原文）**
> "你的收杆平衡保持时间为 {value}s，低于参考范围(≥0.8s)。收杆不稳说明挥杆过程中重心转移存在缺陷。"

**`suggestions`（原文，2 条）**
1. 技术动作调整：收杆时保持重心完全转移至前脚，维持3秒平衡。
2. 专项体能训练：加强下肢力量与平衡能力。

**`manual_excerpt`** ⛔ **文档未提供**（与 AC-18 冲突）

---

### 3.4 风险匹配逻辑（PDD §5.3 原文 + 机位门控）

```
对每个阶段 phase：
  1. 获取该阶段已计算出的所有指标值 metrics[phase]
  2. 遍历风险规则库，筛选 trigger_phase == phase 的规则
  3. 【机位门控，PDD §5.4】先检查该规则所需指标在当前 camera_view 下是否可测
     - 不可测 → 该规则不参与匹配（跳过，不计入触发也不报错）
  4. 检查对应指标值是否满足 trigger_condition（operator ∈ {>, <, >=, <=, ==}）
  5. 满足 → 加入该阶段的"触发风险列表"
  6. 按 risk_level 排序：high > medium > low
  7. 该阶段无任何风险触发 → 返回空数组 []，前端显示「✅ 本阶段动作良好，无高风险项」
```

**机位可用规则数推算**（由 §3.2 门控列 + §4 指标机位归属推导）：

| 机位 | 可参与匹配的规则 | 数量 |
|---|---|---|
| 正面 face_on | RISK-001~008, 010~013, 015~017（除 009 / 014） | **15 / 17** |
| 侧面 down_the_line | RISK-004, 006, 008, 009, 011, 014, 017 | **7 / 17** |

> 📌 侧面机位只能触发 7 条规则，其中 high 级只有 RISK-006、RISK-014 两条。产品上需接受"侧面机位诊断覆盖面天然较窄"这一事实，或按 §8 #A3 调整门控。

**性能约束**：PDD AC-P5 —— 风险匹配耗时 ≤ 50ms（在指标计算完成后）。17 条规则纯 `if` 判断，无压力。

### 3.5 结果页风险区展示规范（PDD §3.4.3）

```
┌─────────────────────────────────────────────────────┐
│ [🔴高风险 / 🟡中风险 / 🟢低风险] 风险名称            │
│                                                     │
│ 触发原因：你的[指标名称](实测值)高于/低于参考范围     │
│           (参考值 X~Y)，这可能导致...                │
│                                                     │
│ 改进建议：                                           │
│   ① [建议1 - 来自手册原文]                           │
│   ② [建议2 - 来自手册原文]                           │
│   ③ [建议3 - 来自手册原文]                           │
│                                                     │
│ 📖 查看手册原文  → 点击弹出半屏弹窗                   │
└─────────────────────────────────────────────────────┘
```

| 要求 | 说明 |
|---|---|
| 排序 | 按 `risk_level` 高→中→低 |
| 空状态 | `✅ 本阶段动作良好，无高风险项`（AC-19） |
| 手册原文 | 点击「📖 查看手册原文」弹出**半屏弹窗**，展示 `manual_excerpt` + `manual_page` |
| 切换联动 | 点击缩略图时，大图 / 指标卡 / 风险区**三者同步刷新**（AC-14） |
| ⚠️ 配色矛盾 | §3.4.3 画的低风险图标是 **🟢 绿色**，§6.5 枚举表写的是 **🔵 蓝色 #3B82F6**。见 §8 #B5 |
| ⛔ 未定义 | 单阶段风险条数上限、是否折叠、是否有全局风险汇总区，PDD 均未定义 |

---

## 4. 侧面机位（down-the-line）专属指标定义

### 4.1 三个侧面专属指标（PDD §4.1 / §4.2 / §4.3 原文）

| 指标 key | 名称 | 计算口径（PDD 原文） | 单位 | 所属阶段 | 参考范围 | 状态阈值（PDD 原文） | 卡片解释行 `description` |
|---|---|---|---|---|---|---|---|
| **`swing_plane`** | 挥杆平面角 | **顶点时引导臂与水平面的夹角** | ° | ④ top | **55°~65°** | `<55` 偏平 / `55–65` 正常 / `>65` 偏陡 | "顶点时手臂与水平面的夹角" |
| **`spine_tilt_change`** | 脊柱前倾变化量 | **击球时前倾角 − Address时前倾角** | ° | ⑥ impact | **< +8°** | `<8` 正常 / `≥8` 偏高（起身） | "击球时相比准备时站直了多少，数值越接近0越好" |
| **`spine_tilt_fwd`** | 脊柱前倾角 | **双肩中点→双髋中点向量与铅垂线在侧面投影的夹角** | ° | ① address | **30°~40°** | `<30` 偏低 / `30–40` 正常 / `>40` 偏高 | "脊柱相对垂直线的向前倾斜角度" |

### 4.2 另外两个侧面指标（PDD §4.1 出现，但归属不完整）

| 指标 key | 名称 | 计算口径（PDD 原文） | 单位 | 所属阶段 | 参考范围 | 状态阈值 | 状态 |
|---|---|---|---|---|---|---|---|
| **`spine_side_bend`** | 脊柱侧弯角 | **顶点时脊柱在侧面投影的侧弯幅度** | ° | ④ top | 10°~20° | `<10` 偏小 / `10–20` 正常 / `>20` 偏大 | ⚠️ 口径过于笼统，**不足以直接编码**（"侧面投影的侧弯"是投影到图像平面还是 world y-z 面？绕哪个轴？）见 §8 #A4 |
| **`lead_hand_position`** | 手部领先量 | **击球时手腕相对于球杆的位置关系（定性）** | `-` | ⑥ impact（推测） | "杆身与手臂呈直线" | ⛔ 无 | ⛔ **仅出现在 §4.1 基础表，未出现在 §4.3 任何阶段明细表**；且为**定性指标**，无数值、无 ref_min/ref_max，无法套用现有 `MetricSpec` 结构；且需要真实杆身。见 §8 #A5 |

### 4.3 双机位下的完整指标分配（由 PDD §4.3 各阶段表按"适用机位"列拆解）

| 阶段 | 正面 face_on 指标（数量） | 侧面 down_the_line 指标（数量） |
|---|---|---|
| ① address | `spine_tilt_side` / `stance_width_ratio` / `knee_flexion` （**3**） | `spine_tilt_fwd` / `knee_flexion` （**2**） |
| ② takeaway | `shoulder_turn` / `hip_turn` / `head_drift` / `lead_arm_straightness` （**4**） | `head_drift` / `lead_arm_straightness` （**2**） |
| ③ backswing | `shoulder_turn` / `hip_turn` / `trail_arm_flexion` / `lead_arm_straightness` （**4**） | `trail_arm_flexion` / `lead_arm_straightness` （**2**） |
| ④ top | `shoulder_turn` / `hip_turn` / `x_factor` / `lead_arm_straightness` （**4**） | `lead_arm_straightness` / `swing_plane` / `spine_side_bend` （**3**） |
| ⑤ downswing | `hip_turn` / `shoulder_turn` / `x_factor_retention` / `pelvis_shift` （**4**） | 🔴 **0** |
| ⑥ impact | `hip_open_angle` / `shoulder_squareness` / `pelvis_shift` （**3**） | 🔴 **1**（仅 `spine_tilt_change`） |
| ⑦ follow_through | `hip_open_angle` / `shoulder_turn` / `trail_arm_flexion` / `spine_tilt_side` （**4**） | 🔴 **1**（仅 `trail_arm_flexion`） |
| ⑧ finish | `hip_toward_target` / `shoulder_total_open` / `pelvis_shift` / `balance_hold` （**4**） | 🔴 **1**（仅 `balance_hold`） |

> 🚨 **阻塞级发现**：侧面机位下 ⑤⑥⑦⑧ 四个阶段的指标数分别为 **0 / 1 / 1 / 1**，**直接违反 PDD 自己的 AC-08「每阶段有截图与 ≥2 个指标」**。这是 PDD v2.0 内部最严重的自相矛盾，必须在开工前拍板。见 §8 #A1。

### 4.4 侧面机位的物理前提（来自 `club-detection-design.md` §4.3，PDD 未覆盖，但必须落到拍摄指引）

| # | 前提 | 对产品的要求 |
|---|---|---|
| 1 | 侧面机位下双踝前后重叠，**不能用踝连线定地平线** → 只能用图像水平线 | 拍摄指引必须强制「**手机保持水平、不倾斜、不俯拍**」；建议结果图上画一条淡色水平参考线供用户自查 |
| 2 | 假设相机光轴与目标线夹角 < 15°，每偏离 10–15° 角度误差约 3–8° | 拍摄指引「镜头垂直于目标线」需强调，且免责声明需说明是**投影角估算，非真实空间角** |
| 3 | 侧面机位下图像肩宽被严重压缩，**不能用作归一化标尺** | `head_drift` / `pelvis_shift` 等以"%肩宽"为单位的指标在侧面机位口径失真——PDD 仍把 `head_drift` 标为"正面/侧面"，需架构师给出 DTL 标尺方案（建议改用图像身高）。见 §8 #A6 |

---

## 5. 指标状态 5 值体系

### 5.1 完整枚举（PDD §6.4 原文）

| 状态值 | 触发条件 | 显示文案 | 配色 | 相对 MVP |
|---|---|---|---|---|
| `low` | 实测值 < `ref_min` | 偏低 | 橙色 `#FF8C00` | 已有 |
| `normal` | `ref_min` ≤ 实测值 ≤ `ref_max` | 正常 | 绿色 `#22C55E` | 已有 |
| `high` | 实测值 > `ref_max` | 偏高 | 橙色 `#FF8C00` | 已有 |
| **`critical_low`** | 实测值 < `ref_min × 0.7` | **严重偏低** | **红色 `#EF4444`** | 🆕 新增 |
| **`critical_high`** | 实测值 > `ref_max × 1.3` | **严重偏高** | **红色 `#EF4444`** | 🆕 新增 |

**PDD 附加说明（原文）**：
> "critical_low 和 critical_high 仅对部分关键指标启用（如 X-Factor、脊柱前倾变化量），作为重度偏离的强化提示。"

### 5.2 判定优先级（本 PRD 补充，PDD 未明写）

必须**先判 critical，再判普通**，否则 `critical_low` 永远走不到：

```python
def judge5(value, ref_min, ref_max, critical_enabled: bool) -> MetricStatus:
    if critical_enabled and value < ref_min * 0.7:  return CRITICAL_LOW
    if critical_enabled and value > ref_max * 1.3:  return CRITICAL_HIGH
    if value < ref_min:  return LOW
    if value > ref_max:  return HIGH
    return NORMAL
```

### 5.3 该规则的三个数学缺陷（必须拍板才能实现）

| # | 缺陷 | 受影响指标 | 现象 |
|---|---|---|---|
| 1 | **`ref_min ≤ 0` 时乘法规则失效** | `head_drift`（ref_min=0）、`spine_tilt_change`（ref_min=0）、`max_head_drift`（0） | `0 × 0.7 = 0`，`critical_low` 永远不可能触发（值不会为负） |
| 2 | **`ref_min < 0` 时乘法规则方向反转** | `shoulder_squareness`（ref_min = −5） | `−5 × 0.7 = −3.5 > −5`。按规则 `value = −4` 会被判 `critical_low`，但 −4 明明**落在正常区间内** —— 直接产生错误红标 |
| 3 | **未给出 `critical_enabled` 白名单** | 全部 | PDD 只举例"如 X-Factor、脊柱前倾变化量"，没给完整清单。研发无法确定其余 20+ 指标是否启用 |

> **PM 倾向性建议**（供架构师参考，非结论）：改用**区间宽度倍数**判定，与 `ARCHITECTURE.md` §8.4 现有前端逻辑同源、且对负值/零值天然安全：
> `critical` ⟺ `|value − 最近边界| / (ref_max − ref_min) > 1.0`
> 若用户坚持乘法规则，则至少要为 `ref_min ≤ 0` 的指标单独定义绝对阈值。

### 5.4 与 MVP 的行为变更

| 项 | MVP v1.0 | v2.0 |
|---|---|---|
| 后端下发状态 | 3 态（`low` / `normal` / `high`） | **5 态** |
| "严重偏离"判定方 | **前端**自行判定（`ARCHITECTURE.md` §8.4） | **后端**判定并下发 |
| 前端配色 | 偏低橙 / 正常绿 / 偏高橙 / 严重偏离红 | 同左，但改为读后端 `status` 直接映射 |

---

## 6. 其他 v2.0 增量点

### 6.1 双机位选择流程（M001 / M002 / M008）

| 项 | 定义 |
|---|---|
| 交互 | 首页两个**卡片式互斥按钮**：「正面机位 (Face-on)」「侧面机位 (Down-the-line)」 |
| 副文案 | 正面："正对身体拍摄，竖持手机"；侧面："垂直于目标线拍摄，横持手机" |
| 联动 | 切换按钮 → 下方拍摄要求图文**同步切换** |
| 通用拍摄要求 | ①全身入镜（头顶到球杆底部不出画）②距离 2~3m ③只拍一次完整挥杆 ④时长 2~15s ⑤建议 60fps ⑥光线充足、避免逆光 |
| 正面专属要求 | ①**手机竖持**，镜头正对身体 ②球手正面面对镜头 ③双脚在画面左右居中 |
| 侧面专属要求 | ①**手机横持**，镜头垂直于目标线 ②球手侧面面对镜头（**右肩侧朝向镜头**）③球杆与目标线在画面中清晰可见 |
| 传参 | 上传接口新增**必填**参数 `camera_view: "face_on" \| "down_the_line"` |
| ⚠️ 与现状差异 | PDD **不提供 `auto` 自动判定**（用户手动二选一）；而现有 `CameraView` 枚举含 `AUTO`，`club-detection-design.md` §4.6 设计了自动判定（画幅先验 + 肩宽压缩比）。建议：`AUTO` 保留为**内部校验**用途——若用户选正面但自动判定为侧面，在 `warnings` 里提示，不阻断。见 §8 #B6 |
| 结果页 | 导航栏新增**机位标签**（"正面机位"/"侧面机位"）+ 分析日期 |
| 验收 | AC-01 机位切换、AC-07 后端正确接收 `camera_view`、AC-09 正面不出现侧面指标、AC-10 侧面出现侧面指标 |

### 6.2 指标卡术语解释行（`description` 字段，M024 / AC-16）

- `StageMetric` **新增 `description: str` 字段**，随指标一起下发，前端在实测值下方常驻展示一行。
- PDD §4.2 给出了 **20 条**文案（全文见下），但 §4.1 基础表共列出 **23** 个指标 key，§4.4 另有 1 个全程指标 key（`max_head_drift`）。
- ⛔ **缺失 4 条**（逐条比对结果）：`hip_toward_target`、`shoulder_total_open`、`lead_hand_position`、`max_head_drift`。与 AC-16「每个指标卡片下方均有解释文案」冲突。见 §8 #B7。

<details>
<summary>PDD §4.2 术语解释文案全文（20 条，逐字抄录）</summary>

| 指标Key | 卡片下方解释文案 |
|---|---|
| `shoulder_turn` | "肩部相对准备位置向后转动的角度" |
| `hip_turn` | "髋部相对准备位置向后转动的角度" |
| `x_factor` | "肩部与髋部转动角度之差，数值越大'上弦'越紧，力量越足" |
| `x_factor_retention` | "下杆初期X-Factor的保留比例，保留越多越能蓄力释放" |
| `lead_arm_straightness` | "左臂伸直程度，180°为完全伸直，越接近越好" |
| `trail_arm_flexion` | "右肘弯曲角度，正常折叠利于力量传导" |
| `spine_tilt_side` | "脊柱向远离目标方向的侧倾角度" |
| `spine_tilt_fwd` | "脊柱相对垂直线的向前倾斜角度" |
| `spine_tilt_change` | "击球时相比准备时站直了多少，数值越接近0越好" |
| `stance_width_ratio` | "双脚间距与肩宽的比值" |
| `knee_flexion` | "膝盖弯曲角度，180°为完全伸直" |
| `hip_open_angle` | "击球时髋部朝向目标的开放角度" |
| `shoulder_squareness` | "击球时肩部朝向目标的方正程度" |
| `pelvis_shift` | "重心向目标方向移动的距离（以肩宽百分比表示）" |
| `head_drift` | "头部相对起始位置的晃动幅度（以肩宽百分比表示）" |
| `swing_plane` | "顶点时手臂与水平面的夹角" |
| `spine_side_bend` | "顶点时脊柱向目标方向侧弯的幅度" |
| `balance_hold` | "收杆后站稳的时间，越长代表平衡性越好" |
| `tempo_ratio` | "上杆时间与下杆时间的比值，接近3:1为理想节奏" |
| `swing_duration` | "从准备到收杆的总时长" |

</details>

> 📌 注意：§7.3 的 JSON 示例中，`spine_tilt_side` 的 `description` 被写成 `"脊柱相对垂直线的侧向倾斜角度"`，与 §4.2 表格的 `"脊柱向远离目标方向的侧倾角度"` **不一致**。建议以 §4.2 表格为准。

### 6.3 全程指标常驻条（M026）

无实质变更，仍为 3 项，底部常驻、不随阶段切换：

| key（PDD） | 现有 key | 名称 | 参考 |
|---|---|---|---|
| `tempo_ratio` | `tempo_ratio` | 节奏比 | `3.0 : 1`（PDD 只给点值，⛔ **未给区间**，建议沿用现有 2.5~3.5） |
| `swing_duration` | `swing_duration` | 挥杆总时长 | 1.0s~1.6s（一致） |
| `max_head_drift` | `max_head_drift_pct` | 头部最大位移 | < 8%（一致，key 需改名） |

### 6.4 手册原文弹窗

- 每条风险卡底部固定「📖 查看手册原文」入口，点击弹出**半屏弹窗**。
- 弹窗内容 = `manual_excerpt`（原文摘录）+ `manual_page`（页码）。
- ⛔ **未定义**：弹窗是否需要展示手册书名/版本、是否需要"跳转完整手册"入口、`manual_page` 为 `-` 时如何展示。

### 6.5 §7 接口对齐要求（PDD 原文 vs 现有实现）

| 项 | PDD v2.0 | 现有实现 | 结论 |
|---|---|---|---|
| 创建任务 | `POST /api/v1/task/create`（form-data：`video` + `camera_view`） | `POST /api/v1/tasks`（form-data：`file`） | **路径 + 字段名均不同** |
| 查询状态 | `GET /api/v1/task/status/{task_id}` | `GET /api/v1/tasks/{task_id}` | 路径不同 |
| 获取结果 | `GET /api/v1/task/result/{task_id}` | `GET /api/v1/tasks/{task_id}/result` | 路径不同 |
| 成功响应包 | `{code:0, message:"success", data:{...}}` | `{code:0, message:"ok", data:{...}}` | `message` 文案不同（无实质影响） |
| 上传错误码 | `10001` 文件过大 / `10002` 格式不支持 / `10003` 时长超范围 / `10004` 服务繁忙 | 统一 `4001`（+ 中文 message） | **体系冲突** |
| 结果错误码 | `20001` 任务不存在或已过期 | `4004` 任务不存在 / `4009` 任务尚未完成 | **体系冲突**；PDD **没有** "任务未完成" 这一码 |
| 分析业务错误 | `code:0` + `data.status="failed"` + `data.error_code="NO_PERSON"` | 一致（`TaskStatusView.error_code`） | ✅ 对齐 |
| `step` 字段 | **字符串**（`"识别8个挥杆阶段"`） | **整数**（`step: int`）+ 独立 `message` | 类型冲突 |
| `video_meta` | `total_frames` | `frame_count` | 字段名不同 |
| 结果顶层 | `camera_view` 在 `data` 顶层 | 在 `video_meta.camera_view` | 位置不同 |
| `PhaseResult` | 新增 `risks: []` 数组 | 无 | 🆕 新增 |
| `StageMetric` | 新增 `description` | 无（已有 `estimated`/`source`/`confidence`） | 🆕 新增 |
| 视频格式 | mp4 **/ mov** | 仅 `.mp4` | 需放开 mov |

---

## 7. 与现状的差异对照表

### 7.1 指标 key 映射（PDD v2.0 ↔ 现有 `reference.py`）

| # | PDD key | 现有 key | 结论 | 说明 |
|---|---|---|---|---|
| 1 | `shoulder_turn` | `shoulder_turn` | ✅ **可直接复用** | 但 ⑦ 送杆下 PDD 用它表示开放角，实际应映射到 `shoulder_open`（见 §3.2 坑 1） |
| 2 | `hip_turn` | `hip_turn` | ✅ **可直接复用** | — |
| 3 | `x_factor` | `x_factor` | ✅ **可直接复用** | — |
| 4 | `x_factor_retention` | `x_factor_retention` | ✅ **可直接复用** | PDD ⛔ 未给上界；现有 `ref_max=130`，建议保留 |
| 5 | `stance_width_ratio` | `stance_width_ratio` | ✅ **可直接复用** | — |
| 6 | `tempo_ratio` | `tempo_ratio` | ✅ **可直接复用** | — |
| 7 | `swing_duration` | `swing_duration` | ✅ **可直接复用** | — |
| 8 | `lead_arm_straightness` | `lead_arm_straight` | 🔧 **改造：仅改名** | 计算口径完全一致（11-13-15 三点夹角） |
| 9 | `knee_flexion` | `knee_flex` | 🔧 **改造：仅改名** | 一致 |
| 10 | `hip_open_angle` | `hip_open` | 🔧 **改造：仅改名** | 一致（`= −hip_turn`） |
| 11 | `shoulder_squareness` | `shoulder_square` | 🔧 **改造：仅改名** | 一致 |
| 12 | `hip_toward_target` | `hip_to_target` | 🔧 **改造：仅改名** | 一致 |
| 13 | `pelvis_shift` | `pelvis_shift_pct` | 🔧 **改造：仅改名** | 一致 |
| 14 | `head_drift` | `head_drift_pct` | 🔧 **改造：仅改名** | 一致；但 DTL 标尺失真（§4.4 #3） |
| 15 | `balance_hold` | `balance_hold_sec` | 🔧 **改造：仅改名** | 一致 |
| 16 | `max_head_drift` | `max_head_drift_pct` | 🔧 **改造：仅改名** | 一致 |
| 17 | `trail_arm_flexion` | `trail_elbow_flex` + `trail_arm_extend` | 🔧 **改造：改名 + 合并** | 现有两个 key 计算口径本就相同（`m_trail_arm_extend` 直接调 `m_trail_elbow_flex`），PDD 合并为一个 key、按阶段给不同 `name`/`ref` —— 与现有 `MetricSpec` 结构完全兼容 |
| 18 | `shoulder_total_open` | `shoulder_open`（⑧） | 🔧 **改造：拆分改名** | 现有 ⑦⑧ 都叫 `shoulder_open`；PDD ⑦ 叫 `shoulder_turn`、⑧ 叫 `shoulder_total_open`。**⑦⑧ 需拆成两个不同 key** |
| 19 | `spine_tilt_fwd` | `spine_forward_tilt` | 🔧 **改造：改名 + 收窄机位** | 参考范围一致（30~40）；但 PDD 限定为**侧面专属**，现有为全机位。且 DTL 下投影平面口径需重新确认（§8 #A7） |
| 20 | `spine_tilt_change` | `spine_tilt_delta` | 🔧 **改造：改名 + 收窄机位 + 符号确认** | **Q2 答案：是同一个量**。差异：①改名 ②PDD 限定侧面（现有全机位）③PDD 公式符号写反（§3.2 坑 2） |
| 21 | `spine_tilt_side` | ①`shoulder_line_tilt` / ⑦`spine_lateral_tilt` | 🔧 **改造：语义变更（不是纯改名）** | **⚠️ 重点**：现有 ① Address 用的是**肩线水平倾角**（`line_tilt(左肩,右肩)`），PDD 要求改成**脊柱侧倾角**（`tilt_from_vertical_xy(spine_vec)`，即现有 `m_spine_lateral_tilt`）—— **两者是不同的几何量，只是碰巧共用 5~12 参考范围**。需确认 5~12 用在脊柱侧倾上是否仍成立（§8 #B8） |
| 22 | `swing_plane` | **无** | 🆕 **全新** | 见 §8 #A8（与 `club-detection-design.md` 定义严重冲突） |
| 23 | `spine_side_bend` | **无** | 🆕 **全新** | 口径笼统，不可直接编码（§8 #A4） |
| 24 | `lead_hand_position` | **无** | 🆕 **全新** | 定性指标，无阶段归属、无数值（§8 #A5） |

**汇总**：可直接复用 **7** 项 / 需改造 **14** 项（其中 11 项仅改名、3 项涉及语义或机位变更）/ 全新 **3** 项。

### 7.2 参考范围差异

逐条比对 PDD §4.3 各阶段表与 `reference.py` 的 32 条阶段 MetricSpec：**所有沿用指标的 `ref_min` / `ref_max` 数值完全一致，无一处冲突**。差异仅来自 key 命名、机位归属、以及 ①/⑦/⑧ 三处的指标替换。

| 阶段 | 现有 4 项 | PDD 变化 |
|---|---|---|
| ① address | `spine_forward_tilt` / `stance_width_ratio` / `shoulder_line_tilt` / `knee_flex` | `shoulder_line_tilt` **→ 换成** `spine_tilt_side`（几何量变了）；`spine_forward_tilt` **→ 收窄为侧面专属** |
| ② takeaway | 4 项 | 仅改名 |
| ③ backswing | 4 项 | 仅改名 |
| ④ top | 4 项 | **+2 项侧面专属**（`swing_plane`、`spine_side_bend`） |
| ⑤ downswing | 4 项 | 仅改名；侧面下变 0 项 🔴 |
| ⑥ impact | 4 项 | `spine_tilt_delta` **→ 收窄为侧面专属**；正面剩 3 项 |
| ⑦ follow_through | 4 项 | `spine_lateral_tilt` → `spine_tilt_side`（同一几何量，✅）；`shoulder_open` → `shoulder_turn`（⚠️ 符号坑） |
| ⑧ finish | 4 项 | `shoulder_open` → `shoulder_total_open`（改名） |

### 7.3 结构性改动清单（供架构师做任务分解）

| 层 | 文件 | 改动 |
|---|---|---|
| 契约 | `schemas.py` | `MetricStatus` +2 值（`CRITICAL_LOW`/`CRITICAL_HIGH`）；`StageMetric` +`description`；`PhaseResult` +`risks: List[RiskItem]`；新增 `RiskLevel` 枚举 + `RiskItem` 模型；`AnalysisResult` 顶层 +`camera_view`（若对齐 PDD） |
| 数据 | `reference.py` | 指标 key 批量改名（14 项）；`views` 字段按 §4.3 落值；新增 `swing_plane` / `spine_side_bend` 两条 spec；`judge()` → `judge5()`；新增 `CRITICAL_ENABLED` 白名单；新增 `DESCRIPTIONS` 文案表 |
| 数据 | **新增** `risk_rules.py` | 17 条 `RiskRule` 静态定义（本文 §3.2 + §3.3 即为数据源） |
| 计算 | `metrics.py` | `METRIC_FUNCS` key 同步改名；新增 `m_swing_plane`、`m_spine_side_bend`；`m_spine_tilt_delta` 符号/裁剪口径确认 |
| 计算 | **新增** `risk_engine.py` | 匹配逻辑（§3.4）+ 机位门控 + 文案渲染（含 RISK-011 三元分支） |
| 接口 | `main.py` | `POST /tasks` 新增 `camera_view` 必填参；放开 `.mov`；错误码体系对齐决策 |
| 流水线 | `pipeline.py` | 指标计算后串入风险匹配；步骤④文案改「计算姿态指标与风险」 |
| 渲染 | `renderer.py` | DTL 机位画水平参考线（来自 `club-detection-design.md` §4.3） |
| 小程序 | `pages/index` | 机位选择卡片 + 动态拍摄要求；上传带 `camera_view` |
| 小程序 | `pages/result` | 风险建议区（区域4）+ 手册原文半屏弹窗 + 指标卡 `description` 行 + 5 值状态配色 + 机位标签 + [查看完整报告] 占位 |
| 文档 | `ARCHITECTURE.md` | §8.3 表格全量重写；新增风险引擎章节 |

---

## 8. 待澄清问题清单

> 共 **21 项**。其中 **A 类 = 阻塞级（8 项，不拍板无法编码）**，**B 类 = 内容/文案级（8 项，影响验收）**，**C 类 = 实现细节级（5 项，可由架构师定）**。

### 8.1 团队历史悬而未决问题 Q1~Q4 的答案

| # | 问题 | 文档中的答案 | 结论 |
|---|---|---|---|
| **Q1** | `swing_plane` 的计算口径？是否需要真实杆身？ | **PDD 有明确定义**：§4.1「**顶点时引导臂与水平面的夹角**」，§4.2 术语解释「顶点时手臂与水平面的夹角」，阶段 = ④ top，参考 55°~65°，侧面专属 | ✅ **已定义 → 不需要球杆检测**。仅用 MediaPipe 左肩(11)→左腕(15) 向量与图像水平线夹角即可。<br>🔴 **但与 `club-detection-design.md` §4.3 严重冲突**：该设计把 `swing_plane` 定义为「⑤ Downswing 下杆段**杆头轨迹**拟合直线相对 base plane 的偏差」，参考 −5~+10°。**阶段、口径、量纲、参考范围四者全不同。** 必须二选一 → 见 #A8 |
| **Q2** | `spine_tilt_change` 与现有 `m_spine_tilt_delta` 是否同一个量？ | **是同一个量**。PDD 定义「击球时前倾角 − Address时前倾角」，现有 `m_spine_tilt_delta = max(0, addr_tilt − impact_tilt)`，都是"起身量 / Early Extension" | ✅ **同一个量**，差异只有三点：①key 改名 ②PDD 收窄为侧面专属（现有全机位）③**PDD 公式的减数被减数写反了**，与其自己的阈值符号矛盾（见 §3.2 坑 2） |
| **Q3** | 每阶段指标数是 2~4 还是 2~6？ | **答案是 2~4，此前的"矛盾"是误读**。PDD §3.4.1/§M024 均写"2~4 个"；§4.3.4 顶点表列 6 行是**跨两个机位**（4 正面 + 2 侧面），而 §1.3 约束 4 明确"每次仅支持单机位"，按机位过滤后：正面 4 项、侧面 3 项 | ✅ **单次分析每阶段 ≤ 4 项**，版式无需改。<br>🔴 **但反向暴露了更严重的问题**：侧面机位下 ⑤⑥⑦⑧ 只有 0/1/1/1 项，**下界破了**（AC-08 要求 ≥2）→ 见 #A1 |
| **Q4** | 错误码是 10001~10004/20001 还是 0/4001/4004/4009/5000？ | **PDD 明确写了 10001~10004（创建任务）与 20001（结果不存在）**，且成功统一 `code:0` | ⚠️ **两套体系并存，必须二选一**。PDD 的码不覆盖"任务尚未完成"（现有 4009）。<br>**PM 倾向**：采用 PDD 码作为对外契约（用户方文档已定稿、小程序按此对接），后端内部保留现有语义，在响应层做一层映射：`4001→10001/10002/10003`（按 message 细分）、`4004→20001`、`5000→10004`、`4009→` **需用户补一个新码**。→ 见 #A9 |

### 8.2 A 类：阻塞级（不拍板无法编码）

| # | 问题 | 影响 | PM 倾向性建议 | 需谁拍板 |
|---|---|---|---|---|
| **A1** | **侧面机位下 ⑤⑥⑦⑧ 四阶段指标数为 0/1/1/1，违反 PDD 自己的 AC-08「每阶段 ≥2 个指标」** | 侧面机位无法通过验收；结果页 4 个阶段近乎空白 | 把 `knee_flexion`、`head_drift`、`lead_arm_straightness`、`trail_arm_flexion`、`balance_hold` 等"正面/侧面通用"指标扩展到更多阶段；或为侧面机位补充 `shaft_lean_impact`（击球杆身前倾）、`shaft_plane_dev_top` 等侧面指标（`club-detection-design.md` §4.3 已有设计）。**需要用户方 PM 补齐侧面指标集** | 用户方 PM |
| **A2** | `spine_tilt_change` 公式「击球 − Address」与阈值 `≥10°`（正值）符号矛盾 | 直接写代码会导致 RISK-014 永不触发 | 以现有实现 `max(0, addr_tilt − impact_tilt)` 为准，PDD 公式判为笔误 | 用户方 PM 确认 |
| **A3** | RISK-014（起身，high 级）被限定为侧面专属，正面机位用户拿不到 | 正面是主力机位，却拿不到最高价值的 high 级风险之一；现有实现在正面本就能算 | 建议放开为全机位，正面机位下打 `estimated` 角标并在文案注明"正面机位估算，侧面更准" | 用户方 PM |
| **A4** | `spine_side_bend`「顶点时脊柱在侧面投影的侧弯幅度」口径过于笼统 | 无法编码：投影到图像平面还是 world y-z？绕哪个轴？与 `spine_tilt_side` 的区别是什么？ | 需用户方给出与 `spine_tilt_side` 同级的精确向量表达式 | 用户方 PM + 架构师 |
| **A5** | `lead_hand_position`（手部领先量）：仅在 §4.1 出现，未归属任何阶段；为定性指标无数值 | 无法套用 `MetricSpec`（需 `ref_min`/`ref_max`/`value: float`）；且需真实杆身 | **建议本期删除**（PDD §4.3 各阶段表本就没引用它） | 用户方 PM |
| **A6** | 侧面机位下"%肩宽"归一化标尺失真（DTL 双肩前后重叠，投影肩宽被压缩） | `head_drift`（PDD 标为正面/侧面通用）在侧面机位数值不可信，RISK-004 会误报 | 侧面机位改用**图像身高**作标尺（`club-detection-design.md` §4.2 Step 2 已有方案），或将 `head_drift` 收窄为正面专属 | 架构师 |
| **A7** | `spine_tilt_fwd` 在侧面机位的投影平面口径：PDD 写"在侧面投影"，现有实现用 world y-z 面 | 两者数值不同；且 world z 在 DTL 下可靠性存疑 | 建议 DTL 下改用**图像平面 x-y**（前提是手机保持水平），与 §4.4 #1 的水平参考线方案配套 | 架构师 |
| **A8** | **`swing_plane` 双定义冲突**：PDD =「④顶点 引导臂与水平面夹角，55~65°」vs `club-detection-design.md` §4.3 =「⑤下杆 杆头轨迹倾角相对 base plane 偏差，−5~+10°」 | 两者阶段/口径/量纲/参考范围全不同；RISK-009（`<50 或 >70`）只对 PDD 定义成立 | **建议以 PDD 定义为本期实现**（无需球杆、可立即交付、RISK-009 阈值自洽）；把球杆版重命名为 `shaft_plane_dev`，作为 P1 的**增强指标**并行存在，不占用 `swing_plane` 这个 key | 用户方 PM + 架构师 |
| **A9** | 错误码体系二选一（详见 Q4） | 小程序与后端契约 | 对外采用 PDD 码 + 响应层映射；请用户方补充"任务尚未完成"的码 | 用户方 PM |

> 注：A1~A8 为纯阻塞项（8 项）；A9 因已在 §8.1 Q4 给出可执行折中方案，归为"待确认"而非硬阻塞。

### 8.3 B 类：内容/文案级（影响 AC-18 / AC-16 验收）

| # | 问题 | 明细 | 建议 |
|---|---|---|---|
| **B1** | **7 条风险规则完全缺失文案** | RISK-003 / 004 / 008 / 009 / 012 / 013 / 015 缺 `trigger_description` + `suggestions` + `manual_excerpt` | 必须由用户方补齐。**研发不得自行编造**。若无法及时补齐，建议本期先上有完整文案的 10 条，其余 7 条挂 P1 |
| **B2** | **2 条风险规则缺手册原文** | RISK-016、RISK-017 有触发文案与建议，但无 `manual_excerpt`（表格给了页码 P8 / P10） | 请用户方按页码补录原文 |
| **B3** | RISK-002 的 `manual_page` 与 `manual_excerpt` 两处不一致 | 页码：§5.2.2 表格 `P6` vs §7.3 JSON `5`；原文：完整版 vs 截断版 | 以 §5.2.2 为准（待确认） |
| **B4** | RISK-014 手册原文与风险名语义不匹配 | 风险 = 起身/Early Extension，原文 = "脊柱过度侧屈…导致肋部骨折" | 疑似引错原文，请用户方复核 |
| **B5** | 低风险配色矛盾 | §3.4.3 图示 🟢 绿色 vs §6.5 枚举 🔵 蓝色 `#3B82F6` | 建议以 §6.5 为准（绿色已被"正常状态"占用，避免语义冲突） |
| **B6** | `camera_view` 是否支持 `auto` | PDD 要求用户手动二选一；现有枚举含 `AUTO`，且已有自动判定设计 | 建议：对外接口必填二选一；`AUTO` 降级为内部**一致性校验**，不一致时进 `warnings` 提示、不阻断 |
| **B7** | 术语解释缺 4 条 | `hip_toward_target`、`shoulder_total_open`、`lead_hand_position`、`max_head_drift` 无 `description`，违反 AC-16 | 请用户方补齐（若 A5 决定删除 `lead_hand_position`，且全程指标条不强制要求解释行，则只需补 2 条） |
| **B8** | ① Address 的 `spine_tilt_side` 沿用 5~12 参考范围是否成立 | 5~12 原本是给「肩线水平倾角」的经验值，现在换成「脊柱侧倾角」这一不同几何量 | 请用户方确认参考范围是否需要重新标定 |

### 8.4 C 类：实现细节级（可由架构师定，但需记录）

| # | 问题 | 建议 |
|---|---|---|
| **C1** | `trigger_condition` 是单 object，无法表达 RISK-009/011/012 的「A 或 B」双区间 | 扩展为 `conditions: List[Condition]` + `logic: "and" \| "or"`，或直接支持 `operator: "outside"` + `[min, max]` |
| **C2** | `manual_page` 声明为 `number`，但实际出现 `"P6/P11"`（两页）与 `"-"`（无） | 改为 `Optional[str]`，前端对 `null` 隐藏页码行 |
| **C3** | 单阶段风险条数上限 / 是否折叠 / 是否需要全局风险汇总区 | PDD 未定义。建议：不设上限（最多同阶段 3~5 条），全部展开；全局汇总区挂 P1（对应 [查看完整报告] 占位） |
| **C4** | 单边指标（`head_drift` / `x_factor_retention` / `balance_hold` / `spine_tilt_change`）的另一侧边界 | PDD 只给单边（如"<4正常"）。建议沿用现有 `reference.py` 的双边值（`head_drift 0~4`、`x_factor_retention 85~130`、`balance_hold 0.8~3.0`、`spine_tilt_delta 0~8`），并对这些指标关闭 `critical_low` |
| **C5** | 免责声明文案更新 | PDD §3.4.4 给了新版全文，需替换 `config.DISCLAIMER`：<br>「以上姿态数据基于单目视频估算，存在测量误差。损伤风险评估基于《高尔夫运动保障手册》中的一般性知识，仅供参考，不构成医学诊断或专业教学建议。如有身体不适，请咨询专业医疗机构。」<br>建议**追加**侧面机位专属说明："挥杆平面角为投影角估算，非真实空间角。" |

---

## 9. 建议的交付切分（供架构师参考）

| 批次 | 内容 | 阻塞依赖 |
|---|---|---|
| **C-1（可立即启动）** | 指标 key 批量改名 + `views` 机位归属落值 + `description` 字段 + 5 值状态（先按 §5.3 建议的区间宽度倍数实现）+ `risk_rules.py` 落 10 条完整规则 + `risk_engine.py` 匹配逻辑 + 小程序风险区 UI | 无（本文 §3.3 已提供全部可编码内容） |
| **C-2（需 A 类拍板）** | `swing_plane` / `spine_side_bend` 实现、侧面机位指标补齐、`spine_tilt_change` 机位归属、DTL 标尺 | A1 / A2 / A3 / A4 / A6 / A7 / A8 |
| **C-3（需 B 类补文案）** | 剩余 7 条风险规则上线、缺失的手册原文与术语解释 | B1 / B2 / B7 |
| **C-4（接口对齐）** | 路径/字段名/错误码统一 | A9（Q4） |

---

**文档结束** · 本文所有阈值与文案均可溯源至 PDD v2.0 原文；凡标注 ⛔ / ⚠️ 处，请勿由研发自行填补。
