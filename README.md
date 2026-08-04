# 高尔夫挥杆姿态分析 MVP

微信小程序上传挥杆视频 → Python 后端 MediaPipe 姿态分析 → 8 阶段切分 → 每阶段输出姿态指标。

参考 GolfSwings / AI Golf 的交互模式，MVP 目标是跑通「上传 → 分析 → 8 阶段结果展示」最小闭环。

---

## 一、环境约束（务必遵守，否则必然踩坑）

| 项 | 要求 |
|---|---|
| **Python 解释器** | 必须用项目自带的便携版：`E:\project\golf\.tools\python312\python.exe`（Python 3.12.9，embeddable 发行版，无 venv） |
| **MediaPipe 版本** | 锁定 `mediapipe==0.10.14`，**只用 legacy API `mp.solutions.pose`** |
| **严禁** | 不要使用系统 Python；不要升级到 MediaPipe 1.x；不要使用 `mediapipe.tasks` / `PoseLandmarker` / 下载 `.task` 模型（国内不可达，会卡死） |
| **NumPy** | 必须 `<2`（0.10.14 不兼容 numpy 2.x） |
| **其他依赖** | `opencv-python-headless`、`fastapi`、`uvicorn[standard]`、`python-multipart` |

> 为什么这么死板：Python 3.13 上的 MediaPipe wheel 是精简包（只有 `tasks` 子模块、无内置模型），1.0.0 已移除 legacy solutions。只有 **3.12 + 0.10.14** 完整 wheel 内置 `pose_landmark_full.tflite`，零外网依赖。这套组合已实测验证。

---

## 二、目录结构

```
E:\project\golf\
├─ backend\              # Python 后端（FastAPI + MediaPipe）
│  ├─ app\              # 业务代码（config/geometry/pose_extractor/segmenter/reference/metrics/renderer/pipeline/schemas/task_store/main）
│  ├─ run.py            # 统一启动入口（serve / segment / check）
│  ├─ requirements.txt  # 依赖锁定
│  └─ tests\            # pytest 测试（205 用例，已全部通过）
├─ miniprogram\         # 原生微信小程序
│  ├─ app.js/json/wxss
│  ├─ project.config.json   # 已设 urlCheck:false
│  ├─ utils/api.js      # BASE_URL 指向 http://127.0.0.1:8000
│  └─ pages\{index,analyzing,result}\
├─ docs\
│  ├─ PRD.md            # 产品需求文档
│  └─ ARCHITECTURE.md   # 架构设计与任务分解（含 8 阶段切分算法）
└─ .tools\python312\    # 便携 Python 3.12.9 环境
```

---

## 三、后端启动

打开终端（Git Bash / PowerShell 均可），**全部使用便携 Python**：

```bash
cd E:\project\golf\backend

# ① 依赖自检：确认 mediapipe=0.10.14、numpy<2
E:\project\golf\.tools\python312\python.exe run.py check

# ② 启动服务（阻塞运行，保持终端开启）
E:\project\golf\.tools\python312\python.exe run.py serve --host 127.0.0.1 --port 8000
```

等价写法（uvicorn 自带 `--app-dir` 会插入当前目录）：

```bash
E:\project\golf\.tools\python312\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## 四、验证后端

### ① 冒烟测试（health）
浏览器或 curl 访问：

```
http://127.0.0.1:8000/api/v1/health
```

预期返回：

```json
{"code":0,"data":{"status":"ok","mediapipe":"0.10.14"},"message":"ok"}
```

### ② 失败路径（错误码 + 中文文案）
上传任意非挥杆 / 过短 / 非 mp4 视频，例如项目内的测试素材 `E:\project\golf\.tools\_probe\t.mp4`（灰度渐变 1.0s）：

- `POST /api/v1/tasks` 上传 → 返回 `201` + `task_id`
- 轮询 `GET /api/v1/tasks/{task_id}` → 终态 `failed`
- `error_code` 为 `BAD_VIDEO` / `NO_PERSON` / `NO_SWING` / `TOO_DARK` 之一，并带**中文 error_message**

> 已实跑验证：`t.mp4` → `failed` / `BAD_VIDEO` / "视频无法解析，请换一段 mp4 视频重试"。

### ③ 成功路径（需真实挥杆视频）
先单独看切分是否合理（不依赖小程序）：

```bash
E:\project\golf\.tools\python312\python.exe run.py segment "你的挥杆视频.mp4"
```

会打印 8 个阶段的帧号 / 时间戳。确认切分没跑偏后，再走小程序完整闭环。

**当前缺口**：项目暂未内置真实挥杆视频，端到端「成功路径」需要你提供一段真实素材（建议 60fps、2–15s、右手球手、正面机位）才能看到 result 页的 8 阶段指标。

---

## 五、小程序端（微信开发者工具）

1. 打开**微信开发者工具** → 导入项目 → 目录选 `E:\project\golf\miniprogram`
2. 项目已设 `urlCheck=false`（关闭域名校验），无需配置合法域名
3. 确认 `utils/api.js` 中 `BASE_URL = 'http://127.0.0.1:8000'`
4. 进入 **index 页** → 拍摄 / 选择挥杆视频 → 自动上传 → **analyzing 页**轮询进度 → **result 页**查看 8 阶段缩略图 + 大图 + 指标卡

---

## 六、两个硬限制

- **真机不支持 http 明文**：微信真机必须 HTTPS 或内网穿透。MVP 以**开发者工具 + 127.0.0.1** 验收为准，真机部署不在本期范围。
- **需要真实挥杆视频**才能看到分析结果：没有它，目前只能验证「失败路径」和「合成数据全链路」，result 页的 8 阶段指标出不来。

---

## 七、接口速查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查，返回 mediapipe 版本 |
| POST | `/api/v1/tasks` | 上传视频（multipart `file`），创建分析任务 |
| GET | `/api/v1/tasks/{task_id}` | 轮询任务状态（status/progress/step/error_code/error_message） |
| GET | `/api/v1/tasks/{task_id}/result` | 获取完整分析结果（8 阶段指标 + 全程指标 + 图片 URL） |
| GET | `/static/{task_id}/{NN}_{key}.jpg` | 访问各阶段骨架叠加图（01_address.jpg … 08_finish.jpg） |

统一响应包：`{"code":0,"data":{...},"message":"ok"}`；code：0 成功 / 4001 参数非法 / 4004 任务不存在 / 4009 任务未完成 / 5000 内部错误。

---

## 八、运行测试

```bash
cd E:\project\golf\backend
E:\project\golf\.tools\python312\python.exe -m pytest tests/ -q
```

全部用例使用合成数据，不依赖真实视频、不下载任何模型。当前 205 个用例通过，核心算法覆盖率 ≈90%。
