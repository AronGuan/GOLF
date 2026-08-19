# 服务器诊断：骨架图实际尺寸

## 你看到的"前端侧躺"——排查步骤

用户报告：分析时视频正向（后端处理正确），但前端展示仍侧躺。
根因候选：后端生成的骨架图 PNG 文件本身就是侧躺（grab_frames 没有真正应用 rotation）。

## 一行诊断命令

SSH 进服务器，跑（改 `<task_id>` 为最新任务目录，参考上一行 `ls -lt` 的输出）：

```bash
cd /root/golf/GOLF/backend/data/tasks/<最新task_id>/
/root/anaconda3/envs/golf/bin/python -c "
import cv2, glob
for p in sorted(glob.glob('*.jpg')):
    img = cv2.imread(p)
    h, w = img.shape[:2]
    orient = '站正' if h > w else ('侧躺/横' if w > h else '方形')
    print(f'{p}: {w}x{h}  {orient}')
"
```

## 期望结果（修复正确）
每个 JPG 的尺寸应该是 **1080×1920**（横屏视频站正后），orientation = `站正`。

## 如果仍然是 1920×1080（侧躺）
说明 grab_frames 没真正应用 rotation——可能是：
- detect_backend_applied 在你服务器环境的 cv2 上判错
- 或者服务器进程用的是旧版代码（没重启）

## 进一步诊断（如果仍侧躺）

看服务器日志里有没有 "backend already applied EXIF rotation"：
```bash
sudo journalctl -u golf-backend -n 200 | grep -i "EXIF\|rotation"
```

如果看到日志说"已应用"——grab_frames 正确返回 raw（站正），但骨架图仍侧躺 → 是 renderer 写了侧躺帧（不是 grab_frames 问题）。
如果没看到日志——grab_frames 没探测/未旋转 → 是 grab_frames 没正确处理 orientation。

## 三种修法（按上面诊断结果对应）

| 骨架图实际 | 日志 | 根因 | 修法 |
|---|---|---|---|
| 1080×1920 站正 | — | 前端缓存或没刷新 | 强刷小程序 |
| 1920×1080 侧躺 | 有"already applied" | renderer 写了侧躺帧 | renderer 显式应用 rotation（加 rotate_frame）|
| 1920×1080 侧躺 | 没日志 | grab_frames 未旋转 | 检查 probe/grab_frames 是否真的调用 orientation |

把脚本输出 + 日志关键词贴回来，我给对症一行修法。