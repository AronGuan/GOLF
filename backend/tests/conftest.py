"""pytest 全局夹具。

两件必须在**任何 app 模块导入之前**完成的事：

1. 把 ``backend/`` 插入 ``sys.path``——便携版 Python（embeddable 发行版，带
   ``python312._pth``）运行在隔离模式，当前工作目录不会自动进 ``sys.path``
   （与 ``run.py`` 同样的处理）。
2. 覆写 ``GOLF_DATA_DIR`` 到临时目录——``app.config`` 在 import 期就读该环境变量
   并据此挂载 ``/static``、初始化 ``task_store``，测试不得污染 ``backend/data``。

此外提供一套**手工构造**的合成挥杆序列（不依赖 MediaPipe、不读真实视频），
用于 segmenter / metrics 的纯函数验证。
"""

from __future__ import annotations

import atexit
import math
import os
import shutil
import sys
import tempfile
from typing import List, Sequence, Tuple

# --- 1. 路径与环境（必须在 import app 之前）---------------------------------

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="golf_qa_data_")
os.environ["GOLF_DATA_DIR"] = _TMP_DATA_DIR
os.environ.setdefault("GOLF_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
atexit.register(shutil.rmtree, _TMP_DATA_DIR, ignore_errors=True)

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from app import geometry  # noqa: E402
from app.schemas import FrameLandmarks, VideoMeta  # noqa: E402

# --- 2. 合成挥杆参数 ---------------------------------------------------------

FPS: float = 30.0
DURATION: float = 4.0
N_FRAMES: int = int(DURATION * FPS)  # 120
VIDEO_W: int = 480
VIDEO_H: int = 854

#: 世界坐标 -> 归一化图像坐标的线性投影（正面机位近似）
SCENE_W: float = 2.0
SCENE_H: float = 2.6
Y0: float = 0.55  # 双髋中点在图像中的 y

HALF_SHOULDER: float = 0.20  # 半肩宽（米）-> 肩宽 0.40m -> 图像肩宽 0.20
HALF_HIP: float = 0.15

#: 关键时刻（秒）：站位结束 / 顶点 / 击球 / 收杆定格
T_ADDRESS_END = 0.70
T_TOP = 1.45
T_IMPACT = 1.80
T_FINISH = 2.40

# 躯干水平转角（度，绕铅垂轴）。rotation_xz * ROTATION_SIGN(-1) 后
# 「背对目标为正」，故上杆期给负值 -> 肩转输出为正。
K_TH_S = [(0.0, 0.0), (T_ADDRESS_END, 0.0), (T_TOP, -78.0), (T_IMPACT, -6.0),
          (2.05, 40.0), (T_FINISH, 92.0), (DURATION, 92.0)]
K_TH_H = [(0.0, 0.0), (T_ADDRESS_END, 0.0), (T_TOP, -52.0), (T_IMPACT, 18.0),
          (2.05, 48.0), (T_FINISH, 84.0), (DURATION, 84.0)]

# 双手（引导腕附近）世界坐标
K_HAND = [
    (0.0, (0.12, 0.12, -0.40)),
    (T_ADDRESS_END, (0.12, 0.12, -0.40)),
    (T_TOP, (-0.42, -0.88, -0.06)),
    (T_IMPACT, (0.10, 0.14, -0.42)),
    (T_FINISH, (0.42, -0.92, 0.02)),
    (DURATION, (0.42, -0.92, 0.02)),
]
# 整体水平位移（向目标 = 图像 +x）
K_SHIFT_X = [(0.0, 0.0), (T_ADDRESS_END, 0.0), (T_TOP, -0.010), (T_IMPACT, 0.060),
             (2.05, 0.090), (T_FINISH, 0.110), (DURATION, 0.110)]
# 双肩中点相对双髋中点的 z / x 偏移（决定脊柱前倾 / 侧倾）
K_SPINE_Z = [(0.0, -0.380), (T_ADDRESS_END, -0.380), (T_TOP, -0.360),
             (T_IMPACT, -0.330), (2.05, -0.240), (T_FINISH, -0.100),
             (DURATION, -0.100)]
K_SPINE_X = [(0.0, 0.0), (T_ADDRESS_END, 0.0), (T_TOP, -0.050), (T_IMPACT, -0.100),
             (2.05, -0.140), (T_FINISH, -0.100), (DURATION, -0.100)]
K_LEAD_ARM = [(0.0, 172.0), (T_ADDRESS_END, 172.0), (T_TOP, 160.0),
              (T_IMPACT, 170.0), (2.05, 166.0), (T_FINISH, 152.0),
              (DURATION, 152.0)]
K_TRAIL_ARM = [(0.0, 168.0), (T_ADDRESS_END, 168.0), (T_TOP, 108.0),
               (T_IMPACT, 142.0), (2.05, 164.0), (T_FINISH, 160.0),
               (DURATION, 160.0)]


def _smoothstep(u: float) -> float:
    """S 形缓动，保证速度曲线连续（真实挥杆没有速度阶跃）。"""
    return u * u * (3.0 - 2.0 * u)


def _interp(t: float, keys: Sequence[Tuple[float, object]]):
    """带缓动的关键帧插值，支持标量与三元组。"""
    if t <= keys[0][0]:
        return keys[0][1]
    if t >= keys[-1][0]:
        return keys[-1][1]
    for i in range(len(keys) - 1):
        t0, v0 = keys[i]
        t1, v1 = keys[i + 1]
        if t0 <= t <= t1:
            u = _smoothstep((t - t0) / max(1e-9, t1 - t0))
            if isinstance(v0, tuple):
                return tuple(a + (b - a) * u for a, b in zip(v0, v1))
            return v0 + (v1 - v0) * u
    return keys[-1][1]


def _joint_for_angle(a: np.ndarray, c: np.ndarray, angle_deg: float) -> np.ndarray:
    """在 a-c 之间放一个中间关节，使 ``∠(a, joint, c) == angle_deg``。"""
    a = np.asarray(a, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    chord = c - a
    length = float(np.linalg.norm(chord))
    if length < 1e-6:
        return (a + c) / 2.0 + np.array([0.0, 0.0, 0.05])
    offset = (length / 2.0) * math.tan(math.radians((180.0 - angle_deg) / 2.0))
    for hint in ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)):
        perp = np.cross(chord, np.array(hint, dtype=np.float64))
        if float(np.linalg.norm(perp)) > 1e-6:
            perp = perp / float(np.linalg.norm(perp))
            return (a + c) / 2.0 + perp * offset
    return (a + c) / 2.0


def build_pose(t: float) -> Tuple[np.ndarray, np.ndarray]:
    """构造 t 时刻的 ``(world(33,3), norm(33,3))``。

    world 约定（架构文档 §10.2）：原点 = 双髋中点，x 右+ / y 下+ / z 远离相机+。
    """
    th_s = math.radians(float(_interp(t, K_TH_S)))
    th_h = math.radians(float(_interp(t, K_TH_H)))
    hand = np.array(_interp(t, K_HAND), dtype=np.float64)
    shift_x = float(_interp(t, K_SHIFT_X))
    spine_z = float(_interp(t, K_SPINE_Z))
    spine_x = float(_interp(t, K_SPINE_X))
    lead_angle = float(_interp(t, K_LEAD_ARM))
    trail_angle = float(_interp(t, K_TRAIL_ARM))

    world = np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64)
    v_s = np.array([math.cos(th_s), 0.0, math.sin(th_s)]) * HALF_SHOULDER
    v_h = np.array([math.cos(th_h), 0.0, math.sin(th_h)]) * HALF_HIP
    hip_mid = np.zeros(3)
    sh_mid = np.array([spine_x, -0.55, spine_z])

    world[geometry.L_HIP] = hip_mid + v_h
    world[geometry.R_HIP] = hip_mid - v_h
    world[geometry.L_SHOULDER] = sh_mid + v_s
    world[geometry.R_SHOULDER] = sh_mid - v_s
    world[geometry.NOSE] = sh_mid + np.array([0.0, -0.28, -0.06])

    side = v_s / max(1e-9, float(np.linalg.norm(v_s)))
    world[geometry.L_WRIST] = hand + side * 0.03
    world[geometry.R_WRIST] = hand - side * 0.03
    world[geometry.L_ELBOW] = _joint_for_angle(
        world[geometry.L_SHOULDER], world[geometry.L_WRIST], lead_angle
    )
    world[geometry.R_ELBOW] = _joint_for_angle(
        world[geometry.R_SHOULDER], world[geometry.R_WRIST], trail_angle
    )

    world[geometry.L_ANKLE] = np.array([0.22, 0.92, 0.0])
    world[geometry.R_ANKLE] = np.array([-0.22, 0.92, 0.0])
    world[geometry.L_KNEE] = _joint_for_angle(
        world[geometry.L_HIP], world[geometry.L_ANKLE], 168.0
    )
    world[geometry.R_KNEE] = _joint_for_angle(
        world[geometry.R_HIP], world[geometry.R_ANKLE], 168.0
    )

    # 面部 / 手指 / 脚掌等非核心点：贴到最近的核心点旁，避免整列为 0
    for idx in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
        world[idx] = world[geometry.NOSE] + np.array([0.02 * (idx - 5), -0.01, 0.0])
    for idx in (17, 19, 21):
        world[idx] = world[geometry.L_WRIST] + np.array([0.03, 0.03, 0.0])
    for idx in (18, 20, 22):
        world[idx] = world[geometry.R_WRIST] + np.array([-0.03, 0.03, 0.0])
    for idx in (29, 31):
        world[idx] = world[geometry.L_ANKLE] + np.array([0.03, 0.05, -0.06])
    for idx in (30, 32):
        world[idx] = world[geometry.R_ANKLE] + np.array([-0.03, 0.05, -0.06])

    norm = np.zeros((geometry.NUM_LANDMARKS, 3), dtype=np.float64)
    norm[:, 0] = 0.5 + (world[:, 0] + shift_x) / SCENE_W
    norm[:, 1] = Y0 + world[:, 1] / SCENE_H
    norm[:, 2] = world[:, 2]
    return world, norm


def make_swing_frames(
    n: int = N_FRAMES, fps: float = FPS, step: int = 1
) -> List[FrameLandmarks]:
    """完整挥杆：站位 -> 上杆 -> 顶点 -> 下杆 -> 击球 -> 送杆 -> 收杆。"""
    frames: List[FrameLandmarks] = []
    for k in range(n):
        raw_index = k * step
        t = raw_index / fps
        world, norm = build_pose(t)
        frames.append(
            FrameLandmarks(
                frame_index=raw_index,
                timestamp=t,
                detected=True,
                norm=norm,
                world=world,
                visibility=np.full(geometry.NUM_LANDMARKS, 0.95),
            )
        )
    return frames


def make_still_frames(n: int = N_FRAMES, fps: float = FPS) -> List[FrameLandmarks]:
    """静止站立：所有帧都取 t=0 的姿态（PRD AC-10 用例）。"""
    world, norm = build_pose(0.0)
    return [
        FrameLandmarks(
            frame_index=k,
            timestamp=k / fps,
            detected=True,
            norm=norm.copy(),
            world=world.copy(),
            visibility=np.full(geometry.NUM_LANDMARKS, 0.95),
        )
        for k in range(n)
    ]


# --- 3. 夹具 -----------------------------------------------------------------


@pytest.fixture(scope="session")
def swing_frames() -> List[FrameLandmarks]:
    """合成的完整挥杆序列（120 帧 @30fps）。"""
    return make_swing_frames()


@pytest.fixture(scope="session")
def still_frames() -> List[FrameLandmarks]:
    """静止站立序列。"""
    return make_still_frames()


@pytest.fixture(scope="session")
def video_meta() -> VideoMeta:
    """与合成序列匹配的视频元信息。"""
    return VideoMeta(
        fps=FPS,
        duration=DURATION,
        width=VIDEO_W,
        height=VIDEO_H,
        frame_count=N_FRAMES,
        sample_step=1,
        low_fps=False,
    )


@pytest.fixture(scope="session")
def synth_video(tmp_path_factory) -> str:
    """把合成骨架画成 mp4，供 probe/亮度/renderer 第二趟解码使用。

    背景刻意画亮（灰度 ≈ 75），避开 ``TOO_DARK`` 阈值 40。
    """
    import cv2

    out_dir = tmp_path_factory.mktemp("synth_video")
    path = str(out_dir / "swing.mp4")
    writer = cv2.VideoWriter(
        path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (VIDEO_W, VIDEO_H)
    )
    assert writer.isOpened(), "cv2.VideoWriter 无法创建 mp4（缺少 mp4v 编码器）"
    for k in range(N_FRAMES):
        _, norm = build_pose(k / FPS)
        img = np.full((VIDEO_H, VIDEO_W, 3), (70, 80, 70), dtype=np.uint8)
        pts = {
            i: (int(norm[i, 0] * VIDEO_W), int(norm[i, 1] * VIDEO_H))
            for i in range(geometry.NUM_LANDMARKS)
        }
        for a, b in geometry.SKELETON_EDGES:
            cv2.line(img, pts[a], pts[b], (200, 200, 200), 4, cv2.LINE_AA)
        for i in geometry.CORE_IDS:
            cv2.circle(img, pts[i], 5, (240, 240, 240), -1, cv2.LINE_AA)
        writer.write(img)
    writer.release()
    assert os.path.getsize(path) > 0
    return path


@pytest.fixture()
def api_client():
    """FastAPI TestClient（不起 uvicorn 进程）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client
