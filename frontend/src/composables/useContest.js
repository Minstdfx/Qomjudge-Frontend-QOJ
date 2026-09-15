import { inject, markRaw, reactive, watch } from 'vue';

export const contestKey = Symbol('contest');
export function useContest() {
  const store = inject(contestKey);
  if (!store) throw new Error('Contest provider is missing');
  return store;
}

export function createContestStore() {
  let loadingState = null;
  let queuedFreshState = null;
  let pollTimer;
  let clockTimer;
  let disposed = false;
  const c = reactive({
    state: null, stateVersion: 0, clockOffset: 0, tick: Date.now() / 1000,
    tab: 'participation', problemId: null, problemRevision: 0, resourceRevision: 0,
    detailsCache: markRaw(new Map()), teamChanging: false, participationChanging: false, accountChanging: false,
    participationError: '', scoreboardError: '', connection: '', connectionError: '',
    lastUpdated: '等待连接', notice: null,
    accountOpen: false, submitOpen: false, submitTarget: null, sourceOpen: false, sourceId: null,
    confirmation: { open: false, title: '', message: '', callback: null },
    get part() {
      if (c.state?.participation) return c.state.participation;
      if (c.state?.demo) return { mode: 'vp', status: c.state.run?.status || 'unregistered', content_available: !!c.state.run, can_submit: c.state.run?.status === 'running', started_at: c.state.run?.started_at, ends_at: c.state.run?.ends_at, actions: [] };
      return { mode: 'vp', status: 'unavailable', content_available: false, can_submit: false, actions: [], message: '正在确认 QOJ 参赛状态。' };
    },
    get revoked() { return c.state?.team?.role === 'revoked'; },
    get hasTeam() { return !!c.state?.team?.id && ['captain', 'member'].includes(c.state.team.role); },
    get canRead() { return c.hasTeam && !c.participationChanging && !c.accountChanging && c.state.permissions?.view_content !== false && c.part.content_available === true && ['running', 'finished'].includes(c.part.status); },
    get running() { return c.canRead && c.part.status === 'running' && (!Number(c.part.ends_at) || Number(c.part.ends_at) > c.tick + c.clockOffset); },
    get problems() { return c.canRead ? c.state?.contest?.problems || [] : []; },
    get context() { const s = c.state; return JSON.stringify([s?.contest?.id, !!s?.demo, !!s?.user?.authenticated, s?.user?.username || '', s?.team?.id || '', s?.team?.role || 'solo', s?.team?.device_id || '', s?.run?.id || '', c.part.mode, c.part.started_at || '', c.canRead, s?.permissions || null]); },
    can(permission) { return !!c.state && !c.revoked && (c.state.permissions ? c.state.permissions[permission] === true : c.state.team?.role !== 'member') && (!['submit', 'print'].includes(permission) || c.canRead) && (permission !== 'submit' || c.part.can_submit === true); },
    now() { return Date.now() / 1000 + c.clockOffset; },
    byId(id) { return c.problems.find(p => String(p.id) === String(id)); },
    statFor(id, row = c.state?.run) { return row?.problem_stats?.[id] || {}; },
    notify(message, kind = 'info') { c.notice = { message, kind }; },
    allowedTab(tab) {
      if (['team', 'participation'].includes(tab)) return true;
      if (!c.hasTeam) return false;
      if (['problems', 'scoreboard', 'printing'].includes(tab)) return c.canRead;
      return c.canRead && c.can('manage') && ['overview', 'submissions', 'attachments'].includes(tab);
    },
    navigate(tab, pid) { location.hash = `#${tab}${pid == null ? '' : '/' + encodeURIComponent(pid)}`; route(); },
    openAccount() { if (c.can('manage')) c.accountOpen = true; },
    openSubmission(pid) {
      if (!c.can('submit') || !c.running) return;
      const p = c.byId(pid) || c.byId(c.problemId) || c.problems[0];
      if (p) { c.submitTarget = p.id; c.submitOpen = true; }
    },
    openSource(id) {
      if (!c.canRead || !c.can('manage') || !/^[1-9]\d*$/.test(String(id)) || !c.state.submissions?.some(s => String(s.id) === String(id))) return;
      c.sourceId = String(id); c.sourceOpen = true;
    },
    confirm(title, message, callback) { c.confirmation = { title, message, callback, open: true }; },
    async api(path, data, { signal } = {}) {
      const options = { credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store', signal };
      if (data !== undefined) {
        if (['/api/start', '/api/reset', '/api/finish'].includes(path) && !c.hasTeam) throw new Error('请先在 Team 创建队伍或通过邀请码加入队伍。');
        if (['/api/start', '/api/mode', '/api/reset', '/api/finish', '/api/refresh', '/api/connect', '/api/logout'].includes(path) && !c.can('manage')) throw new Error('这台电脑没有管理比赛的权限，请由队长操作。');
        if (path === '/api/submit' && !c.can('submit')) throw new Error('这台电脑没有提交代码的权限，请由队长操作。');
        options.method = 'POST'; options.headers['X-CSRF-Token'] = c.state?.csrf_token || '';
        if (data instanceof FormData) options.body = data;
        else { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
      }
      let response;
      try { response = await fetch(path, options); }
      catch (error) { if (error.name === 'AbortError') throw error; throw new Error('无法连接服务器，请检查网络后重试。'); }
      let result;
      try { result = await response.json(); } catch { throw new Error(`服务器返回了无法读取的响应（HTTP ${response.status}）。`); }
      if (!response.ok || result.error) throw new Error(result.error || `请求失败（HTTP ${response.status}）。`);
      return result;
    },
    adoptState(result) {
      c.state = result; c.stateVersion++; c.scoreboardError = ''; c.connectionError = '';
      const offset = Number(result.server_time) - Date.now() / 1000;
      c.clockOffset = Number.isFinite(offset) ? offset : 0;
      route();
    },
    async refreshState(fresh = false) {
      if (paused()) return c.state;
      if (loadingState) {
        if (!fresh) return loadingState;
        // Concurrent manual refreshes share one follow-up request. A late response
        // must never overwrite the newer snapshot or start a second polling loop.
        if (!queuedFreshState) {
          const preceding = loadingState;
          const queued = (async () => { await preceding; return c.refreshState(); })();
          queuedFreshState = queued;
          queued.finally(() => { if (queuedFreshState === queued) queuedFreshState = null; });
        }
        return queuedFreshState;
      }
      if (paused()) return c.state;
      loadingState = (async () => {
        try {
          const result = await c.api('/api/state');
          if (disposed) return null;
          c.adoptState(result); c.connection = 'online';
          c.lastUpdated = `最后更新 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
          return result;
        } catch (error) {
          c.connection = 'offline'; c.lastUpdated = '连接中断，正在重试…';
          if (!c.state) c.connectionError = error.message;
          else c.scoreboardError = error.message;
          return null;
        } finally { loadingState = null; schedule(); }
      })();
      return loadingState;
    },
    async refreshContest() {
      return c.runAccountAction('refresh');
    },
    async runAccountAction(kind, payload = {}) {
      if (disposed || c.accountChanging || c.teamChanging || c.participationChanging || !c.can('manage')) return null;
      const path = { refresh: '/api/refresh', connect: '/api/connect', logout: '/api/logout' }[kind];
      if (!path) throw new Error('未知的账号操作。');
      const previousTab = c.tab, previousProblem = c.problemId;
      c.accountChanging = true; clearTimeout(pollTimer);
      try {
        // Finish an older poll before changing the shared upstream session.
        if (loadingState) await loadingState;
        const response = await c.api(path, payload);
        const result = kind === 'logout' ? await c.api('/api/state') : response;
        if (!result?.contest || !result.user || (!result.demo && !result.participation)) throw new Error('服务器未返回可确认的账号状态，请刷新后重试。');
        c.adoptState(result);
        c.detailsCache.clear(); c.resourceRevision++;
        return result;
      } catch (error) {
        // Cookie import can change the upstream session even if verification fails.
        // Never restore the previous account's cached contest after that failure.
        c.adoptState({
          ...c.state, contest: { id: c.state?.contest?.id, problems: [], attachments: [] },
          user: { authenticated: false, username: '' }, run: null, submissions: [], scoreboard: [],
          participation: { mode: c.part.mode, status: 'unavailable', content_available: false, can_submit: false,
            actions: [], message: '账号状态尚未确认，请重新连接账号或刷新状态。' },
          permissions: { ...c.state?.permissions, view_content: false, submit: false, print: false },
          contest_error: null, scoreboard_message: '',
        });
        throw error;
      } finally {
        c.accountChanging = false;
        if (c.allowedTab(previousTab)) c.navigate(previousTab, previousTab === 'problems' ? previousProblem : undefined);
        else route();
        schedule();
      }
    },
    async updateParticipation(path, payload) {
      if (c.participationChanging || c.accountChanging || c.teamChanging || !c.can('manage')) return;
      c.participationChanging = true; clearTimeout(pollTimer); c.participationError = ''; route();
      let success = false;
      try {
        if (loadingState) await loadingState;
        const result = await c.api(path, payload);
        c.adoptState(result?.contest ? result : await c.api('/api/state')); success = true;
      } catch (error) {
        c.participationError = error.message;
        try { c.adoptState(await c.api('/api/state')); }
        catch { c.adoptState({ ...c.state, participation: { ...c.part, status: 'unavailable', content_available: false, can_submit: false, message: '无法确认当前参赛状态，请检查连接后刷新。' }, permissions: { ...c.state?.permissions, view_content: false } }); }
      } finally {
        c.participationChanging = false;
        c.navigate(success && path === '/api/start' && c.canRead ? c.can('manage') ? 'overview' : 'problems' : 'participation'); schedule();
      }
      return success;
    },
    async runTeamAction(path, body, message, nextTab = 'team') {
      if (c.teamChanging || c.accountChanging || c.participationChanging) return;
      c.teamChanging = true; clearTimeout(pollTimer);
      try {
        if (loadingState) await loadingState;
        const result = await c.api(path, body);
        c.adoptState(result?.contest ? result : await c.api('/api/state'));
        c.navigate(nextTab); if (message) c.notify(message, 'success');
        return result;
      } finally { c.teamChanging = false; schedule(); }
    },
    start() {
      disposed = false; route(); c.refreshState();
      clockTimer = setInterval(() => { c.tick = Date.now() / 1000; }, 1000);
      window.addEventListener('hashchange', route);
      document.addEventListener('visibilitychange', visible);
    },
    dispose() {
      disposed = true; clearTimeout(pollTimer); clearInterval(clockTimer); stopContext();
      window.removeEventListener('hashchange', route); document.removeEventListener('visibilitychange', visible);
    },
  });
  function paused() { return disposed || c.teamChanging || c.participationChanging || c.accountChanging; }
  function schedule() { clearTimeout(pollTimer); if (!paused()) pollTimer = setTimeout(() => c.refreshState(), Math.max(3, Number(c.state?.poll_interval) || 8) * 1000); }
  function visible() { if (!document.hidden) c.refreshState(); }
  function route() {
    const [name, encoded] = location.hash.replace(/^#/, '').split('/');
    let tab = ['participation', 'overview', 'problems', 'attachments', 'submissions', 'scoreboard', 'printing', 'team'].includes(name) ? name : 'participation';
    if (!c.state) tab = 'participation';
    if (c.state && !c.allowedTab(tab)) { tab = c.revoked ? 'team' : !c.canRead ? 'participation' : c.can('manage') ? 'overview' : 'problems'; history.replaceState(null, '', `#${tab}`); }
    c.tab = tab;
    if (tab === 'problems' && c.canRead) {
      let id; try { id = encoded ? decodeURIComponent(encoded) : null; } catch {}
      c.problemId = (c.byId(id) || c.byId(c.problemId) || c.problems[0])?.id ?? null;
    }
  }
  const stopContext = watch(() => c.context, () => {
    c.detailsCache.clear();
    if (!c.accountChanging || !c.can('manage')) c.accountOpen = false;
    c.submitOpen = false; c.sourceOpen = false;
    c.confirmation.open = false; c.confirmation.callback = null;
    c.problemId = null; if (!c.canRead) c.notice = null;
    route();
  }, { flush: 'sync' });
  return c;
}
