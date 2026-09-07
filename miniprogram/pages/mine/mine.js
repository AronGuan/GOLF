/**
 * 我的页面（tabBar 双入口之一）。
 *
 * 职责：展示当前登录用户（头像 / 昵称 / 脱敏 openid）、历史分析空态，
 *       以及退出登录。
 *
 * 注意：
 *   - 头像与昵称本期**只做展示**，不做修改（chooseAvatar / nickname 输入
 *     需微信隐私保护指引审核通过，仍在审核中），页面不含任何编辑入口。
 *   - 这是 tabBar 页：onLoad 只执行一次，切回走 onShow，
 *     所以用户信息拉取放在 onShow，保证从首页切回来能刷新。
 */
const api = require('../../utils/api.js');

/** 默认头像色块的饱和度 / 亮度（后端只下发色相） */
const AVATAR_SATURATION = 45;
const AVATAR_LIGHTNESS = 55;

/** 未设置头像时的兜底色相 */
const AVATAR_HUE_FALLBACK = 210;

Page({
  data: {
    /** 是否已登录（未登录是正常状态，不是错误） */
    loggedIn: false,
    /** @type {object|null} 装饰后的 user，供 WXML 直接使用 */
    user: null,
    /** 是否正在拉取用户信息（防重复点击） */
    loadingMe: false,
    /** 是否正在退出登录（防重复点击） */
    loggingOut: false,
    /** 是否正在上传头像（防重复点击） */
    savingAvatar: false,
    /** 是否正在保存昵称（防重复点击） */
    savingNickname: false
  },

  onShow() {
    this._loadMe();
    this._loadHistory();
  },

  /**
   * 拉取当前登录用户（GET /api/v1/auth/me）。
   *
   * 未登录是 200 + logged_in=false 的正常状态，不弹错误 toast；
   * 请求失败同样静默处理，保持页面可用、不白屏。
   * @return {Promise<void>}
   * @private
   */
  _loadMe() {
    this.setData({ loadingMe: true });
    return api
      .getMe()
      .then((data) => {
        const loggedIn = !!(data && data.logged_in && data.user);
        this.setData({
          loggedIn,
          user: loggedIn ? this._decorate(data.user) : null,
          loadingMe: false
        });
      })
      .catch(() => {
        // 静默失败：保留上一次的渲染结果
        this.setData({ loadingMe: false });
      });
  },

  /**
   * 拉取历史分析记录。
   *
   * TODO: M3 接入 GET /api/v1/tasks/mine
   * 后端该接口尚未实现（排期在后），现在发请求必然 404，
   * 因此本期不发请求，页面固定渲染空态。接口就绪后替换为：
   *   api.getMyTasks().then((data) => { this.setData({ tasks: data.tasks }); })
   *                   .catch(() => { 静默 });
   * @private
   */
  _loadHistory() {
    // 预留：M3 接入 GET /api/v1/tasks/mine
  },

  /**
   * 把后端 user 装饰成 WXML 可直接使用的视图模型。
   * @param {object} raw GET /api/v1/auth/me 返回的 user
   * @return {object}
   * @private
   */
  _decorate(raw) {
    const nickname = raw.nickname || '';
    const hue =
      typeof raw.avatar_hue === 'number' ? raw.avatar_hue % 360 : AVATAR_HUE_FALLBACK;
    const avatarUrl = raw.avatar_url || '';
    return {
      nickname: nickname || '球手',
      nicknameCustom: !!raw.nickname_custom,
      openidMasked: raw.openid_masked || '',
      loginCount: typeof raw.login_count === 'number' ? raw.login_count : 0,
      createdText: (raw.created_at || '').slice(0, 10) || '--',
      /** avatar_url 为空串表示用户没设置过头像，走色块兜底 */
      hasAvatar: avatarUrl !== '',
      avatarUrl,
      avatarText: this._initial(nickname),
      avatarStyle:
        'background:hsl(' +
        hue +
        ',' +
        AVATAR_SATURATION +
        '%,' +
        AVATAR_LIGHTNESS +
        '%);'
    };
  },

  /**
   * 取昵称首字（用 Array.from 兼容 emoji 等代理对字符）。
   * @param {string} nickname
   * @return {string}
   * @private
   */
  _initial(nickname) {
    const chars = Array.from(nickname);
    return chars.length ? chars[0] : '?';
  },

  /**
   * 未登录时点「重新登录」。
   *
   * 登录流程由 app.js 在启动时用 wx.login 的 code 静默完成，本页不重复
   * 发起授权；仅当 api.js 已提供 login() 时补一次登录，否则只重查登录态。
   */
  onRetryLogin() {
    if (this.data.loadingMe) {
      return;
    }
    if (typeof api.login !== 'function') {
      this._loadMe();
      return;
    }
    wx.login({
      success: (res) => {
        api
          .login(res.code)
          .then(() => this._loadMe())
          .catch(() => {
            // 静默：未登录是正常状态
          })
          .then(() => {
            this.setData({ loadingMe: false });
          });
      },
      fail: () => {
        this.setData({ loadingMe: false });
      }
    });
  },

  /** 空态引导：跳回首页去拍视频分析（tabBar 页必须用 switchTab） */
  onGoAnalyze() {
    wx.switchTab({ url: '/pages/index/index' });
  },

  /** 退出登录（二次确认，避免误触） */
  onLogout() {
    if (this.data.loggingOut) {
      return;
    }
    wx.showModal({
      title: '退出登录',
      content: '退出后需要重新登录才能查看历史记录，确定继续吗？',
      confirmColor: '#1D9E75',
      success: (res) => {
        if (res.confirm) {
          this._doLogout();
        }
      }
    });
  },

  /**
   * 调用退出登录接口并回到未登录降级态。
   * @private
   */
  _doLogout() {
    this.setData({ loggingOut: true });
    api
      .logout()
      .then(() => {
        this.setData({ loggedIn: false, user: null, loggingOut: false });
      })
      .catch(() => {
        // 这是用户主动触发的操作，失败时给一次明确反馈
        this.setData({ loggingOut: false });
        wx.showToast({ title: '退出失败，请稍后重试', icon: 'none' });
      });
  },

  // -------------------------------------------------------------------------
  // M2.5 头像 / 昵称修改
  // -------------------------------------------------------------------------

  /**
   * 微信 ``chooseAvatar`` 回调（用户选了图）。保存到后端。
   *
   * ⚠️ **静默回调坑**：微信组件已内置内容安全检测，违规头像/被风控时
   * **不会触发 bindchooseavatar**，用户点了毫无反应像 bug。
   * 兜底：起 3 秒定时器，到点还没拿到回调就给提示。
   *
   * @param {object} e 微信回调，e.detail.avatarUrl 是本地临时路径
   */
  onChooseAvatar(e) {
    const avatarPath = (e && e.detail && e.detail.avatarUrl) || '';
    if (!avatarPath || this.data.savingAvatar) {
      return;
    }
    this.setData({ savingAvatar: true });

    // 兜底定时器：3 秒内没拿到 uploadAvatar resolve，就当作"被风控"
    const silentTimer = setTimeout(() => {
      if (this.data.savingAvatar) {
        this.setData({ savingAvatar: false });
        wx.showToast({
          title: '头像未通过安全检测，请更换',
          icon: 'none',
          duration: 2500
        });
      }
    }, 3000);

    api
      .uploadAvatar(avatarPath)
      .then((data) => {
        clearTimeout(silentTimer);
        this.setData({ savingAvatar: false });
        if (data) {
          // 成功：重新拉 me 刷新头像 URL（已含 ?v= 时间戳绕过缓存）
          this._loadMe();
          wx.showToast({ title: '头像已更新', icon: 'success', duration: 1200 });
        } else {
          // 后端拒绝（格式/大小/频率超限等）
          wx.showToast({ title: '头像保存失败，请稍后重试', icon: 'none' });
        }
      })
      .catch(() => {
        clearTimeout(silentTimer);
        this.setData({ savingAvatar: false });
        wx.showToast({ title: '头像保存失败，请稍后重试', icon: 'none' });
      });
  },

  /**
   * 昵称输入框失焦（用户编辑完后离开）。保存到后端。
   *
   * ⚠️ **异步清空坑**：微信内容安全检测违规昵称时，会**清空 input**
   * （不返回错误事件）。所以 ``e.detail.value`` 收到的是空串，无法
   * 区分"用户主动清空"和"被检测清空"。一律按"未修改"处理。
   *
   * @param {object} e 微信回调，e.detail.value 是当前输入值
   */
  onNicknameBlur(e) {
    const nickname = (e && e.detail && (e.detail.value || '')).trim();
    const current = this.data.user ? this.data.user.nickname : '';
    if (!nickname || nickname === current || this.data.savingNickname) {
      return;
    }
    this.setData({ savingNickname: true });

    api
      .updateNickname(nickname)
      .then((data) => {
        this.setData({ savingNickname: false });
        if (data) {
          this._loadMe();
          wx.showToast({ title: '昵称已更新', icon: 'success', duration: 1200 });
        } else {
          // 后端拒绝（长度/频率/格式等）
          wx.showToast({ title: '昵称保存失败，请稍后重试', icon: 'none' });
        }
      })
      .catch(() => {
        this.setData({ savingNickname: false });
        wx.showToast({ title: '昵称保存失败，请稍后重试', icon: 'none' });
      });
  }
});
