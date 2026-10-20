/**
 * 邻里充电运营平台前端（原生 JS 单页应用）。
 * 菜单按角色渲染（体验优化），真正的鉴权与数据范围控制由后端完成。
 */

// ================= 常量与字典 =================

const ROLE_NAMES = {
  admin: '系统管理员', operator: '运营人员', maintenance: '维修人员',
  finance: '财务/审批人员', resident: '居民用户',
};

const PAGE_META = {
  dashboard: { title: '仪表盘', icon: '📊' },
  devices: { title: '设备管理', icon: '🔌' },
  reservations: { title: '充电预约', icon: '📅' },
  orders: { title: '订单管理', icon: '🧾' },
  tickets: { title: '工单与维修', icon: '🛠️' },
  approvals: { title: '审批中心', icon: '✅' },
  reports: { title: '运营报表', icon: '📈' },
  agent: { title: 'Agent 工作台', icon: '🤖' },
  system: { title: '系统管理', icon: '⚙️' },
};

const MENUS = {
  admin: ['dashboard', 'devices', 'reservations', 'orders', 'tickets', 'approvals', 'reports', 'agent', 'system'],
  operator: ['dashboard', 'devices', 'orders', 'tickets', 'reports', 'agent'],
  maintenance: ['dashboard', 'devices', 'tickets'],
  finance: ['approvals', 'orders', 'reports'],
  resident: ['devices', 'reservations', 'orders', 'agent'],
};

const PILE_STATUS = { online: ['在线', 'green'], fault: ['故障', 'red'], offline: ['离线', 'gray'], maintenance: ['检修', 'yellow'] };
const GUN_STATUS = { available: ['空闲', 'green'], charging: ['充电中', 'blue'], fault: ['故障', 'red'], offline: ['离线', 'gray'] };
const RES_STATUS = { active: ['生效中', 'green'], completed: ['已完成', 'gray'], cancelled: ['已取消', 'gray'] };
const ORDER_STATUS = {
  pending_payment: ['待支付', 'yellow'], reserved: ['已预约', 'blue'], charging: ['充电中', 'cyan'],
  completed: ['已完成', 'green'], cancelled: ['已取消', 'gray'], abnormal: ['异常', 'red'],
};
const TICKET_STATUS = {
  pending_dispatch: ['待分派', 'yellow'], dispatched: ['已分派', 'blue'], accepted: ['已接单', 'cyan'],
  processing: ['处理中', 'blue'], pending_acceptance: ['待验收', 'yellow'], closed: ['已关闭', 'green'],
};
const APPROVAL_STATUS = {
  pending: ['待审批', 'yellow'], approved: ['已批准', 'blue'], rejected: ['已驳回', 'gray'],
  executing: ['执行中', 'cyan'], executed: ['已执行', 'green'],
};
const APPROVAL_TYPES = { refund: '退款', shutdown: '停机', price_change: '调价', batch_reassign: '批量改派' };
const SEVERITY = { info: ['提示', 'gray'], warn: ['警告', 'yellow'], critical: ['严重', 'red'] };
const PRIORITY_COLOR = { P1: 'red', P2: 'yellow', P3: 'gray' };
const EVENT_TYPES = ['heartbeat', 'power', 'current', 'temperature', 'offline', 'charge_interrupted', 'fault', 'overload'];

// ================= 全局状态 =================

const REFS = { communities: [], zones: [], piles: [] };
let currentUser = null;

// ================= 小工具 =================

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(c));
  }
  return el;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toLocalInput(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function pct(v) { return v === null || v === undefined ? '—' : (v * 100).toFixed(1) + '%'; }

function statusBadge(map, key) {
  const [text, color] = map[key] || [key || '—', 'gray'];
  return h('span', { class: `badge badge-${color}` }, text);
}

function toast(message, type = 'info') {
  const root = document.getElementById('toast-root');
  const el = h('div', { class: `toast toast-${type}` }, message);
  root.append(el);
  setTimeout(() => el.remove(), 4200);
}

function okToast(message, data) {
  const tid = data && data.trace_id ? `（trace: ${String(data.trace_id).slice(0, 8)}…）` : '';
  toast(message + tid, 'success');
}

function errToast(err) {
  toast(err && err.message ? err.message : '操作失败', 'error');
}

function modal(title, body, footer, size = '') {
  const root = document.getElementById('modal-root');
  const box = h('div', { class: `modal ${size}` },
    h('div', { class: 'modal-header' },
      h('h3', {}, title),
      h('button', { class: 'modal-close', onclick: close }, '×')),
    h('div', { class: 'modal-body' }, body),
    footer ? h('div', { class: 'modal-footer' }, footer) : null);
  const mask = h('div', { class: 'modal-mask', onclick: e => { if (e.target === mask) close(); } }, box);
  function close() { mask.remove(); }
  root.append(mask);
  return { close, body: box.querySelector('.modal-body') };
}

function confirmBox(message, okText = '确认') {
  return new Promise(resolve => {
    const footer = h('div', {},
      h('button', { class: 'btn', onclick: () => { m.close(); resolve(false); } }, '取消'),
      h('button', { class: 'btn btn-primary', onclick: () => { m.close(); resolve(true); } }, okText));
    const m = modal('操作确认', h('p', {}, message), footer);
  });
}

function communityName(id) {
  const c = REFS.communities.find(x => x.id === id);
  return c ? c.name : (id || '—');
}
function pileById(id) { return REFS.piles.find(p => p.id === id); }
function pileName(id) { const p = pileById(id); return p ? p.name : (id || '—'); }
function gunLabel(gunId) {
  for (const p of REFS.piles) {
    const g = (p.guns || []).find(x => x.id === gunId);
    if (g) return `${p.name} · ${g.connector_no}号枪`;
  }
  return gunId || '—';
}

async function ensureRefs() {
  if (REFS.piles.length) return;
  const [cs, zs, ps] = await Promise.all([
    API.request('GET', '/communities'),
    API.request('GET', '/zones'),
    API.request('GET', '/piles'),
  ]);
  REFS.communities = cs.items || [];
  REFS.zones = zs.items || [];
  REFS.piles = ps.items || [];
}

function communitySelect(withAll, selected) {
  const sel = h('select', {},
    withAll ? h('option', { value: '' }, '全部社区') : null,
    REFS.communities.map(c => h('option', { value: c.id, selected: c.id === selected || undefined }, c.name)));
  return sel;
}

function gunOptions(pileFilter = null) {
  const opts = [];
  for (const p of REFS.piles) {
    if (pileFilter && p.id !== pileFilter) continue;
    for (const g of p.guns || []) {
      const st = GUN_STATUS[g.status] || [g.status];
      opts.push({
        value: g.id,
        label: `${p.name} · ${g.connector_no}号枪（${communityName(p.community_id)}，${st[0]}）`,
        unavailable: g.status === 'fault' || g.status === 'offline',
      });
    }
  }
  return opts;
}

function gunSelect(pileFilter = null, disableUnavailable = false) {
  return h('select', {},
    gunOptions(pileFilter).map(o =>
      h('option', { value: o.value, disabled: disableUnavailable && o.unavailable || undefined }, o.label)));
}

function emptyRow(colspan, text = '暂无数据') {
  return h('tr', {}, h('td', { colspan: String(colspan) }, h('div', { class: 'empty' }, text)));
}

// ================= 认证流程 =================

function showLogin() {
  document.getElementById('app-view').classList.add('hidden');
  document.getElementById('login-view').classList.remove('hidden');
  loadDemoAccounts();
}

async function loadDemoAccounts() {
  try {
    const meta = await API.request('GET', '/meta', { auth: false });
    if (!meta.demo_accounts || !meta.demo_accounts.length) return;
    const list = document.getElementById('demo-list');
    list.innerHTML = '';
    for (const a of meta.demo_accounts) {
      list.append(h('button', {
        class: 'demo-chip', type: 'button',
        onclick: () => {
          document.getElementById('login-username').value = a.username;
          document.getElementById('login-password').value = a.password;
        },
      }, a.username, h('span', { class: 'role-tag' }, a.role_name)));
    }
    document.getElementById('demo-accounts').classList.remove('hidden');
  } catch { /* meta 不可用时隐藏演示账号区 */ }
}

async function doLogin(e) {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  btn.disabled = true;
  try {
    const data = await API.request('POST', '/auth/login', {
      auth: false,
      body: {
        username: document.getElementById('login-username').value.trim(),
        password: document.getElementById('login-password').value,
      },
    });
    API.saveSession(data);
    toast(`欢迎回来，${data.user.name}`, 'success');
    await initApp();
  } catch (err) {
    errToast(err);
  } finally {
    btn.disabled = false;
  }
}

async function doLogout() {
  try { await API.request('POST', '/logout'); } catch { /* 忽略 */ }
  API.clearSession();
  showLogin();
}

// ================= 应用初始化与路由 =================

async function initApp() {
  currentUser = API.getUser();
  if (!currentUser) { showLogin(); return; }
  try {
    await ensureRefs();
  } catch (err) {
    if (err.status === 401) { showLogin(); return; }
    errToast(err);
  }
  document.getElementById('login-view').classList.add('hidden');
  document.getElementById('app-view').classList.remove('hidden');

  document.getElementById('user-avatar').textContent = (currentUser.name || currentUser.username)[0];
  document.getElementById('user-name').textContent = currentUser.name || currentUser.username;
  document.getElementById('user-role').textContent = ROLE_NAMES[currentUser.role] || currentUser.role;

  const nav = document.getElementById('nav-menu');
  nav.innerHTML = '';
  for (const page of MENUS[currentUser.role] || []) {
    const meta = PAGE_META[page];
    nav.append(h('div', {
      class: 'nav-item', 'data-page': page,
      onclick: () => { location.hash = '#/' + page; },
    }, h('span', { class: 'nav-icon' }, meta.icon), h('span', { class: 'nav-text' }, meta.title)));
  }
  if (!location.hash || !location.hash.startsWith('#/')) {
    location.hash = '#/' + (MENUS[currentUser.role] || ['dashboard'])[0];
  } else {
    route();
  }
}

const PAGES = {
  dashboard: renderDashboard,
  devices: renderDevices,
  reservations: renderReservations,
  orders: renderOrders,
  tickets: renderTickets,
  approvals: renderApprovals,
  reports: renderReports,
  agent: renderAgent,
  system: renderSystem,
};

async function route() {
  const page = (location.hash || '#/dashboard').slice(2);
  const allowed = MENUS[currentUser.role] || [];
  const target = allowed.includes(page) ? page : allowed[0];
  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.page === target));
  document.getElementById('page-title').textContent = PAGE_META[target].title;
  document.getElementById('topbar-right').innerHTML = '';
  document.getElementById('topbar-right').append(
    h('span', { class: 'badge badge-blue' }, ROLE_NAMES[currentUser.role] || currentUser.role));
  const content = document.getElementById('page-content');
  content.innerHTML = '';
  try {
    await PAGES[target](content);
  } catch (err) {
    if (err.status === 401) return;
    content.append(h('div', { class: 'card' }, h('div', { class: 'empty' }, '加载失败：' + err.message)));
  }
}

// ================= 仪表盘 =================

function metricCards(m) {
  const items = [
    ['充电桩', `${m.piles_online} / ${m.piles_total}`, '在线 / 总数'],
    ['设备可用率', pct(m.device_availability)],
    ['利用率', pct(m.utilization)],
    ['故障率', pct(m.fault_rate)],
    ['平均修复时长', m.mttr_hours === null ? '—' : m.mttr_hours + ' h'],
    ['预约成功率', pct(m.reservation_success_rate)],
    ['订单收入', '¥' + m.revenue],
    ['退款金额', '¥' + m.refund_amount],
    ['订单总数', m.orders_total],
    ['未关闭工单', m.open_tickets],
  ];
  return h('div', { class: 'metric-grid' }, items.map(([label, value]) =>
    h('div', { class: 'metric-card' },
      h('div', { class: 'metric-label' }, label),
      h('div', { class: 'metric-value' }, String(value)))));
}

async function renderDashboard(el) {
  const canFilter = ['admin', 'finance'].includes(currentUser.role) || (currentUser.communities || []).length > 1;
  const sel = communitySelect(true);
  const toolbar = h('div', { class: 'toolbar' },
    canFilter ? sel : h('span', { class: 'text-light' }, '数据范围：' + (currentUser.communities || []).map(communityName).join('、')),
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: load }, '刷新'));
  const body = h('div', {});
  el.append(toolbar, body);
  sel.addEventListener('change', load);

  async function load() {
    body.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const q = sel.value ? `?community_id=${sel.value}` : '';
      const m = await API.request('GET', '/dashboard' + q);
      body.innerHTML = '';
      body.append(metricCards(m));
      if (m.per_community && m.per_community.length) {
        body.append(h('div', { class: 'card mb0' },
          h('div', { class: 'card-title' }, '社区对比'),
          h('div', { class: 'table-wrap' }, h('table', {},
            h('thead', {}, h('tr', {},
              h('th', {}, '社区'), h('th', {}, '充电桩'), h('th', {}, '可用率'),
              h('th', {}, '收入'), h('th', {}, '待办工单'))),
            h('tbody', {}, m.per_community.map(c => h('tr', {},
              h('td', {}, c.community_name),
              h('td', {}, String(c.piles)),
              h('td', {}, pct(c.availability)),
              h('td', {}, '¥' + c.revenue),
              h('td', {}, String(c.open_tickets)))))))));
      }
    } catch (err) {
      body.innerHTML = '';
      body.append(h('div', { class: 'empty' }, err.message));
    }
  }
  await load();
}

// ================= 设备管理 =================

async function renderDevices(el) {
  const canIngest = ['admin', 'operator', 'maintenance'].includes(currentUser.role);
  const cSel = communitySelect(true);
  const sSel = h('select', {},
    h('option', { value: '' }, '全部状态'),
    ...Object.entries(PILE_STATUS).map(([k, v]) => h('option', { value: k }, v[0])));
  const toolbar = h('div', { class: 'toolbar' },
    cSel, sSel,
    h('div', { class: 'spacer' }),
    canIngest ? h('button', { class: 'btn', onclick: () => openEventModal() }, '上报设备事件') : null,
    h('button', { class: 'btn btn-primary', onclick: load }, '刷新'));
  const grid = h('div', { class: 'pile-grid' });
  const eventsCard = h('div', { class: 'card mt16 mb0' },
    h('div', { class: 'card-title' }, '近期设备事件'),
    h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, '加载中…')));
  el.append(toolbar, grid, eventsCard);
  cSel.addEventListener('change', load);
  sSel.addEventListener('change', load);

  async function load() {
    grid.innerHTML = '<div class="empty">加载中…</div>';
    const params = new URLSearchParams();
    if (cSel.value) params.set('community_id', cSel.value);
    if (sSel.value) params.set('status', sSel.value);
    const [ps, evs] = await Promise.all([
      API.request('GET', '/piles?' + params),
      API.request('GET', '/device-events?limit=30'),
    ]);
    grid.innerHTML = '';
    if (!ps.items.length) grid.append(h('div', { class: 'empty' }, '暂无设备'));
    for (const p of ps.items) {
      const zone = REFS.zones.find(z => z.id === p.zone_id);
      grid.append(h('div', { class: 'pile-card', onclick: () => openPileDetail(p.id) },
        h('div', { class: 'pile-head' },
          h('span', { class: 'pile-name' }, p.name),
          statusBadge(PILE_STATUS, p.status)),
        h('div', { class: 'pile-meta' },
          `${communityName(p.community_id)}${zone ? ' · ' + zone.name : ''}`,
          h('br'),
          `${p.power_kw}kW · 电价 ¥${p.price_kwh}/kWh · 时段费 ¥${p.price_hour}/h`,
          h('br'),
          `可用枪口 ${p.available_guns}/${p.gun_count}`),
        h('div', { class: 'gun-row' }, (p.guns || []).map(g =>
          h('span', { class: 'gun-chip' }, `${g.connector_no}号枪 `, statusBadge(GUN_STATUS, g.status))))));
    }
    // 事件表
    const wrap = eventsCard.querySelector('.table-wrap');
    wrap.innerHTML = '';
    const rows = evs.items || [];
    wrap.append(h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '事件编号'), h('th', {}, '设备'), h('th', {}, '类型'),
        h('th', {}, '级别'), h('th', {}, '详情'), h('th', {}, '时间'))),
      h('tbody', {}, rows.length ? rows.map(e => h('tr', {},
        h('td', { class: 'mono' }, e.event_no),
        h('td', {}, pileName(e.pile_id)),
        h('td', {}, e.type),
        h('td', {}, statusBadge(SEVERITY, e.severity)),
        h('td', { class: 'mono text-light' }, JSON.stringify(e.payload || {})),
        h('td', {}, fmtTime(e.ts)))) : emptyRow(6))));
  }
  await load();
}

async function openPileDetail(pileId) {
  let data;
  try {
    data = await API.request('GET', '/piles/' + pileId);
  } catch (err) { return errToast(err); }
  const { pile, recent_events, tickets } = data;
  const zone = REFS.zones.find(z => z.id === pile.zone_id);
  const body = h('div', {},
    h('div', { class: 'desc-grid' },
      desc('编号', pile.id), desc('状态', statusBadge(PILE_STATUS, pile.status)),
      desc('社区', communityName(pile.community_id)), desc('区域', zone ? zone.name : '—'),
      desc('功率', pile.power_kw + 'kW'), desc('电价', `¥${pile.price_kwh}/kWh + ¥${pile.price_hour}/h`)),
    h('div', { class: 'section-title' }, '枪口'),
    h('div', { class: 'gun-row' }, pile.guns.map(g =>
      h('span', { class: 'gun-chip' }, `${g.connector_no}号枪 `, statusBadge(GUN_STATUS, g.status)))),
    h('div', { class: 'section-title' }, '近期事件'),
    recent_events.length
      ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '编号'), h('th', {}, '类型'), h('th', {}, '级别'), h('th', {}, '时间'))),
        h('tbody', {}, recent_events.slice(0, 8).map(e => h('tr', {},
          h('td', { class: 'mono' }, e.event_no), h('td', {}, e.type),
          h('td', {}, statusBadge(SEVERITY, e.severity)), h('td', {}, fmtTime(e.ts)))))))
      : h('div', { class: 'text-light' }, '暂无事件'),
    h('div', { class: 'section-title' }, '关联工单'),
    tickets.length
      ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '工单'), h('th', {}, '优先级'), h('th', {}, '标题'), h('th', {}, '状态'))),
        h('tbody', {}, tickets.map(t => h('tr', {},
          h('td', { class: 'mono' }, t.id),
          h('td', {}, h('span', { class: `badge badge-${PRIORITY_COLOR[t.priority] || 'gray'}` }, t.priority)),
          h('td', {}, t.title),
          h('td', {}, statusBadge(TICKET_STATUS, t.status)))))))
      : h('div', { class: 'text-light' }, '暂无工单'));
  const canOp = ['admin', 'operator', 'maintenance'].includes(currentUser.role);
  const m = modal(`充电桩 ${pile.name}`, body, h('div', {},
    canOp ? h('button', { class: 'btn', onclick: () => { m.close(); openEventModal(pileId); } }, '上报事件') : null,
    canOp || currentUser.role === 'resident'
      ? h('button', { class: 'btn btn-primary', onclick: () => { m.close(); openTicketCreateModal(pileId); } }, '创建工单')
      : null), 'modal-lg');
}

function openEventModal(prefillPile) {
  const pileSel = h('select', {}, REFS.piles.map(p =>
    h('option', { value: p.id, selected: p.id === prefillPile || undefined },
      `${p.name}（${communityName(p.community_id)}）`)));
  const noInput = h('input', { type: 'text', value: 'E-' + Date.now(), maxlength: 64 });
  const typeSel = h('select', {}, EVENT_TYPES.map(t => h('option', { value: t }, t)));
  const sevSel = h('select', {},
    h('option', { value: 'info' }, 'info'),
    h('option', { value: 'warn', selected: true }, 'warn'),
    h('option', { value: 'critical' }, 'critical'));
  const payloadInput = h('textarea', { class: 'mono' }, '{}');
  const body = h('div', {},
    h('div', { class: 'field' }, '充电桩', pileSel),
    h('div', { class: 'field' }, '事件编号（用于幂等去重）', noInput),
    h('div', { class: 'form-row' },
      h('div', { class: 'field' }, '事件类型', typeSel),
      h('div', { class: 'field' }, '严重级别', sevSel)),
    h('div', { class: 'field' }, 'Payload（JSON）', payloadInput,
      h('div', { class: 'hint' }, '故障/离线等事件达到阈值会自动创建工单（告警升级）')));
  const submit = h('button', { class: 'btn btn-primary', onclick: send }, '上报');
  const m = modal('上报设备事件', body, h('div', {},
    h('button', { class: 'btn', onclick: () => m.close() }, '取消'), submit));

  async function send() {
    let payload;
    try { payload = JSON.parse(payloadInput.value || '{}'); }
    catch { return toast('Payload 必须是合法 JSON', 'error'); }
    submit.disabled = true;
    try {
      const data = await API.request('POST', '/device-events', {
        body: { event_no: noInput.value.trim(), pile_id: pileSel.value, type: typeSel.value, severity: sevSel.value, payload },
      });
      m.close();
      if (data.duplicate) toast('重复事件，已幂等去重（未重复建单）', 'warn');
      else if (data.ticket_id) okToast(`事件已接入，自动创建工单 ${data.ticket_id}`, data);
      else okToast('事件已接入', data);
      route();
    } catch (err) { errToast(err); submit.disabled = false; }
  }
}

// ================= 预约 =================

async function renderReservations(el) {
  const toolbar = h('div', { class: 'toolbar' },
    h('span', { class: 'text-light' }, currentUser.role === 'resident' ? '仅显示本人预约' : '授权社区内的预约'),
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: load }, '刷新'),
    h('button', { class: 'btn btn-primary', onclick: openCreate }, '新建预约'));
  const card = h('div', { class: 'card mb0' }, h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, '加载中…')));
  el.append(toolbar, card);

  async function load() {
    const data = await API.request('GET', '/reservations');
    const wrap = card.querySelector('.table-wrap');
    wrap.innerHTML = '';
    const rows = data.items || [];
    wrap.append(h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '预约号'), h('th', {}, '设备'), h('th', {}, '社区'),
        h('th', {}, '开始'), h('th', {}, '结束'), h('th', {}, '状态'), h('th', {}, '操作'))),
      h('tbody', {}, rows.length ? rows.map(r => h('tr', {},
        h('td', { class: 'mono' }, r.id),
        h('td', {}, gunLabel(r.gun_id)),
        h('td', {}, communityName(r.community_id)),
        h('td', {}, fmtTime(r.start_time)),
        h('td', {}, fmtTime(r.end_time)),
        h('td', {}, statusBadge(RES_STATUS, r.status)),
        h('td', {}, r.status === 'active'
          ? h('button', { class: 'btn btn-sm', onclick: () => cancel(r) }, '取消')
          : '—'))) : emptyRow(7))));
  }

  async function cancel(r) {
    if (!await confirmBox(`确认取消预约 ${r.id}？`)) return;
    try {
      const data = await API.request('POST', `/reservations/${r.id}/cancel`);
      okToast('预约已取消', data);
      await load();
    } catch (err) { errToast(err); }
  }

  function openCreate() {
    const gunSel = gunSelect(null, true);
    const start = new Date(Date.now() + 3600e3);
    const end = new Date(Date.now() + 2 * 3600e3);
    const startInput = h('input', { type: 'datetime-local', value: toLocalInput(start) });
    const endInput = h('input', { type: 'datetime-local', value: toLocalInput(end) });
    const noteInput = h('input', { type: 'text', maxlength: 200, placeholder: '选填' });
    const body = h('div', {},
      h('div', { class: 'field' }, '选择枪口（故障/离线不可选）', gunSel),
      h('div', { class: 'form-row' },
        h('div', { class: 'field' }, '开始时间', startInput),
        h('div', { class: 'field' }, '结束时间', endInput)),
      h('div', { class: 'field' }, '备注', noteInput),
      h('div', { class: 'hint' }, '同一枪口时间重叠将返回 409 冲突'));
    const submit = h('button', { class: 'btn btn-primary', onclick: send }, '提交预约');
    const m = modal('新建预约', body, h('div', {},
      h('button', { class: 'btn', onclick: () => m.close() }, '取消'), submit));

    async function send() {
      submit.disabled = true;
      try {
        const data = await API.request('POST', '/reservations', {
          headers: { 'Idempotency-Key': newIdempotencyKey() },
          body: { gun_id: gunSel.value, start_time: startInput.value, end_time: endInput.value, note: noteInput.value.trim() },
        });
        m.close();
        okToast(`预约成功：${data.reservation.id}`, data);
        await load();
      } catch (err) { errToast(err); submit.disabled = false; }
    }
  }
  await load();
}

// ================= 订单 =================

async function renderOrders(el) {
  const staff = ['admin', 'operator', 'maintenance'].includes(currentUser.role);
  const sSel = h('select', {},
    h('option', { value: '' }, '全部状态'),
    ...Object.entries(ORDER_STATUS).map(([k, v]) => h('option', { value: k }, v[0])));
  const toolbar = h('div', { class: 'toolbar' },
    sSel,
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: load }, '刷新'),
    h('button', { class: 'btn btn-primary', onclick: openCreate }, '创建订单'));
  const card = h('div', { class: 'card mb0' }, h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, '加载中…')));
  el.append(toolbar, card);
  sSel.addEventListener('change', load);

  async function load() {
    const q = sSel.value ? `?status=${sSel.value}` : '';
    const data = await API.request('GET', '/orders' + q);
    const wrap = card.querySelector('.table-wrap');
    wrap.innerHTML = '';
    const rows = data.items || [];
    wrap.append(h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '订单号'), h('th', {}, '设备'), h('th', {}, '社区'), h('th', {}, '状态'),
        h('th', {}, '金额'), h('th', {}, '电量'), h('th', {}, '创建时间'), h('th', {}, '操作'))),
      h('tbody', {}, rows.length ? rows.map(o => h('tr', {},
        h('td', { class: 'mono' }, o.id, o.test_order ? ' ' : '',
          o.test_order ? h('span', { class: 'badge badge-cyan' }, '测试') : null),
        h('td', {}, gunLabel(o.gun_id)),
        h('td', {}, communityName(o.community_id)),
        h('td', {}, statusBadge(ORDER_STATUS, o.status)),
        h('td', {}, '¥' + o.amount),
        h('td', {}, o.kwh + ' kWh'),
        h('td', {}, fmtTime(o.created_at)),
        h('td', {}, orderActions(o)))) : emptyRow(8))));
  }

  function orderActions(o) {
    const btns = [];
    if (o.status === 'pending_payment') {
      btns.push(h('button', { class: 'btn btn-sm btn-primary', onclick: () => act('pay', o) }, '支付'));
    }
    if (o.status === 'charging' && staff) {
      btns.push(h('button', { class: 'btn btn-sm btn-success', onclick: () => act('complete', o) }, '完成'));
    }
    if (['pending_payment', 'reserved', 'abnormal'].includes(o.status)) {
      btns.push(h('button', { class: 'btn btn-sm', onclick: () => act('cancel', o) }, '取消'));
    }
    return btns.length ? btns : '—';
  }

  async function act(action, o) {
    const names = { pay: '支付', complete: '完成', cancel: '取消' };
    if (!await confirmBox(`确认${names[action]}订单 ${o.id}？`)) return;
    try {
      const data = await API.request('POST', `/orders/${o.id}/${action}`);
      okToast(`订单 ${o.id} 已${names[action]}`, data);
      await load();
    } catch (err) { errToast(err); }
  }

  function openCreate() {
    const gunSel = gunSelect();
    const resSel = h('select', {}, h('option', { value: '' }, '不使用预约'));
    API.request('GET', '/reservations').then(d => {
      for (const r of (d.items || []).filter(x => x.status === 'active')) {
        resSel.append(h('option', { value: r.id }, `${r.id}（${gunLabel(r.gun_id)}）`));
      }
    }).catch(() => { });
    const amountInput = h('input', { type: 'number', value: '0', min: '0', step: '0.01' });
    const testInput = h('input', { type: 'checkbox' });
    const body = h('div', {},
      h('div', { class: 'field' }, '选择枪口', gunSel),
      h('div', { class: 'field' }, '关联预约（可选）', resSel),
      h('div', { class: 'field' }, '金额（元）', amountInput),
      h('div', { class: 'field checkbox-line' }, testInput,
        h('span', {}, '工单验收测试订单（创建即完成，用于关闭工单前的验收门禁）')),
      h('div', { class: 'hint' }, '接口幂等由 Idempotency-Key 保证，重复提交返回原订单'));
    const submit = h('button', { class: 'btn btn-primary', onclick: send }, '创建订单');
    const m = modal('创建订单', body, h('div', {},
      h('button', { class: 'btn', onclick: () => m.close() }, '取消'), submit));

    async function send() {
      submit.disabled = true;
      try {
        const data = await API.request('POST', '/orders', {
          headers: { 'Idempotency-Key': newIdempotencyKey() },
          body: {
            gun_id: gunSel.value,
            reservation_id: resSel.value || null,
            amount: parseFloat(amountInput.value || '0'),
            test_order: testInput.checked,
          },
        });
        m.close();
        okToast(`订单已创建：${data.order.id}${data.replayed ? '（幂等重放）' : ''}`, data);
        await load();
      } catch (err) { errToast(err); submit.disabled = false; }
    }
  }
  await load();
}

// ================= 工单 =================

async function renderTickets(el) {
  const sSel = h('select', {},
    h('option', { value: '' }, '全部状态'),
    ...Object.entries(TICKET_STATUS).map(([k, v]) => h('option', { value: k }, v[0])));
  const toolbar = h('div', { class: 'toolbar' },
    sSel,
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: load }, '刷新'),
    h('button', { class: 'btn btn-primary', onclick: () => openTicketCreateModal() }, '创建工单'));
  const card = h('div', { class: 'card mb0' }, h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, '加载中…')));
  el.append(toolbar, card);
  sSel.addEventListener('change', load);

  async function load() {
    const q = sSel.value ? `?status=${sSel.value}` : '';
    const data = await API.request('GET', '/tickets' + q);
    const wrap = card.querySelector('.table-wrap');
    wrap.innerHTML = '';
    const rows = data.items || [];
    wrap.append(h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '工单号'), h('th', {}, '优先级'), h('th', {}, '标题'), h('th', {}, '设备'),
        h('th', {}, '状态'), h('th', {}, '处理人'), h('th', {}, '创建时间'), h('th', {}, '操作'))),
      h('tbody', {}, rows.length ? rows.map(t => h('tr', {},
        h('td', { class: 'mono' }, t.id),
        h('td', {}, h('span', { class: `badge badge-${PRIORITY_COLOR[t.priority] || 'gray'}` }, t.priority)),
        h('td', {}, t.title),
        h('td', {}, pileName(t.pile_id)),
        h('td', {}, statusBadge(TICKET_STATUS, t.status)),
        h('td', {}, t.assignee || '—'),
        h('td', {}, fmtTime(t.created_at)),
        h('td', {}, h('button', { class: 'btn btn-sm', onclick: () => openTicketDetail(t.id) }, '详情'))))
        : emptyRow(8))));
  }
  el._reload = load;
  await load();
}

function openTicketCreateModal(prefillPile) {
  const isResident = currentUser.role === 'resident';
  const pileSel = h('select', {}, REFS.piles.map(p =>
    h('option', { value: p.id, selected: p.id === prefillPile || undefined },
      `${p.name}（${communityName(p.community_id)}）`)));
  const titleInput = h('input', { type: 'text', maxlength: 128, placeholder: '如：枪头损坏无法启动' });
  const prioSel = h('select', {},
    h('option', { value: 'P1' }, 'P1 紧急'),
    h('option', { value: 'P2' }, 'P2 较高'),
    h('option', { value: 'P3', selected: true }, 'P3 一般'));
  const descInput = h('textarea', { maxlength: 500, placeholder: '问题描述（选填）' });
  const body = h('div', {},
    h('div', { class: 'field' }, '充电桩', pileSel),
    h('div', { class: 'field' }, '标题', titleInput),
    isResident
      ? h('div', { class: 'hint' }, '居民提交视为投诉工单，优先级固定为 P3')
      : h('div', { class: 'field' }, '优先级', prioSel),
    h('div', { class: 'field' }, '描述', descInput));
  const submit = h('button', { class: 'btn btn-primary', onclick: send }, '提交');
  const m = modal(isResident ? '投诉报修' : '创建工单', body, h('div', {},
    h('button', { class: 'btn', onclick: () => m.close() }, '取消'), submit));

  async function send() {
    if (!titleInput.value.trim()) return toast('请填写标题', 'warn');
    submit.disabled = true;
    try {
      const data = await API.request('POST', '/tickets', {
        body: {
          pile_id: pileSel.value, title: titleInput.value.trim(),
          priority: prioSel.value, description: descInput.value.trim(), source: 'manual',
        },
      });
      m.close();
      okToast(`工单已创建：${data.ticket.id}`, data);
      route();
    } catch (err) { errToast(err); submit.disabled = false; }
  }
}

async function openTicketDetail(ticketId) {
  let data;
  try {
    data = await API.request('GET', '/tickets');
  } catch (err) { return errToast(err); }
  const t = (data.items || []).find(x => x.id === ticketId);
  if (!t) return toast('工单不存在或无权查看', 'error');

  const role = currentUser.role;
  const isStaff = ['admin', 'operator', 'maintenance'].includes(role);
  const body = h('div', {},
    h('div', { class: 'desc-grid' },
      desc('工单号', h('span', { class: 'mono' }, t.id)),
      desc('状态', statusBadge(TICKET_STATUS, t.status)),
      desc('优先级', h('span', { class: `badge badge-${PRIORITY_COLOR[t.priority] || 'gray'}` }, t.priority)),
      desc('来源', t.source + (t.source_event_no ? `（${t.source_event_no}）` : '')),
      desc('设备', pileName(t.pile_id)),
      desc('社区', communityName(t.community_id)),
      desc('处理人', t.assignee || '—'),
      desc('创建时间', fmtTime(t.created_at)),
      desc('标题', t.title, true),
      desc('描述', t.description || '—', true),
      t.dispatch_note ? desc('分派备注', t.dispatch_note, true) : null,
      t.repair_note ? desc('维修回填', t.repair_note, true) : null),
    h('div', { class: 'section-title' }, '处理记录'),
    h('ul', { class: 'timeline' }, (t.history || []).map(s =>
      h('li', {},
        h('div', { class: 'tl-title' }, (TICKET_STATUS[s.status] || [s.status])[0] || s.status),
        h('div', { class: 'tl-meta' }, `${s.operator} · ${fmtTime(s.ts)}`)))));

  // 待验收：展示门禁状态
  if (t.status === 'pending_acceptance') {
    body.append(h('div', { class: 'section-title' }, '验收门禁（关闭前必须全部通过）'),
      h('div', { class: 'gate-list' },
        h('div', { class: 'gate-item', id: 'gate-heartbeat' },
          h('span', { class: 'gate-dot' }), '设备心跳检查（新鲜窗口内）'),
        h('div', { class: 'gate-item', id: 'gate-testorder' },
          h('span', { class: 'gate-dot' }), '测试订单验证（该桩存在已完成的测试订单）')));
  }

  const footer = h('div', {});
  const m = modal(`工单 ${t.id}`, body, footer, 'modal-lg');
  renderTicketActions();

  function renderTicketActions() {
    footer.innerHTML = '';
    footer.append(h('button', { class: 'btn', onclick: () => m.close() }, '关闭'));

    if (t.status === 'pending_dispatch' && ['admin', 'operator'].includes(role)) {
      const assignee = h('input', { type: 'text', placeholder: '维修人员用户名，如 fix1', maxlength: 64 });
      const note = h('input', { type: 'text', placeholder: '分派备注（选填）', maxlength: 200 });
      body.append(h('div', { class: 'section-title' }, '分派工单'),
        h('div', { class: 'form-row' },
          h('div', { class: 'field' }, '分派给', assignee),
          h('div', { class: 'field' }, '备注', note)));
      footer.append(h('button', {
        class: 'btn btn-primary', onclick: async () => {
          try {
            const data = await API.request('POST', `/tickets/${t.id}/dispatch`,
              { body: { assignee: assignee.value.trim(), note: note.value.trim() } });
            okToast('已分派', data);
            m.close(); route();
          } catch (err) { errToast(err); }
        },
      }, '确认分派'));
    }

    if (t.status === 'dispatched' && (role === 'admin' || (role === 'maintenance' && t.assignee === currentUser.username))) {
      footer.append(flowBtn('接单', 'accept'));
    }
    if (t.status === 'accepted' && ['admin', 'maintenance'].includes(role)) {
      footer.append(flowBtn('开始处理', 'start'));
    }
    if (t.status === 'processing' && ['admin', 'maintenance'].includes(role)) {
      footer.append(h('button', {
        class: 'btn btn-primary', onclick: () => {
          const noteInput = h('textarea', { maxlength: 512, placeholder: '维修过程与结果，如：更换温控传感器并复检正常' });
          const submit = h('button', {
            class: 'btn btn-primary', onclick: async () => {
              if (!noteInput.value.trim()) return toast('请填写维修结果', 'warn');
              try {
                const data = await API.request('POST', `/tickets/${t.id}/resolve`,
                  { body: { repair_note: noteInput.value.trim() } });
                m2.close(); okToast('已回填，进入待验收', data);
                m.close(); openTicketDetail(t.id);
              } catch (err) { errToast(err); }
            },
          }, '提交回填');
          const m2 = modal('维修回填', h('div', { class: 'field' }, '维修结果', noteInput),
            h('div', {}, h('button', { class: 'btn', onclick: () => m2.close() }, '取消'), submit));
        },
      }, '维修回填'));
    }

    if (t.status === 'pending_acceptance' && isStaff) {
      footer.append(
        h('button', { class: 'btn', onclick: checkHeartbeat }, '① 心跳检查'),
        h('button', { class: 'btn', onclick: createTestOrder }, '② 创建测试订单'),
        h('button', { class: 'btn btn-success', onclick: closeTicket }, '③ 关闭工单'));
    }

    function flowBtn(text, action) {
      return h('button', {
        class: 'btn btn-primary', onclick: async () => {
          try {
            const data = await API.request('POST', `/tickets/${t.id}/${action}`);
            okToast(`工单已${text}`, data);
            m.close(); route();
          } catch (err) { errToast(err); }
        },
      }, text);
    }

    async function checkHeartbeat() {
      try {
        const data = await API.request('POST', `/tickets/${t.id}/check-heartbeat`);
        const gate = document.getElementById('gate-heartbeat');
        if (gate) gate.className = 'gate-item ' + (data.ok ? 'pass' : 'fail');
        toast(data.ok
          ? `心跳新鲜，检查通过（最近心跳 ${fmtTime(data.last_heartbeat)}）`
          : `心跳不新鲜或不存在（窗口 ${data.window_minutes} 分钟），请先上报心跳事件`,
          data.ok ? 'success' : 'warn');
      } catch (err) { errToast(err); }
    }

    function createTestOrder() {
      const pile = pileById(t.pile_id);
      const gunSel = gunSelect(t.pile_id);
      const submit = h('button', {
        class: 'btn btn-primary', onclick: async () => {
          try {
            const data = await API.request('POST', '/orders', {
              headers: { 'Idempotency-Key': newIdempotencyKey() },
              body: { gun_id: gunSel.value, test_order: true },
            });
            m3.close();
            okToast(`测试订单 ${data.order.id} 已创建并完成`, data);
            const gate = document.getElementById('gate-testorder');
            if (gate) gate.className = 'gate-item pass';
          } catch (err) { errToast(err); }
        },
      }, '创建并验证');
      const m3 = modal('创建测试订单（验收用）',
        h('div', {},
          h('div', { class: 'field' }, `选择 ${pile ? pile.name : t.pile_id} 的枪口`, gunSel),
          h('div', { class: 'hint' }, '测试订单创建即完成，用于验证设备修复后可正常充电')),
        h('div', {}, h('button', { class: 'btn', onclick: () => m3.close() }, '取消'), submit));
    }

    async function closeTicket() {
      if (!await confirmBox('确认关闭工单？系统将校验心跳检查与测试订单两项验收门禁。', '关闭工单')) return;
      try {
        const data = await API.request('POST', `/tickets/${t.id}/close`);
        okToast(`工单 ${t.id} 已关闭，设备恢复可用`, data);
        m.close(); route();
      } catch (err) { errToast(err); }
    }
  }
}

function desc(k, v, full = false) {
  return h('div', { class: 'desc-item' + (full ? ' full' : '') },
    h('span', { class: 'k' }, k + '：'), h('span', {}, v));
}

// ================= 审批 =================

async function renderApprovals(el) {
  const role = currentUser.role;
  const sSel = h('select', {},
    h('option', { value: '' }, '全部状态'),
    ...Object.entries(APPROVAL_STATUS).map(([k, v]) => h('option', { value: k }, v[0])));
  const toolbar = h('div', { class: 'toolbar' },
    sSel,
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: load }, '刷新'),
    role !== 'maintenance' ? h('button', { class: 'btn btn-primary', onclick: openCreate }, '发起审批') : null);
  const card = h('div', { class: 'card mb0' }, h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, '加载中…')));
  el.append(toolbar, card);
  sSel.addEventListener('change', load);

  async function load() {
    const q = sSel.value ? `?status=${sSel.value}` : '';
    const data = await API.request('GET', '/approvals' + q);
    const wrap = card.querySelector('.table-wrap');
    wrap.innerHTML = '';
    const rows = data.items || [];
    wrap.append(h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '审批号'), h('th', {}, '类型'), h('th', {}, '对象'), h('th', {}, '金额'),
        h('th', {}, '申请人'), h('th', {}, '状态'), h('th', {}, '创建时间'), h('th', {}, '操作'))),
      h('tbody', {}, rows.length ? rows.map(a => h('tr', {},
        h('td', { class: 'mono' }, a.id),
        h('td', {}, APPROVAL_TYPES[a.type] || a.type),
        h('td', { class: 'mono' }, a.target),
        h('td', {}, a.amount ? '¥' + a.amount : '—'),
        h('td', {}, a.applicant),
        h('td', {}, statusBadge(APPROVAL_STATUS, a.status)),
        h('td', {}, fmtTime(a.created_at)),
        h('td', {}, h('button', { class: 'btn btn-sm', onclick: () => openDetail(a.id) }, '详情'))))
        : emptyRow(8))));
  }

  async function openDetail(aid) {
    let list;
    try { list = await API.request('GET', '/approvals'); }
    catch (err) { return errToast(err); }
    const a = (list.items || []).find(x => x.id === aid);
    if (!a) return toast('审批不存在或无权查看', 'error');

    const body = h('div', {},
      h('div', { class: 'desc-grid' },
        desc('审批号', h('span', { class: 'mono' }, a.id)),
        desc('状态', statusBadge(APPROVAL_STATUS, a.status)),
        desc('类型', APPROVAL_TYPES[a.type] || a.type),
        desc('影响对象', h('span', { class: 'mono' }, a.target)),
        desc('金额', a.amount ? '¥' + a.amount : '—'),
        desc('申请人', a.applicant),
        desc('社区', a.community_id ? communityName(a.community_id) : '—'),
        desc('创建时间', fmtTime(a.created_at)),
        desc('申请理由', a.reason, true),
        desc('风险评估', a.risk_assessment || '—', true),
        a.approver ? desc('审批人', `${a.approver}（${fmtTime(a.decided_at)}）`) : null,
        a.receipt ? desc('执行回执', a.receipt, true) : null));

    const footer = h('div', {}, h('button', { class: 'btn', onclick: () => m.close() }, '关闭'));
    const m = modal(`审批 ${a.id}`, body, footer, 'modal-lg');

    const canDecide = ['admin', 'finance'].includes(currentUser.role);
    const comment = h('input', { type: 'text', placeholder: '审批意见（选填）', maxlength: 200 });
    if (a.status === 'pending' && canDecide) {
      body.append(h('div', { class: 'section-title' }, '审批决策'),
        h('div', { class: 'field' }, comment),
        h('div', { class: 'hint' }, '仅待审批状态可决策；决策后不可重复审批'));
      footer.append(
        h('button', {
          class: 'btn btn-danger', onclick: () => decide('rejected'),
        }, '驳回'),
        h('button', { class: 'btn btn-success', onclick: () => decide('approved') }, '批准'));
    }
    if (a.status === 'approved' && canDecide) {
      footer.append(h('button', { class: 'btn btn-primary', onclick: execute }, '执行审批'));
      body.append(h('div', { class: 'hint mt8' }, '已批准待执行；执行带执行锁，禁止重复执行'));
    }

    async function decide(decision) {
      try {
        const data = await API.request('POST', `/approvals/${a.id}/decide`,
          { body: { decision, comment: comment.value.trim() } });
        okToast(decision === 'approved' ? '已批准' : '已驳回', data);
        m.close(); route();
      } catch (err) { errToast(err); }
    }
    async function execute() {
      if (!await confirmBox(`确认执行审批 ${a.id}？执行后将直接产生业务效果（退款/停机/调价等）。`, '执行')) return;
      try {
        const data = await API.request('POST', `/approvals/${a.id}/execute`);
        okToast('执行完成：' + (data.approval.receipt || ''), data);
        m.close(); route();
      } catch (err) { errToast(err); }
    }
  }

  function openCreate() {
    const isResident = currentUser.role === 'resident';
    const typeSel = h('select', {},
      (isResident ? ['refund'] : Object.keys(APPROVAL_TYPES)).map(k =>
        h('option', { value: k }, APPROVAL_TYPES[k])));
    const targetInput = h('input', { type: 'text', placeholder: '如：O-1006 / CP-A02 / p5', maxlength: 128 });
    const cSel = communitySelect(true);
    const amountInput = h('input', { type: 'number', value: '0', min: '0', step: '0.01' });
    const reasonInput = h('textarea', { maxlength: 500, placeholder: '业务理由（必填）' });
    const body = h('div', {},
      h('div', { class: 'form-row' },
        h('div', { class: 'field' }, '审批类型', typeSel),
        h('div', { class: 'field' }, '影响对象', targetInput)),
      h('div', { class: 'form-row' },
        h('div', { class: 'field' }, '所属社区（选填）', cSel),
        h('div', { class: 'field' }, '金额（元）', amountInput)),
      h('div', { class: 'field' }, '业务理由', reasonInput),
      h('div', { class: 'hint' }, '相同对象的待审批申请禁止重复提交（409）'));
    const submit = h('button', { class: 'btn btn-primary', onclick: send }, '提交申请');
    const m = modal('发起审批', body, h('div', {},
      h('button', { class: 'btn', onclick: () => m.close() }, '取消'), submit));

    async function send() {
      if (!targetInput.value.trim() || !reasonInput.value.trim()) {
        return toast('请填写影响对象与业务理由', 'warn');
      }
      submit.disabled = true;
      try {
        const data = await API.request('POST', '/approvals', {
          body: {
            type: typeSel.value, target: targetInput.value.trim(),
            community_id: cSel.value || null,
            amount: parseFloat(amountInput.value || '0'),
            reason: reasonInput.value.trim(),
          },
        });
        m.close();
        okToast(`审批申请已提交：${data.approval.id}`, data);
        await load();
      } catch (err) { errToast(err); submit.disabled = false; }
    }
  }
  await load();
}

// ================= 报表 =================

async function renderReports(el) {
  const pSel = h('select', {},
    h('option', { value: 'day' }, '日报'),
    h('option', { value: 'week' }, '周报'));
  const cSel = communitySelect(true);
  const toolbar = h('div', { class: 'toolbar' }, pSel, cSel,
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: load }, '刷新'));
  const body = h('div', {});
  el.append(toolbar, body);
  pSel.addEventListener('change', load);
  cSel.addEventListener('change', load);

  async function load() {
    body.innerHTML = '<div class="empty">加载中…</div>';
    const params = new URLSearchParams({ period: pSel.value });
    if (cSel.value) params.set('community_id', cSel.value);
    const data = await API.request('GET', '/reports?' + params);
    body.innerHTML = '';
    body.append(metricCards(data.metrics));

    const rv = data.review || {};
    body.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' }, '异常复盘'),
      h('p', { class: 'text-light' }, rv.summary || '—'),
      h('div', { class: 'section-title' }, '严重（critical）事件'),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '编号'), h('th', {}, '设备'), h('th', {}, '类型'), h('th', {}, '时间'))),
        h('tbody', {}, (rv.critical_events || []).length
          ? rv.critical_events.map(e => h('tr', {},
            h('td', { class: 'mono' }, e.event_no), h('td', {}, pileName(e.pile_id)),
            h('td', {}, e.type), h('td', {}, fmtTime(e.ts))))
          : emptyRow(4))))),
      h('div', { class: 'card mb0' },
        h('div', { class: 'card-title' }, '未关闭 P1 工单'),
        h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, '工单'), h('th', {}, '标题'), h('th', {}, '设备'), h('th', {}, '状态'))),
          h('tbody', {}, (rv.open_p1_tickets || []).length
            ? rv.open_p1_tickets.map(t => h('tr', {},
              h('td', { class: 'mono' }, t.id), h('td', {}, t.title),
              h('td', {}, pileName(t.pile_id)), h('td', {}, statusBadge(TICKET_STATUS, t.status))))
            : emptyRow(4))))));
  }
  await load();
}

// ================= Agent 工作台 =================

function renderAgent(el) {
  let sessionId = sessionStorage.getItem('agent_session') || '';
  let busy = false;

  const chat = h('div', { class: 'agent-chat' });
  const input = h('textarea', { placeholder: '输入问题，Enter 发送，Shift+Enter 换行…', maxlength: 2000 });
  const sendBtn = h('button', { class: 'btn btn-primary', onclick: send }, '发送');

  const suggests = ['运营指标怎么样？', '阳光花园的设备状态', 'CP-B01 为什么故障？',
    '帮我给订单 O-1006 申请退款', '申请滨江苑 CP-A02 停机', '我的预约情况'];

  el.append(h('div', { class: 'agent-wrap' },
    h('div', { class: 'suggest-bar' }, suggests.map(s =>
      h('button', { class: 'suggest-chip', onclick: () => { input.value = s; send(); } }, s))),
    chat,
    h('div', { class: 'agent-input-bar' }, input, sendBtn)));

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  chat.append(h('div', { class: 'msg msg-bot' },
    h('div', { class: 'msg-bubble' },
      '你好，我是邻里充电运营 Agent。可以查询运营指标/设备/订单，分析故障归因，发起退款、停机、调价等申请（高风险动作只会创建审批，不会直接执行）。')));

  async function send() {
    const message = input.value.trim();
    if (!message || busy) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = '';

    chat.append(h('div', { class: 'msg msg-user' }, h('div', { class: 'msg-bubble' }, message)));

    const steps = h('div', { class: 'node-steps' });
    const botMsg = h('div', { class: 'msg msg-bot' },
      h('div', { class: 'msg-bubble' }, h('span', { class: 'typing-dot' }, '思考中…')));
    chat.append(steps, botMsg);
    chat.scrollTop = chat.scrollHeight;

    try {
      await API.stream('/agent/chat',
        { message, session_id: sessionId || null, stream: true },
        event => {
          if (event.final) { botMsg.remove(); renderFinal(event.final); return; }
          if (event.node) {
            steps.append(h('div', { class: 'node-step' + (event.done ? ' done' : '') },
              h('span', { class: 'node-name' }, event.node),
              h('span', {}, event.summary || '')));
            chat.scrollTop = chat.scrollHeight;
          }
        });
    } catch (err) {
      botMsg.querySelector('.msg-bubble').textContent = '请求失败：' + err.message;
    }
    busy = false;
    sendBtn.disabled = false;
    chat.scrollTop = chat.scrollHeight;
  }

  function renderFinal(f) {
    sessionId = f.session_id || sessionId;
    sessionStorage.setItem('agent_session', sessionId);
    const bubble = botMsgFor(f);
    function botMsgFor(f) {
      const meta = [];
      if (f.intent_name) meta.push(h('span', { class: 'badge badge-blue' }, '意图：' + f.intent_name));
      const riskMap = { read: ['只读', 'green'], write: ['普通写', 'yellow'], high: ['高风险→审批', 'red'] };
      const rk = riskMap[f.risk_level] || [f.risk_level, 'gray'];
      meta.push(h('span', { class: `badge badge-${rk[1]}` }, rk[0]));
      for (const ds of f.data_sources || []) meta.push(h('span', { class: 'badge badge-gray' }, ds));
      if (f.trace_id) meta.push(h('span', {}, 'trace: ' + f.trace_id.slice(0, 12) + '…'));
      if (f.approval_id) {
        meta.push(h('a', {
          onclick: () => { location.hash = '#/approvals'; },
        }, `查看审批 ${f.approval_id} →`));
      }
      const box = h('div', { class: 'msg msg-bot' },
        h('div', { class: 'msg-bubble' }, f.answer || '（无回答）'),
        h('div', { class: 'msg-meta' }, meta));
      chat.append(box);
      return box;
    }
  }
}

// ================= 系统管理 =================

async function renderSystem(el) {
  const [users, audits] = await Promise.all([
    API.request('GET', '/users'),
    API.request('GET', '/audits?limit=100'),
  ]);
  el.append(
    h('div', { class: 'card' },
      h('div', { class: 'card-title' }, '用户与角色'),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {},
          h('th', {}, '用户名'), h('th', {}, '姓名'), h('th', {}, '角色'),
          h('th', {}, '授权社区'), h('th', {}, '状态'))),
        h('tbody', {}, users.items.map(u => h('tr', {},
          h('td', { class: 'mono' }, u.username),
          h('td', {}, u.name),
          h('td', {}, u.role_name),
          h('td', {}, (u.communities || []).map(communityName).join('、') || '—'),
          h('td', {}, statusBadge({ active: ['启用', 'green'], disabled: ['停用', 'gray'] }, u.status)))))))),
    h('div', { class: 'card mb0' },
      h('div', { class: 'card-title' }, '审计日志（最近 100 条）'),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {},
          h('th', {}, '时间'), h('th', {}, '用户'), h('th', {}, '动作'),
          h('th', {}, '对象'), h('th', {}, '详情'), h('th', {}, '结果'), h('th', {}, 'trace_id'))),
        h('tbody', {}, audits.items.length ? audits.items.map(a => h('tr', {},
          h('td', {}, fmtTime(a.ts)),
          h('td', {}, `${a.user}（${a.role}）`),
          h('td', { class: 'mono' }, a.action),
          h('td', { class: 'mono' }, a.object),
          h('td', { class: 'text-light' }, a.detail || '—'),
          h('td', {}, statusBadge({ ok: ['成功', 'green'], fail: ['失败', 'red'], denied: ['拒绝', 'red'], conflict: ['冲突', 'yellow'] }, a.result)),
          h('td', { class: 'mono text-light' }, a.trace_id ? a.trace_id.slice(0, 8) + '…' : '—'))) : emptyRow(7))))));
}

// ================= 启动 =================

document.getElementById('login-form').addEventListener('submit', doLogin);
document.getElementById('logout-btn').addEventListener('click', doLogout);
window.addEventListener('hashchange', route);

(async function bootstrap() {
  if (API.getToken() && API.getUser()) {
    await initApp();
  } else {
    showLogin();
  }
})();
