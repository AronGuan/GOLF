"""球杆 ONNX 推理器（生产环境用）。

架构：
    - 隔离 conda 环境 ``golfpose`` 把 GolfPose 的 PyTorch 模型导出为 ONNX
      （见 :file:`.workbuddy/export_golfpose_onnx.py`）
    - 生产环境（Python 3.12 + mediapipe + numpy 1.26）装 ``onnxruntime`` 即可推理，
      **不引入** mmdet/mmpose/mmcv（重框架会破坏 numpy 约束）
    - 与 swingnet_detector 同模式：懒加载 + 失败回退（错误返回 ``[]``）

功能：
    1. ``detect_club(image_bgr)``     YOLOX-s 2cls -> [{bbox, score}]  原图坐标
    2. ``detect_keypoints(image, bbox)``  HRNet-w48  -> [{name, x, y, score}]×5
    3. ``detect_full(image)``         检测+关键点联合 -> [{bbox, score, keypoints}]

IMPORTANT — 两个模型的预处理约定**恰好相反**，这是本项目最容易踩的坑（实测验证过）：

    ┌──────────────┬──────────────────────────────────────┬────────────────┐
    │ 模型         │ 输入变换                              │ 来源           │
    ├──────────────┼──────────────────────────────────────┼────────────────┤
    │ YOLOX 检测器 │ **恒等**：BGR 原始 0~255，不减均值     │ DetDataPreproc-│
    │              │ 不除方差、不转 RGB（config 未配       │ essor 默认     │
    │              │ mean/std/bgr_to_rgb，默认全 None）    │ mean=std=None  │
    │ HRNet 关键点 │ ImageNet 归一化 + **BGR→RGB**         │ PoseDataPrepro-│
    │              │ + 各向同性仿射 warp（非 letterbox）   │ cessor 显式配置│
    └──────────────┴──────────────────────────────────────┴────────────────┘

    检测器若误加 ImageNet 归一化，objectness 的 sigmoid 会全域塌成 ~1e-6，
    ``detect_club()`` 恒返回空列表（且不报错，极难排查）。
    验证脚本见 ``.workbuddy/diag_confirm_raw.py`` 与 ``.workbuddy/diag_pose_diff.py``。

后处理要点（参考 mmdet YOLOXHead / mmpose TopdownAffine + MSRAHeatmap 源码）：
    - YOLOX 用 **sigmoid** 激活 cls/objectness（非 softmax），score = cls * obj
    - bbox pred: cx,cy 是相对 grid 中心偏移（×stride），w/h 在 log 空间（需 exp）
    - grid 中心 = ``grid_x * stride``（``MlvlPointGenerator`` 默认 ``offset=0``，不加 0.5）
    - topdown 裁剪：bbox → center/scale(×1.25) → **按 w/h=0.75 修正纵横比**
      → 各向同性仿射 warp 到 288×384（缩放系数只由 bbox **宽度**决定）
    - heatmap 解码：argmax → ``sign(邻域差) * 0.25`` 亚像素精化 → ×4 到输入空间
      → ``/input_size * input_scale + center - 0.5*input_scale`` 回原图
    - ``flip_test=True``：原图与水平翻转图各推理一次，heatmap 翻转回正后取平均
"""
from __future__ import annotations

import logging
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from app import config

logger = logging.getLogger(__name__)

# === ONNX 模型输入尺寸（来自 config：作者训练时硬编码）===
DET_INPUT_SIZE: int = 640                                    # YOLOX-s 训练 640×640
POSE_INPUT_H: int = 384                                       # HRNet input H
POSE_INPUT_W: int = 288                                       # HRNet input W
HEATMAP_H: int = 96                                          # codec heatmap_h
HEATMAP_W: int = 72                                          # codec heatmap_w

# === 类别定义（来自 config/mmdet/golfpose_detector_2cls_yolox_s.py）===
#: 类别 id（与 config classes = ('person', 'club') 对齐）
CLS_PERSON: int = 0
CLS_CLUB: int = 1
NUM_CLASSES: int = 2

# === YOLOX 锚点 ===
YOLOX_STRIDES: Tuple[int, int, int] = (8, 16, 32)

# === 关键点 schema（来自 config/mmpose/_base_/datasets/golfswing_club.py）===
KP_NAMES: Tuple[str, ...] = ("shaft", "hosel", "heel", "toe_down", "toe_up")
SKELETON: Tuple[Tuple[int, int], ...] = ((0, 1), (1, 2), (2, 3), (3, 4))

# === 后处理超参 ===
SCORE_THR_DEFAULT: float = 0.30
NMS_IOU_THR: float = 0.65
TOPK_PER_LEVEL: int = 200
KP_THR_DEFAULT: float = 0.20
TOPDOWN_BBOX_SCALE: float = 1.25       # mmpose GetBBoxCenterScale 的 padding
#: 亚像素精化步长（mmpose refine_keypoints 里的固定值，非可配项）
KP_REFINE_STEP: float = 0.25

# === ImageNet 归一化（mmpose PoseDataPreprocessor：mean/std + bgr_to_rgb=True）===
#    ⚠️ 只给关键点模型用；检测器**不要**用（见模块 docstring 的对照表）。
IMAGENET_MEAN: Tuple[float, float, float] = (123.675, 116.28, 103.53)
IMAGENET_STD: Tuple[float, float, float] = (58.395, 57.12, 57.375)

# === 检测器 letterbox 填充值（mmdet Pad 默认 img pad_val=114）===
DET_PAD_VALUE: int = 114

# === 关键点 TTA（对齐 config test_cfg: flip_test=True, shift_heatmap=True）===
#: 是否做水平翻转测试时增强。对齐作者 config，默认开启。
#: ⚠️ golfswing_club 的 flip_indices 是**恒等排列** [0,1,2,3,4]，
#:    翻转平均会把左右不对称的球杆关键点横向平滑（实测约 2px 系统偏差）。
#:    若后续发现杆头点位偏软，可置 False 并回归验证。
CLUB_POSE_FLIP_TEST: bool = True
#: golfswing_club 数据集的 flip_indices（恒等排列：无左右对称点对）
CLUB_FLIP_INDICES: Tuple[int, ...] = (0, 1, 2, 3, 4)

# ⚠️ 关键坑（实测踩过）：mmdet 的 ``DetDataPreprocessor`` 默认 ``mean=None, std=None``，
#    **不做归一化、也不做 BGR→RGB**；本项目的检测器 config 没有覆盖它，
#    所以输入就是「pad 114 后的 BGR 原始 0~255 浮点」。
#    之前误加 ImageNet 归一化后，objectness 的 sigmoid 全域塌成 ~1e-6，
#    导致 ``detect_club()`` 恒返回 0 个框。详见 .workbuddy/diag_confirm_raw.py。


# ==============================================================================
# 纯 numpy 后处理工具
# ==============================================================================

def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> List[int]:
    """单类 NMS（boxes: (N, 4) xyxy in float32）"""
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: List[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        inds = np.where(iou <= iou_thr)[0]
        order = order[inds + 1]
    return keep


def _letterbox_params(src_h: int, src_w: int, dst: int) -> Tuple[int, int, float]:
    """返回 (new_h, new_w, scale)；无填充像素、仅等比缩放"""
    scale = dst / float(max(src_h, src_w))
    new_h = int(round(src_h * scale))
    new_w = int(round(src_w * scale))
    return new_h, new_w, scale


# ==============================================================================
# 主类
# ==============================================================================

class ClubOnnxDetector:
    """GolfPose ONNX 推理器（YOLOX 检测器 + HRNet 关键点）。

    Args:
        det_onnx_path: 检测器 ONNX 路径；缺省用 ``config.CLUB_DET_ONNX``。
        pose_onnx_path: 关键点 ONNX 路径；缺省用 ``config.CLUB_POSE_ONNX``。
        score_thr: 检测置信度阈值（默认 :data:`SCORE_THR_DEFAULT`）。
        nms_iou: NMS IoU 阈值（默认 :data:`NMS_IOU_THR`）。
        kp_thr: 关键点置信度阈值。
        flip_test: 关键点是否做水平翻转 TTA（默认 :data:`CLUB_POSE_FLIP_TEST`）。
    """

    def __init__(
        self,
        det_onnx_path: Optional[str] = None,
        pose_onnx_path: Optional[str] = None,
        score_thr: float = SCORE_THR_DEFAULT,
        nms_iou: float = NMS_IOU_THR,
        kp_thr: float = KP_THR_DEFAULT,
        flip_test: bool = CLUB_POSE_FLIP_TEST,
    ) -> None:
        self.det_onnx_path: str = str(
            det_onnx_path if det_onnx_path else getattr(config, 'CLUB_DET_ONNX', '')
        )
        self.pose_onnx_path: str = str(
            pose_onnx_path if pose_onnx_path else getattr(config, 'CLUB_POSE_ONNX', '')
        )
        self.score_thr: float = score_thr
        self.nms_iou: float = nms_iou
        self.kp_thr: float = kp_thr
        self.flip_test: bool = flip_test
        self._det_sess = None
        self._pose_sess = None
        self._det_input_name: str = 'input'
        self._pose_input_name: str = 'input'

    # ---------- 懒加载 ----------

    def _load_det(self) -> bool:
        if self._det_sess is not None:
            return True
        if not self.det_onnx_path or not os.path.exists(self.det_onnx_path):
            logger.warning('ClubOnnxDetector: 检测器 ONNX 不存在: %s', self.det_onnx_path)
            return False
        try:
            import onnxruntime as ort
            self._det_sess = ort.InferenceSession(
                self.det_onnx_path, providers=['CPUExecutionProvider']
            )
            self._det_input_name = self._det_sess.get_inputs()[0].name
            logger.info('ClubOnnxDetector: 检测器已加载 (onnxruntime)')
            return True
        except Exception as e:
            logger.warning('ClubOnnxDetector: 检测器加载失败: %s', e)
            return False

    def _load_pose(self) -> bool:
        if self._pose_sess is not None:
            return True
        if not self.pose_onnx_path or not os.path.exists(self.pose_onnx_path):
            logger.warning('ClubOnnxDetector: 关键点 ONNX 不存在: %s', self.pose_onnx_path)
            return False
        try:
            import onnxruntime as ort
            self._pose_sess = ort.InferenceSession(
                self.pose_onnx_path, providers=['CPUExecutionProvider']
            )
            self._pose_input_name = self._pose_sess.get_inputs()[0].name
            logger.info('ClubOnnxDetector: 关键点已加载 (onnxruntime)')
            return True
        except Exception as e:
            logger.warning('ClubOnnxDetector: 关键点加载失败: %s', e)
            return False

    @property
    def is_det_ready(self) -> bool:
        return bool(self.det_onnx_path) and os.path.exists(self.det_onnx_path)

    @property
    def is_pose_ready(self) -> bool:
        return bool(self.pose_onnx_path) and os.path.exists(self.pose_onnx_path)

    # ---------- 检测器：YOLOX-s 2cls ----------

    def detect_club(
        self,
        image_bgr: np.ndarray,
        score_thr: Optional[float] = None,
    ) -> List[Dict]:
        """检测球杆 bbox（原图坐标）。

        Args:
            image_bgr: H×W×3 BGR uint8 图像。
            score_thr: 置信度阈值；None 用 self.score_thr。

        Returns:
            ``[{"bbox": [x1,y1,x2,y2], "score": float}, ...]``，原图坐标。
            模型不可用/推理失败返回 ``[]``（由调用方决定回退规则法）。
        """
        if score_thr is None:
            score_thr = self.score_thr
        if not self._load_det():
            return []
        try:
            return self._run_yolox(image_bgr, score_thr)
        except Exception as e:
            logger.warning('ClubOnnxDetector.detect_club 失败: %s', e)
            return []

    def _run_yolox(self, image_bgr: np.ndarray, score_thr: float) -> List[Dict]:
        h0, w0 = image_bgr.shape[:2]
        # letterbox: 等比缩放到 640 内（不填 pad，因为 mmdet 测试 pipeline 用 keep_ratio+Pad_to_square）
        # 这里简化：等比缩放到 [nh, nw] 后 pad 到 640×640（右下角填 114）
        scale = DET_INPUT_SIZE / float(max(h0, w0))
        nh = max(1, int(round(h0 * scale)))
        nw = max(1, int(round(w0 * scale)))
        resized = cv2.resize(image_bgr, (nw, nh))
        canvas = np.full(
            (DET_INPUT_SIZE, DET_INPUT_SIZE, 3), DET_PAD_VALUE, dtype=np.uint8
        )
        canvas[:nh, :nw] = resized
        # ⚠️ 预处理（对齐 mmdet DetDataPreprocessor 的默认行为 = 恒等变换）：
        #    本检测器 config 未配 mean/std/bgr_to_rgb，因此**只做 uint8 → float32**，
        #    既不减均值也不除方差、更不转 RGB。
        #    误加 ImageNet 归一化会让 objectness sigmoid 全域塌成 ~0，检不出任何目标。
        x = canvas.astype(np.float32)
        x = x.transpose(2, 0, 1)[None]   # (1,3,640,640) BGR, 0~255

        outs = self._det_sess.run(None, {self._det_input_name: x})
        # ONNX 输出顺序（详见 export_golfpose_onnx.py 验证日志）：
        # outs[0] cls_score stride=8   (1, 2, 80, 80)
        # outs[1] cls_score stride=16  (1, 2, 40, 40)
        # outs[2] cls_score stride=32  (1, 2, 20, 20)
        # outs[3] bbox_pred stride=8   (1, 4, 80, 80)
        # outs[4] bbox_pred stride=16  (1, 4, 40, 40)
        # outs[5] bbox_pred stride=32  (1, 4, 20, 20)
        # outs[6] objectness stride=8  (1, 1, 80, 80)
        # outs[7] objectness stride=16 (1, 1, 40, 40)
        # outs[8] objectness stride=32 (1, 1, 20, 20)

        all_boxes: List[np.ndarray] = []
        all_scores: List[np.ndarray] = []
        for i, stride in enumerate(YOLOX_STRIDES):
            cls = outs[i][0]                # (2, H, W)
            bbox_pred = outs[i + 3][0]       # (4, H, W)
            obj = outs[i + 6][0]             # (1, H, W)
            boxes_i, scores_i = self._decode_yolox_level(
                cls, bbox_pred, obj, stride, score_thr
            )
            if boxes_i.shape[0] > 0:
                all_boxes.append(boxes_i)
                all_scores.append(scores_i)

        if not all_boxes:
            return []
        boxes = np.concatenate(all_boxes, axis=0)
        scores = np.concatenate(all_scores, axis=0)
        # 跨尺度 NMS
        keep = _nms(boxes, scores, self.nms_iou)
        # 反变换：letterbox 坐标 → 原图坐标
        inv_scale = 1.0 / scale
        results: List[Dict] = []
        for i in keep:
            x1, y1, x2, y2 = boxes[i]
            x1 *= inv_scale; x2 *= inv_scale
            y1 *= inv_scale; y2 *= inv_scale
            x1 = float(np.clip(x1, 0, w0 - 1))
            y1 = float(np.clip(y1, 0, h0 - 1))
            x2 = float(np.clip(x2, 0, w0 - 1))
            y2 = float(np.clip(y2, 0, h0 - 1))
            results.append({
                "bbox": [x1, y1, x2, y2],
                "score": float(scores[i]),
            })
        # 按 score 降序
        results.sort(key=lambda d: -d["score"])
        return results

    def _decode_yolox_level(
        self,
        cls: np.ndarray,
        bbox_pred: np.ndarray,
        obj: np.ndarray,
        stride: int,
        score_thr: float,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """单尺度 YOLOX decode → boxes (N, 4) xyxy, scores (N,)。"""
        # sigmoid 激活（YOLOX 不用 softmax）
        cls_sig = _sigmoid(cls)              # (2, H, W)
        obj_sig = _sigmoid(obj)[0]           # (H, W)
        # 取最大类 + id
        cls_max = cls_sig.max(axis=0)        # (H, W)
        cls_id = cls_sig.argmax(axis=0)      # (H, W)
        score = cls_max * obj_sig             # (H, W)
        # 仅保留 club
        mask = (cls_id == CLS_CLUB) & (score > score_thr)
        if not mask.any():
            return np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32)
        # ⚠️ YOLOX MlvlPointGenerator 默认 offset=0，
        # grid 中心 = grid_x * stride（不加 0.5）。错位会导致 bbox 偏移 4~16 像素。
        H, W = cls.shape[1], cls.shape[2]
        gy, gx = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
        cx_center = gx.astype(np.float32) * stride
        cy_center = gy.astype(np.float32) * stride
        # bbox pred 解码
        dx = bbox_pred[0]
        dy = bbox_pred[1]
        dw = bbox_pred[2]
        dh = bbox_pred[3]
        cx = dx * stride + cx_center
        cy = dy * stride + cy_center
        bw = np.exp(dw) * stride
        bh = np.exp(dh) * stride
        x1 = cx - bw / 2.0
        y1 = cy - bh / 2.0
        x2 = cx + bw / 2.0
        y2 = cy + bh / 2.0
        boxes = np.stack([x1, y1, x2, y2], axis=-1)
        boxes = boxes[mask]
        scores = score[mask]
        return boxes.astype(np.float32), scores.astype(np.float32)

    # ---------- 关键点：HRNet-w48 topdown ----------

    def detect_keypoints(
        self,
        image_bgr: np.ndarray,
        bbox: Sequence[float],
        kp_thr: Optional[float] = None,
    ) -> List[Dict]:
        """对 bbox 裁剪区域跑 HRNet，输出 5 关键点（原图坐标）。

        Args:
            image_bgr: 原图 BGR uint8。
            bbox: ``[x1, y1, x2, y2]`` 原图坐标。
            kp_thr: 关键点置信度阈值（用于过滤无效点；仅记录，不影响返回）。

        Returns:
            ``[{"name": str, "x": float, "y": float, "score": float}, ...]``，长度 5。
            模型不可用/bbox 异常返回 ``[]``。
        """
        if kp_thr is None:
            kp_thr = self.kp_thr
        if not self._load_pose():
            return []
        try:
            return self._run_hrnet(image_bgr, list(bbox), kp_thr)
        except Exception as e:
            logger.warning('ClubOnnxDetector.detect_keypoints 失败: %s', e)
            return []

    def _run_hrnet(
        self, image_bgr: np.ndarray, bbox: List[float], kp_thr: float
    ) -> List[Dict]:
        """复刻 mmpose topdown 推理全链路（逐步对齐 mmpose 1.3.1 源码）。

        链路：
            bbox → center/scale(×1.25) → 纵横比修正(w/h=0.75) → 各向同性仿射 warp
            → BGR→RGB + ImageNet 归一化 → 原图/翻转图各推理一次
            → heatmap 翻转回正后取平均 → argmax + 0.25 亚像素精化 → 回原图坐标
        """
        x1, y1, x2, y2 = [float(v) for v in bbox]
        if x2 <= x1 or y2 <= y1:
            return []

        # --- 1) bbox → center / scale（GetBBoxCenterScale: padding=1.25）---
        cx = (x1 + x2) * 0.5
        cy = (y1 + y2) * 0.5
        sw = (x2 - x1) * TOPDOWN_BBOX_SCALE
        sh = (y2 - y1) * TOPDOWN_BBOX_SCALE
        # TopdownAffine._fix_aspect_ratio：把 scale 修正到 w/h == 288/384
        aspect_ratio = POSE_INPUT_W / float(POSE_INPUT_H)      # 0.75
        if sw > sh * aspect_ratio:
            sh = sw / aspect_ratio
        else:
            sw = sh * aspect_ratio
        if sw <= 0 or sh <= 0:
            return []

        # --- 2) 各向同性仿射 warp（get_warp_matrix, fix_aspect_ratio=True）---
        #    ⚠️ 缩放系数只由 bbox **宽度** sw 决定（= POSE_INPUT_W / sw）；
        #       高度方向的覆盖范围由 384/288 被动决定 —— 这是**仿射 warp，不是 letterbox**。
        src = np.array(
            [[cx, cy], [cx - sw * 0.5, cy], [cx - sw * 0.5, cy + sw * 0.5]],
            dtype=np.float32,
        )
        dst = np.array(
            [
                [POSE_INPUT_W * 0.5, POSE_INPUT_H * 0.5],
                [0.0, POSE_INPUT_H * 0.5],
                [0.0, (POSE_INPUT_H + POSE_INPUT_W) * 0.5],
            ],
            dtype=np.float32,
        )
        warp_mat = cv2.getAffineTransform(src, dst)
        warped = cv2.warpAffine(
            image_bgr, warp_mat, (POSE_INPUT_W, POSE_INPUT_H),
            flags=cv2.INTER_LINEAR,
        )

        # --- 3) 归一化：BGR→RGB + ImageNet（PoseDataPreprocessor）---
        #    ⚠️ 必须显式 float32：``np.array(IMAGENET_MEAN)`` 默认是 float64，
        #       会把整个张量提升成 double，onnxruntime 直接报
        #       "Unexpected input data type. Actual: (tensor(double))"。
        mean = np.array(IMAGENET_MEAN, dtype=np.float32)
        std = np.array(IMAGENET_STD, dtype=np.float32)
        x = warped.astype(np.float32)[..., ::-1].copy()        # BGR -> RGB
        x = (x - mean) / std
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])   # (1,3,384,288)

        # --- 4) 推理（+ 水平翻转 TTA，对齐 test_cfg.flip_test）---
        hm = self._pose_sess.run(None, {self._pose_input_name: x})[0][0]  # (5,96,72)
        if self.flip_test:
            hm_f = self._pose_sess.run(
                None,
                {self._pose_input_name: np.ascontiguousarray(x[:, :, :, ::-1])},
            )[0][0]
            # flip_heatmaps：水平翻转 → 通道按 flip_indices 置换 → shift_heatmap 右移 1 列
            hm_f = hm_f[:, :, ::-1].copy()
            hm_f = hm_f[list(CLUB_FLIP_INDICES)]
            hm_f[:, :, 1:] = hm_f[:, :, :-1]
            hm = (hm + hm_f) * 0.5

        # --- 5) 解码：argmax + 0.25 亚像素精化（refine_keypoints）---
        sf_x = POSE_INPUT_W / float(HEATMAP_W)                 # 288/72 = 4
        sf_y = POSE_INPUT_H / float(HEATMAP_H)                 # 384/96 = 4
        n_kp = min(hm.shape[0], len(KP_NAMES))
        results: List[Dict] = []
        for k in range(n_kp):
            hm_k = hm[k]
            H, W = hm_k.shape
            yi, xi = np.unravel_index(int(hm_k.argmax()), (H, W))
            score = float(hm_k[yi, xi])
            if score <= 0.0:                                   # get_heatmap_maximum
                xi, yi = -1, -1
            # refine_keypoints：沿梯度符号方向向次大值移动 0.25 像素
            # （条件直接照抄 mmpose，x/y 边界判定不完全对称，勿"修正"）
            dx = (hm_k[yi, xi + 1] - hm_k[yi, xi - 1]
                  if (1 < xi < W - 1 and 0 < yi < H) else 0.0)
            dy = (hm_k[yi + 1, xi] - hm_k[yi - 1, xi]
                  if (1 < yi < H - 1 and 0 < xi < W) else 0.0)
            x_hm = xi + float(np.sign(dx)) * KP_REFINE_STEP
            y_hm = yi + float(np.sign(dy)) * KP_REFINE_STEP
            # heatmap 空间 → 模型输入空间（× scale_factor）
            x_in = x_hm * sf_x
            y_in = y_hm * sf_y
            # 模型输入空间 → 原图（TopdownPoseEstimator 的还原公式）
            x_orig = x_in / POSE_INPUT_W * sw + cx - 0.5 * sw
            y_orig = y_in / POSE_INPUT_H * sh + cy - 0.5 * sh
            results.append({
                "name": KP_NAMES[k],
                "x": float(x_orig),
                "y": float(y_orig),
                "score": score,
            })
        return results

    # ---------- 联合 ----------

    def detect_full(
        self,
        image_bgr: np.ndarray,
        score_thr: Optional[float] = None,
        kp_thr: Optional[float] = None,
    ) -> List[Dict]:
        """联合：检测 + 关键点（取 score 最高的 club bbox）"""
        if score_thr is None:
            score_thr = self.score_thr
        if kp_thr is None:
            kp_thr = self.kp_thr
        bboxes = self.detect_club(image_bgr, score_thr)
        if not bboxes:
            return []
        best = bboxes[0]   # 已按 score 降序
        kps = self.detect_keypoints(image_bgr, best["bbox"], kp_thr)
        return [{
            "bbox": best["bbox"],
            "score": best["score"],
            "keypoints": kps,
        }]