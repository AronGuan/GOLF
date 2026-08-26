【locate_impact 机位感知改造 — 完成回传】

## 1. 改动文件清单
| 文件 | 变更 |
|---|---|
| `backend/app/segmenter.py` | `locate_impact` 加 `view: CameraView = FACE_ON` 尾部默认参数；DTL 分支穿越成功后直接用 `i_cross`；`segment_swing` 把 `view` 透传给 `locate_impact`；docstring 注明机位差异与设计理由 |
| `backend/tests/test_segmenter.py` | 新增 `TestViewAwareImpact`（4 测试）：face-on 默认/穿越+速度峰/向后兼容；DTL 穿越成功=穿越点；DTL 穿越失败走兜底；`segment_swing` 透传 view 给 `locate_impact`（monkeypatch spy） |
| `backend/_probe_out/probe_impact_view_before.py` | 新增：机位改造前快照探针（手工复刻旧速度峰路径，独立于新代码） |
| `backend/_probe_out/probe_impact_view_after.py` | 新增：机位改造后对比探针（face-on 0 变化断言 + DTL 速度峰 vs 穿越点对比） |
| `backend/_probe_out/probe_dtl_inspect.py` | 新增：DTL 信号深度检查（h 剖面/穿越点/窗口边界） |
| `backend/_probe_out/probe_impact_view_before.json` | 新增：before 快照数据 |
| `backend/_probe_out/dtl143_f{110,112,114,115,116,117,118,122}.jpg` | 新增：dtl_143 关键帧截图（视觉目检用） |

**未改动**：`config.py`（DTL 阈值不需新增，沿用 ⑥ 穿越点即可）、`pipeline.py`（已传 view 给 segment_swing，自动透传）、`impact_refiner.py`（不调用 locate_impact，只文档提及）、`reanchor_impact`（只调 locate_intermediate，不调 locate_impact）。

## 2. 核心判据代码（segmenter.py 第 309-330 行附近）
```python
if crossed.size > 0:
    i_cross = int(i_top + 1 + crossed[0])
    if view is CameraView.DOWN_THE_LINE:
        # DTL（侧面）：直接用穿越点作击球帧（2026-08 用户拍板）
        i_impact = i_cross
        estimated = False
    else:
        # face-on（默认，历史行为逐字节不变）：穿越点 ± 速度峰
        radius = max(1, int(round(config.IMPACT_WIN_SEC * fe)))
        a = max(i_top + 1, i_cross - radius)
        b = min(hi, i_cross + radius + 1)
        if b <= a:
            b = min(hi, a + 1)
        i_impact = a + int(np.argmax(sig.speed[a:b]))
        estimated = False
else:
    i_impact = i_top + 1 + int(np.argmax(sig.speed[i_top + 1 : hi]))
    estimated = True
```

`segment_swing` 透传：`i_impact, e_impact = locate_impact(signals, i_top, i_addr, view=view)`

## 3. 正面样本击球帧实证（0 变化）

### 3a. 原始 `locate_impact` 输出（无 CLUBLITE refine）
| 样本 | before(速度峰) | after(默认/face-on) | diff | estimated |
|---|---|---|---|---|
| 正面1 | 37 | 37 | **0** | True (兜底) |
| 正面2 | 284 | 284 | **0** | False |
| 正面3 | 70 | 70 | **0** | True (兜底) |
| 22030124 | 113 | 113 | **0** | False |

### 3b. 完整主链路（CLUBLITE refine + reanchor，`probe_faceon_regression.py`）
| 样本 | ④ | ⑤ | ⑥ | default==face? |
|---|---|---|---|---|
| 正面1 | 28 | 35 | 37 | **True** |
| 正面2 | 252 | 281 | 291 | **True** |
| 正面3 | 61 | 68 | 73 | **True** |
| 22030124 | 106 | 113 | 115 | **True** |

**正面逐字节零变化** ✅

## 4. 侧面样本对比 + 视觉目检

### 4a. 现状速度峰 vs 新穿越点对比
| 样本 | before(速度峰) | after(DTL 穿越点) | diff | estimated | 视觉确认 |
|---|---|---|---|---|---|
| 11a6594b | 213 | 213 | +0 | True（兜底） | 窗口内无穿越 → 行为不变 |
| f470c599 | 245 | 245 | +0 | True（兜底） | 窗口内无穿越 → 行为不变 |
| **dtl_143** | **114** | **116** | **+2** | False | **f116 = 接触瞬间 ✅** |

注：11a6594b / f470c599 的 h 信号在 DTL 双肩压缩下幅度极大（h≈20~30），窗口内无法穿越 `h_addr + IMPACT_Y_TOL`，因此走速度峰兜底 —— 改造后行为完全不变（这正是用户要求的"穿越失败时保留兜底"）。

### 4b. dtl_143 关键帧目检（穿越点 f116 vs 旧速度峰 f114）
| 帧 | 画面描述 | 是否接触 |
|---|---|---|
| **f114**（旧 face-on 速度峰） | 球杆高举过头、手在肩水平、杆头远在球上方 | **❌ 中段下杆，完全不是接触** |
| f115（穿越点前 1 帧） | 杆头接近球但未接触 | ❌ 未接触 |
| **f116**（新 DTL 穿越点 = ⑥） | 杆头在球位、杆身竖直、手在髋水平 | **✅ 接触瞬间（球+杆完美位置）** |
| f117（穿越点后 1 帧） | 杆头过球、开始扬起送杆 | ❌ 已过接触 |
| f122（窗口全局速度峰） | 杆高举过头、完整送杆姿态 | ❌ 送杆（印证用户"速度峰落在击球后"） |

**视觉确认 f116 正是"接触瞬间"**，f114 显然是错误定位（中段下杆），f122 是送杆。改造方向完全正确。

## 5. 测试数量
- **基线（改造前）**: 436 passed
- **改造后**: **440 passed**（436 + 4 新增 `TestViewAwareImpact`）
- 命令：`E:\project\golf\.tools\python312\python.exe -m pytest backend/tests -q`
- 结果：`440 passed, 4 warnings in 28.60s` ✅
- 新增测试覆盖：face-on 默认/穿越+速度峰/向后兼容；DTL 穿越成功=穿越点；DTL 穿越失败走兜底；`segment_swing` 透传 view 给 `locate_impact`（monkeypatch spy 在合成 fixture 上验证 DTL 分支可达）

## 6. 全局一致性审查
**IS_PASS: YES** ✅

逐项核查：
- ✅ **跨文件 import 一致性**：`CameraView` 已在 segmenter.py 第 26 行 import；test_segmenter.py 第 16 行 import。无新增/循环依赖。
- ✅ **接口契约**：`locate_impact` 尾部默认参数 `view: CameraView = FACE_ON`，向后兼容。返回类型 `Tuple[int, bool]` 不变。
- ✅ **数据流正确性**：pipeline.py prelim 调用（默认 FACE_ON）只取 Address 做机位判定，view 不影响 Address；第二次调用传解析后的 view，DTL 走穿越点路径。
- ✅ **无重复实现**：DTL 穿越点逻辑仅在 `view is DOWN_THE_LINE` 分支一份；face-on 分支逐字节与原代码相同。
- ✅ **边界处理**：
    - DTL 穿越失败 → 速度峰兜底（estimated=True），与 face-on 共用；
    - DTL AUTO view → 落到 face-on 分支（安全默认，向后兼容）；
    - `i_impact - i_top < min_gap` 守卫对 DTL 同样生效（i_cross ∈ [i_top+1, hi)）。
- ✅ **reanchor_impact 不调用 locate_impact**（仅调 locate_intermediate），无需改动；已 grep 全文确认。
- ✅ **impact_refiner.py 不调用 locate_impact()**（仅文档提及），无需改动。
- ✅ **440 测试全绿**：基线 436 + 新增 4。

## 7. 关键设计决策与遗留

### 设计决策
1. **DTL 直接用 `i_cross`**：当窗口内存在穿越点（`h[i] <= h_addr + IMPACT_Y_TOL`），DTL 直接取穿越点作击球帧，不再做速度峰偏移。理由：DTL 双肩压缩使 h 幅度异常（实测 11a6594b h≈20+），但穿越点仍能正确定位接触瞬间；速度峰在 DTL 下与接触时刻相关性差（dtl_143 速度峰 114 vs 穿越 116，且全局速度峰 122 是送杆）。
2. **穿越失败保留兜底**：用户未要求改兜底逻辑，两机位共用同一速度峰兜底（estimated=True），最大化向后兼容。
3. **face-on 分支逐字节不变**：通过 `view is CameraView.DOWN_THE_LINE` 严格枚举判断，只在 DTL 分支走新路径，face-on 分支代码与原版相同 → 正面样本 0 变化实证。

### 已知遗留 / 注意点
- **11a6594b / f470c599 仍走兜底**：这两个 DTL 样本的 h 信号因肩宽标尺 S 极小（0.014 / 0.005，约为正面 1/20）导致 h 幅度极大（20~30），窗口内永远无法穿越 `h_addr + IMPACT_Y_TOL`（h_addr 极负）。这是 DTL 信号本身的特点，非新代码问题。用户决策"穿越失败保留兜底"已显式覆盖此场景。
- **dtl_143 的 +2 帧位移**：旧 114（速度峰）→ 新 116（穿越点）。dtl_143 是新引入样本（`微信视频2026-08-26_104443_143.mp4`），不在用户原始命名列表（11a6594b / f470c599），但它是唯一能展示 DTL 穿越分支的样本，视觉目检确认 f116 = 接触瞬间。如认为 +2 帧位移过大，可单独再校准（但当前实现与用户决策完全一致："直接用穿越点"）。
- **AUTO view 行为**：当前代码 `view is DOWN_THE_LINE` 严格判断，AUTO 落到 face-on 分支。pipeline.py 在进入 segment_swing 前已经过 view_detector.resolve()，实际不会传 AUTO。安全默认。

未执行 git 操作（按要求不动 git 元数据，主理人自检后统一提交）。