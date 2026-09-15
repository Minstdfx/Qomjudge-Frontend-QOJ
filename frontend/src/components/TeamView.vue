<script setup>
import { computed, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { dateTime } from '../lib/format.js';
import CliToolsPanel from './CliToolsPanel.vue';

const c = useContest();
const teamError = ref('');
const createName = ref('');
const createDevice = ref('队长电脑');
const joinInput = ref('');
const joinDevice = ref('队员电脑');
const activeAction = ref('');
const codeInput = ref(null);
const team = computed(() => c.state?.team || null);
const captain = computed(() => team.value?.role === 'captain');
const member = computed(() => team.value?.role === 'member');
const revoked = computed(() => team.value?.role === 'revoked');
const busy = computed(() => !!activeAction.value || c.teamChanging || c.participationChanging);
const joinCode = computed(() => captain.value && c.can('manage') ? team.value?.join_code || '' : '');
const devices = computed(() => team.value && !revoked.value && Array.isArray(team.value.devices) ? team.value.devices : []);
const roleLabel = computed(() => captain.value ? '队长' : member.value ? '队员' : '访问已撤销');
const roleDescription = computed(() => captain.value
  ? '由你连接 QOJ、选择参赛模式、注册或开始比赛、提交代码，并上传队员需要阅读的打印文件。'
  : member.value
    ? '代码提交、QOJ 账号和参赛操作由队长管理；开赛后可以查看比赛内容。'
    : '当前电脑无法继续读取比赛内容。离开队伍后可以重新加入。');

watch(() => c.context, () => {
  teamError.value = '';
  joinInput.value = '';
  createName.value = c.state?.run?.team_name || 'My Team';
}, { immediate: true, flush: 'sync' });

async function changeTeam(path, body, message, action) {
  if (busy.value) return;
  teamError.value = '';
  activeAction.value = action;
  try {
    await c.runTeamAction(path, body, message);
    joinInput.value = '';
    c.navigate('team');
  } catch (error) {
    teamError.value = error.message || '无法更新队伍，请重试。';
    throw error;
  } finally {
    activeAction.value = '';
  }
}

async function createTeam() {
  if (team.value || !c.can('manage') || busy.value) return;
  try {
    await changeTeam('/api/team/create', { name: createName.value.trim(), device_name: createDevice.value.trim() },
      '队伍已创建，将邀请码提供给队员即可加入。', 'create');
  } catch { /* The inline error is retained beside the forms. */ }
}

async function joinTeam() {
  if (team.value || !c.state || busy.value) return;
  try {
    await changeTeam('/api/team/join', { code: joinInput.value.trim(), device_name: joinDevice.value.trim() },
      '已加入队伍，当前电脑为队员模式。', 'join');
  } catch { /* The inline error is retained beside the forms. */ }
}

function confirmTeamAction(title, message, allowed, path, body, success, action) {
  if (busy.value || !allowed()) return;
  const context = c.context;
  c.confirm(title, message, async () => {
    if (context !== c.context || !allowed()) {
      teamError.value = '当前队伍或权限已变化，请重新确认操作。';
      throw new Error(teamError.value);
    }
    await changeTeam(path, body, success, action);
  });
}

function rotateCode() {
  confirmTeamAction('更换队员邀请码？', '旧邀请码将立即失效。已经加入的队员电脑会保持连接。',
    () => captain.value && c.can('manage'), '/api/team/rotate', {}, '邀请码已更换。', 'rotate');
}

function revokeDevice(device) {
  const id = String(device.id);
  confirmTeamAction('移除队员电脑？',
    `移除「${device.name || '这台电脑'}」后，这台电脑将无法继续查看比赛和打印文件；邀请码会自动更换，旧码立即失效。`,
    () => captain.value && c.can('manage') && devices.value.some(item => String(item.id) === id && item.role === 'member'),
    '/api/team/revoke', { device_id: id }, '队员电脑已移除，邀请码已自动更换。', `revoke-${id}`);
}

function leaveTeam() {
  confirmTeamAction('离开当前队伍？', '这台电脑将结束对当前队伍的访问。之后可以使用邀请码重新加入。',
    () => !!team.value && !captain.value, '/api/team/leave', {}, '已离开队伍，可重新加入。', 'leave');
}

async function copyCode() {
  const code = joinCode.value;
  const context = c.context;
  if (!code || busy.value) return;
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(code);
      copied = true;
    }
  } catch { /* A selected readonly field also works on a LAN HTTP origin. */ }
  if (context !== c.context || joinCode.value !== code) return;
  if (!copied) {
    codeInput.value?.focus();
    codeInput.value?.select();
    try { copied = document.execCommand('copy'); } catch { /* Keep the selection. */ }
  }
  c.notify(copied ? '邀请码已复制。' : '邀请码已选中，请使用复制快捷键复制。', copied ? 'success' : 'info');
}
</script>

<template>
  <section id="view-team" class="view" aria-label="队伍设置" :hidden="c.tab !== 'team'">
    <div class="section-heading"><h2>Team <span class="heading-note">队伍协作</span></h2></div>
    <p class="panel-note">队长使用自己的 QOJ 账号管理参赛和提交代码；最多两台队员电脑通过邀请码加入，共享题面、榜单和虚拟打印。</p>
    <div id="team-error" class="inline-error" role="alert" :hidden="!teamError">{{ teamError }}</div>
    <div id="team-solo" class="team-setup-grid" :hidden="!!team">
      <section id="team-create-panel" class="team-settings-card" :hidden="!c.can('manage')">
        <h3>创建队伍</h3><p class="field-help">以当前电脑作为队长，继续使用当前账号和参赛进度。</p>
        <form id="team-create-form" @submit.prevent="createTeam">
          <label for="team-create-name">队伍名称</label>
          <input id="team-create-name" v-model="createName" type="text" maxlength="80" placeholder="例如 My Team" :disabled="busy" required>
          <label for="team-create-device">这台电脑的名称</label>
          <input id="team-create-device" v-model="createDevice" type="text" maxlength="40" :disabled="busy" required>
          <button id="team-create-button" type="submit" class="button button-primary" :disabled="busy || !c.can('manage')">{{ activeAction === 'create' ? '正在创建…' : '创建队伍' }}</button>
        </form>
      </section>
      <section class="team-settings-card">
        <h3>加入队伍</h3><p class="field-help">向队长获取邀请码。加入后，这台电脑仅用于查看题面、榜单和打印文件。</p>
        <form id="team-join-form" @submit.prevent="joinTeam">
          <label for="team-join-input">邀请码</label>
          <input id="team-join-input" v-model="joinInput" type="text" maxlength="80" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="输入队长提供的邀请码" :disabled="busy" required>
          <label for="team-join-device">这台电脑的名称</label>
          <input id="team-join-device" v-model="joinDevice" type="text" maxlength="40" :disabled="busy" required>
          <button id="team-join-button" type="submit" class="button button-primary" :disabled="busy || !c.state">{{ activeAction === 'join' ? '正在加入…' : '加入队伍' }}</button>
        </form>
      </section>
    </div>
    <section id="team-current" class="team-settings-card" :hidden="!team">
      <div class="team-current-heading"><h3 id="team-display-name">{{ team?.name || '当前队伍' }}</h3><span id="team-role-label" class="status-pill status-neutral">{{ roleLabel }}</span></div>
      <p id="team-device-description" class="field-help">当前电脑：{{ team?.device_name || (captain ? '队长电脑' : '队员电脑') }}</p>
      <p id="team-role-description" class="field-help">{{ roleDescription }}</p>
      <div id="team-captain-panel" :hidden="!captain || !c.can('manage')">
        <label for="team-join-code">队员邀请码</label>
        <div class="team-code-row">
          <input id="team-join-code" ref="codeInput" :value="joinCode" type="text" readonly spellcheck="false" aria-describedby="team-code-help">
          <button id="team-code-copy" type="button" class="button button-outline" :disabled="!joinCode || busy" @click="copyCode">复制</button>
          <button id="team-code-rotate" type="button" class="button button-outline" :disabled="busy" @click="rotateCode">更换邀请码</button>
        </div>
        <p id="team-code-help" class="field-help">将邀请码提供给队员。更换后旧邀请码立即失效，已加入的电脑仍保持连接。</p>
      </div>
      <div id="team-devices" class="table-scroll" :hidden="!team || revoked">
        <table v-if="team && !revoked" class="team-devices-table">
          <thead><tr><th scope="col">电脑</th><th scope="col">角色</th><th scope="col">最近连接</th><th scope="col" class="cell-right">操作</th></tr></thead>
          <tbody>
            <tr v-for="device in devices" :key="device.id">
              <td>{{ device.name || '未命名电脑' }}<span v-if="String(device.id) === String(team.device_id)" class="device-self">当前电脑</span></td>
              <td>{{ device.role === 'captain' ? '队长' : '队员' }}</td><td class="small">{{ dateTime(device.last_seen) }}</td>
              <td class="cell-right"><button v-if="captain && c.can('manage') && device.role === 'member'" type="button" class="button button-danger-outline button-small" :data-revoke-device="device.id" :data-device-name="device.name || '这台电脑'" :disabled="busy" @click="revokeDevice(device)">移除</button><span v-else class="muted">—</span></td>
            </tr>
            <tr v-if="!devices.length" class="empty-row"><td colspan="4">尚无已连接电脑。</td></tr>
          </tbody>
        </table>
      </div>
      <div class="team-member-actions"><span id="team-member-count" class="muted small">{{ team && !revoked ? `队员电脑：${Number(team.member_count) || 0} / ${Number(team.max_members) || 2}` : '' }}</span><button id="team-leave-button" type="button" class="button button-danger-outline" :hidden="!team || captain" :disabled="busy" @click="leaveTeam">离开队伍</button></div>
    </section>
    <CliToolsPanel />
  </section>
</template>
