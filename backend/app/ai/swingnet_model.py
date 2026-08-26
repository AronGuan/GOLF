"""SwingNet 模型定义（自 GolfDB 官方仓库 ``wmcnally/golfdb`` 搬入，保留 CPU 适配）。

GolfDB 预训练的 SwingNet 结构：MobileNetV2 backbone 提取逐帧视觉特征，
再接一层双向 LSTM 建模时序，输出 9 类（8 事件 + 1 个"无事件"背景类）
逐帧 logits。

搬入时保留两处 CPU 适配：
1. ``EventDetector.__init__`` 中 ``mobilenet_v2.pth.tar`` 冗余加载容错
   （该权重不存在时静默跳过，只用于可选 backbone 预训练）；
2. ``EventDetector.init_hidden`` 显式接收 ``device`` 参数（原实现硬编码
   ``.cuda()``，CPU 推理会失败）。

模块级仅定义网络结构，**不加载任何权重**（权重由
:mod:`app.ai.swingnet_detector` 懒加载）。
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn
from torch.autograd import Variable


def conv_bn(inp: int, oup: int, stride: int) -> nn.Sequential:
    """3×3 卷积 + BN + ReLU6。"""
    return nn.Sequential(
        nn.Conv2d(inp, oup, 3, stride, 1, bias=False),
        nn.BatchNorm2d(oup),
        nn.ReLU6(inplace=True),
    )


def conv_1x1_bn(inp: int, oup: int) -> nn.Sequential:
    """1×1 卷积 + BN + ReLU6。"""
    return nn.Sequential(
        nn.Conv2d(inp, oup, 1, 1, 0, bias=False),
        nn.BatchNorm2d(oup),
        nn.ReLU6(inplace=True),
    )


class InvertedResidual(nn.Module):
    """MobileNetV2 倒残差块。"""

    def __init__(self, inp: int, oup: int, stride: int, expand_ratio: int):
        super(InvertedResidual, self).__init__()
        self.stride = stride
        assert stride in [1, 2]

        hidden_dim = round(inp * expand_ratio)
        self.use_res_connect = self.stride == 1 and inp == oup

        if expand_ratio == 1:
            self.conv = nn.Sequential(
                # depthwise
                nn.Conv2d(hidden_dim, hidden_dim, 3, stride, 1, groups=hidden_dim, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6(inplace=True),
                # pointwise-linear
                nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
                nn.BatchNorm2d(oup),
            )
        else:
            self.conv = nn.Sequential(
                # pointwise
                nn.Conv2d(inp, hidden_dim, 1, 1, 0, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6(inplace=True),
                # depthwise
                nn.Conv2d(hidden_dim, hidden_dim, 3, stride, 1, groups=hidden_dim, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6(inplace=True),
                # pointwise-linear
                nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
                nn.BatchNorm2d(oup),
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.use_res_connect:
            return x + self.conv(x)
        return self.conv(x)


class MobileNetV2(nn.Module):
    """MobileNetV2 backbone（torchvision 移植实现）。

    参考 https://github.com/tonylins/pytorch-mobilenet-v2
    """

    def __init__(self, n_class: int = 1000, input_size: int = 224, width_mult: float = 1.0):
        super(MobileNetV2, self).__init__()
        block = InvertedResidual
        min_depth = 16
        input_channel = 32
        last_channel = 1280
        interverted_residual_setting = [
            # t, c, n, s
            [1, 16, 1, 1],
            [6, 24, 2, 2],
            [6, 32, 3, 2],
            [6, 64, 4, 2],
            [6, 96, 3, 1],
            [6, 160, 3, 2],
            [6, 320, 1, 1],
        ]

        # building first layer
        assert input_size % 32 == 0
        input_channel = int(input_channel * width_mult) if width_mult >= 1.0 else input_channel
        self.last_channel = int(last_channel * width_mult) if width_mult > 1.0 else last_channel
        self.features = [conv_bn(3, input_channel, 2)]
        # building inverted residual blocks
        for t, c, n, s in interverted_residual_setting:
            output_channel = max(int(c * width_mult), min_depth)
            for i in range(n):
                if i == 0:
                    self.features.append(block(input_channel, output_channel, s, expand_ratio=t))
                else:
                    self.features.append(block(input_channel, output_channel, 1, expand_ratio=t))
                input_channel = output_channel
        # building last several layers
        self.features.append(conv_1x1_bn(input_channel, self.last_channel))
        # make it nn.Sequential
        self.features = nn.Sequential(*self.features)

        # building classifier
        self.classifier = nn.Sequential(
            nn.Dropout(0.2),
            nn.Linear(self.last_channel, n_class),
        )

        self._initialize_weights()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        x = x.mean(3).mean(2)
        x = self.classifier(x)
        return x

    def _initialize_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2.0 / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                n = m.weight.size(1)
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()


class EventDetector(nn.Module):
    """SwingNet 事件检测器：MobileNetV2（截断） + BiLSTM -> 9 类 logits。

    截断点 ``[:19]`` 取到 MobileNetV2 features 的最后一个 1×1 层（1280 通道
    全局均值池化前的输出），与 GolfDB 预训练权重逐层对齐。
    """

    def __init__(
        self,
        pretrain: bool,
        width_mult: float,
        lstm_layers: int,
        lstm_hidden: int,
        bidirectional: bool = True,
        dropout: bool = True,
    ):
        super(EventDetector, self).__init__()
        self.width_mult = width_mult
        self.lstm_layers = lstm_layers
        self.lstm_hidden = lstm_hidden
        self.bidirectional = bidirectional
        self.dropout = dropout

        net = MobileNetV2(width_mult=width_mult)
        try:
            state_dict_mobilenet = torch.load("mobilenet_v2.pth.tar")
        except FileNotFoundError:
            state_dict_mobilenet = None
        if pretrain and state_dict_mobilenet is not None:
            net.load_state_dict(state_dict_mobilenet)

        self.cnn = nn.Sequential(*list(net.children())[0][:19])
        self.rnn = nn.LSTM(
            int(1280 * width_mult if width_mult > 1.0 else 1280),
            self.lstm_hidden,
            self.lstm_layers,
            batch_first=True,
            bidirectional=bidirectional,
        )
        if self.bidirectional:
            self.lin = nn.Linear(2 * self.lstm_hidden, 9)
        else:
            self.lin = nn.Linear(self.lstm_hidden, 9)
        if self.dropout:
            self.drop = nn.Dropout(0.5)

    def init_hidden(self, batch_size: int, device: torch.device) -> tuple:
        """构造 LSTM 初始隐状态（显式落到 ``device``，CPU 可跑）。"""
        if self.bidirectional:
            return (
                Variable(
                    torch.zeros(2 * self.lstm_layers, batch_size, self.lstm_hidden, device=device),
                    requires_grad=True,
                ),
                Variable(
                    torch.zeros(2 * self.lstm_layers, batch_size, self.lstm_hidden, device=device),
                    requires_grad=True,
                ),
            )
        return (
            Variable(
                torch.zeros(self.lstm_layers, batch_size, self.lstm_hidden, device=device),
                requires_grad=True,
            ),
            Variable(
                torch.zeros(self.lstm_layers, batch_size, self.lstm_hidden, device=device),
                requires_grad=True,
            ),
        )

    def forward(self, x: torch.Tensor, lengths: torch.Tensor = None) -> torch.Tensor:
        batch_size, timesteps, C, H, W = x.size()
        self.hidden = self.init_hidden(batch_size, x.device)

        # CNN forward
        c_in = x.view(batch_size * timesteps, C, H, W)
        c_out = self.cnn(c_in)
        c_out = c_out.mean(3).mean(2)
        if self.dropout:
            c_out = self.drop(c_out)

        # LSTM forward
        r_in = c_out.view(batch_size, timesteps, -1)
        r_out, states = self.rnn(r_in, self.hidden)
        out = self.lin(r_out)
        out = out.view(batch_size * timesteps, 9)

        return out
