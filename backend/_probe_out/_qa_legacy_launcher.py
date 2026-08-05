
import sys, os
sys.path.insert(0, r"E:\project\golf\backend")
from app import config
config.API_CODE_STYLE = "legacy"   # 线上回滚开关：切常量
from app.main import app
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=8014, log_level="warning")
