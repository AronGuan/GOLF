// pages/result/result.js 分析结果页
const app = getApp();
const api = require('../../utils/api.js');

/** 五态中文文案（v2：3 态 -> 5 态）。 */
const STATUS_TEXT = {
  low: '偏低',
  normal: '正常',
  high: '偏高',
  critical_low: '严重偏低',
  critical_high: '严重偏高'
};

/** 风险等级展示（v2 区域4；配色以架构 §6.5 为准）。 */
const RISK_LEVEL_TEXT = { high: '高风险', medium: '中风险', low: '低风险' };

/** 单位 -> 小数位数。 */
const UNIT_DIGITS = { '°': 1, '%': 1, s: 2, ':1': 1, '': 2 };

/** 机位标签。 */
const VIEW_LABEL = { face_on: '正面机位', down_the_line: '侧面机位' };

/** 手动帧微调：事件帧 ± 可调帧数（与后端 config.FRAME_ADJUST_RANGE 对齐）。 */
const FRAME_RANGE = 30;

/**
 * 数值格式化。
 * @param {number} value 原始值
 * @param {string} unit 单位
 * @returns {string}
 */
function formatValue(value, unit) {
  const digits = UNIT_DIGITS[unit] === undefined ? 1 : UNIT_DIGITS[unit];
  const num = typeof value === 'number' && isFinite(value) ? value : 0;
  return num.toFixed(digits);
}

/**
 * 把数值限制在 [lo, hi]。
 * @param {number} v 值
 * @param {number} lo 下界
 * @param {number} hi 上界
 * @returns {number}
 */
function clamp(v, lo, hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

/**
 * 为一个指标计算迷你区间条的几何量。
 *
 * 显示域 = 参考区间向两侧各外扩 60%，保证参考带居中、越界值仍可见。
 * @param {object} m 后端返回的 StageMetric
 * @returns {object} 带展示字段的指标对象
 */
function decorate(m) {
  const refMin = Number(m.ref_min) || 0;
  const refMax = Number(m.ref_max) || 0;
  let span = refMax - refMin;
  if (span <= 0) span = Math.max(Math.abs(refMax), 1) * 0.3;
  const lo = refMin - span * 0.6;
  const hi = refMax + span * 0.6;
  const total = hi - lo || 1;
  const value = typeof m.value === 'number' && isFinite(m.value) ? m.value : 0;

  const refFrom = clamp(((refMin - lo) / total) * 100, 0, 100);
  const refTo = clamp(((refMax - lo) / total) * 100, 0, 100);
  const markPct = clamp(((value - lo) / total) * 100, 0, 100);
  const status = m.status || 'normal';
  const unit = m.unit || '';

  return {
    key: m.key,
    name: m.name,
    unit: unit,
    status: status,
    statusText: STATUS_TEXT[status] || '正常',
    valueText: formatValue(value, unit),
    refText: formatValue(refMin, unit) + '~' + formatValue(refMax, unit),
    refFrom: refFrom.toFixed(2),
    refWidth: Math.max(refTo - refFrom, 1).toFixed(2),
    markPct: markPct.toFixed(2),
    description: m.description || ''
  };
}

/**
 * 为一条风险装饰展示字段。
 * @param {object} r 后端返回的 RiskItem
 * @returns {object}
 */
function decorateRisk(r) {
  return {
    rule_id: r.rule_id,
    risk_name: r.risk_name,
    level: r.risk_level || 'low',
    levelText: RISK_LEVEL_TEXT[r.risk_level] || '低风险',
    trigger_description: r.trigger_description || '',
    suggestions: Array.isArray(r.suggestions) ? r.suggestions : [],
    manual_excerpt: r.manual_excerpt || '',
    manual_page: r.manual_page || '',
    metric_name: r.metric_name || '',
    valueText: formatValue(r.value, r.unit || ''),
    unit: r.unit || ''
  };
}

Page({
  data: {
    loaded: false,
    current: 3, // 默认选中 ④ 顶点
    scrollInto: '',
    phases: [],
    cur: null,
    globals: [],
    meta: {},
    warnings: [],
    disclaimer: '',
    taskId: '',
    // ---- v2 ----
    viewLabel: '',
    analyzedDate: '',
    manual: null // 手册原文弹窗内容
  },

  onLoad(options) {
    // 分析中页跳转参数为 task_id（与 analyzing.js 保持一致）
    const taskId =
      (options && (options.task_id || options.taskId)) || app.globalData.taskId || '';
    const cached = app.getResult(taskId);
    if (cached) {
      this._apply(cached);
      return;
    }
    if (!taskId) {
      this.setData({ loaded: false });
      return;
    }
    this._fetch(taskId);
  },

  /**
   * 缓存缺失时按 taskId 重新拉取结果。
   * @param {string} taskId 任务 ID
   */
  _fetch(taskId) {
    wx.showLoading({ title: '加载中', mask: true });
    api
      .getResult(taskId)
      .then((result) => {
        wx.hideLoading();
        app.setResult(result);
        this._apply(result);
      })
      .catch((err) => {
        wx.hideLoading();
        this.setData({ loaded: false });
        wx.showToast({ title: err.message || '结果已失效', icon: 'none' });
      });
  },

  /**
   * 把后端结果转换成页面渲染数据。
   * @param {object} result AnalysisResult
   */
  _apply(result) {
    const vm = result.video_meta || {};
    const totalFrames = vm.total_frames || vm.frame_count || 0;
    const fps = Number(vm.fps) || 0;

    const phases = (result.phases || []).map((p) => {
      const eventFrame = Number(p.frame_index) || 0;
      const timeText =
        (Number(p.timestamp) || 0).toFixed(2) + 's · 第 ' + eventFrame + ' 帧';
      return {
        index: p.index,
        key: p.key,
        name_cn: p.name_cn,
        name_en: p.name_en,
        frame_index: eventFrame,
        estimated: !!p.estimated,
        image_url: p.image_url,
        // ---- 手动帧微调（v3）：事件帧原值 + 调整态 ----
        origImageUrl: p.image_url,
        origTimeText: timeText,
        timeText: timeText,
        eventFrame: eventFrame,
        adjCur: eventFrame, // 当前展示帧号（调整后 = 实际渲染帧）
        adjFrame: null, // 非空 = 已手动调整过
        adjActive: false, // 手动微调视觉标识
        adjLoading: false,
        adjMin: Math.max(0, eventFrame - FRAME_RANGE),
        adjMax:
          totalFrames > 0
            ? Math.min(totalFrames - 1, eventFrame + FRAME_RANGE)
            : eventFrame + FRAME_RANGE,
        metrics: (p.metrics || []).map(decorate),
        // v2 区域4：风险区（后端已按 high→medium→low 排序）
        risks: (p.risks || []).map(decorateRisk),
        emptyMetrics: !p.metrics || p.metrics.length === 0
      };
    });

    const gm = result.global_metrics || {};
    const globals = (gm.metrics || []).map(decorate);

    const meta = {
      width: vm.width || 0,
      height: vm.height || 0,
      frame_count: vm.frame_count || 0,
      fps: fps,
      fpsText: (Number(vm.fps) || 0).toFixed(0),
      durationText: (Number(vm.duration) || 0).toFixed(1)
    };

    // v2：机位标签（顶层 camera_view 优先，回落 video_meta）
    const rawView = result.camera_view || vm.camera_view || 'face_on';
    const viewLabel = VIEW_LABEL[rawView] || '正面机位';
    const now = new Date();
    const analyzedDate =
      now.getFullYear() + '-' + (now.getMonth() + 1) + '-' + now.getDate();

    const current = phases.length > 3 ? 3 : 0; // 默认 ④ 顶点
    this.setData({
      loaded: phases.length > 0,
      phases: phases,
      current: current,
      cur: phases[current] || null,
      scrollInto: 'thumb-' + current,
      globals: globals,
      meta: meta,
      warnings: result.warnings || [],
      disclaimer: result.disclaimer || '',
      viewLabel: viewLabel,
      analyzedDate: analyzedDate,
      taskId: result.task_id || this.data.taskId
    });
  },

  /**
   * 切换到指定阶段。
   * @param {number} index 阶段下标 0~7
   */
  _select(index) {
    const phases = this.data.phases;
    if (!phases.length) return;
    const i = clamp(index, 0, phases.length - 1);
    if (i === this.data.current) return;
    // 切换阶段时该阶段帧号/图片复位为事件帧（产品决策：手动微调不跨阶段记忆）
    const phase = phases[i];
    const reset = Object.assign({}, phase, {
      image_url: phase.origImageUrl,
      timeText: phase.origTimeText,
      adjCur: phase.eventFrame,
      adjFrame: null,
      adjActive: false,
      adjLoading: false
    });
    const nextPhases = phases.slice();
    nextPhases[i] = reset;
    this.setData({
      phases: nextPhases,
      current: i,
      cur: reset,
      scrollInto: 'thumb-' + i
    });
  },

  onSelect(e) {
    this._select(Number(e.currentTarget.dataset.index));
  },

  /**
   * 缩略图下 ◀/▶ 按钮：当前阶段事件帧 ±1 帧（结果页手动微调，v3）。
   * @param {Event} e dataset.delta = -1 | 1
   */
  onFrameStep(e) {
    const delta = Number(e.currentTarget.dataset.delta);
    this._stepFrame(delta);
  },

  /**
   * 按方向步进当前阶段帧号，并做边界/加载中校验。
   * @param {number} delta -1 上一帧 / +1 下一帧
   */
  _stepFrame(delta) {
    const i = this.data.current;
    const phase = this.data.phases[i];
    if (!phase || phase.adjLoading) return;
    const target = phase.adjCur + delta;
    if (target < phase.adjMin || target > phase.adjMax) return;
    this._loadFrame(i, target);
  },

  /**
   * 拉取指定帧骨架图并替换当前阶段缩略图/大图。
   * @param {number} i 阶段下标
   * @param {number} target 目标帧号
   */
  _loadFrame(i, target) {
    const phase = this.data.phases[i];
    const taskId = this.data.taskId;
    if (!phase || !taskId) return;
    this.setData({ ['phases[' + i + '].adjLoading']: true });
    api
      .getFrameImage(taskId, target)
      .then((res) => {
        const fps = this.data.meta.fps || 0;
        const sec = fps > 0 ? res.frameIndex / fps : 0;
        const updated = Object.assign({}, phase, {
          image_url: res.tempFilePath,
          adjCur: res.frameIndex,
          adjFrame: res.frameIndex,
          adjActive: true,
          adjLoading: false,
          timeText: sec.toFixed(2) + 's · 第 ' + res.frameIndex + ' 帧'
        });
        const patch = { ['phases[' + i + ']']: updated };
        if (i === this.data.current) patch.cur = updated;
        this.setData(patch);
      })
      .catch((err) => {
        this.setData({ ['phases[' + i + '].adjLoading']: false });
        wx.showToast({ title: err.message || '帧加载失败', icon: 'none' });
      });
  },

  onPrev() {
    this._select(this.data.current - 1);
  },

  onNext() {
    this._select(this.data.current + 1);
  },

  onPreview() {
    const urls = this.data.phases.map((p) => p.image_url);
    if (!urls.length) return;
    wx.previewImage({ urls: urls, current: urls[this.data.current] });
  },

  onSaveImage() {
    const cur = this.data.cur;
    if (!cur || !cur.image_url) return;
    const url = cur.image_url;
    // 手动微调后 image_url 是本地临时 PNG（wxfile://...），直接存相册
    const isLocal =
      url.indexOf('wxfile://') === 0 || url.indexOf('http') !== 0;
    if (isLocal) {
      wx.showLoading({ title: '保存中', mask: true });
      wx.saveImageToPhotosAlbum({
        filePath: url,
        success: () => {
          wx.hideLoading();
          wx.showToast({ title: '已保存到相册', icon: 'success' });
        },
        fail: () => {
          wx.hideLoading();
          wx.showToast({ title: '需要相册权限', icon: 'none' });
        }
      });
      return;
    }
    wx.showLoading({ title: '保存中', mask: true });
    wx.downloadFile({
      url: url,
      success: (res) => {
        if (res.statusCode !== 200) {
          wx.hideLoading();
          wx.showToast({ title: '图片下载失败', icon: 'none' });
          return;
        }
        wx.saveImageToPhotosAlbum({
          filePath: res.tempFilePath,
          success: () => {
            wx.hideLoading();
            wx.showToast({ title: '已保存到相册', icon: 'success' });
          },
          fail: () => {
            wx.hideLoading();
            wx.showToast({ title: '需要相册权限', icon: 'none' });
          }
        });
      },
      fail: () => {
        wx.hideLoading();
        wx.showToast({ title: '图片下载失败', icon: 'none' });
      }
    });
  },

  /** 打开手册原文半屏弹窗（v2） */
  onOpenManual(e) {
    const index = Number(e.currentTarget.dataset.index);
    const risk = (this.data.cur && this.data.cur.risks[index]) || null;
    if (!risk || !risk.manual_excerpt) {
      return;
    }
    this.setData({ manual: risk });
  },

  /** 关闭手册原文弹窗 */
  onCloseManual() {
    this.setData({ manual: null });
  },

  /** 阻止冒泡 */
  noop() {},

  /** [查看完整报告] 占位按钮（P1，本期不实现功能） */
  onFullReport() {
    wx.showToast({ title: '即将上线', icon: 'none' });
  },

  onRetake() {
    wx.redirectTo({ url: '/pages/index/index' });
  },

  onShareAppMessage() {
    return {
      title: '我的高尔夫挥杆 8 阶段分析报告',
      path: '/pages/index/index'
    };
  }
});
