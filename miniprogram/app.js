/**
 * 全局入口。
 *
 * 本期不做登录、不做历史记录（PRD §4.1 克制说明），
 * globalData 只承担「跨页兜底缓存」的职责：
 *   - taskId: 最近一次创建的任务 id
 *   - result: 最近一次成功的分析结果（结果页优先用它，拉取失败时兜底）
 */
App({
  globalData: {
    /** @type {string} 最近一次任务 id */
    taskId: '',
    /** @type {object|null} 最近一次分析结果 */
    result: null,
    /** @type {object|null} 最近一次选择的视频信息 */
    lastVideo: null,
    /** @type {string} 用户选择的机位（v2）：'face_on' | 'down_the_line' */
    cameraView: 'face_on'
  },

  onLaunch() {
    // 兜底：把上次结果从本地缓存恢复（关掉小程序即失效属预期，这里只做同会话兜底）
    try {
      const cached = wx.getStorageSync('golf_last_result');
      if (cached && cached.task_id) {
        this.globalData.result = cached;
        this.globalData.taskId = cached.task_id;
      }
    } catch (e) {
      // 读取缓存失败不影响启动
    }
  },

  /**
   * 缓存一次成功的分析结果。
   * @param {object} result AnalysisResult
   */
  setResult(result) {
    if (!result) {
      return;
    }
    this.globalData.result = result;
    this.globalData.taskId = result.task_id || this.globalData.taskId;
    try {
      wx.setStorageSync('golf_last_result', result);
    } catch (e) {
      // 存储失败不影响主流程
    }
  },

  /**
   * 取指定任务的缓存结果。
   * @param {string} taskId
   * @return {object|null}
   */
  getResult(taskId) {
    const cached = this.globalData.result;
    if (cached && (!taskId || cached.task_id === taskId)) {
      return cached;
    }
    return null;
  }
});
