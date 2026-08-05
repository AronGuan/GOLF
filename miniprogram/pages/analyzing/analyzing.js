/**
 * 分析中页。
 *
 * 1.5s 轮询任务状态；成功 redirectTo 结果页；失败展示中文原因 + 重拍入口；
 * 累计 120s（80 次）未完成前端主动判超时。
 */
const api = require('../../utils/api.js');

/** 轮询间隔（毫秒） */
const POLL_INTERVAL = 1500;
/** 最大轮询次数（1500ms × 80 = 120s） */
const MAX_POLLS = 80;

/** 4 个步骤的静态定义（v2：step4 文案 = 「计算姿态指标与风险」） */
const STEP_DEFS = [
  { id: 1, index: '①', name: '上传完成' },
  { id: 2, index: '②', name: '提取身体关键点' },
  { id: 3, index: '③', name: '识别 8 个挥杆阶段' },
  { id: 4, index: '④', name: '计算姿态指标与风险' }
];

Page({
  data: {
    taskId: '',
    progress: 0,
    step: 1,
    message: '正在排队...',
    steps: [],
    etaText: '',
    failed: false,
    errorText: ''
  },

  /** @type {number} 定时器 id @private */
  _timer: 0,
  /** @type {number} 已轮询次数 @private */
  _polls: 0,
  /** @type {number} 页面进入时间戳 @private */
  _startedAt: 0,

  onLoad(options) {
    const taskId = (options && options.task_id) || getApp().globalData.taskId || '';
    this._startedAt = Date.now();

    if (!taskId) {
      this._fail('任务不存在，请重新拍摄上传');
      return;
    }

    this.setData({ taskId, steps: this._buildSteps(1, 0) });
    this._poll();
    this._timer = setInterval(() => this._poll(), POLL_INTERVAL);
  },

  onUnload() {
    this._clear();
  },

  onHide() {
    // 页面隐藏时暂停轮询，避免后台空转
    this._clear();
  },

  /** 清理定时器 @private */
  _clear() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = 0;
    }
  },

  /**
   * 构建 4 个步骤的展示态。
   * @param {number} currentStep
   * @param {boolean|number} finished 是否全部完成
   * @return {object[]}
   * @private
   */
  _buildSteps(currentStep, finished) {
    return STEP_DEFS.map((def) => {
      let state = 'todo';
      let icon = '○';
      if (finished || def.id < currentStep) {
        state = 'done';
        icon = '✓';
      } else if (def.id === currentStep) {
        state = 'doing';
        icon = '◐';
      }
      return { id: def.id, index: def.index, name: def.name, state, icon };
    });
  },

  /** 单次轮询 @private */
  _poll() {
    if (this.data.failed) {
      return;
    }
    this._polls += 1;
    if (this._polls > MAX_POLLS) {
      this._fail(api.messageOf('TIMEOUT'));
      return;
    }

    api
      .getTaskStatus(this.data.taskId)
      .then((state) => this._apply(state))
      .catch((error) => {
        // 网络抖动不立刻判失败，连续失败到超时上限自然会退出
        // v2：PDD 码 20001（任务不存在）；兼容旧码 4004
        if (error && (error.code === 20001 || error.code === 4004)) {
          this._fail('任务不存在，请重新拍摄上传');
        }
      });
  },

  /**
   * 应用一次状态。
   * @param {object} state 后端 TaskStatusView
   * @private
   */
  _apply(state) {
    if (!state) {
      return;
    }

    if (state.status === 'failed') {
      this._fail(api.messageOf(state.error_code, state.error_message));
      return;
    }

    const progress = Math.max(this.data.progress, state.progress || 0);
    const step = Math.max(this.data.step, state.step || 1);

    if (state.status === 'success') {
      this.setData({ progress: 100, step: 4, steps: this._buildSteps(4, true) });
      this._clear();
      wx.redirectTo({ url: '/pages/result/result?task_id=' + this.data.taskId });
      return;
    }

    // v2：优先展示后端 step_text（PDD 字符串 step），缺省回落本地 4 步文案
    const message = state.step_text || state.message || this._stepName(step);
    this.setData({
      progress,
      step,
      message,
      steps: this._buildSteps(step, false),
      etaText: this._eta(progress)
    });
  },

  /** 本地 step 文案兜底 @private */
  _stepName(step) {
    const def = STEP_DEFS.find((s) => s.id === step);
    return (def ? def.name : '正在分析...');
  },

  /**
   * 粗略预估剩余时间。
   * @param {number} progress
   * @return {string}
   * @private
   */
  _eta(progress) {
    if (progress <= 3 || progress >= 100) {
      return '';
    }
    const elapsed = (Date.now() - this._startedAt) / 1000;
    const remain = Math.round((elapsed / progress) * (100 - progress));
    if (remain <= 0 || remain > 180) {
      return '';
    }
    return '预计还需 ' + remain + ' 秒';
  },

  /**
   * 进入失败态。
   * @param {string} text
   * @private
   */
  _fail(text) {
    this._clear();
    this.setData({ failed: true, errorText: text || api.messageOf('INTERNAL') });
  },

  /** 重新拍摄 */
  onRetake() {
    wx.redirectTo({ url: '/pages/index/index' });
  }
});
