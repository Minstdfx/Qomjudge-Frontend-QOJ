<script setup>
import { computed, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { dateTime } from '../lib/format.js';

const c = useContest();
const selectedMode = ref('vp');
const selectedAction = ref('');
const fieldValues = ref(Object.create(null));
const refreshing = ref(false);
const demoStarting = ref(false);
const teamName = ref('My Team');
const teamNameInput = ref(null);
const mode = computed(() => c.part?.mode === 'official' ? 'official' : 'vp');
const actions = computed(() => (Array.isArray(c.part?.actions) ? c.part.actions : [])
  .filter((action) => action && ['string', 'number'].includes(typeof action.id)));
const action = computed(() => actions.value.find((item) => String(item.id) === selectedAction.value) || actions.value[0]);
const fields = computed(() => {
  const selected = action.value;
  if (Array.isArray(selected?.fields) && selected.fields.length) {
    return selected.fields.filter((field) => field && typeof field.name === 'string'
      && ['select', 'radio', 'checkbox', 'text', 'datetime-local', 'number', 'date', 'time'].includes(field.type));
  }
  if (Array.isArray(selected?.choices) && selected.choices.length) {
    return [{ name: selected.selection_name || 'selection', label: '选择参赛时间', type: 'select',
      required: selected.selection_required === true, options: selected.choices }];
  }
  return [];
});
const optionsFor = (field) => (Array.isArray(field.options) ? field.options : [])
  .filter((option) => option && option.value !== undefined);

watch(() => [mode.value, c.participationChanging], () => {
  if (!c.participationChanging) selectedMode.value = mode.value;
}, { immediate: true });
watch(() => JSON.stringify([mode.value, actions.value.map((item) => [String(item.id), item.label])]), () => {
  if (!actions.value.some((item) => String(item.id) === selectedAction.value)) {
    selectedAction.value = actions.value.length ? String(actions.value[0].id) : '';
  }
}, { immediate: true });
// Schema/context changes reset defaults; polling an unchanged form preserves edits.
watch(() => JSON.stringify([c.context, mode.value, action.value?.id, fields.value]), () => {
  const values = Object.create(null);
  for (const field of fields.value) {
    const initial = field.default ?? field.value ?? '';
    values[field.name] = field.type === 'checkbox' ? initial === true : String(initial);
  }
  fieldValues.value = values;
}, { immediate: true });

const modeHelp = computed(() => c.state?.demo ? '当前为离线演示，使用本地模拟比赛。'
  : c.can('manage') ? '切换模式仅查询参赛状态。下方的注册或开始按钮会执行 QOJ 对应操作。'
    : '参赛模式和 QOJ 注册、开始操作由队长电脑管理。');
const statusTitle = computed(() => {
  if (c.participationChanging) return '正在更新参赛状态…';
  return ({
    unregistered: mode.value === 'official' ? '尚未注册正式比赛' : '尚未开始 QOJ VP',
    scheduled: '已安排参赛，等待开赛', running: '比赛正在进行',
    finished: '比赛已结束', unavailable: '暂时无法确认参赛状态',
  })[c.part?.status] || '正在确认参赛状态';
});
const statusMessage = computed(() => c.state && !c.hasTeam ? '请先在 Team 创建队伍，或使用队长提供的邀请码加入队伍。完成组队并确认开赛后显示比赛内容。'
  : c.participationChanging ? '请稍候，正在与 QOJ 同步。'
  : c.part?.message || (!c.can('manage') ? '请等待队长完成参赛操作；开赛后会自动显示比赛内容。'
    : c.part?.status === 'scheduled' ? '到达 QOJ 安排的开赛时间并确认已开赛后，比赛内容将自动开放。'
      : c.canRead ? '可以进入比赛查看题面和成绩。' : '连接 QOJ 账号后，使用此处提供的参赛操作。'));
const schedule = computed(() => {
  const parts = [];
  if (Number(c.part?.started_at)) parts.push(`开始：${dateTime(c.part.started_at)}`);
  if (Number(c.part?.ends_at)) parts.push(`结束：${dateTime(c.part.ends_at)}`);
  return parts.join(' · ');
});
const startLabel = computed(() => c.participationChanging ? '正在处理…'
  : action.value?.label || (action.value?.kind === 'register' ? '在 QOJ 注册参赛' : '在 QOJ 开始参赛'));
const demoLabel = computed(() => c.state?.run ? '重新开始离线演示' : '开始离线演示');
const enterTab = computed(() => c.can('manage') ? 'overview' : 'problems');

function changeMode() {
  if (!c.can('manage') || c.state?.demo || c.participationChanging
    || !['vp', 'official'].includes(selectedMode.value)) return;
  c.updateParticipation('/api/mode', { mode: selectedMode.value });
}

async function refresh() {
  if (refreshing.value || c.participationChanging || !c.state || c.state.team?.role === 'revoked') return;
  refreshing.value = true;
  c.participationError = '';
  try {
    const state = await c.refreshState(true);
    if (!state) c.participationError = '无法确认参赛状态，请检查网络后重试。';
  } catch (error) { c.participationError = error.message || '无法确认参赛状态，请检查网络后重试。'; }
  finally { refreshing.value = false; }
}

function startNative() {
  if (!c.hasTeam || !c.can('manage') || c.state?.demo || c.participationChanging || !action.value || !c.state?.user?.authenticated) return;
  const selection = Object.create(null);
  for (const field of fields.value) {
    selection[field.name] = field.type === 'checkbox' ? fieldValues.value[field.name] === true
      : String(fieldValues.value[field.name] ?? '');
  }
  c.updateParticipation('/api/start', { mode: mode.value, action: String(action.value.id), selection });
}

async function startDemo(reset, name) {
  if (!c.hasTeam || !c.state?.demo || !c.can('manage') || demoStarting.value || c.participationChanging) return;
  demoStarting.value = true;
  try {
    await c.api(reset ? '/api/reset' : '/api/start', c.state.team ? {} : { team_name: name });
    await c.refreshState(true);
    c.navigate('overview');
    c.notify('离线演示已开始。', 'success');
  } finally { demoStarting.value = false; }
}

async function submitDemo() {
  if (!c.hasTeam || !c.state?.demo || !c.can('manage') || demoStarting.value || c.participationChanging) return;
  const name = c.state.team?.name || teamName.value.trim();
  if (!name) { teamNameInput.value?.focus(); return; }
  if (c.state.run) {
    c.confirm('重新开始离线演示？', '这会重新计时，并清空本轮演示的提交和成绩。', () => startDemo(true, name));
  } else {
    try { await startDemo(false, name); }
    catch (error) { c.notify(error.message, 'error'); }
  }
}
</script>

<template>
  <section id="view-participation" class="view" aria-label="参赛入口" :hidden="c.tab !== 'participation'">
    <section class="participation-card">
      <div class="section-heading">
        <h2>Contest <span class="heading-note">参赛入口</span></h2>
        <button id="participation-refresh" type="button" class="button button-outline button-small"
                :disabled="refreshing || c.participationChanging || !c.state || c.state.team?.role === 'revoked'"
                @click="refresh">{{ refreshing ? '正在刷新…' : '刷新状态' }}</button>
      </div>
      <div class="participation-body">
        <div class="participation-mode-row">
          <label for="participation-mode">参赛模式</label>
          <select id="participation-mode" v-model="selectedMode"
                  :disabled="!c.can('manage') || c.participationChanging || !!c.state?.demo" @change="changeMode">
            <option value="vp">QOJ 虚拟参赛（VP）</option>
            <option value="official">QOJ 正式比赛</option>
          </select>
        </div>
        <p id="participation-mode-help" class="field-help">{{ modeHelp }}</p>
        <div class="participation-status-box" :class="{ 'participation-uncertain': c.part?.entry_uncertain === true }">
          <h3 id="participation-status-title">{{ statusTitle }}</h3>
          <p id="participation-message">{{ statusMessage }}</p>
          <p id="participation-schedule" class="small" :hidden="!schedule">{{ schedule }}</p>
        </div>
        <p id="participation-locked-note" class="field-help" :hidden="c.canRead">{{ c.hasTeam ? '开赛前不显示比赛标题、题目、附件、榜单、提交记录或打印文件。' : '未创建或加入队伍时，仅开放 Contest 与 Team 页面。' }}</p>
        <div class="participation-entry-actions">
          <a id="participation-team-setup" class="button button-primary" href="#team" :hidden="!c.state || c.hasTeam"
             @click.prevent="c.navigate('team')">{{ c.revoked ? '前往 Team 重新加入' : '前往 Team 创建或加入队伍' }}</a>
          <button id="participation-connect" type="button" class="button button-primary"
                  :hidden="!c.can('manage') || !!c.state?.demo || !!c.state?.user?.authenticated" @click="c.openAccount()">连接 QOJ 账号</button>
          <a id="participation-enter" class="button button-primary" :href="`#${enterTab}`" :hidden="!c.canRead"
             @click.prevent="c.navigate(enterTab)">进入比赛</a>
        </div>
        <form id="participation-start-form" :hidden="!c.hasTeam || !c.can('manage') || !!c.state?.demo || !actions.length" @submit.prevent="startNative">
          <div id="participation-action-choice" class="participation-field" :hidden="actions.length < 2">
            <label for="participation-action">QOJ 参赛操作</label>
            <select id="participation-action" v-model="selectedAction" :disabled="c.participationChanging">
              <option v-for="item in actions" :key="String(item.id)" :value="String(item.id)">{{ item.label || item.id }}</option>
            </select>
          </div>
          <div id="participation-fields">
            <div v-for="(field, index) in fields" :key="`${action?.id}:${index}:${field.name}`" class="participation-field">
              <label v-if="field.type === 'checkbox'" class="participation-checkbox" :for="`participation-field-${index}`">
                <input :id="`participation-field-${index}`" v-model="fieldValues[field.name]" type="checkbox"
                       :data-participation-field="field.name" :required="!!field.required" :disabled="c.participationChanging">
                <span>{{ field.label || field.name }}</span>
              </label>
              <template v-else>
                <label :for="`participation-field-${index}`">{{ field.label || field.name }}</label>
                <select v-if="['select', 'radio'].includes(field.type)" :id="`participation-field-${index}`"
                        v-model="fieldValues[field.name]" :data-participation-field="field.name"
                        :required="!!field.required" :disabled="c.participationChanging">
                  <option value="">请选择</option>
                  <option v-for="(option, optionIndex) in optionsFor(field)" :key="optionIndex" :value="String(option.value)">{{ option.label ?? option.value }}</option>
                </select>
                <input v-else :id="`participation-field-${index}`" v-model="fieldValues[field.name]" :type="field.type"
                       :data-participation-field="field.name" maxlength="500" autocomplete="off"
                       :min="field.min" :max="field.max" :step="field.step" :required="!!field.required" :disabled="c.participationChanging">
              </template>
            </div>
          </div>
          <button id="participation-start-button" type="submit" class="button button-primary"
                  :disabled="!c.hasTeam || c.participationChanging || !c.can('manage') || !action || !c.state?.user?.authenticated">{{ startLabel }}</button>
        </form>
        <div id="participation-error" class="inline-error" role="alert" :hidden="!c.participationError">{{ c.participationError }}</div>
      </div>
    </section>
    <section id="start-panel" class="start-panel" :hidden="!c.hasTeam || !c.state?.demo || c.running || !c.can('manage') || mode === 'official'">
      <div><h2>{{ demoLabel }}</h2><p class="muted">演示计时开始后显示模拟比赛内容。</p></div>
      <form id="start-form" @submit.prevent="submitDemo">
        <label id="team-name-label" class="sr-only" for="team-name" :hidden="!!c.state?.team">队伍名称</label>
        <input id="team-name" ref="teamNameInput" v-model="teamName" name="team_name" maxlength="80" placeholder="输入队伍名称"
               :hidden="!!c.state?.team" :disabled="!!c.state?.team" :required="!c.state?.team">
        <span id="start-team-name" class="muted" :hidden="!c.state?.team">{{ c.state?.team ? `队伍：${c.state.team.name}` : '' }}</span>
        <button id="start-button" class="button button-primary" type="submit"
                :disabled="!c.hasTeam || !c.state?.demo || !!c.state?.contest_error || c.participationChanging || demoStarting">{{ demoStarting ? '正在开始…' : demoLabel }}</button>
      </form>
    </section>
  </section>
</template>
