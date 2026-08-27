# 高尔夫挥杆分析小程序 — 项目现状简报（2026-08-27 审计）

> 大湾区靓仔（项目总监）牵头现状审计。交叉核对：PRD.md / ARCHITECTURE*.md / STATUS.md / 实际代码 / git 历史。
> 结论：**项目不是从零起步，而是已演进到 v3 功能态的"准上线"状态，唯一硬阻塞是上线前置（域名/HTTPS）。**

---

## 一、项目是什么
微信小程序（原生，3 页面）+ Python 后端的高尔夫挥杆姿态分析工具。
**闭环**：用户拍/选挥杆视频 → 上传 → 后端 MediaPipe 抽 33 关键点 → 切**经典 8 阶段**（Address/Takeaway/Backswing/Top/Downswing/Impact/Follow-through/Finish，对齐 GolfDB）→ 每阶段算身体角度指标（核心 = **X-Factor 肩髋分离度**）→ 小程序结果页展示 8 张骨架叠加图 + 指标卡 + 风险提示。

## 二、技术底座（已实测，勿推翻）
- **Python 3.12.9 便携版**（无 venv）+ **MediaPipe 0.10.14 legacy API**（内置 tflite，零外网）+ **numpy<2** + opencv-headless 4.11
- 后端 **FastAPI + uvicorn 单 worker**（任务状态存进程内 dict）；前端**原生小程序零构建**
- 部署：阿里云 ECS `39.102.63.30:8000`（conda env `golf` + systemd），本地 git→GitHub→ECS pull→`deploy-aliyun-conda.sh`
- DTL 侧面切分用 **SwingNet**（GolfDB 预训练，权重 63MB 需手动 scp，缺失自动回退规则引擎）

## 三、功能完成度（代码+测试+STATUS 三方一致）
| 模块 | 状态 | 备注 |
|---|---|---|
| MVP v1.0（face-on 8 阶段 + 35 指标） | ✅ 已上云 | X-Factor 核心诊断 |
| PDD v2.0 双机位（face-on + DTL） | ✅ | `view_detector` 自动判定 **9/9 命中** |
| 侧面指标 swing_plane / spine_tilt_change / spine_tilt_fwd | ✅ | **纯 MediaPipe，已落地**（陈旧记忆"未落地"已纠正） |
| 损伤风险引擎（17 条 RISK-001~017） | 🟡 10 启用 / 7 关 | 7 条缺文案 `enabled=False`，补文案即可翻开关 |
| 指标 5 态 + 术语行 + 手册弹窗 + 全程条 | ✅ | 红橙配色 |
| 接口契约（双路径 + PDD 错误码） | ✅ | 灰度双活不破坏已上线小程序 |
| ClubLite 击球帧校正 | ✅ | 12 段 9 段有效，opens=1，零回归 |
| 手动帧微调（◀▶ + 实时重算指标） | ✅ | 467→483 passed |
| 整页截图到相册 | ✅ | 离屏 canvas2d |
| DTL 机位感知 + SwingNet 切分 | ✅ | face-on 逐字节不变 |

## 四、测试与质量
- **单测 483 passed**；真实视频端到端 7/9（2 段残缺确定性 NO_SWING）；机位判定 9/9；击球校正 12 段有效
- 文档齐全：`STATUS.md`(权威快照) + PRD/ARCH-ARCH-v2/ARCH-v3-clublite + 多份 VALIDATION/QA 报告

## 五、当前真实阻塞（按优先级）
1. 🔴 **上线硬前提（头号）**：备案域名 + HTTPS + 微信 request/uploadFile/downloadFile 白名单未做；`api.js` 写死 `http://39.102.63.30:8000`（裸 IP+HTTP），真机预览/正式版必被微信拦截。**需用户商务推进（1~3 周）**。
2. 🟡 7 条风险规则缺文案（RISK-003/004/008/009/012/013/015）→ PM 郑天虹补，零代码。
3. 🟡 参考区间待重标：`spine_tilt_side` ref 5~12 需复标；`swing_plane` 个别样本偏低需人工复核。
4. 🟡 SwingNet 权重需手动 scp（63MB，不入 git）。
5. ⚪ 小项：`API_CODE_STYLE` 硬编码非环境变量；RISK-002 手册页码冲突。

## 六、建议的下一步（待你拍板）
- **A. 推进上线前置**（你侧商务：备案+域名+HTTPS+微信白名单）→ 改 `BASE_URL` 为 `https://域名` → 真机可发体验版
- **B. 补 7 条风险文案**（最快出量，零代码，纯 PM 文案）
- **C. 参考区间重标 + 更多真实素材回归**（需你提供 10+ 段双机位真实视频 + 教练/理疗审核）
- **D. 其它**（如新功能、性能、文档整理）

> 备注：本简报依据 `docs/STATUS.md`（2026-08-18 快照，尾注 2026-08-27）+ 代码实读。STATUS.md 内测试数记 467、git 最新 483，属正常小幅漂移。

---

## 七、本次 UI 改动（2026-08-27 · 机位图标升级）

> 触发：用户吐槽首页"正面机位"view-icon 🙋 不像正面打高尔夫球。审计发现 view-icon 是 emoji，`project P0`（禁止 emoji 图标）；趁机一起替换。

### 改动清单
| 操作 | 路径 | 说明 |
|---|---|---|
| 新增 | `miniprogram/assets/icons/view_face_on.png` | 51KB · 512×512 · 92% 透明 · 正面握杆姿势 |
| 新增 | `miniprogram/assets/icons/view_down_the_line.png` | 52KB · 512×512 · 95% 透明 · 侧面 address 静态站姿 |
| 改 | `miniprogram/pages/index/index.wxml` (L17, L26) | `<view class="view-icon">🙋/🏌️</view>` → `<image class="view-icon" src="../../assets/icons/view_*.png" mode="aspectFit" />` |
| 改 | `miniprogram/pages/index/index.wxss` (L43-48) | `.view-icon` 由 `font-size: 44rpx` 改 `display:block; width:96rpx; height:96rpx; margin:0 auto 4rpx;` |

### 关键技术点
- **图标来源**：ImageGen（path 2）生成 stroke-style flat icon，单色 #1fbf75，transparent background
- **后处理**：face_on AI 误出**白底**而非透明；side 出**黑底+水印**。自写 `_bg2transparent_v2.py`：采样图片四角像素作背景基准 + RGB 距离阈值判定，抠透保留主体
- **P0 顺带**：本轮 view-icon emoji 全部清除 ✅；**剩余 5 处 emoji 待清**（首页 ⛳/📹/🖼/✓/✗，未动）
- **实际渲染 96rpx ≈ 48px**，48px 缩略图下肉眼不可见水印残留；mock 放大版才看出

### 预览
- 实际尺寸（48px）：见 `.workbuddy/generated-icons/preview_actual_size.png`
- 放大对比（150px）：见 `.workbuddy/generated-icons/preview_after_change.png`
