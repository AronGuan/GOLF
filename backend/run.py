"""本地启动 / 调试统一入口。

为什么需要这个文件：
    项目使用的便携版 Python（embeddable 发行版，带 ``python312._pth``）运行在
    隔离模式下——**当前工作目录不会被加入 ``sys.path``**，``PYTHONPATH`` 也被忽略。
    因此 ``python -m app.segmenter <video>`` 会报 ``No module named 'app'``。
    本脚本先把自身所在目录（即 ``backend/``）插入 ``sys.path``，再分发子命令，
    保证在任意 Python 发行版下命令行为一致。

用法::

    python run.py serve [--host 127.0.0.1] [--port 8000] [--reload]
    python run.py segment <video_path>
    python run.py check            # 依赖 & 版本自检

``uvicorn`` 自带 ``--app-dir .``（默认值）会自行插入当前目录，所以
``python -m uvicorn app.main:app`` 也可以正常工作，两种方式等价。
"""

from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _cmd_serve(args: argparse.Namespace) -> int:
    """启动 FastAPI 服务。"""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=BASE_DIR,
        log_level="info",
    )
    return 0


def _cmd_segment(args: argparse.Namespace) -> int:
    """对单个视频跑「抽帧 + 切分」，打印 8 阶段定位结果。"""
    from app.segmenter import run_cli

    return run_cli(args.video)


def _cmd_check(args: argparse.Namespace) -> int:
    """打印关键依赖版本，确认环境符合约束。"""
    import cv2
    import mediapipe
    import numpy

    from app import config

    print(f"python        = {sys.version.split()[0]}")
    print(f"mediapipe     = {mediapipe.__version__} (expect {config.MEDIAPIPE_VERSION})")
    print(f"numpy         = {numpy.__version__} (expect <2)")
    print(f"opencv        = {cv2.__version__}")
    print(f"ROTATION_SIGN = {config.ROTATION_SIGN}")
    print(f"TARGET_DIR_X  = {config.TARGET_DIR_X}")
    ok = mediapipe.__version__ == config.MEDIAPIPE_VERSION and numpy.__version__ < "2"
    print("RESULT        =", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="run.py", description="高尔夫挥杆分析后端入口")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=_cmd_serve)

    p_seg = sub.add_parser("segment", help="对视频跑切分自检")
    p_seg.add_argument("video", help="视频文件路径")
    p_seg.set_defaults(func=_cmd_segment)

    p_check = sub.add_parser("check", help="依赖版本自检")
    p_check.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
