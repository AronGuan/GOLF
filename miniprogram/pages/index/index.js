/**
 * 首页 / 上传页。
 *
 * 职责：机位二选一 + 拍摄引导（随机位切换）+ wx.chooseMedia 选视频 +
 *       本地三项校验 + 上传（带 camera_view 与百分比）-> navigateTo 分析中页。
 */
const api = require('../../utils/api.js');

/** 时长下限（秒） */
const MIN_DURATION = 2;
/** 时长上限（秒） */
const MAX_DURATION = 20;
/** 大小上限（字节） */
const MAX_SIZE = 40 * 1024 * 1024;

/** 机位定义（互斥二选一，v2） */
const VIEWS = {
  face_on: {
    key: 'face_on',
    label: '正面机位',
    sub: '正对身体拍摄 · 竖持手机',
    icon: '🙋',
    requirements: [
      '正面机位：镜头正对身体（面向你）',
      '手机竖持固定，不要手持晃动',
      '全身入镜，头顶与球杆均不出画',
      '距离 2~3 米',
      '时长 2~20 秒，只拍一次挥杆',
      '建议 60fps 以上（拍摄设置）'
    ]
  },
  down_the_line: {
    key: 'down_the_line',
    label: '侧面机位',
    sub: '垂直于目标线拍摄 · 横持手机',
    icon: '🏌️',
    requirements: [
      '侧面机位：镜头垂直于目标线',
      '球手侧面面对镜头（右肩侧朝向镜头）',
      '手机横持固定，保持水平不倾斜',
      '球杆与目标线在画面中清晰可见',
      '全身入镜，距离 2~3 米',
      '时长 2~20 秒，只拍一次挥杆'
    ]
  }
};

Page({
  data: {
    views: VIEWS,
    /** 当前选中机位 key */
    cameraView: 'face_on',
    requirements: VIEWS.face_on.requirements,
    /** @type {object|null} 已选视频信息 */
    video: null,
    valid: false,
    errorText: '',
    canSubmit: false,
    uploading: false,
    uploadPercent: 0
  },

  onLoad() {
    // 恢复上次选择的机位（跨页面保持）
    const app = getApp();
    const saved = app.globalData.cameraView;
    if (saved && VIEWS[saved]) {
      this.setData({
        cameraView: saved,
        requirements: VIEWS[saved].requirements
      });
    }
  },

  onShow() {
    // 从结果页 redirect 回来时重置上传态，避免按钮卡在「上传中」
    if (this.data.uploading) {
      this.setData({ uploading: false, uploadPercent: 0 });
      this._refreshSubmit();
    }
  },

  /** 切换机位（互斥二选一） */
  onSelectView(e) {
    const key = e.currentTarget.dataset.view;
    if (!VIEWS[key] || key === this.data.cameraView) {
      return;
    }
    getApp().globalData.cameraView = key;
    this.setData({ cameraView: key, requirements: VIEWS[key].requirements });
  },

  /** 现场拍摄 */
  onShoot() {
    this._chooseMedia(['camera']);
  },

  /** 相册选择 */
  onAlbum() {
    this._chooseMedia(['album']);
  },

  /**
   * 调起 wx.chooseMedia。
   * @param {string[]} sourceType
   * @private
   */
  _chooseMedia(sourceType) {
    if (this.data.uploading) {
      return;
    }
    wx.chooseMedia({
      count: 1,
      mediaType: ['video'],
      sourceType,
      maxDuration: MAX_DURATION,
      camera: 'back',
      success: (res) => {
        const file = (res.tempFiles || [])[0];
        if (!file) {
          return;
        }
        this._applyFile(file);
      },
      fail: (err) => {
        if (err && err.errMsg && err.errMsg.indexOf('cancel') >= 0) {
          return;
        }
        wx.showToast({ title: '选择视频失败，请重试', icon: 'none' });
      }
    });
  },

  /**
   * 校验并写入已选视频。
   * @param {object} file wx.chooseMedia 返回的 tempFile
   * @private
   */
  _applyFile(file) {
    const path = file.tempFilePath || '';
    const size = file.size || 0;
    const duration = file.duration || 0;
    const name = path.split('/').pop() || 'video.mp4';

    const check = this._validate(path, size, duration);
    this.setData({
      video: {
        path,
        name,
        size,
        duration,
        durationText: duration ? duration.toFixed(1) + 's' : '未知',
        sizeText: (size / 1024 / 1024).toFixed(1) + 'MB'
      },
      valid: check.ok,
      errorText: check.reason
    });
    this._refreshSubmit();
  },

  /**
   * 本地三项校验：时长 / 大小 / 格式（v2 放开 .mov）。
   * @param {string} path
   * @param {number} size
   * @param {number} duration
   * @return {{ok:boolean, reason:string}}
   * @private
   */
  _validate(path, size, duration) {
    const lower = (path || '').toLowerCase();
    const clean = lower.split('?')[0];
    const okExt = clean.endsWith('.mp4') || clean.endsWith('.mov');
    if (!okExt) {
      return { ok: false, reason: '只支持 mp4 / mov 格式的视频，请重新选择' };
    }
    if (!duration || duration < MIN_DURATION) {
      return { ok: false, reason: '视频太短了，请拍摄 2~20 秒的完整挥杆' };
    }
    if (duration > MAX_DURATION) {
      return { ok: false, reason: '视频超过 20 秒，请重新拍摄一段更短的挥杆' };
    }
    if (size > MAX_SIZE) {
      return { ok: false, reason: '视频大小超过 40MB，请降低画质后重试' };
    }
    return { ok: true, reason: '' };
  },

  /** 刷新提交按钮可用态 @private */
  _refreshSubmit() {
    this.setData({
      canSubmit: !!(this.data.video && this.data.valid && !this.data.uploading)
    });
  },

  /** 开始分析：上传并跳转 */
  onSubmit() {
    if (!this.data.canSubmit) {
      return;
    }
    const filePath = this.data.video.path;
    const cameraView = this.data.cameraView;
    this.setData({ uploading: true, uploadPercent: 0, canSubmit: false });

    api
      .uploadVideo(filePath, cameraView, (percent) => {
        this.setData({ uploadPercent: percent });
      })
      .then((data) => {
        const app = getApp();
        app.globalData.taskId = data.task_id;
        app.globalData.result = null;
        this.setData({ uploading: false, uploadPercent: 100 });
        this._refreshSubmit();
        wx.navigateTo({ url: '/pages/analyzing/analyzing?task_id=' + data.task_id });
      })
      .catch((error) => {
        this.setData({ uploading: false, uploadPercent: 0 });
        this._refreshSubmit();
        wx.showModal({
          title: '上传失败',
          content: (error && error.message) || '请稍后重试',
          showCancel: true,
          cancelText: '取消',
          confirmText: '重试',
          success: (res) => {
            if (res.confirm) {
              this.onSubmit();
            }
          }
        });
      });
  }
});
