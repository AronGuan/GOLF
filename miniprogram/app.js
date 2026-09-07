/**
 * 全局入口。
 *
 * 职责：
 *   - 跨页兜底缓存（最近一次任务 id / 结果）
 *   - **静默登录单例**（M1）：onLaunch 触发一次 wx.login + /auth/login，
 *     后续 ``ensureLogin()`` 共享同一 Promise，避免 onLaunch 与页面 onLoad
 *     并发触发导致 code 被重复使用（微信 40163）
 *
 * 登录是增强项，不是门槛。任何失败（网络 / 微信 / 后端）都把 loginState
 * 标记为 ``anonymous``，主流程照常可用。失败永不阻断上传分析。
 */
const api = require('./utils/api.js');

/**
 * @typedef {'pending'|'ok'|'anonymous'} LoginState
 * - pending  : onLaunch 后第一次 ensureLogin 还在跑
 * - ok       : token 有效，user 可用
 * - anonymous: 登录失败或未启用，本机位不会有 user 但分析功能照常
 */

App({
  globalData: {
    /** @type {string} 最近一次任务 id */
    taskId: '',
    /** @type {object|null} 最近一次分析结果 */
    result: null,
    /** @type {object|null} 最近一次选择的视频信息 */
    lastVideo: null,
    /** @type {string} 用户选择的机位（v2）：'face_on' | 'down_the_line' | 'auto' */
    cameraView: 'auto',

    /** M1 登录态（由 ensureLogin 写入） */
    /** @type {string} 登录 token；空串 = 未登录 */
    token: '',
    /** @type {object|null} 用户基本信息（脱敏 openid / 昵称 / 头像） */
    user: null,
    /** @type {string} token 过期时间（ISO 字符串） */
    expiresAt: '',
    /** @type {LoginState} */
    loginState: 'pending'
  },

  /** 登录 Promise 单例：避免 onLaunch 与页面 onLoad 并发触发 wx.login */
  _loginPromise: null,

  onLaunch() {
    // 1. 静默登录（异步、不阻塞 UI、失败静默）
    this.ensureLogin().catch(() => {});

    // 2. 兜底：把上次结果从本地缓存恢复（关掉小程序即失效属预期，这里只做同会话兜底）
    try {
      const cached = wx.getStorageSync('golf_last_result');
      if (cached && cached.task_id) {
        this.globalData.result = cached;
        this.globalData.taskId = cached.task_id;
      }
      // token 也尝试从缓存恢复（同会话内跨冷启动用；用户主动 logout 会清掉）
      if (!this.globalData.token) {
        const cachedToken = wx.getStorageSync('golf_token');
        if (cachedToken) {
          this.globalData.token = cachedToken;
          this.globalData.loginState = 'pending';
          // 用 token 后台补一次 /auth/me 拿 user；失败就当匿名
          this._refreshMe().catch(() => {});
        }
      }
    } catch (e) {
      // 读取缓存失败不影响启动
    }
  },

  /**
   * 取当前登录 Promise。**多个并发调用方共享同一 Promise**：
   * - 已完成 -> 直接返回当前 globalData（轻量 wrapper）
   * - 进行中 -> 返回同一 Promise（不再发起新的 wx.login）
   * - 未开始 -> 启动新的登录
   *
   * @return {Promise<globalData>} 永远 resolve，永不 reject
   */
  ensureLogin() {
    // 有 token 就视为「已就绪」，但 state 还是 pending 时允许后台刷新
    if (this.globalData.token && this.globalData.loginState === 'ok') {
      return Promise.resolve(this.globalData);
    }
    if (this._loginPromise) {
      return this._loginPromise;
    }
    this._loginPromise = new Promise((resolve) => {
      // wx.login 可能在某些环境直接 fail（无网络/被禁），用 fail 兜底
      wx.login({
        success: (loginRes) => {
          if (!loginRes || !loginRes.code) {
            this.globalData.loginState = 'anonymous';
            resolve(this.globalData);
            return;
          }
          api
            .login(loginRes.code)
            .then((data) => {
              if (data) {
                // api.login 已把 token/user 写进 globalData 与缓存，这里只标状态
                this.globalData.loginState = 'ok';
              } else {
                this.globalData.loginState = 'anonymous';
              }
              resolve(this.globalData);
            })
            .catch(() => {
              this.globalData.loginState = 'anonymous';
              resolve(this.globalData);
            });
        },
        fail: () => {
          this.globalData.loginState = 'anonymous';
          resolve(this.globalData);
        },
        complete: () => {
          // 释放单例槽位，允许下一次 ensureLogin 重新发起（罕见，例如 token 过期后）
          this._loginPromise = null;
        }
      });
    });
    return this._loginPromise;
  },

  /**
   * 用已有 token 拉一次 ``/auth/me`` 刷新 user。
   * 用于：onLaunch 从缓存恢复 token 后、登出/被踢后。
   * @private
   */
  _refreshMe() {
    return api.getMe().then((data) => {
      if (data && data.logged_in && data.user) {
        this.globalData.user = data.user;
        this.globalData.loginState = 'ok';
      } else {
        // token 失效（被撤销/过期），清理
        this.globalData.token = '';
        this.globalData.user = null;
        this.globalData.loginState = 'anonymous';
        try {
          wx.removeStorageSync('golf_token');
        } catch (e) {
          // 静默
        }
      }
    });
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