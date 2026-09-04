# ADR-002｜击球帧判定**不结合**球杆检测

- **状态**：已接受（Accepted）
- **日期**：2026-09-04
- **决策人**：主理人 / 架构师（高见远）
- **执行人**：工程师（寇豆码）
- **相关文档**：`docs/ADR-001-club-detection.md`、`docs/ARCHITECTURE-v3-clublite.md`、
  `docs/VALIDATION-CLUBLITE.md`、`docs/CLUB_DATA_REQUIREMENTS.md`
- **相关代码**：`app/segmenter.py:349 locate_impact`、`app/impact_refiner.py`、
  `app/club_detector.py:359 _detect_hough` / `:488 _detect_framediff`、
  `app/ai/club_probe.py:309 _observe_rule`

---

## 1. 背景

2026-09-04 方案 A 落地时得到一个**反直觉结论**：规则法 Hough（既有代码
`app/club_detector.py`，零 ML 依赖）在真实素材上**远胜 GolfPose ONNX 关键点**——
慢段（address/top/finish）3/3 命中，`swing_plane` 从代理值 39.40° 修正为真值 53.50°。

这自然引出一个问题：

> **既然杆检测这么准，击球帧（⑥ impact）判定是不是也该结合杆？**

历史上否决过这个想法，但当时的理由是「`club_detector` 在真实视频置信度仅
0.206~0.462、L0 从未出现」——**这个前提已被推翻**（见 §3.1）。因此需要重新实证。

---

## 2. 决策

**不用杆头位置直接替换 impact。** 击球帧主判据维持运动学，但**杆已作为锚点在用**（见 §2.1）。

| 环节 | 判据 | 是否用杆 |
|---|---|---|
| `locate_impact`（`segmenter.py:349`） | 髋部相对高度 `h` 回落穿越 Address 容差带（DTL）／穿越点±速度峰（face-on） | ❌ 纯运动学 |
| `impact_refiner`（CLUBLITE M1） | 地面 ROI **区域**帧差运动峰 × 贴地度 | ❌ 区域运动，非杆头定位 |
| CLUBLITE M2 + **D 方案** | 简化 Hough 杆头最低点 → **先验锚点**，邻域内按综合 score 选帧 | ✅ **已在用**（降级用法） |
| SwingNet（DTL） | 深度学习 8 事件检测 | ❌ |

### 2.1 ⚠️ 重要：「用杆头最低点定击球帧」**早已落地**（D 方案）

2026-09-04 追问时发现：用户设想的方案**不是"能否实现"，而是"已经在跑"**。

- 开关：`config.CLUBLITE_USE_ANCHOR=True`
- 实现：M2 的 `_shaft_lowest_y` 找杆头 y 最大帧 → `ImpactRefineResult.shaft_lowest_index`
- 用法：**不直接替换** impact，而是作**先验锚点**，只在锚点
  ±`CLUBLITE_ANCHOR_WINDOW`（=3）邻域内按综合 score 选帧
- 守卫：`CLUBLITE_ANCHOR_MIN_SCORE_RATIO=0.7`，假锚点则回退 v2 全窗口

**历史依据**（`impact_refiner.py:32-44`，唯一有人工目视 ground truth 的样本
`22030124ed3bce12cdec7c629d0c6cc8`）：

| 来源 | 帧号 | 相对真实击球 |
|---|---|---|
| 真实击球（人工目视） | 115 | — |
| **M2 杆身最低点** | **116** | **+1（几乎完美）** |
| M1 区域运动峰 | 121 | +6（系统性偏晚） |

→ 在那个样本上**杆头最低点远准于运动峰**，这正是 D 方案被采纳的原因。

**结论**：正确的用法是**降级**——把不可靠的绝对判据（杆头位置）降级为
可靠的相对先验（锚点 + 邻域精修 + 守卫）。直接替换则不行（见 §3.2）。

---

## 3. 理由（实证数据）

### 3.1 先证伪历史理由：杆检测「不可靠」的说法已过时

在**密集采样**（step=1~2）下，规则法在慢段是可靠的：

| 指标 | 规则法 Hough | GolfPose ONNX |
|---|---|---|
| 覆盖率（frame 0~80） | **40/40 (100%)** | 16/40 (40%) |
| 帧间差 median | **4.1°** | 10.4° |
| 慢段关键帧命中 | **3/3** | 0/3 |

→ 当年「置信度 0.206~0.462」的失败，根因是在**稀疏事件帧**上触发了级联失效
（速度门控切帧差 → `last_dir` 冻结），**不是算法本身不行**。

### 3.2 但在**击球窗口**，规则法两条路径都失效

⚠️ **关键：必须读原始检测值，不能读 `detect()` 的产出**（见 §5.1 踩坑）。

对 2 段 DTL 视频、2 种算法、2 种时序模式，在 `[top-2, finish+2]` 逐帧实测：

| 视频 | 算法 | 全窗口检出 | conf≥0.30 | **impact±2 可信** |
|---|---|---|---|---|
| 横屏 DTL | Hough | 13/22 (59%) | 9 (41%) | **1/5 (20%)** |
| 横屏 DTL | 帧差 | 6/22 (27%) | 0 (0%) | **0/5 (0%)** |
| 竖屏 DTL | Hough | 14/24 (58%) | 13 (54%) | 3/5 (60%) |
| 竖屏 DTL | 帧差 | 5/24 (21%) | 2 (8%) | 1/5 (20%) |

**决定性证据：impact 帧本身在所有 4 组配置下 100% 漏检。**
横屏 `impact=31`、竖屏 `impact=163` —— Hough 与帧差都检不出。

「杆头最低点相对 impact 的偏移」在四种配置下给出 **+4 / +8 / +2 / -5**，
**完全发散**，不存在可用于标定的稳定偏移量。

### 3.3 根因：运动模糊是物理限制，不是参数问题

`app/club_detector.py:499` 早已预言：

> 下杆到击球段杆头线速度可超 30 m/s，杆身在单帧内被抹成一片糊影，
> **直线检测失效**；但运动残影恰好勾勒出杆扫过的区域，帧差反而是这一段最稳的信号。

实测证明**连帧差也救不了**：横屏帧差在 22~28 段 y 值锁死在 307~308
（conf 仅 0.06~0.15），是锁到背景固定运动区域的**假阳性**，不是杆头。

### 3.4 规则法的适用域由此被精确划定

| 区段 | 运动速度 | Hough 直线 | 帧差残影 | 结论 |
|---|---|---|---|---|
| ① address 准备 | 静止 | ✅ 可靠 | — | ✅ 用作真值源 |
| ④ top 顶点 | 瞬时静止 | ✅ 可靠 | — | ✅ 用作真值源 |
| ⑧ finish 收杆 | 静止 | ✅ 可靠 | — | ✅ 用作真值源 |
| ⑤ 下杆 / ⑥ 击球 / ⑦ 送杆 | **>30 m/s** | ❌ 糊影失效 | ❌ 锁背景 | ❌ **禁用** |

**方案 A 只选 address/top/finish 三个慢段，恰好避开这个物理盲区——这不是巧合，是自洽的设计。**

### 3.5 为什么 CLUBLITE 能扛住，而杆检测不能

CLUBLITE M1 用的是**区域运动强度**（地面 ROI 内帧差像素总量），
**不要求定位到杆头**，只要求"这一帧地面带有运动"——对模糊天然鲁棒。

而任何基于杆的击球帧判定，都要求**定位杆头**，一旦模糊就崩。
这是「区域信号」与「点位信号」的本质差异。

---

## 4. 维持现状的额外收益

- **零新增风险**：`impact_refiner` 的 G0 降级链（任何失败 → `available=False`，
  保持原 `locate_impact`）完全不受影响。
- **D 方案假锚点守卫已生效**：`CLUBLITE_USE_ANCHOR=True` 把「杆头最低点」当先验锚点，
  但配了 `CLUBLITE_ANCHOR_MIN_SCORE_RATIO=0.7` 守卫。历史实测
  `0bb16a97/1446d1b9/a4fba3d2` 的"杆身最低点"是 Hough 假阳性，ratio 仅
  0.11/0.55/0.36 → 正确回退全窗口。**本次数据再次支持保留该守卫。**

---

## 4.1 ⚠️ 新发现的遗留问题：横屏 refine 疑似把 impact 推偏

调研过程中意外发现一个**生产问题**，与「是否结合杆」相关但独立存在。

### 现象

`.workbuddy/verify_anchor_in_prod.py` 显示 D 方案在真实链路**正在生效**：

| 视频 | 原始 impact | refine 后 | delta | M1 运动峰 | M2 杆头最低点 | 锚点生效 |
|---|---|---|---|---|---|---|
| 横屏 DTL | 31 | **38** | +7 | 39 | 39 | `anchor_used=True` |
| 竖屏 DTL | 163 | **167** | +4 | 168 | 162 | `anchor_used=False` |

### 几何合理性检验（不依赖 ground truth）

以「真实挥杆下杆 0.20~0.30s、击球到收杆 ≥0.5s」为基准：

| 判据 | 横屏 | 下杆 | 送杆 | 竖屏 | 下杆 | 送杆 |
|---|---|---|---|---|---|---|
| A `locate_impact` | 31 | **0.23s** ✅ | 0.33s | 163 | **0.20s** ✅ | 0.43s |
| B 全窗口杆头最低点 | 35 | 0.37s ⚠️ | 0.20s | 165 | 0.27s ✅ | 0.37s |
| C 当前 refine | 38 | 0.47s ❌ | **0.10s** ❌ | 167 | 0.33s ⚠️ | 0.30s |

**横屏 C=38 明显不合理**：下杆比真实上限长 57%，送杆仅 0.10s（远低于 0.5s），
且距 finish=41 只剩 3 帧 —— 疑似把**送杆期的横扫运动**误判为击球
（正是 `impact_refiner.py:32-38` 记录的「横扫式运动峰偏晚」问题）。

### 判据 1 与判据 2 的一致性（重要）

在 Address 帧测杆头 y 作为「球位基准」，再找下杆段最接近该高度的帧：

| 视频 | Address 球位 y | 最低点帧 | 该帧距球位 | 回球位帧 | 结论 |
|---|---|---|---|---|---|
| 横屏 | 548.4 (f5) | 35 | −46.6 px | **35** | 两判据一致 |
| 竖屏 | 768.7 (f140) | 165 | −10.9 px | **165** | 两判据一致 |

**「杆头 y 最大」与「杆头回到球位高度」给出完全相同的结果** —— 说明在这
两段素材上，杆头轨迹最低点恰好就是触球瞬间的球位高度，两个独立判据互相印证。
（但这是巧合还是普遍规律，需更多样本；理论上杆头最低点应略滞后于触球。）

### 根因：M2 受限于 M1 的候选集

横屏 M2 杆头最低点 = 39，但全窗口原始 Hough 的真实最低点 = **35**。
差异来源：M2 的 `_shaft_lowest_y` **只在 M1 挑出的 Top-K 候选（Top-K=3）里选**，
横屏候选集是 `[34, 39]`，而 f34 杆头未检出 → 被迫选 39（送杆期横扫帧）。

### 修复进展（2026-09-04）

#### ✅ 已落地：物理窗口守卫

配置项：:data:`config.CLUBLITE_MIN_FOLLOW_THROUGH_SEC` /
:data:`config.CLUBLITE_MAX_DOWNSTROKE_SEC`
代码位置：:func:`app.impact_refiner.refine_impact` Step 7

把原来的**单条下界**扩展为**三条**守卫（任一不满足 -> G0，保持原 events）：

| 守卫 | 判据 | 阈值 | 物理依据 |
|---|---|---|---|
| 下界（既有） | ``impact - top >= min_gap`` | 0.06s | 避免 reanchor 挤出 NO_SWING |
| **送杆下界（新增）** | ``finish - impact >= min_follow`` | **0.25s** | 真实送杆 ≥0.5s，取保守下限 |
| **下杆上界（新增）** | ``impact - top <= max_down`` | **0.40s** | 真实下杆 0.20~0.30s + 0.10s 余量 |

**实测效果**（2 段真实 DTL，脚本 ``.workbuddy/verify_anchor_in_prod.py``）：

| 视频 | top | 原 impact | 守卫前 | 守卫后 | 结果 |
|---|---|---|---|---|---|
| 横屏 1446d1b9 | 24 | 31 | 38 | **31（G0）** | ✅ 双守卫触发（下杆 0.47s / 送杆 0.10s） |
| 竖屏 a4fba3d2 | 157 | 163 | 167 | **167** | 放行（下杆 0.33s / 送杆 0.30s） |

日志佐证::

    impact refine rejected: impact 38 violates guard
    (top=24 finish=41 | min_gap=2 min_follow=8 max_down=12
     | lower=True follow=False down=False)

→ 横屏 +7 帧的偏晚被拦掉，竖屏无回归。测试 **509 passed / 2 skipped / 0 failed**。

#### ⚠️ 守卫的能力边界（务必知悉）

**守卫只能拦住被推出物理窗口的极端偏晚，拦不住窗口内的偏晚。**

实测案例 ``8.8.110``（用户报告）：impact=119 / finish=131 → 送杆 0.40s、
下杆约 0.23~0.30s，**两条守卫均在窗口内、均不触发**。

这类「refine 在物理合理区间内选偏晚」需要的是**提高 refine 精度**
（即下方建议 1「M2 全窗口化」），继续加边界守卫无效——边界只会把
横屏这类极端样本挡掉，中间的偏移它管不到。

#### ✅ 已落地（但验证无效）：M2 全窗口化（方案 A）

配置项：:data:`config.CLUBLITE_M2_FULL_WINDOW`（**默认 False**，勿改回 True）
代码位置：:func:`app.impact_refiner._shaft_scan_window` /
:func:`app.impact_refiner._pick_full_window_anchor`，Step 6b 接入两级锚点源
（``full`` 全窗口 → ``topk`` 候选），失败回退 v2 全候选集。

**思路正确**：让 M2 锚点不再受 M1 Top-K 限制，自己到整个 refine 窗口
（``[impact-0.05s, impact+0.25s]``，30fps 约 11 帧）找杆头最低点，
理论上能修掉「真实最低点不在候选里」的横屏根因。

**但实测证明信号源不可信**（脚本 ``.workbuddy/diag_m2_full_window.py`` /
``.workbuddy/probe_shaft_lines.py``）：

- ``_shaft_lowest_y`` 的「延长线过握把」过滤（垂距 < 0.12×杆长）**不约束端点到
  握把的距离**——只要直线的延长线指向握把就算"过握把"；
- 横屏 1446d1b9 击球窗口内检出的"过握把"线段，**端点到握把距离 500~1000px**
  （杆长先验仅 163px）、方向 44°~86°，全是广告牌/网笼/草地边的背景假阳性，
  **没有一条真杆身线**；
- 因此全窗口锚点被背景线污染（横屏锚到 array 31、竖屏锚到 array 170），
  与本 ADR §3.2/§3.3 的「击球段杆头 >30 m/s 模糊、Hough 物理不可检测」一致。

最终 ``_anchor_window_credible``（0.7）守卫把假锚点全部拦回，**结果与关闭时
完全一致**（横屏 G0→31，竖屏 167）。结论：**扩了锚点来源也白扩，因为信号本身
不可信**。保留开关与代码，待 **120fps / 高质量素材**（杆头清晰，如历史样本
22030124）到位、并配合 ``_shaft_lowest_y`` 的杆长约束修复后再开启。

#### 待办（按优先级）

1. **修 ``_shaft_lowest_y`` 的杆长约束**（惠及既有 topk 版本，不只全窗口）：
   过滤「远端端点到握把距离 ∈ [0.3, 1.6]×杆长」+「方向接近竖直」，
   一刀切掉 500~1000px 的背景线。⚠️ 需注意：修复后击球段可能**大量返回 None**
   （真杆身本就不可检测），会让既有 D 方案锚点退化——改前必须实测竖屏 163→167
   等已标定样本不回归。
2. 守卫阈值需更多样本标定（当前 n=2）
3. 合成测试视频的事件分布不物理（top=42、refine 目标 56 → 下杆 0.467s），
   守卫会一致拒绝。故 ``test_impact_refiner.py`` 用 autouse fixture
   默认关闭守卫，守卫本身由 :class:`TestPhysicalGuard` 专项覆盖

⚠️ `finish` 定位本身可能偏早（横屏 5→41 仅 1.2s，疑似半挥杆），
**送杆下界阈值对 finish 的准确性敏感，需更多素材复核**。

#### §4.3 独立实测：杆头最低点方案在 SwingNet 修复后的真实表现（2026-09-04 19:4x）

用户追问「杆离地面最近作为击球帧能否实现」——脚本 `.workbuddy/probe_lowest_point_11_hough.py`
在 11.mp4（SwingNet 修复后）上跑了 fresh 模式 `_detect_hough` 路径 A
逐帧探测 [Top, Finish] = [109, 157] 共 49 帧：

| 指标 | 实测值 |
|---|---|
| 路径 A 覆盖率 | **49/49（100%）** |
| head_y 最大帧 | **117**（952.0px，conf 0.70） |
| 相对 SwingNet Impact(116) | **+1 帧 / +33ms** |
| 头部 10 帧范围 | [114, 119] = Impact ±3 帧 |

**与历史 ground truth 对照（两段素材独立实测）**：

| | 真实击球 | 杆头最低点 | 偏差 |
|---|---|---|---|
| 历史（横屏 22030124，impact_refiner.py:32-44） | 115 | 116 | **+1 帧** |
| 当前（11.mp4，SwingNet 修复后） | SwingNet 116 | 117 | **+1 帧** |

两段素材、两种切分路径、独立实测，**「杆头最低点天然晚于真实击球 +1 帧」** 被重复验证——
这不是偶然，是**杆头越过最低点（=球接触球的瞬间）之后还有继续下行的弧段**这一物理事实。

**结论分级**：

| 用法 | 能不能 |
|---|---|
| 用「杆头最低点」**替换** SwingNet Impact | ❌ 会让击球帧偏晚 **+1 帧**（116 → 117） |
| 用「杆头最低点 - SwingNet Impact」**反向校验**切分 | ✅ 差值 ∈ [0, +3] 帧 = SwingNet 准 |
| 作 **CLUBLITE D 方案的锚点**（已是现状） | ✅ 提供独立信号源，让 refine 不只靠地面运动峰 |

**不是提升方向——SwingNet 修复后击球帧已经准了。**

进一步提升的杠杆点：SwingNet Impact 与杆头最低点的差值作「哨兵」，
未来若切分因故退化（比如规则引擎 fallback），差值异常 (>3 帧) 即触发告警。

#### §4.4 ⚠️ 用户反转结论 + M3「杆头最低点 = 击球帧」落地（2026-09-04 20:1x）

用户在截图里明确指出：**实际击球帧在 117，系统捕获 116**，要求「找杆头离地面
最近的点作为击球帧」。这**推翻了 §4.3 的「最低点天然晚 +1 帧、替换 ❌」结论**——
用户目视真值是 117（= 杆头最低点），SwingNet 的 116 反而偏早 1 帧。

> ⚠️ §4.3 的「物理事实」推断（杆头越过最低点后仍有下行弧段）在 11.mp4 上
> **不成立**——用户确认最低点帧即视觉接触帧。这是 ground truth 与理论推断的
> 冲突，以用户目视真值为准。

**关键前提（决定 M3 接线位置）**：11.mp4 是 SwingNet DTL 路径（`used_swingnet=True`），
pipeline 里 `if CLUBLITE_ENABLED and not used_swingnet` **跳过 CLUBLITE 校正**，
所以 116 直接来自 SwingNet，M1/M2/锚点改造都对它无效。要改 116→117，必须
**在 SwingNet DTL 分支**新增校正。

**M3 落地**（默认 `CLUBLITE_M3_FRESH_ANCHOR=False`）：
- `impact_refiner._shaft_scan_window_fresh`：全窗口逐帧 **fresh 模式**
  `_detect_hough`（路径 A，pred_dir 向下 + fan/dir_tol 拉满让方向约束失效）
  —— 与 M2 的简化 `_shaft_lowest_y` 信号源不同，是 49/49 命中的关键（§5.3）
- `impact_refiner.refine_impact_lowest_point`：独立校正入口，返回 Optional[array 下标]；
  信号链 = fresh 扫描 → 杆头 y 最大 + 运动支持门槛 → 物理窗口守卫 → 位移守卫
- `pipeline.py` SwingNet 分支（else 块）追加 M3：解窗口 + 最低点校正 + reanchor，
  失败保持 SwingNet 原值
- 实测 11.mp4：`refine_impact_lowest_point` → **117**，reanchor 后 impact=117
- 全量测试 525 passed / 2 skipped / 0 failed（+6）

**默认关的原因**：高速段 Hough 可靠性取决于素材杆头是否清晰（ADR-002 一贯边界），
需更多真实素材回归后默认开启；规则引擎路径暂不受 M3 影响（仍走 M1+M2+D 方案）。

---

## 5. 踩坑记录（务必阅读，防止重蹈）

### 5.1 `detect()` 的产出含插值伪影，不能用于定量分析

`app/club_detector.py` Step 7（`_smooth_track`，约 `:690-713`）做了两件事：

1. `np.interp` —— **未检出的帧用邻居插值填充** `head`
2. `moving_average(_SMOOTH_WINDOW)` —— 全序列再平滑

后果：**`conf=0.00` 的帧不是"检测到但不确定"，而是压根没检测到、被插值出来的。**

第一次实验（`.workbuddy/probe_impact_vs_club.py`）读到 `detect()` 产出，
得到「两段视频都差 +2 帧」的漂亮一致性 —— **但那两个最低点（横屏 f33 /
竖屏 f165）恰好都是 conf=0.00 的插值帧**，结论完全错误。

竖屏 f170~178 的 `head_y` 恒为 614.5，是 `np.interp` 尾部边界值平推。

> **判据：定量分析必须绕过 Step 7，逐帧直接调 `_detect_hough` / `_detect_framediff`。**

### 5.2 `except` 吞异常会制造假阴性

第二次实验（`.workbuddy/probe_impact_diff_vs_hough.py`）首轮得到
「帧差 0/22、0/24」—— 看似帧差完全失效。真实原因是**函数名写错**
（写成 `_detect_diff`，正确是 `_detect_framediff`），`AttributeError` 被
`except Exception: d = None` 吞掉，全部变成"未检出"。

修正后帧差在竖屏有 2 个可信帧（8%），横屏仍是 0 —— 结论方向没变，但数据可信了。

> **判据：诊断脚本的 `except` 必须打印异常，绝不静默置 None。**

### 5.3 `_shaft_lowest_y` 不是 `_detect_hough` 的可替换替代品

第三次实验（`.workbuddy/probe_lowest_point_11.py`）用 `_shaft_lowest_y` 跑
11.mp4 [Top, Finish] 49 帧，结果 **0/49 命中**。看似「物理上杆头检测不可行」，
实为 **`_shaft_lowest_y` 是简化版**：全图 Hough、无 ROI、无扇形 mask、无
骨架共线过滤。同一窗口换 `_detect_hough`（路径 A）**49/49 命中**。

`_shaft_lowest_y` 在生产链路（`_shaft_scan_window`）里被设计为**对极少数
候选帧**调用，所以没 ROI 也能跑；**但用于「全窗口逐帧探测」时它的过滤
远弱于路径 A**。

> **判据：M2 锚点选源用 `_shaft_lowest_y`，全窗口逐帧探测用 `_detect_hough`**
> **（路径 A），两者不可混用。**

---

## 6. 未来重新评估的触发条件（满足**任意一条**才重开此议题）

| # | 条件 | 说明 |
|---|---|---|
| 1 | 拿到 **≥20 段 60fps** 素材 | 30fps 下击球窗口仅 1~2 帧，60fps 可翻倍采样密度，模糊也减轻 |
| 2 | 引入**事件相机 / 全局快门**等高帧率硬件 | 从根本上消除运动模糊 |
| 3 | 有**人工标注的 true impact 帧** ≥20 段 | 现状无法判断 ±2 帧究竟谁对，缺 ground truth |
| 4 | 杆头检测模型在**高速段实测可信率 ≥80%** | 现状横屏 impact±2 仅 20%/0%，差一个数量级 |

**在此之前，任何"用杆定击球帧"的改动都应被拒绝。**

---

## 7. 关联：这不影响方案 A

本次结论与方案 A **不矛盾，反而互相印证**：

- 方案 A 用杆的**方向**（杆身角度），取自三个**静止**阶段 → ✅ 物理上可行
- 击球帧要用的杆的**位置**（杆头最低点），取自**高速**阶段 → ❌ 物理上不可行

同一个检测器，在不同运动阶段的可用性天差地别。**选阶段比选算法更重要。**

---

## 8. 复现命令

```bash
# 慢段：规则法 vs ONNX 对决（支持方案 A）
E:/project/golf/.tools/python312/python.exe .workbuddy/plot_rule_vs_onnx.py

# 快段：Hough vs 帧差对决（支持本 ADR §3.2）
E:/project/golf/.tools/python312/python.exe .workbuddy/probe_impact_diff_vs_hough.py

# 原始逐帧 Hough，去插值去平滑（indep / chain 两种模式）
E:/project/golf/.tools/python312/python.exe .workbuddy/probe_impact_raw_hough.py

# D 方案实况：M1 运动峰 / M2 杆头最低点 / 最终采纳 三方对比（§4.1）
E:/project/golf/.tools/python312/python.exe .workbuddy/verify_anchor_in_prod.py

# 杆头回球位判据 + 几何合理性检验（§4.1）
E:/project/golf/.tools/python312/python.exe .workbuddy/probe_head_returns_to_address.py

# ⚠️ 以下两个脚本结论/方法有问题，仅作踩坑留档，勿再引用其数字
#     .workbuddy/probe_impact_vs_club.py                （读 detect() 产出，含插值伪影）
#     .workbuddy/verify_lowest_point_triangulation.py   （误把系统性偏晚的运动峰当裁判）
```
