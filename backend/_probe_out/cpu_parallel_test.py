# -*- coding: utf-8 -*-
"""实测：MediaPipe pose 推理在单进程多线程下能否用满多核（GIL 是否释放）。
串行 4 次 vs ThreadPoolExecutor(4) 并行，对比总耗时。
若并行 ≈ 串行/4 → 用满多核；若并行 ≈ 串行 → 单核限制。"""
import time
import numpy as np
import mediapipe as mp
from concurrent.futures import ThreadPoolExecutor

N_FRAMES = 300          # 每任务推理帧数
N_TASKS = 4             # 任务数
H, W = 320, 240         # 与压测视频同分辨率


def _rgb_frame(i: int) -> np.ndarray:
    # 与真实视频类似的彩色帧（RGB，MediaPipe 输入格式）
    return np.ones((H, W, 3), np.uint8) * (i % 255)


def run_one(seed: int) -> float:
    """单任务：独立 Pose 实例推理 N_FRAMES 帧，返回耗时。"""
    pose = mp.solutions.pose.Pose(
        static_image_mode=False,
        model_complexity=1,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    t0 = time.perf_counter()
    for i in range(N_FRAMES):
        pose.process(_rgb_frame(i + seed))
    pose.close()
    return time.perf_counter() - t0


def main():
    # 预热（模型加载 + 首次推理）
    run_one(0)
    print(f"核数={os_cpu()}  N_FRAMES={N_FRAMES}  N_TASKS={N_TASKS}")

    # 串行
    t0 = time.perf_counter()
    ser = [run_one(100 + i) for i in range(N_TASKS)]
    t_serial = time.perf_counter() - t0
    print(f"[串行] 总耗时={t_serial:.2f}s  单任务均={sum(ser)/N_TASKS:.2f}s")

    # 并行（4 线程）
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=N_TASKS) as ex:
        par = list(ex.map(run_one, range(200, 200 + N_TASKS)))
    t_parallel = time.perf_counter() - t0
    print(f"[并行] 总耗时={t_parallel:.2f}s  单任务均={sum(par)/N_TASKS:.2f}s")

    speedup = t_serial / t_parallel
    print(f"\n加速比 = {speedup:.2f}x")
    if speedup >= N_TASKS * 0.7:
        print("结论: 用满多核（推理释放 GIL，真并行）✅")
    elif speedup >= 1.5:
        print("结论: 部分并行（有 GIL 争用或系统开销）⚠️")
    else:
        print("结论: 单核限制（推理未释放 GIL，串行）❌")


def os_cpu():
    import os
    return os.cpu_count()


if __name__ == "__main__":
    main()
