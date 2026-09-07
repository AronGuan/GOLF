/**
 * 网络请求封装。
 *
 * 页面内**禁止**裸调 wx.request / wx.uploadFile，一律走本模块。
 * 真机调试请把 BASE_URL 改成开发机的局域网 IP（并在开发者工具勾选
 * 「不校验合法域名、web-view、TLS 版本以及 HTTPS 证书」）。
 *
 * v2（架构 §6.5）：三条 URL 改 PDD 主路径；上传携带 ``camera_view``；
 * 错误码映射新增 PDD 码（10001/10002/20001/20002）。
 */

/** 后端基地址 */
const BASE_URL = 'http://39.102.63.30:8000';

/** 接口前缀 */
const API_PREFIX = '/api/v1';

/** 请求超时（毫秒） */
const TIMEOUT = 20000;

/**
 * token 本地存储 key（与 app.js 保持一致，失效时清理）。
 */
const TOKEN_KEY = 'golf_token';

/**
 * 读取登录态 token。优先级：globalData > 持久化缓存。
 * @return {string}
 */
function _readToken() {
  try {
    const app = getApp();
    if (app && app.globalData && app.globalData.token) {
      return app.globalData.token;
    }
    const cached = wx.getStorageSync(TOKEN_KEY);
    return cached || '';
  } catch (e) {
    return '';
  }
}

/**
 * 清理登录态。token 失效（后端用匿名降级了）或用户主动登出时调用。
 */
function _clearToken() {
  try {
    const app = getApp();
    if (app && app.globalData) {
      app.globalData.token = '';
      app.globalData.user = null;
      app.globalData.expiresAt = '';
    }
    wx.removeStorageSync(TOKEN_KEY);
  } catch (e) {
    // 静默
  }
}

/**
 * 后端业务错误码 -> 中文文案（兜底用；优先使用后端下发的 error_message）
 */
const ERROR_MESSAGES = {
  NO_PERSON: '没有检测到人物，请确保全身在画面内后重拍',
  NO_SWING: '没有识别到完整的挥杆动作，请拍摄从站位到收杆的完整过程',
  TOO_DARK: '画面过暗，建议在光线充足的环境下拍摄',
  LOW_QUALITY: '人物识别不稳定，请固定手机、避免遮挡后重拍',
  BAD_VIDEO: '视频无法解析，请换一段 mp4 视频重试',
  BAD_ORIENTATION: '检测到视频方向异常，请将手机竖向拍摄后重试',
  TIMEOUT: '当前系统比较繁忙，请稍后再试',
  INTERNAL: '分析失败了，请稍后重试'
};

/** PDD 对外错误码 -> 中文文案（v2 新增） */
const PDD_ERROR_MESSAGES = {
  10001: '视频大小超过 40MB',
  10002: '只支持 mp4 / mov 格式的视频',
  10003: '视频时长需在 2~20 秒之间',
  10004: '服务器内部错误，请稍后重试',
  10005: '检测到视频方向异常，请将手机竖向拍摄后重试',
  20001: '任务不存在或已过期',
  20002: '任务尚未完成，请稍后再试',
  20003: '帧号超出可调整范围',
  20004: '阶段参数不合法'
};

/**
 * 按错误码取中文文案。
 * @param {string|number} code 后端 ErrorCode 或 PDD 错误码
 * @param {string} [fallback] 后端已下发的文案，优先使用
 * @return {string}
 */
function messageOf(code, fallback) {
  if (fallback) {
    return fallback;
  }
  if (typeof code === 'number') {
    return PDD_ERROR_MESSAGES[code] || ERROR_MESSAGES.INTERNAL;
  }
  return ERROR_MESSAGES[code] || ERROR_MESSAGES.INTERNAL;
}

/**
 * 解析统一响应包。
 * @param {*} raw wx 返回的 data（对象或 JSON 字符串）
 * @return {{code:number, data:*, message:string}}
 */
function parseEnvelope(raw) {
  let body = raw;
  if (typeof body === 'string') {
    try {
      body = JSON.parse(body);
    } catch (e) {
      return { code: 5000, data: null, message: '服务器返回格式异常' };
    }
  }
  if (!body || typeof body !== 'object') {
    return { code: 5000, data: null, message: '服务器返回格式异常' };
  }
  return {
    code: typeof body.code === 'number' ? body.code : 5000,
    data: body.data === undefined ? null : body.data,
    message: body.message || ''
  };
}

/**
 * 通用请求。
 * @param {{url:string, method?:string, data?:object, header?:object}} options
 * @return {Promise<*>} resolve 统一响应包的 data
 */
function request(options) {
  const { url, method = 'GET', data = {}, header = {} } = options;
  // 自动注入登录 token。空 token 时不发 Authorization 头（匿名调用）。
  const token = _readToken();
  const authHeader = token ? { Authorization: 'Bearer ' + token } : {};
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + API_PREFIX + url,
      method,
      data,
      timeout: TIMEOUT,
      header: Object.assign({ 'content-type': 'application/json' }, authHeader, header),
      success(res) {
        const body = parseEnvelope(res.data);
        if (body.code === 0) {
          resolve(body.data);
        } else {
          reject({ code: body.code, message: body.message || '请求失败' });
        }
      },
      fail() {
        reject({ code: -1, message: '网络连接失败，请检查后端服务是否已启动' });
      }
    });
  });
}

/**
 * 上传视频并创建任务（PDD 主路径 /api/v1/task/create，字段名 video）。
 * @param {string} filePath 本地临时文件路径
 * @param {string} [cameraView] 'face_on' | 'down_the_line' | 'auto'
 * @param {function(number):void} [onProgress] 上传百分比回调 0~100
 * @return {Promise<{task_id:string, status:string}>}
 */
function uploadVideo(filePath, cameraView, onProgress) {
  // 2026-09-04：默认从 'face_on' 改为 'auto'，与后端 _parse_camera_view 默认对齐
  const view = cameraView || 'auto';
  // 2026-09-07：补注入 Authorization 头。之前漏写导致即使已登录，
  // 后端 create_task 也读不到 openid，operation_logs.upload 行是 NULL，
  // state.openid 也是 NULL——任务就成「孤儿」，查不到归属。
  // 修法对齐 uploadAvatar：token 来自 _readToken()，空 token 不发头（匿名）。
  const token = _readToken();
  const authHeader = token ? { Authorization: 'Bearer ' + token } : {};
  return new Promise((resolve, reject) => {
    const task = wx.uploadFile({
      url: BASE_URL + API_PREFIX + '/task/create',
      filePath,
      name: 'video',
      formData: { camera_view: view },
      header: authHeader,
      timeout: 120000,
      success(res) {
        // 注意：wx.uploadFile 的 res.data 是字符串，必须 JSON.parse
        const body = parseEnvelope(res.data);
        if (body.code === 0 && body.data && body.data.task_id) {
          resolve(body.data);
        } else {
          reject({ code: body.code, message: body.message || '上传失败，请重试' });
        }
      },
      fail() {
        reject({ code: -1, message: '上传失败，请检查网络后重试' });
      }
    });

    if (typeof onProgress === 'function' && task && task.onProgressUpdate) {
      task.onProgressUpdate((res) => {
        onProgress(res.progress || 0);
      });
    }
  });
}

/**
 * 查询任务状态（PDD 主路径 /api/v1/task/status/{id}）。
 * @param {string} taskId
 * @return {Promise<object>}
 */
function getTaskStatus(taskId) {
  return request({ url: '/task/status/' + taskId });
}

/**
 * 获取完整分析结果（PDD 主路径 /api/v1/task/result/{id}）。
 * @param {string} taskId
 * @return {Promise<object>}
 */
function getResult(taskId) {
  return request({ url: '/task/result/' + taskId });
}

/**
 * 健康检查（调试用）。
 * @return {Promise<object>}
 */
function health() {
  return request({ url: '/health' });
}

/**
 * 把 ArrayBuffer 按字节解码成字符串（解析错误包 JSON 用，不引入 TextDecoder）。
 * @param {ArrayBuffer} buf
 * @return {string}
 */
function bytesToText(buf) {
  if (!buf) return '';
  const bytes = new Uint8Array(buf);
  let out = '';
  for (let i = 0; i < bytes.length; i += 1) {
    out += String.fromCharCode(bytes[i]);
  }
  return out;
}

/**
 * 拉取指定帧的骨架叠加图 PNG（结果页缩略图 ◀▶ 手动微调，v3 新增）。
 *
 * 后端返回二进制 PNG（非统一 JSON 包），故不走 request() 封装；响应头
 * ``X-Frame-Index`` 回传实际渲染帧号（降采样视频中间帧会被快照到最近采样帧）。
 * 图片写入本地临时文件后返回，供 <image> 直接使用。
 *
 * @param {string} taskId 任务 ID
 * @param {number} frameIndex 原视频帧号
 * @return {Promise<{tempFilePath:string, frameIndex:number}>}
 */
function getFrameImage(taskId, frameIndex) {
  const url = BASE_URL + API_PREFIX + '/task/' + taskId + '/frame/' + frameIndex;
  return new Promise((resolve, reject) => {
    wx.request({
      url,
      method: 'GET',
      responseType: 'arraybuffer',
      timeout: TIMEOUT,
      header: { 'content-type': 'application/json' },
      success(res) {
        if (res.statusCode === 200 && res.data) {
          const raw = res.header || {};
          let actual = Number(raw['X-Frame-Index']);
          if (!isFinite(actual)) actual = Number(raw['x-frame-index']);
          if (!isFinite(actual)) actual = frameIndex;
          const filePath =
            wx.env.USER_DATA_PATH + '/frame_' + taskId + '_' + frameIndex + '.png';
          wx.getFileSystemManager().writeFile({
            filePath,
            data: res.data,
            encoding: 'binary',
            success() {
              resolve({ tempFilePath: filePath, frameIndex: actual });
            },
            fail() {
              reject({ code: -1, message: '图片保存失败，请重试' });
            }
          });
        } else if (res.statusCode >= 400) {
          // 错误响应是统一 JSON 包（ArrayBuffer 形式），解码后解析
          let body = { code: -1, message: '请求失败' };
          try {
            body = JSON.parse(bytesToText(res.data));
          } catch (e) {
            // 保持默认 body
          }
          reject({
            code: typeof body.code === 'number' ? body.code : -1,
            message: body.message || '请求失败'
          });
        } else {
          reject({ code: -1, message: '请求失败' });
        }
      },
      fail() {
        reject({ code: -1, message: '网络连接失败，请检查后端服务是否已启动' });
      }
    });
  });
}

/**
 * 手动微调时实时重算目标阶段指标（v3 新增，纯增量）。
 *
 * 走统一 JSON 包 ``request()`` 封装；后端返回
 * ``{phase, frame_index, metrics}``，其中 ``metrics`` 为当前帧下重算出的
 * ``StageMetric`` 数组（字段与结果页一致，可直接交给 ``decorate()``）。
 *
 * @param {string} taskId 任务 ID
 * @param {string} phase PhaseKey 值（如 'downswing'）
 * @param {number} frameIndex 原视频帧号
 * @return {Promise<{phase:string, frame_index:number, metrics:Array}>}
 */
function getPhaseMetrics(taskId, phase, frameIndex) {
  return request({
    url: '/task/' + taskId + '/phase_metrics/' + phase + '/' + frameIndex
  });
}

// ---------------------------------------------------------------------------
// 登录与用户资料（M1）
//
// 设计：登录是增强项，失败一律静默降级为匿名。调用方只需
// ``.catch(() => null)`` 兜住所有错误，不能让登录失败阻塞主流程。
// ---------------------------------------------------------------------------

/**
 * 微信静默登录。成功后 token 自动写入 ``globalData`` + 本地缓存，
 * **后续所有 request() 会自动带上 Authorization 头**，调用方无须重复传。
 *
 * @param {string} code 来自 ``wx.login({success: r => r.code})`` 的临时凭证
 * @return {Promise<{token, expires_at, is_new_user, user}|null>}
 *         失败时返回 ``null``（不抛异常，调用方 ``catch(() => null)``）。
 */
function login(code) {
  return request({
    url: '/auth/login',
    method: 'POST',
    data: { code }
  })
    .then((data) => {
      try {
        const app = getApp();
        if (app && app.globalData) {
          app.globalData.token = data.token;
          app.globalData.user = data.user;
          app.globalData.expiresAt = data.expires_at;
          app.globalData.loginState = 'ok';
        }
        wx.setStorageSync(TOKEN_KEY, data.token);
      } catch (e) {
        // 写入失败不阻断登录成功
      }
      return data;
    })
    .catch(() => null);
}

/**
 * 拉取当前用户。**未登录返回 ``{logged_in:false}`` 不是错误**。
 * @return {Promise<{logged_in:boolean, user:object|null}>}
 */
function getMe() {
  return request({ url: '/auth/me', method: 'GET' });
}

/**
 * 退出登录：撤销后端 token + 清理本地缓存。
 * 即便后端调用失败也会清理本地，保证用户视角「确实退出了」。
 * @return {Promise<{ok:boolean}>}
 */
function logout() {
  return request({ url: '/auth/logout', method: 'POST' })
    .catch(() => null)
    .then(() => {
      _clearToken();
      return { ok: true };
    });
}

/**
 * 拉取当前用户的历史分析任务。
 *
 * TODO: M3 接入 —— 当前后端 ``GET /api/v1/tasks/mine`` 尚未实现，
 * 调用必返回 404，因此本期返回空数组使页面渲染空态。
 * 接口就绪后只需把下面这一行换成：
 *   return request({ url: '/tasks/mine', method: 'GET' });
 *
 * @return {Promise<{tasks: Array}>}
 */
function getMyTasks() {
  return Promise.resolve({ tasks: [] });
}

// ---------------------------------------------------------------------------
// 用户资料修改（M2.5）
//
// 与 M1 一致：失败时**不抛异常**，返回 ``null`` 让调用方统一处理。
// 调用方可以 ``.catch(() => null)`` 或直接 await + null 检查。
// ---------------------------------------------------------------------------

/**
 * 上传头像。M2.5 微信组件已内置内容安全检测，无需服务端 imgSecCheck。
 *
 * @param {string} filePath 本地临时路径（如 chooseAvatar 回调的 ``e.detail.avatarUrl``）
 * @return {Promise<{avatar_url:string, updated_at:string}|null>}
 */
function uploadAvatar(filePath) {
  return new Promise((resolve) => {
    const token = _readToken();
    const authHeader = token ? { Authorization: 'Bearer ' + token } : {};
    wx.uploadFile({
      url: BASE_URL + API_PREFIX + '/user/avatar',
      filePath,
      name: 'file',
      timeout: 30000,
      header: authHeader,
      success(res) {
        const body = parseEnvelope(res.data);
        if (body.code === 0 && body.data) {
          resolve(body.data);
        } else {
          // 业务码 → 中文（20005 频率超限等）
          resolve(null);
        }
      },
      fail() {
        resolve(null);
      }
    });
  });
}

/**
 * 更新昵称。
 * @param {string} nickname 1~16 字符（由后端做长度校验）
 * @return {Promise<{nickname:string, nickname_custom:boolean, updated_at:string}|null>}
 */
function updateNickname(nickname) {
  return request({
    url: '/user/profile',
    method: 'POST',
    data: { nickname }
  }).catch(() => null);
}

module.exports = {
  BASE_URL,
  API_PREFIX,
  ERROR_MESSAGES,
  PDD_ERROR_MESSAGES,
  messageOf,
  request,
  uploadVideo,
  getTaskStatus,
  getResult,
  getFrameImage,
  getPhaseMetrics,
  health,
  // M1 登录
  login,
  getMe,
  logout,
  getMyTasks,
  // M2.5 用户资料
  uploadAvatar,
  updateNickname
};
