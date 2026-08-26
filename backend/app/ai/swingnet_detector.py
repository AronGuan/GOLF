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

        # argmax over 时间轴 -> 每个事件类取概率最大的帧；[:-1] 去掉背景类(第 9 类)
        events = np.argmax(probs, axis=0)[:-1]
        result: Dict[str, Dict] = {}
        for i, name in enumerate(EVENT_NAMES):
            frame_index = int(events[i])
            confidence = float(probs[frame_index, i])
            result[name] = {"frame_index": frame_index, "confidence": round(confidence, 6)}
        return result

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

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
