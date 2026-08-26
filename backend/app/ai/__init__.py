"""AI 能力包（SwingNet DTL 事件检测）。

M1：仅封装 SwingNet 为独立模块，**不接入 pipeline**（M2 才切换）。
face-on 机位保持规则引擎，**不调用** SwingNet。

本包 ``__init__`` 刻意不导入 :mod:`app.ai.swingnet_detector`，以保证
``import app.ai`` 不会连带加载 torch（torch 仅在 DTL 推理路径按需加载）。
"""

from __future__ import annotations
