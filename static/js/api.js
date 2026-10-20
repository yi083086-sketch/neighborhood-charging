/**
 * API 客户端：令牌管理、统一错误处理、401 自动续期、SSE 流式读取。
 * 后端错误格式：{ detail: { code, message } }；写接口响应含 trace_id。
 */
const API = {
  base: '/api',

  // ---------- 令牌 ----------
  getToken() { return localStorage.getItem('access_token'); },
  getRefresh() { return localStorage.getItem('refresh_token'); },
  getUser() {
    try { return JSON.parse(localStorage.getItem('user') || 'null'); }
    catch { return null; }
  },
  saveSession(data) {
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    if (data.user) localStorage.setItem('user', JSON.stringify(data.user));
  },
  clearSession() {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    localStorage.removeItem('user');
  },

  // ---------- 基础请求 ----------
  async request(method, path, { body, headers = {}, auth = true, retry = true } = {}) {
    const opts = { method, headers: { ...headers } };
    if (auth && this.getToken()) opts.headers['Authorization'] = 'Bearer ' + this.getToken();
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(this.base + path, opts);

    // 401 时尝试用 refresh_token 续期一次
    if (res.status === 401 && auth && retry && this.getRefresh()) {
      const ok = await this.tryRefresh();
      if (ok) return this.request(method, path, { body, headers, auth, retry: false });
      this.clearSession();
      showLogin();
      throw new ApiError('TOKEN_EXPIRED', '登录已过期，请重新登录');
    }

    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }

    if (!res.ok) {
      const detail = data && data.detail;
      const code = detail && detail.code ? detail.code : 'HTTP_' + res.status;
      const message = detail && detail.message ? detail.message : ('请求失败（' + res.status + '）');
      throw new ApiError(code, message, res.status);
    }
    return data;
  },

  async tryRefresh() {
    try {
      const res = await fetch(this.base + '/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: this.getRefresh() }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      this.saveSession(data);
      return true;
    } catch {
      return false;
    }
  },

  // ---------- SSE 流式（Agent 对话） ----------
  // 行格式：`data: {json}`，结束标记 `data: [DONE]`
  async stream(path, body, onEvent) {
    const res = await fetch(this.base + path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + this.getToken(),
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = null;
      try { detail = (await res.json()).detail; } catch { /* ignore */ }
      throw new ApiError(detail?.code || 'HTTP_' + res.status,
        detail?.message || '请求失败（' + res.status + '）', res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (payload === '[DONE]') return;
          try { onEvent(JSON.parse(payload)); } catch { /* 忽略非 JSON 行 */ }
        }
      }
    }
  },
};

class ApiError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

// 幂等键生成（订单等写接口必须携带）
function newIdempotencyKey() {
  return (crypto.randomUUID ? crypto.randomUUID() :
    Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
}
