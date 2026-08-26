"""SwingNet 推理脚本（CPU 版，适配 backend_ai）。

用法：
    .venv/Scripts/python.exe scripts/infer.py <视频路径> [--weights models/swingnet_1800.pth.tar]

输出：8 事件帧号 + 置信度（Address/Toe-up/Mid-backswing/Top/Mid-downswing/Impact/Mid-follow-through/Finish）
"""
import argparse
import os
import sys

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms

# 把 golfdb_repo 加入 sys.path，导入 SwingNet 相关类
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "golfdb_repo")
sys.path.insert(0, REPO)

from model import EventDetector  # noqa: E402

EVENT_NAMES = [
    "Address",
    "Toe-up",
    "Mid-backswing",
    "Top",
    "Mid-downswing",
    "Impact",
    "Mid-follow-through",
    "Finish",
]


class ToTensor(object):
    def __call__(self, sample):
        images, labels = sample["images"], sample["labels"]
        images = images.transpose((0, 3, 1, 2))
        return {"images": torch.from_numpy(images).float().div(255.),
                "labels": torch.from_numpy(labels).long()}


class Normalize(object):
    def __init__(self, mean, std):
        self.mean = torch.tensor(mean, dtype=torch.float32)
        self.std = torch.tensor(std, dtype=torch.float32)

    def __call__(self, sample):
        images, labels = sample["images"], sample["labels"]
        images.sub_(self.mean[None, :, None, None]).div_(self.std[None, :, None, None])
        return {"images": images, "labels": labels}


class SampleVideo(Dataset):
    def __init__(self, path, input_size=160, transform=None):
        self.path = path
        self.input_size = input_size
        self.transform = transform
    def __len__(self):
        return 1

    def __getitem__(self, idx):
        cap = cv2.VideoCapture(self.path)
        frame_size = [cap.get(cv2.CAP_PROP_FRAME_HEIGHT), cap.get(cv2.CAP_PROP_FRAME_WIDTH)]
        ratio = self.input_size / max(frame_size)
        new_size = tuple([int(x * ratio) for x in frame_size])
        delta_w = self.input_size - new_size[1]
        delta_h = self.input_size - new_size[0]
        top, bottom = delta_h // 2, delta_h - (delta_h // 2)
        left, right = delta_w // 2, delta_w - (delta_w // 2)

        images = []
        for pos in range(int(cap.get(cv2.CAP_PROP_FRAME_COUNT))):
            ok, img = cap.read()
            if not ok:
                break
            resized = cv2.resize(img, (new_size[1], new_size[0]))
            b_img = cv2.copyMakeBorder(
                resized, top, bottom, left, right,
                cv2.BORDER_CONSTANT, value=[0.406 * 255, 0.456 * 255, 0.485 * 255],
            )
            b_img_rgb = cv2.cvtColor(b_img, cv2.COLOR_BGR2RGB)
            images.append(b_img_rgb)
        cap.release()
        labels = np.zeros(len(images))
        sample = {"images": np.asarray(images), "labels": np.asarray(labels)}
        if self.transform:
            sample = self.transform(sample)
        return sample


def infer(video_path, weights_path, seq_length=64):
    print(f"Preparing video: {video_path}")
    ds = SampleVideo(
        video_path,
        transform=transforms.Compose([
            ToTensor(),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]),
    )
    dl = DataLoader(ds, batch_size=1, shuffle=False, drop_last=False)

    model = EventDetector(pretrain=True, width_mult=1., lstm_layers=1,
                          lstm_hidden=256, bidirectional=True, dropout=False)

    if not os.path.exists(weights_path):
        print(f"权重不存在: {weights_path}")
        return
    save_dict = torch.load(weights_path, map_location="cpu")
    model.load_state_dict(save_dict["model_state_dict"])
    device = torch.device("cpu")
    model.to(device)
    model.eval()
    print("Loaded model weights")

    for sample in dl:
        images = sample["images"]
        batch = 0
        probs = None
        while batch * seq_length < images.shape[1]:
            if (batch + 1) * seq_length > images.shape[1]:
                image_batch = images[:, batch * seq_length:, :, :, :]
            else:
                image_batch = images[:, batch * seq_length:(batch + 1) * seq_length, :, :, :]
            logits = model(image_batch.to(device))
            cur = F.softmax(logits.data, dim=1).cpu().numpy()
            probs = cur if probs is None else np.append(probs, cur, 0)
            batch += 1

    events = np.argmax(probs, axis=0)[:-1]
    confidence = [float(probs[e, i]) for i, e in enumerate(events)]

    print("=" * 50)
    for i, e in enumerate(events):
        print(f"  {EVENT_NAMES[i]:28s} frame={int(e):5d}  conf={confidence[i]:.3f}")
    print("=" * 50)
    return events, confidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="视频路径")
    parser.add_argument("--weights", default="models/swingnet_1800.pth.tar")
    args = parser.parse_args()
    infer(args.path, args.weights)
