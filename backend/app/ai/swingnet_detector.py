"""SwingNet 8 事件检测器封装（DTL 专用，face-on 不调用）。

把 GolfDB 官方 SwingNet 的推理逻辑封装成可直接调用的类：

- **懒加载**：模型与 60MB 权重在首次 :meth:`SwingNetDetector.detect` 时才加载，
  ``import app.ai.swingnet_detector`` 只引入 torch 模块、不读权重文件。
- **无 torchvision 依赖**：backend 便携环境只装了 torch/cv2/numpy，POC 里
  依赖的 ``torchvision.transforms.ToTensor/Normalize`` 在此内联为等价实现。
- **错误语义**：输入非法（不存在/不可解码/无帧）抛 ``ValueError``，权重缺失抛
  ``FileNotFoundError``，由调用方（M2 的 pipeline）决定是否回退规则引擎。

事件名采用 GolfDB 原始命名：Address / Toe-up / Mid-backswing / Top /
Mid-downswing / Impact / Mid-follow-through / Finish。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from app import config
from app.ai.swingnet_model import EventDetector

#: GolfDB 8 事件原始命名（顺序即挥杆时序）。
EVENT_NAMES: List[str] = [
    "Address",
    "Toe-up",
    "Mid-backswing",
    "Top",
    "Mid-downswing",
    "Impact",
    "Mid-follow-through",
    "Finish",
]

#: 模型输入短边尺寸（GolfDB 训练时的 160×160 输入）。
INPUT_SIZE: int = 160

#: 每次前向送入 LSTM 的帧数（POC 默认，控制内存占用）。
SEQ_LENGTH: int = 64

#: ImageNet 归一化参数（RGB 顺序，与 POC 完全一致）。
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


class SwingNetDetector:
    """SwingNet 8 事件检测器。

    Args:
        weights_path: 权重文件路径；缺省用 :data:`config.SWINGNET_WEIGHTS_PATH`
            （可用 ``GOLF_SWINGNET_WEIGHTS`` 环境变量覆盖）。
        device: 推理设备，默认 ``"cpu"``。
    """

    def __init__(self, weights_path: Optional[str] = None, device: str = "cpu"):
        self.weights_path: str = str(weights_path) if weights_path else str(config.SWINGNET_WEIGHTS_PATH)
        self.device: str = device
        self._model: Optional[EventDetector] = None

    @property
    def is_loaded(self) -> bool:
        """模型是否已加载（懒加载状态标记）。"""
        return self._model is not None

    def detect(self, video_path: str) -> Dict[str, Dict]:
        """检测视频的 8 个事件帧，返回 ``{事件名: {"frame_index", "confidence"}}``。

        Args:
            video_path: 本地视频文件路径（DTL 侧面机位）。

        Returns:
            8 事件 -> 帧号与置信度的映射；事件名见 :data:`EVENT_NAMES`。

        Raises:
            ValueError: 视频不存在 / 不可解码 / 无可读帧。
            FileNotFoundError: 权重文件缺失。
        """
        self._validate_video(video_path)
        self._load_model()
        images = self._read_and_transform(video_path)  # (1, T, C, H, W)
        total_frames = images.shape[1]

        probs: Optional[np.ndarray] = None
        with torch.no_grad():
            batch = 0
            while batch * SEQ_LENGTH < total_frames:
                start = batch * SEQ_LENGTH
                end = min((batch + 1) * SEQ_LENGTH, total_frames)
                image_batch = images[:, start:end, :, :, :]
                logits = self._model(image_batch.to(self.device))
                cur = F.softmax(logits, dim=1).cpu().numpy()
                probs = cur if probs is None else np.append(probs, cur, axis=0)
                batch += 1

        if probs is None:
            return {}

        return self._constrained_events(probs)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _constrained_events(probs: np.ndarray) -> Dict[str, Dict]:
        """从逐帧概率 ``probs`` 解析 8 事件，采用「锚点约束 + 区间重定位」。

        ``probs`` 形状 ``(T, 9)``：第 0~7 类对应 :data:`EVENT_NAMES`，第 8 类为
        背景。历史实现对每个事件类**独立全视频 argmax**（无时序约束），导致
        过渡事件（Toe-up / Mid-backswing / Mid-downswing / Mid-follow-through）
        被视频首尾的静止段或重复姿态假峰抢走——实测 11.mp4 上 Toe-up 取到
        帧 17（视频开头）、Mid-backswing 取到帧 100（与 Address 同帧），8 事件
        乱序触发 pipeline 单调守卫、整体回退到更差的规则引擎。

        本方法改为：

        1. 先取 4 个主锚点（Address / Top / Impact / Finish）的全局 argmax——
           GolfDB 模型对主事件的区分度远高于过渡事件，锚点可信；
        2. 若锚点严格递增，则把 4 个过渡事件**限制在锚点区间内**重定位 argmax
           （Toe-up ∈ (Address, Top)、Mid-backswing ∈ (Toe-up, Top)、
           Mid-downswing ∈ (Top, Impact)、Mid-follow-through ∈ (Impact, Finish)）；
        3. 对最终帧号做严格递增强制（相邻过渡事件 argmax 到同一帧时后推 1 帧），
           保证物理时序，避免 pipeline 单调守卫误回退；
        4. 若锚点本身乱序（多段挥杆 / 非单次挥杆），保持全视频 argmax 原结果，
           交由调用方（pipeline 单调守卫）回退规则引擎。

        Returns:
            8 事件 -> ``{"frame_index", "confidence"}`` 映射，事件名见
            :data:`EVENT_NAMES`。锚点乱序时返回的帧号可能不单调，由调用方兜底。
        """
        n_frames = probs.shape[0]

        # 全视频 argmax（锚点乱序时的回退结果）
        global_events = np.argmax(probs, axis=0)[:-1]

        def _make(frames) -> Dict[str, Dict]:
            out: Dict[str, Dict] = {}
            for i, name in enumerate(EVENT_NAMES):
                fi = int(frames[i])
                out[name] = {
                    "frame_index": fi,
                    "confidence": round(float(probs[fi, i]), 6),
                }
            return out

        # 4 个主锚点全局 argmax（EVENT_NAMES 下标：Address=0, Top=3, Impact=5, Finish=7）
        a_frame = int(global_events[0])
        t_frame = int(global_events[3])
        i_frame = int(global_events[5])
        f_frame = int(global_events[7])

        if not (a_frame < t_frame < i_frame < f_frame):
            # 锚点乱序：模型在此视频上不可信，返回原始结果，由 pipeline 回退。
            return _make(global_events)

        def _argmax_in(cls_idx: int, lo_excl: int, hi_excl: int) -> int:
            """开区间 ``(lo_excl, hi_excl)`` 内的峰值帧；退化时 clamp 到单帧。"""
            lo = lo_excl + 1
            hi = hi_excl - 1
            if hi < lo:  # 锚点相邻、无中间帧 -> 退化为闭区间 [lo_excl, hi_excl]
                lo, hi = lo_excl, hi_excl
            lo = max(0, lo)
            hi = min(n_frames - 1, hi)
            if hi < lo:
                hi = lo
            return int(np.argmax(probs[lo : hi + 1, cls_idx])) + lo

        toe_up = _argmax_in(1, a_frame, t_frame)        # Toe-up ∈ (Address, Top)
        mid_back = _argmax_in(2, toe_up, t_frame)       # Mid-backswing ∈ (Toe-up, Top)
        mid_down = _argmax_in(4, t_frame, i_frame)      # Mid-downswing ∈ (Top, Impact)
        mid_follow = _argmax_in(6, i_frame, f_frame)    # Mid-follow-through ∈ (Impact, Finish)

        frames = [
            a_frame, toe_up, mid_back, t_frame,
            mid_down, i_frame, mid_follow, f_frame,
        ]
        # 严格递增强制：相邻过渡事件 argmax 到同一帧时后推 1 帧（竖屏 DTL 上
        # Toe-up 与 Mid-backswing 区分度低，可能同帧），保证 8 事件严格递增。
        for k in range(1, len(frames)):
            if frames[k] <= frames[k - 1]:
                frames[k] = frames[k - 1] + 1
        frames = [min(f, n_frames - 1) for f in frames]

        return _make(frames)

    def _load_model(self) -> None:
        """懒加载模型与权重（仅首次调用时执行）。"""
        if self._model is not None:
            return
        if not os.path.isfile(self.weights_path):
            raise FileNotFoundError(f"SwingNet 权重不存在: {self.weights_path}")

        model = EventDetector(
            pretrain=True,
            width_mult=1.0,
            lstm_layers=1,
            lstm_hidden=256,
            bidirectional=True,
            dropout=False,
        )
        save_dict = torch.load(self.weights_path, map_location=self.device)
        model.load_state_dict(save_dict["model_state_dict"])
        model.to(self.device)
        model.eval()
        self._model = model

    @staticmethod
    def _validate_video(video_path: str) -> None:
        """校验视频存在且可解码。"""
        if not video_path:
            raise ValueError("video_path 不能为空")
        if not os.path.isfile(video_path):
            raise ValueError(f"视频文件不存在: {video_path}")

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            cap.release()
            raise ValueError(f"视频无法解码: {video_path}")
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if frame_count <= 0:
            raise ValueError(f"视频无可读帧: {video_path}")

    def _read_and_transform(self, video_path: str) -> torch.Tensor:
        """读取视频帧并按 POC 预处理，返回 ``(1, T, C, 160, 160)`` float 张量。

        与 GolfDB 官方 ``test_video.py`` 的 ``SampleVideo`` 逐帧一致：
        等比缩放短边到 160 → 用 ImageNet 均值填充到 160×160 → BGR→RGB →
        转 float 张量并归一化。``torchvision.transforms`` 被内联为等价实现。
        """
        cap = cv2.VideoCapture(video_path)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        ratio = INPUT_SIZE / max(height, width)
        new_w = int(width * ratio)
        new_h = int(height * ratio)
        delta_w = INPUT_SIZE - new_w
        delta_h = INPUT_SIZE - new_h
        top, bottom = delta_h // 2, delta_h - (delta_h // 2)
        left, right = delta_w // 2, delta_w - (delta_w // 2)
        # BGR 顺序的 ImageNet 均值（填充时帧仍是 BGR）
        border_value = [int(0.406 * 255), int(0.456 * 255), int(0.485 * 255)]

        frames: List[np.ndarray] = []
        while True:
            ok, img = cap.read()
            if not ok:
                break
            resized = cv2.resize(img, (new_w, new_h))
            padded = cv2.copyMakeBorder(
                resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=border_value
            )
            frames.append(cv2.cvtColor(padded, cv2.COLOR_BGR2RGB))
        cap.release()

        if not frames:
            raise ValueError(f"视频无可读帧: {video_path}")

        # 等价 torchvision.transforms.ToTensor：HWC -> CHW 且 /255
        arr = np.asarray(frames)  # (T, H, W, 3)
        tensor = torch.from_numpy(arr.transpose(0, 3, 1, 2)).float().div_(255.0)
        # 等价 torchvision.transforms.Normalize（RGB 顺序）
        mean = torch.tensor(IMAGENET_MEAN, dtype=torch.float32)
        std = torch.tensor(IMAGENET_STD, dtype=torch.float32)
        tensor.sub_(mean[None, :, None, None]).div_(std[None, :, None, None])
        return tensor.unsqueeze(0)
