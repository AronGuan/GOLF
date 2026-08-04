"""golf_swing_analyzer 后端应用包。

分层管道架构::

    API 层(main)  ->  编排层(pipeline)
                      -> 能力层(pose_extractor / segmenter / metrics / renderer)
                         -> 工具层(geometry / reference / config / schemas)
"""

__version__ = "1.0.0"
