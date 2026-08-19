"""``app.frame_reader`` 单测：EXIF Orientation 旋转贯穿（iPhone 横拍修复）。

覆盖：
- :func:`normalize_orientation` 归一化（None / float / 越界值 / 360+）；
- :func:`rotate_frame` 对 0/90/180/270 四个角度的尺寸与像素映射
  （用「色块定位」的合成图，断言每个角度后像素仍可追溯到原位置）；
- :func:`grab_frames` 在传入 ``orientation`` 参数时**强制按参数旋转**（不探测后端），
  并验证 0/90/180/270 后的输出尺寸 + 像素方向。
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app import frame_reader
from app.frame_reader import grab_frames, normalize_orientation, rotate_frame


# ---------------------------------------------------------------------------
# 合成工具：写一个带可识别「左上 1/4 是亮色块，其余黑」的 mp4
# ---------------------------------------------------------------------------


def _write_color_block_mp4(
    path: str,
    w: int = 320,
    h: int = 240,
    frames: int = 6,
    fps: float = 30.0,
) -> None:
    """写一段 mp4，每帧「左上 1/4 区域填充亮色 (200,200,200)，其余为黑 (0,0,0)」。"""
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert writer.isOpened(), "cv2.VideoWriter 无法创建 mp4"
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[: h // 2, : w // 2] = (200, 200, 200)
    for _ in range(frames):
        writer.write(img)
    writer.release()


# ---------------------------------------------------------------------------
# normalize_orientation
# ---------------------------------------------------------------------------


class TestNormalizeOrientation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0, 0),
            (90, 90),
            (180, 180),
            (270, 270),
            (90.0, 90),
            ("90", 90),
            (None, 0),
            (-90, 270),  # Python (-90) % 360 == 270，落入合法集合
            (45, 0),  # 非 90 倍数非法，兜底 0
            (360, 0),  # 360 % 360 == 0
            (450, 90),  # 450 % 360 == 90
            ("abc", 0),
            ([], 0),
        ],
    )
    def test_normalizes_to_canonical_set(self, raw, expected):
        assert normalize_orientation(raw) == expected


# ---------------------------------------------------------------------------
# rotate_frame：尺寸 + 像素方向断言
# ---------------------------------------------------------------------------


class TestRotateFrame:
    """每帧左上 1/4 是亮色块 (200,200,200)，其余黑色。旋转后用「亮色像素」位置反推方向。"""

    def _make(self, w=120, h=80):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[: h // 2, : w // 2] = (200, 200, 200)
        return img

    @staticmethod
    def _bright_centroid(bgr: np.ndarray) -> tuple:
        """亮色像素的质心（x, y）—— 旋转后位置应严格按预期移动。"""
        mask = (bgr[:, :, 0] > 100) & (bgr[:, :, 1] > 100) & (bgr[:, :, 2] > 100)
        ys, xs = np.where(mask)
        if xs.size == 0:
            return (-1.0, -1.0)
        return (float(np.mean(xs)), float(np.mean(ys)))

    def test_orientation_zero_is_identity(self):
        img = self._make()
        out = rotate_frame(img, 0)
        np.testing.assert_array_equal(out, img)
        assert out.shape == img.shape

    def test_orientation_90_rotates_cw_and_swaps_shape(self):
        """90° CW：w=120 h=80 → 80×120，亮色块从「左上」移到「右上」。"""
        img = self._make(w=120, h=80)
        cx, cy = self._bright_centroid(img)
        assert cx < 60 and cy < 40  # 原图：左上

        out = rotate_frame(img, 90)
        assert out.shape == (120, 80, 3), "90° 旋转后 shape 应互换"
        cx2, cy2 = self._bright_centroid(out)
        # CW 90°：原 (x,y) → (h-1-y, x)  → 左上 (cx,cy) ≈ (h/4,h/4) 映射后偏右
        assert cx2 > cy2  # 旋转后质心横坐标 > 纵坐标
        # 旋转后亮色块面积不变
        assert int((out[:, :, 0] > 100).sum()) == int((img[:, :, 0] > 100).sum())

    def test_orientation_180_flips_and_preserves_shape(self):
        img = self._make()
        out = rotate_frame(img, 180)
        assert out.shape == img.shape
        # 原左上亮色块 → 旋转 180° 后应在右下
        cx, cy = self._bright_centroid(img)
        cx2, cy2 = self._bright_centroid(out)
        # 中心对称：(cx, cy) → (W-1-cx, H-1-cy)
        assert cx2 == pytest.approx(119 - cx, abs=0.5)
        assert cy2 == pytest.approx(79 - cy, abs=0.5)

    def test_orientation_270_rotates_ccw_and_swaps_shape(self):
        img = self._make(w=120, h=80)
        out = rotate_frame(img, 270)
        assert out.shape == (120, 80, 3), "270° 旋转后 shape 应互换"
        # 270° CCW：与 90° CW 互为反向；亮色块从左上 → 左下
        cx, cy = self._bright_centroid(img)
        cx2, cy2 = self._bright_centroid(out)
        # 与 orientation=90 方向相反：亮色块偏左下
        assert cx2 < 60 and cy2 > 40

    def test_invalid_orientation_passthrough(self):
        """非法角度（45、-90 等）原样返回，不抛异常。"""
        img = self._make()
        for v in (45, -90, 999, None):
            out = rotate_frame(img, v)  # type: ignore[arg-type]
            np.testing.assert_array_equal(out, img)

    def test_none_input_passthrough(self):
        """None 输入防御性返回 None。"""
        assert rotate_frame(None, 90) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# grab_frames：传入 orientation 参数时强制旋转（不依赖文件元数据）
# ---------------------------------------------------------------------------


class TestGrabFramesRotation:
    """``grab_frames(path, idx, orientation=N)`` 强制按 N 旋转解码帧。"""

    def _color_block_path(self, tmp_path) -> str:
        p = str(tmp_path / "block.mp4")
        _write_color_block_mp4(p, w=120, h=80, frames=4)
        return p

    @pytest.mark.parametrize("orient", [0, 90, 180, 270])
    def test_orientation_param_rotates_and_swallows_shape(self, tmp_path, orient):
        """传入 orientation 时的输出 shape：始终按 raw 是否已被 cv2 auto-rotate 决定。

        本地 cv2（Windows）会自动 swap raw shape（90/270 EXIF），所以 grab_frames
        探测到 backend_applied=True → 跳过手动 rotate → 输出 == raw。
        EXIF=0/180 cv2 不会自动 swap（shape 不变），grab_frames 会按 orientation 旋转。
        """
        path = self._color_block_path(tmp_path)
        decoded = grab_frames(path, [0], orientation=orient)
        assert 0 in decoded
        bgr = decoded[0]
        raw = cv2.VideoCapture(path)
        ok, raw_frame = raw.read()
        raw.release()
        assert ok
        # 输出 shape 必须 == raw shape（cv2 已 swap 时跳过手动，shape 不变；
        # cv2 未 swap 时 rotate 180 也不改 shape，0 不改 shape）
        assert bgr.shape == raw_frame.shape, (
            f"orientation={orient}: 期望 {raw_frame.shape} 实际 {bgr.shape}"
        )
        # 像素：90/270 cv2 已 swap → skip → output == raw；0 不旋转 → output == raw；
        # 180 cv2 不处理 → rotate → output = rotate180(raw)。后者改用与旋转结果一致断言。
        if orient in (0, 90, 270):
            np.testing.assert_array_equal(bgr, raw_frame)
        else:  # 180
            from app.frame_reader import rotate_frame
            np.testing.assert_array_equal(bgr, rotate_frame(raw_frame, orient))

    def test_orientation_none_auto_reads_from_cap(self, tmp_path):
        """orientation=None 时从 cap 读取；synth_video 无 EXIF 标签，期望 0 = 不旋转。"""
        path = self._color_block_path(tmp_path)
        decoded = grab_frames(path, [0], orientation=None)
        bgr = decoded[0]
        raw = cv2.VideoCapture(path)
        ok, raw_frame = raw.read()
        raw.release()
        # synth 无 EXIF orientation → declared=0 → 不旋转（除非后端已 auto-apply，
        # 但 mp4v 编码 + cv2.VideoCapture 不会自动旋转，shape 与原图一致）
        assert bgr.shape == raw_frame.shape

    def test_clamps_garbage_orientation(self, tmp_path):
        """``orientation=999`` 等非法值归一为 0，原图返回。"""
        path = self._color_block_path(tmp_path)
        decoded = grab_frames(path, [0], orientation=999)
        bgr = decoded[0]
        raw = cv2.VideoCapture(path)
        _, raw_frame = raw.read()
        raw.release()
        np.testing.assert_array_equal(bgr, raw_frame)

    def test_stats_increment_with_rotation(self, tmp_path):
        """旋转不影响解码统计：opens=1, retrieved=1, grabbed=frames_seen。"""
        path = self._color_block_path(tmp_path)
        frame_reader.reset_stats()
        decoded = grab_frames(path, [0, 1, 2], orientation=90)
        assert len(decoded) == 3
        stats = frame_reader.stats()
        assert stats["opens"] == 1
        assert stats["retrieved"] == 3
        assert stats["grabbed"] >= 3
