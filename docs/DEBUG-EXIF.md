# 在 ECS 服务器上跑这一行（一次性诊断，30 秒）
cd /root/golf/GOLF/backend
/root/anaconda3/envs/golf/bin/python -c "
import cv2, sys
sys.path.insert(0, '.')
from app.pose_extractor import probe_video

p = '/root/.../2026-08-19_125638_118.mp4'  # 改为你实际的视频绝对路径

# 1. 后端 metadata 读的原始宽高
cap = cv2.VideoCapture(p)
print('CAP_PROP_FRAME_WIDTH x HEIGHT =', int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), 'x', int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
print('CAP_PROP_ORIENTATION_META   =', cap.get(cv2.CAP_PROP_ORIENTATION_META))
# 2. 首帧真实 shape（最关键：判断 cv2 是否自动应用了 EXIF）
ok, frame = cap.read()
print('首帧 shape (h, w) =', frame.shape[:2] if ok else 'read fail')
cap.release()

# 3. probe_video 转正后的 dims（看代码层是否互换了）
m = probe_video(p)
print('probe meta dims =', m.width, 'x', m.height, '  orientation=', m.orientation)
"