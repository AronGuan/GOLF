/**
 * 网络请求封装。
 *
 * 页面内**禁止**裸调 wx.request / wx.uploadFile，一律走本模块。
 * 真机调试请把 BASE_URL 改成开发机的局域网 IP（并在开发者工具勾选
 * 「不校验合法域名、web-view、TLS 版本以及 HTTPS 证书」）。
 */

/** 后端基地址 */
const BASE_URL = 'http://39.102.63.30:8000';

/** 接口前缀 */
const API_PREFIX = '/api/v1';

/** 请求超时（毫秒） */
const TIMEOUT = 20000;

/**
 * 后端错误码 -> 中文文案（兜底用；优先使用后端下发的 error_message）
 */
const ERROR_MESSAGES = {
  NO_PERSON: '没有检测到人物，请确保全身在画面内后重拍',
  NO_SWING: '没有识别到完整的挥杆动作，请拍摄从站位到收杆的完整过程',
  TOO_DARK: '画面过暗，建议在光线充足的环境下拍摄',
  LOW_QUALITY: '人物识别不稳定，请固定手机、避免遮挡后重拍',
  BAD_VIDEO: '视频无法解析，请换一段 mp4 视频重试',
  TIMEOUT: '分析超时了，请稍后重试',
  INTERNAL: '分析失败了，请稍后重试'
};

/**
 * 按错误码取中文文案。
 * @param {string} code 后端 ErrorCode
 * @param {string} [fallback] 后端已下发的文案，优先使用
 * @return {string}
 */
function messageOf(code, fallback) {
  if (fallback) {
    return fallback;
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
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + API_PREFIX + url,
      method,
      data,
      timeout: TIMEOUT,
      header: Object.assign({ 'content-type': 'application/json' }, header),
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
 * 上传视频并创建任务。
 * @param {string} filePath 本地临时文件路径
 * @param {function(number):void} [onProgress] 上传百分比回调 0~100
 * @return {Promise<{task_id:string, status:string}>}
 */
function uploadVideo(filePath, onProgress) {
  return new Promise((resolve, reject) => {
    const task = wx.uploadFile({
      url: BASE_URL + API_PREFIX + '/tasks',
      filePath,
      name: 'file',
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
 * 查询任务状态。
 * @param {string} taskId
 * @return {Promise<object>}
 */
function getTaskStatus(taskId) {
  return request({ url: '/tasks/' + taskId });
}

/**
 * 获取完整分析结果。
 * @param {string} taskId
 * @return {Promise<object>}
 */
function getResult(taskId) {
  return request({ url: '/tasks/' + taskId + '/result' });
}

/**
 * 健康检查（调试用）。
 * @return {Promise<object>}
 */
function health() {
  return request({ url: '/health' });
}

module.exports = {
  BASE_URL,
  API_PREFIX,
  ERROR_MESSAGES,
  messageOf,
  request,
  uploadVideo,
  getTaskStatus,
  getResult,
  health
};
