<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { dateTime } from '../lib/format.js';

const emit = defineEmits(['locked']);
const c = useContest();
const room = ref(null);
const loaded = ref(false);
const loading = ref(false);
const error = ref('');
const readError = ref('');
const documentVisible = ref(!document.hidden);
const activeAction = ref('');
const joinCode = ref(new URLSearchParams(location.search).get('joint') || '');
const inviteInput = ref(null);
const consent = ref(false);
const values = ref(Object.create(null));
let generation = 0;
let pollTimer;
let requestController;
let disposed = false;

const captain = computed(() => c.state?.team?.role === 'captain' && c.can('manage'));
const self = computed(() => room.value?.self || {});
const plan = computed(() => captain.value ? self.value.plan : null);
const action = computed(() => plan.value?.action);
const fields = computed(() => {
  if (Array.isArray(action.value?.fields)) return action.value.fields.filter(field => field && typeof field.name === 'string'
    && ['select', 'radio', 'checkbox', 'text', 'datetime-local', 'number', 'date', 'time'].includes(field.type));
  if (Array.isArray(action.value?.choices) && action.value.choices.length) return [{
    name: action.value.selection_name || 'selection', label: '选择参赛时间', type: 'select',
    required: action.value.selection_required === true, options: action.value.choices,
  }];
  return [];
});
const optionsFor = field => (Array.isArray(field.options) ? field.options : []).filter(option => option && option.value !== undefined);
const busy = computed(() => !!activeAction.value || c.teamChanging || c.accountChanging || c.participationChanging);
const waiting = computed(() => room.value?.status === 'waiting');
const ownLocked = computed(() => ['ready', 'starting', 'uncertain'].includes(self.value.status));
const members = computed(() => Array.isArray(room.value?.members) ? room.value.members : []);
const readyCount = computed(() => members.value.filter(member => member.status === 'ready').length);
const active = computed(() => !disposed && c.hasTeam && !c.state?.demo && c.part?.mode === 'vp'
  && c.tab === 'participation' && documentVisible.value && !c.teamChanging && !c.accountChanging && !c.participationChanging);
const identity = computed(() => JSON.stringify([c.state?.contest?.id, c.state?.team?.id, c.state?.team?.device_id,
  c.state?.team?.role, c.state?.user?.username, !!c.state?.user?.authenticated, c.part?.mode, !!c.state?.demo]));
const invite = computed(() => {
  if (!captain.value || !room.value?.code) return '';
  const url = new URL(location.href);
  url.search = '';
  url.searchParams.set('joint', room.value.code);
  url.hash = 'participation';
  return url.href;
});
const skew = computed(() => {
  const starts = members.value.map(member => Number(member.started_at)).filter(value => Number.isFinite(value) && value > 0);
  if (starts.length < 2) return '';
  return `${Math.round((Math.max(...starts) - Math.min(...starts)) * 10) / 10} 秒`;
});
const roomLabel = computed(() => ({ waiting: '等待各队准备', starting: '正在启动各队 VP', started: '启动流程已完成', cancelled: '联合 VP 已取消' })[room.value?.status] || '联合 VP');
const statusLabel = status => ({ waiting: '未准备', ready: '已准备', starting: '正在启动', started: 'QOJ 已开赛',
  uncertain: '状态待核查', error: '启动异常', needs_confirmation: '需要队长确认' })[status] || '状态待确认';
const statusClass = status => ({ ready: 'joint-ready', started: 'joint-started', starting: 'joint-starting',
  uncertain: 'joint-warning', error: 'joint-warning', needs_confirmation: 'joint-warning' })[status] || '';

watch(() => [ownLocked.value, !!activeAction.value], () => emit('locked', ownLocked.value || !!activeAction.value), { immediate: true });
watch(() => JSON.stringify([plan.value?.fingerprint, action.value?.id, fields.value]), () => {
  const next = Object.create(null);
  for (const field of fields.value) next[field.name] = field.type === 'checkbox' ? false : String(field.default ?? field.value ?? '');
  values.value = next;
  consent.value = false;
}, { immediate: true });

function schedule() {
  clearTimeout(pollTimer);
  if (active.value) pollTimer = setTimeout(() => loadRoom(), room.value?.status === 'starting' ? 1500 : 5000);
}

function adopt(result) {
  const wasStarted = self.value.status === 'started';
  room.value = result?.joint_vp || null;
  loaded.value = true;
  if (!wasStarted && self.value.status === 'started') c.refreshState(true);
}

async function loadRoom() {
  if (!active.value || loading.value || activeAction.value) return;
  const current = generation;
  const controller = new AbortController();
  requestController = controller;
  loading.value = true;
  try {
    const result = await c.api('/api/joint-vp', undefined, { signal: controller.signal });
    if (current === generation && !disposed) { adopt(result); readError.value = ''; }
  } catch (failure) {
    if (current === generation && failure.name !== 'AbortError') readError.value = failure.message || '无法读取联合 VP 状态。';
  } finally {
    if (current === generation) {
      loading.value = false;
      requestController = null;
      schedule();
    }
  }
}

async function mutate(name, payload = {}) {
  if (!captain.value || busy.value) return;
  // A stale GET must not replace the result of a newer POST.
  requestController?.abort();
  const current = ++generation;
  clearTimeout(pollTimer);
  loading.value = false;
  activeAction.value = name;
  error.value = '';
  const controller = new AbortController();
  requestController = controller;
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const result = await c.api(`/api/joint-vp/${name}`, payload, { signal: controller.signal });
    if (current !== generation || disposed) return;
    adopt(result);
    if (['join', 'create', 'leave', 'cancel', 'unready', 'ready'].includes(name)) consent.value = false;
    if (name === 'refresh' && self.value.status !== 'started') await c.refreshState(true);
  } catch (failure) {
    if (current !== generation || disposed) return;
    error.value = failure.name === 'AbortError'
      ? '请求超时，操作可能已经执行。请刷新状态核查；系统不会自动重发。'
      : failure.message || '无法完成操作，请刷新状态核查。';
  } finally {
    clearTimeout(timeout);
    if (current === generation) {
      activeAction.value = '';
      requestController = null;
      // This is a local status GET only, never a retry of a mutation.
      await loadRoom();
      schedule();
    }
  }
}

function ready() {
  if (!waiting.value || !plan.value?.fingerprint || !action.value || !consent.value || busy.value) return;
  const selection = Object.create(null);
  for (const field of fields.value) selection[field.name] = field.type === 'checkbox'
    ? values.value[field.name] === true : String(values.value[field.name] ?? '');
  mutate('ready', { fingerprint: plan.value.fingerprint, action: String(action.value.id), values: selection, consent: true });
}

function confirmAction(name, title, message) {
  if (!captain.value || busy.value || !room.value) return;
  const context = identity.value, roomId = room.value.id;
  c.confirm(title, message, async () => {
    if (context !== identity.value || roomId !== room.value?.id || !captain.value || busy.value) {
      throw new Error('队伍或联合 VP 状态已变化，请重新确认操作。');
    }
    await mutate(name);
  });
}

async function copyInvite() {
  const link = invite.value, context = identity.value;
  if (!link) return;
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(link); copied = true; }
  } catch { /* LAN HTTP can use the selected readonly field. */ }
  if (context !== identity.value || link !== invite.value) return;
  if (!copied) {
    inviteInput.value?.focus(); inviteInput.value?.select();
    try { copied = document.execCommand('copy'); } catch { /* Keep the selection for manual copying. */ }
  }
  c.notify(copied ? '联合 VP 邀请链接已复制。' : '链接已选中，请使用复制快捷键复制。', copied ? 'success' : 'info');
}

function invalidate() {
  generation++;
  requestController?.abort(); requestController = null;
  clearTimeout(pollTimer);
  loading.value = false; activeAction.value = '';
}

watch(identity, () => {
  invalidate(); room.value = null; loaded.value = false; error.value = ''; readError.value = ''; consent.value = false;
  if (active.value) loadRoom();
}, { immediate: true, flush: 'sync' });
watch(() => [c.tab, c.teamChanging, c.accountChanging, c.participationChanging], () => {
  if (active.value) loadRoom();
  else if (!activeAction.value) { invalidate(); }
});
function visibilityChanged() {
  documentVisible.value = !document.hidden;
  if (active.value) loadRoom();
  else clearTimeout(pollTimer);
}
document.addEventListener('visibilitychange', visibilityChanged);
onBeforeUnmount(() => { disposed = true; invalidate(); emit('locked', false); document.removeEventListener('visibilitychange', visibilityChanged); });
</script>

<template>
  <section id="joint-vp-panel" class="participation-card" aria-label="多队联合 VP">
    <div class="section-heading">
      <h2>Joint VP <span class="heading-note">多队联合开赛</span></h2>
      <button id="joint-vp-refresh" type="button" class="button button-outline button-small" :disabled="busy || loading"
              @click="captain && room ? mutate('refresh') : loadRoom()">{{ activeAction === 'refresh' || loading ? '正在刷新…' : '刷新状态' }}</button>
    </div>
    <div class="participation-body">
      <p class="field-help">各队准备后，由组织者一起启动 QOJ VP。实际开赛可能相差几秒；各队计时、榜单和封榜均以 QOJ 为准。</p>
      <p v-if="!loaded && !error && !readError" class="field-help" role="status">正在读取联合 VP 状态…</p>
      <div v-if="loaded && !room && captain" class="joint-setup">
        <div>
          <h3>组织一次联合 VP</h3>
          <p class="field-help">邀请其他队伍一起参加当前比赛，每队连接各自的 QOJ 账号。</p>
          <button id="joint-vp-create" type="button" class="button button-primary" :disabled="busy" @click="mutate('create')">创建联合 VP</button>
        </div>
        <form id="joint-vp-join-form" @submit.prevent="mutate('join', { code: joinCode.trim() })">
          <label for="joint-vp-join-code">联合 VP 邀请码</label>
          <div class="joint-inline">
            <input id="joint-vp-join-code" v-model="joinCode" type="text" maxlength="160" autocomplete="off" autocapitalize="none"
                   spellcheck="false" placeholder="输入组织者提供的邀请码" required :disabled="busy">
            <button id="joint-vp-join" type="submit" class="button button-outline" :disabled="busy || !joinCode.trim()">加入</button>
          </div>
          <p class="field-help">此邀请码用于整支队伍参加联合 VP，与 Team 的队员加入码不同。</p>
        </form>
      </div>
      <p v-if="loaded && !room && !captain" class="field-help">当前队伍尚未加入联合 VP，请由队长创建或加入。</p>
      <template v-if="room">
        <div class="joint-heading"><h3 id="joint-vp-status">{{ roomLabel }}</h3><span v-if="waiting" class="muted small">已准备 {{ readyCount }} / {{ members.length }} 队</span></div>
        <div v-if="captain && invite && waiting" class="joint-invite">
          <label for="joint-vp-invite">邀请其他队伍</label>
          <div class="joint-inline"><input id="joint-vp-invite" ref="inviteInput" :value="invite" type="text" readonly spellcheck="false"><button class="button button-outline" type="button" @click="copyInvite">复制链接</button></div>
          <p class="field-help">也可提供邀请码：<strong id="joint-vp-code">{{ room.code }}</strong></p>
        </div>
        <div class="table-scroll">
          <table id="joint-vp-members" class="joint-members">
            <thead><tr><th scope="col">队伍</th><th scope="col">状态</th><th scope="col">QOJ 开始时间</th><th scope="col">QOJ 结束时间</th></tr></thead>
            <tbody><tr v-for="member in members" :key="member.team_id" :data-joint-team="member.team_id">
              <td>{{ member.name }}<span v-if="String(member.team_id) === String(c.state?.team?.id)" class="device-self">本队</span></td>
              <td><span class="joint-status" :class="statusClass(member.status)">{{ statusLabel(member.status) }}</span><p v-if="member.message" class="joint-message">{{ member.message }}</p></td>
              <td class="small">{{ member.started_at ? dateTime(member.started_at) : '—' }}</td><td class="small">{{ member.ends_at ? dateTime(member.ends_at) : '—' }}</td>
            </tr></tbody>
          </table>
        </div>
        <p v-if="skew" id="joint-vp-skew" class="field-help">已确认队伍的最大开赛时间差：{{ skew }}。各队倒计时使用自己的 QOJ 时间。</p>
        <p v-if="self.status === 'ready'" class="joint-note">本队已准备，等待组织者启动。如需修改报名选项或单独开赛，请先取消准备。</p>
        <p v-if="self.status === 'starting'" class="joint-note" role="status">正在为本队启动 QOJ VP，请等待状态确认，勿重复点击 Start。</p>
        <p v-if="self.status === 'uncertain'" class="joint-note">尚未确认本队是否已开赛。请刷新状态核查，系统不会自动重复发送开赛请求。</p>
        <p v-if="self.status === 'error' || !waiting && self.status === 'needs_confirmation'" class="joint-note">请刷新状态并检查上方 QOJ 参赛提示，确认尚未开赛后可由队长单独完成报名或补开。</p>
        <template v-if="captain">
          <div v-if="waiting && !ownLocked" class="joint-prepare">
            <button id="joint-vp-prepare" type="button" class="button button-outline" :disabled="busy || !c.state?.user?.authenticated" @click="mutate('prepare')">{{ activeAction === 'prepare' ? '正在读取报名选项…' : plan ? '重新读取 QOJ 报名选项' : '读取 QOJ 报名选项' }}</button>
            <p v-if="!c.state?.user?.authenticated" class="field-help">请先连接本队的 QOJ 账号。</p>
            <form v-if="plan && action" id="joint-vp-ready-form" @submit.prevent="ready">
              <p v-if="plan.message" id="joint-vp-plan-message" class="joint-note">{{ plan.message }}</p>
              <div v-for="(field, index) in fields" :key="field.name" class="participation-field">
                <label v-if="field.type === 'checkbox'" class="participation-checkbox" :for="`joint-field-${index}`">
                  <input :id="`joint-field-${index}`" v-model="values[field.name]" type="checkbox" :data-joint-field="field.name" :required="!!field.required" :disabled="busy">
                  <span>{{ field.label || field.name }}</span>
                </label>
                <template v-else>
                  <label :for="`joint-field-${index}`">{{ field.label || field.name }}</label>
                  <select v-if="['select', 'radio'].includes(field.type)" :id="`joint-field-${index}`" v-model="values[field.name]" :data-joint-field="field.name" :required="!!field.required" :disabled="busy">
                    <option value="">请选择</option><option v-for="(option, optionIndex) in optionsFor(field)" :key="optionIndex" :value="String(option.value)">{{ option.label ?? option.value }}</option>
                  </select>
                  <input v-else :id="`joint-field-${index}`" v-model="values[field.name]" :type="field.type" :data-joint-field="field.name" maxlength="500" autocomplete="off" :min="field.min" :max="field.max" :step="field.step" :required="!!field.required" :disabled="busy">
                </template>
              </div>
              <label class="participation-checkbox joint-consent" for="joint-vp-consent"><input id="joint-vp-consent" v-model="consent" type="checkbox" required :disabled="busy"><span>我已确认 QOJ 报名选项，并授权组织者启动本次 VP。</span></label>
              <button id="joint-vp-ready" type="submit" class="button button-primary" :disabled="busy || !consent">{{ activeAction === 'ready' ? '正在准备…' : '确认准备' }}</button>
            </form>
          </div>
          <div class="joint-actions">
            <button v-if="waiting && self.status === 'ready'" id="joint-vp-unready" type="button" class="button button-outline" :disabled="busy" @click="mutate('unready')">取消准备</button>
            <button v-if="room.is_host && waiting" id="joint-vp-start" type="button" class="button button-primary" :disabled="busy || !room.can_start"
                    @click="confirmAction('start', '一起启动 QOJ VP？', '将为所有已准备队伍启动本次 QOJ VP，各队开赛时间可能相差几秒。单队失败不影响其他队伍；已开始的 QOJ VP 无法撤回。')">一起 Start</button>
            <button v-if="room.is_host && room.status !== 'starting'" id="joint-vp-cancel" type="button" class="button button-danger-outline" :disabled="busy"
                    @click="confirmAction('cancel', waiting ? '取消联合 VP？' : '结束联合组织？', '将停止本次联合开赛组织并清除各队准备状态。已经开始的 QOJ VP 不受影响。')">{{ waiting ? '取消联合 VP' : '结束联合组织' }}</button>
            <button v-if="!room.is_host && room.status !== 'starting'" id="joint-vp-leave" type="button" class="button button-outline" :disabled="busy || self.status === 'starting'"
                    @click="confirmAction('leave', '离开联合 VP？', '本队将退出本次联合开赛组织，已经开始的 QOJ VP、提交记录和队员连接不受影响。')">离开联合 VP</button>
          </div>
          <p v-if="room.is_host && waiting && !room.can_start" class="field-help">所有队伍准备完成后，组织者才能一起启动。</p>
        </template>
        <p v-else class="field-help">联合 VP 的准备和开赛操作由队长电脑管理。</p>
      </template>
      <p v-if="error || readError" id="joint-vp-error" class="inline-error" role="alert">{{ error || readError }}</p>
    </div>
  </section>
</template>

<style scoped>
.joint-setup{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:18px}
.joint-setup h3,.joint-heading h3{font-size:16px;font-weight:500}
.joint-setup .field-help{margin:10px 0}
.joint-inline,.joint-heading,.joint-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.joint-inline input{min-width:0;flex:1}
.joint-heading{justify-content:space-between;margin:20px 0 12px}
.joint-invite{margin-bottom:18px}
.joint-members{width:100%;border-collapse:collapse}
.joint-members th,.joint-members td{padding:12px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.joint-members th{font-size:12px;font-weight:600;background:#f2f6fa;white-space:nowrap}
.joint-members td:first-child{min-width:100px;overflow-wrap:anywhere}
.joint-members td:nth-child(2){min-width:120px}
.joint-status{display:inline-block;border-radius:3px;padding:3px 7px;background:#edf0f3;font-size:12px;white-space:nowrap}
.joint-ready{background:#e1f0e3;color:#245f2f}.joint-started{background:#d3e9db;color:#1e5936}
.joint-starting{background:#e8f1f9;color:#305b7b}.joint-warning{background:#fff5d4;color:#775b15}
.joint-message{font-size:12px;line-height:1.5;margin-top:6px;overflow-wrap:anywhere;white-space:pre-line}
.joint-note{background:#f2f6fa;border-left:3px solid #a5bdd2;padding:10px 12px;font-size:13px;line-height:1.6;margin:15px 0;white-space:pre-line;overflow-wrap:anywhere}
.joint-prepare{margin-top:20px}.joint-consent{margin:18px 0}.joint-actions{margin-top:18px}
@media(max-width:680px){.joint-setup{grid-template-columns:1fr;gap:20px}.joint-members th,.joint-members td{padding:9px 7px}.joint-inline{align-items:stretch}.joint-inline input{width:100%}}
</style>
