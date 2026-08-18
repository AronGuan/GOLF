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

// ---------------- 整页截图（v3，canvas 2d 离屏合成）常量 ----------------
/** 截图逻辑宽度（与 rpx 基准一致），高度按内容动态计算。 */
const SNAP_W = 750;
/** 卡片内边距。 */
const SNAP_PAD = 30;
/** 内容宽度。 */
const SNAP_CW = SNAP_W - SNAP_PAD * 2;
/** hero 大图区高度。 */
const SNAP_HERO_H = 460;
/** 整页截图配色（浅色分享卡）。 */
const SNAP_COLORS = {
  bg: '#ffffff',
  card: '#f3f6f9',
  text: '#1a2430',
  sub: '#7a8699',
  line: '#e4e9ef',
  accent: '#0f9d6e',
  accentSoft: '#e6f7f0',
  amber: '#b97a12',
  amberSoft: '#fdf1dc',
  heroBg: '#0b1218',
  high: '#ef4444',
  medium: '#f59e0b',
  low: '#3b82f6'
};
/** 指标五态 -> 颜色。 */
const SNAP_STATUS_COLOR = {
  normal: '#0f9d6e',
  low: '#3b82f6',
  high: '#f59e0b',
  critical_low: '#ef4444',
  critical_high: '#ef4444'
};
/** 风险等级 -> 颜色。 */
const SNAP_LEVEL_COLOR = { high: '#ef4444', medium: '#f59e0b', low: '#3b82f6' };

/**
 * 文本按宽度换行（CJK 全角 / ASCII 半角宽度估算）。
 * 纯函数：同时用于「高度预估」与「实际绘制」，保证二者一致。
 * @param {string} text 文本
 * @param {number} maxWidth 最大宽度（逻辑 px）
 * @param {number} fontSize 字号（逻辑 px）
 * @returns {string[]}
 */
function wrapLines(text, maxWidth, fontSize) {
  if (!text) return [];
  const lines = [];
  let cur = '';
  let w = 0;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text.charAt(i);
    const cw = ch.charCodeAt(0) > 0x2e80 ? fontSize : fontSize * 0.55;
    if (w + cw > maxWidth && cur) {
      lines.push(cur);
      cur = ch;
      w = cw;
    } else {
      cur += ch;
      w += cw;
    }
  }
  if (cur) lines.push(cur);
  return lines;
}

/**
 * 绘制圆角矩形路径（仅路径，由调用方 fill/stroke）。
 * @param {CanvasRenderingContext2D} ctx
 * @param {number} x
 * @param {number} y
 * @param {number} w
 * @param {number} h
 * @param {number} r 圆角半径
 */
function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

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
    manual: null, // 手册原文弹窗内容
    // ---- v3 整页截图 ----
    snapSaving: false, // 保存中（按钮 loading，防重入）
    snapH: 1000 // 离屏 canvas 高度（先算内容高度再 setData）
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

  // ================= 整页截图（v3）：离屏 canvas 合成 -> 保存相册 =================

  /** [保存当前页到相册] 入口 */
  onSavePage() {
    if (this.data.snapSaving) return;
    const cur = this.data.cur;
    if (!cur || !cur.image_url) {
      wx.showToast({ title: '暂无可用内容', icon: 'none' });
      return;
    }
    if (!this._checkCanvas2d()) {
      wx.showModal({
        title: '版本过低',
        content: '保存整页需要微信 2.9.0 及以上版本，请升级微信后重试',
        showCancel: false
      });
      return;
    }
    // 先算内容高度 -> setData 画布尺寸 -> 再绘制（保证离屏画布高度与内容一致）
    const layout = this._composeSnapshot(cur);
    this.setData({ snapSaving: true, snapH: layout.height }, () => {
      const next = typeof wx.nextTick === 'function' ? wx.nextTick : (fn) => setTimeout(fn, 16);
      next(() => this._drawSnapshot(cur));
    });
  },

  /** 基础库版本比较：v1 >= v2 返回 >=0。 */
  _compareVersion(v1, v2) {
    const a = String(v1).split('.').map(Number);
    const b = String(v2).split('.').map(Number);
    for (let i = 0; i < 3; i += 1) {
      const x = a[i] || 0;
      const y = b[i] || 0;
      if (x !== y) return x > y ? 1 : -1;
    }
    return 0;
  },

  /** canvas 2d 需要基础库 2.9.0+。 */
  _checkCanvas2d() {
    try {
      const ver = wx.getSystemInfoSync().SDKVersion || '';
      if (!ver) return true;
      return this._compareVersion(ver, '2.9.0') >= 0;
    } catch (e) {
      return true;
    }
  },

  /** 像素比（封顶 2，避免大画布超真机纹理上限）。 */
  _pixelRatio() {
    try {
      const r = wx.getSystemInfoSync().pixelRatio || 2;
      return Math.min(Math.max(r, 1), 2);
    } catch (e) {
      return 2;
    }
  },

  /** 判断图源是否为本地文件（wxfile:// 或 USER_DATA_PATH 的 http://usr）。 */
  _isLocalImage(url) {
    if (!url) return false;
    if (/^https?:\/\/usr\b/i.test(url)) return true;
    return !/^https?:\/\//i.test(url);
  },

  /** 计算整页截图布局（纯数据；sections 供绘制，height 供画布尺寸）。 */
  _composeSnapshot(cur) {
    const metrics = cur.metrics || [];
    const risks = cur.risks || [];
    const disclaimer = this.data.disclaimer || '';
    const sections = [];
    let y = SNAP_PAD;

    // 1. 标题行
    sections.push({ type: 'title', y: y });
    y += 44;

    // 2. hero 大图
    sections.push({ type: 'hero', y: y, w: SNAP_CW, h: SNAP_HERO_H });
    y += SNAP_HERO_H + 20;

    // 3. 阶段名 + 帧号
    sections.push({ type: 'stage', y: y, cur: cur });
    y += 52;

    // 4. 阶段指标
    sections.push({
      type: 'sectionTitle',
      y: y,
      text: '阶段指标',
      count: metrics.length,
      countUnit: '项'
    });
    y += 42;
    if (metrics.length === 0) {
      sections.push({ type: 'emptyMetric', y: y });
      y += 88;
    } else {
      metrics.forEach((m) => {
        const rawDesc = wrapLines(m.description || '', SNAP_CW - 40, 22);
        const descLines = rawDesc.slice(0, 2);
        if (rawDesc.length > 2) descLines[descLines.length - 1] += '…';
        const cardH =
          18 + 42 + (descLines.length ? 10 + descLines.length * 30 : 0) + 18;
        sections.push({ type: 'metric', y: y, h: cardH, m: m, descLines: descLines });
        y += cardH + 14;
      });
    }

    // 5. 风险与改进建议
    sections.push({
      type: 'sectionTitle',
      y: y,
      text: '风险与改进建议',
      count: risks.length,
      countUnit: '条'
    });
    y += 42;
    if (risks.length === 0) {
      sections.push({ type: 'riskOk', y: y });
      y += 84;
    } else {
      risks.forEach((r) => {
        const rawReason = wrapLines(r.trigger_description || '', SNAP_CW - 40, 24);
        const reasonLines = rawReason.slice(0, 4);
        if (rawReason.length > 4) reasonLines[reasonLines.length - 1] += '…';
        const suggestLines = [];
        (r.suggestions || []).forEach((sg) => {
          wrapLines(sg, SNAP_CW - 56, 22)
            .slice(0, 2)
            .forEach((ln) => suggestLines.push(ln));
        });
        const cut = suggestLines.slice(0, 6);
        if (suggestLines.length > 6) cut[cut.length - 1] += '…';
        const cardH =
          18 +
          44 +
          reasonLines.length * 34 +
          (cut.length ? 10 + 26 + cut.length * 30 + 12 : 0) +
          18;
        sections.push({
          type: 'risk',
          y: y,
          h: cardH,
          r: r,
          reasonLines: reasonLines,
          suggestLines: cut
        });
        y += cardH + 14;
      });
    }

    // 6. 底部品牌 / 免责
    const rawFooter = wrapLines(disclaimer, SNAP_CW, 20);
    const footerLines = rawFooter.slice(0, 3);
    if (rawFooter.length > 3) footerLines[footerLines.length - 1] += '…';
    sections.push({ type: 'footer', y: y, lines: footerLines });
    y += 16 + 30 + footerLines.length * 30 + 10;

    y += SNAP_PAD;
    return { sections: sections, height: y };
  },

  /** 拉取 hero 图（本地直接用；远程先 downloadFile 落地再画）。 */
  _drawSnapshot(cur) {
    const url = cur.image_url || '';
    const start = (src) => {
      const query = wx.createSelectorQuery().in(this);
      query.select('#snapshotCanvas').fields({ node: true, size: true }, (res) => {
        if (!res || !res.node) {
          this._snapFail('当前微信版本过低，无法合成整页图片');
          return;
        }
        const canvas = res.node;
        const ctx = canvas.getContext('2d');
        const img = canvas.createImage();
        img.onload = () => {
          this._renderSnapshot(canvas, ctx, img, cur);
        };
        img.onerror = () => {
          this._snapFail('骨架图加载失败，请重试');
        };
        img.src = src;
      }).exec();
    };
    if (this._isLocalImage(url)) {
      start(url);
      return;
    }
    wx.downloadFile({
      url: url,
      success: (res) => {
        if (res.statusCode === 200) {
          start(res.tempFilePath);
        } else {
          this._snapFail('图片下载失败，请重试');
        }
      },
      fail: () => {
        this._snapFail('图片下载失败，请重试');
      }
    });
  },

  /** 设置画布尺寸并逐段绘制，随后导出临时文件。 */
  _renderSnapshot(canvas, ctx, img, cur) {
    const dpr = this._pixelRatio();
    const layout = this._composeSnapshot(cur);
    canvas.width = SNAP_W * dpr;
    canvas.height = layout.height * dpr;
    ctx.scale(dpr, dpr);

    ctx.fillStyle = SNAP_COLORS.bg;
    ctx.fillRect(0, 0, SNAP_W, layout.height);

    layout.sections.forEach((s) => {
      if (s.type === 'title') this._drawTitle(ctx, s);
      else if (s.type === 'hero') this._drawHero(ctx, s, img);
      else if (s.type === 'stage') this._drawStage(ctx, s);
      else if (s.type === 'sectionTitle') this._drawSectionTitle(ctx, s);
      else if (s.type === 'emptyMetric') this._drawEmptyMetric(ctx, s);
      else if (s.type === 'metric') this._drawMetric(ctx, s);
      else if (s.type === 'riskOk') this._drawRiskOk(ctx, s);
      else if (s.type === 'risk') this._drawRisk(ctx, s);
      else if (s.type === 'footer') this._drawFooter(ctx, s);
    });

    wx.canvasToTempFilePath(
      {
        canvas: canvas,
        success: (res) => {
          this._saveToAlbum(res.tempFilePath);
        },
        fail: () => {
          this._snapFail('图片生成失败，请重试');
        }
      },
      this
    );
  },

  /** 标题行：应用名 + 机位/日期。 */
  _drawTitle(ctx, s) {
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    ctx.font = 'bold 26px sans-serif';
    ctx.fillStyle = SNAP_COLORS.text;
    ctx.fillText('高尔夫挥杆姿态分析', SNAP_PAD, s.y);
    const right = this.data.viewLabel + ' · ' + this.data.analyzedDate;
    ctx.font = '20px sans-serif';
    ctx.fillStyle = SNAP_COLORS.sub;
    ctx.textAlign = 'right';
    ctx.fillText(right, SNAP_W - SNAP_PAD, s.y + 6);
  },

  /** hero 大图：深色底 + aspectFit 居中。 */
  _drawHero(ctx, s, img) {
    const x = SNAP_PAD;
    const y = s.y;
    const w = s.w;
    const h = s.h;
    roundRect(ctx, x, y, w, h, 12);
    ctx.fillStyle = SNAP_COLORS.heroBg;
    ctx.fill();
    const iw = img.width || 1;
    const ih = img.height || 1;
    const scale = Math.min(w / iw, h / ih);
    const dw = iw * scale;
    const dh = ih * scale;
    const dx = x + (w - dw) / 2;
    const dy = y + (h - dh) / 2;
    ctx.drawImage(img, dx, dy, dw, dh);
  },

  /** 阶段名 + 帧号行（含手动微调角标）。 */
  _drawStage(ctx, s) {
    const cur = s.cur;
    const x = SNAP_PAD;
    const y = s.y;
    ctx.textBaseline = 'top';
    // 序号圆标
    ctx.textAlign = 'center';
    ctx.font = 'bold 24px sans-serif';
    ctx.fillStyle = SNAP_COLORS.accent;
    ctx.beginPath();
    ctx.arc(x + 17, y + 17, 17, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.fillText(String(cur.index), x + 17, y + 5);
    // 阶段名
    ctx.textAlign = 'left';
    ctx.font = 'bold 32px sans-serif';
    ctx.fillStyle = SNAP_COLORS.text;
    const cnW = ctx.measureText(cur.name_cn).width;
    ctx.fillText(cur.name_cn, x + 44, y + 4);
    ctx.font = '22px sans-serif';
    ctx.fillStyle = SNAP_COLORS.sub;
    ctx.fillText(cur.name_en, x + 44 + cnW + 10, y + 12);
    // 右侧帧号（+ 手动角标）
    ctx.textAlign = 'right';
    ctx.font = '22px sans-serif';
    ctx.fillStyle = SNAP_COLORS.sub;
    const timeW = ctx.measureText(cur.timeText).width;
    ctx.fillText(cur.timeText, SNAP_W - SNAP_PAD, y + 12);
    if (cur.adjActive) {
      const tagW = 58;
      const tagH = 28;
      const tagX = SNAP_W - SNAP_PAD - timeW - tagW - 10;
      roundRect(ctx, tagX, y + 10, tagW, tagH, 14);
      ctx.fillStyle = SNAP_COLORS.amberSoft;
      ctx.fill();
      ctx.textAlign = 'center';
      ctx.font = '18px sans-serif';
      ctx.fillStyle = SNAP_COLORS.amber;
      ctx.fillText('手动', tagX + tagW / 2, y + 15);
    }
  },

  /** 区块标题。 */
  _drawSectionTitle(ctx, s) {
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    ctx.font = 'bold 28px sans-serif';
    ctx.fillStyle = SNAP_COLORS.text;
    ctx.fillText(s.text, SNAP_PAD, s.y);
    if (s.count > 0) {
      ctx.font = '20px sans-serif';
      ctx.fillStyle = SNAP_COLORS.sub;
      ctx.textAlign = 'right';
      ctx.fillText('共 ' + s.count + ' ' + (s.countUnit || '项'), SNAP_W - SNAP_PAD, s.y + 7);
    }
  },

  /** 指标空态。 */
  _drawEmptyMetric(ctx, s) {
    const x = SNAP_PAD;
    const y = s.y;
    const w = SNAP_CW;
    const h = 84;
    roundRect(ctx, x, y, w, h, 12);
    ctx.fillStyle = SNAP_COLORS.card;
    ctx.fill();
    ctx.textBaseline = 'top';
    ctx.textAlign = 'center';
    ctx.font = '24px sans-serif';
    ctx.fillStyle = SNAP_COLORS.sub;
    ctx.fillText('该机位在本阶段暂无可测指标', SNAP_W / 2, y + 30);
  },

  /** 指标卡：名称左 / 数值中 / 状态右 + description 灰行。 */
  _drawMetric(ctx, s) {
    const m = s.m;
    const x = SNAP_PAD;
    const y = s.y;
    const w = SNAP_CW;
    const h = s.h;
    const pad = 20;
    const color = SNAP_STATUS_COLOR[m.status] || SNAP_STATUS_COLOR.normal;
    roundRect(ctx, x, y, w, h, 12);
    ctx.fillStyle = SNAP_COLORS.card;
    ctx.fill();

    ctx.textBaseline = 'top';
    // 名称（左，超宽截断）
    ctx.textAlign = 'left';
    ctx.font = '26px sans-serif';
    ctx.fillStyle = SNAP_COLORS.text;
    let name = m.name || '';
    const nameMax = 300;
    if (ctx.measureText(name).width > nameMax) {
      while (name.length > 1 && ctx.measureText(name + '…').width > nameMax) {
        name = name.slice(0, -1);
      }
      name += '…';
    }
    ctx.fillText(name, x + pad, y + 28);

    // 数值（中）
    const valText = m.valueText;
    const unitText = m.unit || '';
    ctx.font = 'bold 34px sans-serif';
    const valW = ctx.measureText(valText).width;
    ctx.font = '20px sans-serif';
    const unitW = ctx.measureText(unitText).width;
    const totalW = valW + (unitText ? 6 + unitW : 0);
    let vx = SNAP_W / 2 - totalW / 2;
    ctx.textAlign = 'left';
    ctx.font = 'bold 34px sans-serif';
    ctx.fillStyle = color;
    ctx.fillText(valText, vx, y + 22);
    if (unitText) {
      ctx.font = '20px sans-serif';
      ctx.fillStyle = SNAP_COLORS.sub;
      ctx.fillText(unitText, vx + valW + 6, y + 36);
    }

    // 状态 chip（右）
    const statusText = m.statusText || '正常';
    ctx.font = '20px sans-serif';
    const chipW = ctx.measureText(statusText).width + 18;
    const chipH = 30;
    const chipX = x + w - pad - chipW;
    const chipY = y + 24;
    roundRect(ctx, chipX, chipY, chipW, chipH, 15);
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.textAlign = 'center';
    ctx.fillStyle = color;
    ctx.fillText(statusText, chipX + chipW / 2, chipY + 6);

    // description 术语解释行
    if (s.descLines.length) {
      ctx.textAlign = 'left';
      ctx.font = '22px sans-serif';
      ctx.fillStyle = SNAP_COLORS.sub;
      s.descLines.forEach((ln, i) => {
        ctx.fillText(ln, x + pad, y + 70 + i * 30);
      });
    }
  },

  /** 无风险空态。 */
  _drawRiskOk(ctx, s) {
    const x = SNAP_PAD;
    const y = s.y;
    const w = SNAP_CW;
    const h = 76;
    roundRect(ctx, x, y, w, h, 12);
    ctx.fillStyle = SNAP_COLORS.accentSoft;
    ctx.fill();
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    ctx.font = '26px sans-serif';
    ctx.fillStyle = SNAP_COLORS.accent;
    ctx.fillText('✅ 本阶段动作良好', x + 24, y + 25);
  },

  /** 风险卡：等级色条 + 名称/等级 + 触发文案 + 建议。 */
  _drawRisk(ctx, s) {
    const r = s.r;
    const x = SNAP_PAD;
    const y = s.y;
    const w = SNAP_CW;
    const h = s.h;
    const pad = 20;
    const color = SNAP_LEVEL_COLOR[r.level] || SNAP_LEVEL_COLOR.low;

    // 左侧等级色条
    ctx.fillStyle = color;
    roundRect(ctx, x, y, 6, h, 3);
    ctx.fill();
    // 卡片底
    roundRect(ctx, x + 6, y, w - 6, h, 12);
    ctx.fillStyle = SNAP_COLORS.card;
    ctx.fill();

    ctx.textBaseline = 'top';
    // 头部：圆点 + 名称 + 等级
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x + pad + 7, y + 31, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.textAlign = 'left';
    ctx.font = 'bold 26px sans-serif';
    ctx.fillStyle = SNAP_COLORS.text;
    let rn = r.risk_name || '';
    const rnMax = w - pad * 2 - 130;
    if (ctx.measureText(rn).width > rnMax) {
      while (rn.length > 1 && ctx.measureText(rn + '…').width > rnMax) {
        rn = rn.slice(0, -1);
      }
      rn += '…';
    }
    ctx.fillText(rn, x + pad + 20, y + 23);
    ctx.textAlign = 'right';
    ctx.font = '20px sans-serif';
    ctx.fillStyle = color;
    ctx.fillText(r.levelText || '低风险', x + w - pad, y + 24);

    // 触发文案
    ctx.textAlign = 'left';
    ctx.font = '24px sans-serif';
    ctx.fillStyle = SNAP_COLORS.text;
    s.reasonLines.forEach((ln, i) => {
      ctx.fillText(ln, x + pad, y + 62 + i * 34);
    });

    // 改进建议块
    if (s.suggestLines.length) {
      const blockTop = y + 62 + s.reasonLines.length * 34 + 10;
      const blockH = 26 + s.suggestLines.length * 30 + 12;
      roundRect(ctx, x + pad, blockTop, w - pad - 6, blockH, 8);
      ctx.fillStyle = 'rgba(15, 32, 48, 0.04)';
      ctx.fill();
      ctx.font = '20px sans-serif';
      ctx.fillStyle = SNAP_COLORS.sub;
      ctx.fillText('改进建议：', x + pad + 14, blockTop + 6);
      ctx.font = '22px sans-serif';
      ctx.fillStyle = SNAP_COLORS.text;
      s.suggestLines.forEach((ln, i) => {
        ctx.fillText('· ' + ln, x + pad + 14, blockTop + 26 + i * 30);
      });
    }
  },

  /** 底部品牌/免责小字。 */
  _drawFooter(ctx, s) {
    const y = s.y;
    ctx.fillStyle = SNAP_COLORS.line;
    ctx.fillRect(SNAP_PAD, y, SNAP_CW, 1);
    let ty = y + 16;
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    ctx.font = '20px sans-serif';
    ctx.fillStyle = SNAP_COLORS.sub;
    const meta = this.data.meta || {};
    const brand =
      '高尔夫挥杆姿态分析 · ' +
      this.data.viewLabel +
      ' · ' +
      this.data.analyzedDate +
      (meta.fpsText ? ' · ' + meta.fpsText + 'fps' : '') +
      (meta.width && meta.height ? ' · ' + meta.width + '×' + meta.height : '');
    ctx.fillText(brand, SNAP_PAD, ty);
    ty += 30;
    s.lines.forEach((ln) => {
      ctx.fillText(ln, SNAP_PAD, ty);
      ty += 30;
    });
  },

  /** 保存临时文件到相册（含授权失败引导）。 */
  _saveToAlbum(filePath) {
    wx.saveImageToPhotosAlbum({
      filePath: filePath,
      success: () => {
        this._snapDone();
        wx.showToast({ title: '已保存到相册', icon: 'success' });
      },
      fail: (err) => {
        this._snapDone();
        this._handleAlbumAuthFail(err);
      }
    });
  },

  /** 相册授权失败处理：拒绝 -> 引导去设置页。 */
  _handleAlbumAuthFail(err) {
    const msg = (err && err.errMsg) || '';
    const denied = /auth|deny|permission|cancel/i.test(msg);
    if (denied) {
      wx.showModal({
        title: '需要相册权限',
        content: '保存图片需要「添加到相册」权限，请在设置中开启后重试',
        confirmText: '去设置',
        cancelText: '暂不',
        success: (r) => {
          if (r.confirm) wx.openSetting({});
        }
      });
    } else {
      wx.showToast({ title: '保存失败，请重试', icon: 'none' });
    }
  },

  /** 收尾：复位保存中状态。 */
  _snapDone() {
    if (this.data.snapSaving) this.setData({ snapSaving: false });
  },

  /** 失败出口：复位状态 + 轻提示。 */
  _snapFail(msg) {
    this._snapDone();
    wx.showToast({ title: msg || '保存失败', icon: 'none' });
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
