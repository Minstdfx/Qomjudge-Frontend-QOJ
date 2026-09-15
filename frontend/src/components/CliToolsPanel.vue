<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { dateTime } from '../lib/format.js';

const c = useContest();
const enabled = ref(null);
const createdAt = ref(null);
const activeAction = ref('');
const error = ref('');
const downloadURL = ref('');
const allowed = computed(() => !!c.state?.team?.id && c.state.team.role === 'captain' && c.can('manage')
  && !c.teamChanging && !c.accountChanging && !c.participationChanging);
const busy = computed(() => !!activeAction.value || !allowed.value);
const status = computed(() => activeAction.value === 'status' ? '正在读取状态…'
  : enabled.value === true ? '已启用' : enabled.value === false ? '尚未启用' : '状态未确认');
let controller;
let revision = 0;
let downloadContext = '';

function clearDownload() {
  if (downloadURL.value) URL.revokeObjectURL(downloadURL.value);
  downloadURL.value = '';
  downloadContext = '';
}

function reset() {
  revision++;
  controller?.abort();
  controller = null;
  activeAction.value = '';
  enabled.value = null;
  createdAt.value = null;
  error.value = '';
  clearDownload();
}

function current(requestRevision, context) {
  return requestRevision === revision && context === c.context && allowed.value;
}

async function loadStatus(preserveError = false) {
  if (!allowed.value || activeAction.value || c.tab !== 'team') return;
  const context = c.context;
  const requestRevision = ++revision;
  const request = new AbortController();
  controller = request;
  activeAction.value = 'status';
  if (!preserveError) error.value = '';
  try {
    const result = await c.api('/api/cli', undefined, { signal: request.signal });
    if (!current(requestRevision, context)) return;
    if (typeof result.enabled !== 'boolean') throw new Error('Invalid CLI status');
    const nextCreatedAt = Number(result.created_at) || null;
    if (!result.enabled || (createdAt.value && nextCreatedAt !== createdAt.value)) clearDownload();
    enabled.value = result.enabled;
    createdAt.value = nextCreatedAt;
  } catch (failure) {
    if (current(requestRevision, context) && failure.name !== 'AbortError') {
      enabled.value = null;
      error.value = '无法确认命令行配置状态，请刷新状态后重试。';
      clearDownload();
    }
  } finally {
    if (current(requestRevision, context)) { activeAction.value = ''; controller = null; }
  }
}

async function changeConfig(action, context) {
  if (context !== c.context || !allowed.value) throw new Error('当前队伍或权限已变化，请重新确认操作。');
  if (activeAction.value || enabled.value === null) throw new Error('请等待配置状态更新后重试。');
  const requestRevision = ++revision;
  const request = new AbortController();
  controller = request;
  activeAction.value = action;
  error.value = '';
  clearDownload();
  let reconcile = false;
  try {
    const result = await c.api(action === 'generate' ? '/api/cli/token' : '/api/cli/revoke', {}, { signal: request.signal });
    if (!current(requestRevision, context)) return;
    if (action === 'generate') {
      const config = result.config;
      if (!config || config.version !== 1 || typeof config.token !== 'string' || !config.token
        || String(config.team_id) !== String(c.state.team.id) || String(config.contest_id) !== String(c.state.contest.id)) {
        throw new Error('Invalid CLI configuration');
      }
      const server = new URL(config.server_url);
      if (!['http:', 'https:'].includes(server.protocol) || server.username || server.password) throw new Error('Invalid CLI server');
      downloadURL.value = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2) + '\n'], { type: 'application/json;charset=utf-8' }));
      downloadContext = context;
      enabled.value = true;
      createdAt.value = Number(result.created_at) || null;
      c.notify('命令行配置已生成，请下载到队长电脑。', 'success');
    } else {
      if (result.enabled !== false) throw new Error('Invalid CLI revocation');
      enabled.value = false;
      createdAt.value = null;
      c.notify('命令行配置已撤销。', 'success');
    }
  } catch (failure) {
    if (current(requestRevision, context) && failure.name !== 'AbortError') {
      enabled.value = null;
      error.value = action === 'generate'
        ? '未能取得新的配置文件。请确认状态并重新生成后下载；请勿使用旧配置。'
        : '未能确认撤销结果，请刷新状态后重试。';
      reconcile = true;
      // Configuration responses contain a credential. Never echo an upstream
      // response or exception into the page, notification, or browser console.
      throw new Error(error.value);
    }
  } finally {
    if (current(requestRevision, context)) {
      activeAction.value = '';
      controller = null;
      if (reconcile) await loadStatus(true);
    }
  }
}

async function generate() {
  if (busy.value || enabled.value === null) return;
  const context = c.context;
  if (enabled.value) {
    c.confirm('重新生成命令行配置？',
      '当前队伍的旧配置将立即失效。生成后，请在队长电脑下载并替换 qoj-cli.json；网页中的参赛和队员连接不受影响。',
      () => changeConfig('generate', context));
    return;
  }
  try { await changeConfig('generate', context); } catch { /* Show the credential-free inline error. */ }
}

function revoke() {
  if (busy.value || !enabled.value) return;
  const context = c.context;
  c.confirm('撤销命令行配置？',
    '当前队伍已下载的配置将立即失效，无法再使用 submit 或 printfile。网页中的参赛和队员连接不受影响。',
    () => changeConfig('revoke', context));
}

function download(event) {
  if (busy.value || !enabled.value || !downloadURL.value || downloadContext !== c.context) event.preventDefault();
}

watch([() => c.context, allowed], () => {
  reset();
  if (allowed.value && c.tab === 'team') void loadStatus();
}, { immediate: true, flush: 'sync' });
watch(() => c.tab, tab => { if (tab === 'team') void loadStatus(); });
onBeforeUnmount(reset);
</script>

<template>
  <section v-if="allowed" id="cli-tools-panel" class="team-settings-card" aria-labelledby="cli-tools-title">
    <div class="team-current-heading">
      <h3 id="cli-tools-title">命令行工具 <span class="heading-note">submit / printfile</span></h3>
      <span id="cli-status" class="status-pill status-neutral" role="status">{{ status }}</span>
    </div>
    <p class="field-help">在队长电脑的终端使用 <code>submit</code> 提交代码，使用 <code>printfile</code> 上传虚拟打印文件，供本队队员在网页查看。虚拟打印不会连接实体打印机。</p>
    <p class="field-help">配置仅供队长电脑使用，持有者可代表本队提交代码和上传打印文件。文件不含 QOJ Cookie，请妥善保存。</p>
    <p class="field-help">从局域网另一台电脑使用时，配置中的 <code>server_url</code> 须为该电脑能访问的反代地址；建议先用服务器的局域网 IP 打开本页，再生成配置。</p>
    <p v-if="createdAt" id="cli-created-at" class="field-help">生成时间：{{ dateTime(createdAt) }}</p>
    <div id="cli-error" class="inline-error" role="alert" :hidden="!error">{{ error }}</div>
    <div class="cli-actions">
      <button id="cli-generate" type="button" class="button button-primary" :disabled="busy || enabled === null" @click="generate">{{ activeAction === 'generate' ? '正在生成…' : enabled ? '重新生成配置' : '生成配置' }}</button>
      <a v-if="downloadURL" id="cli-download" class="button button-outline" :href="downloadURL" download="qoj-cli.json" :aria-disabled="busy || !enabled" @click="download">下载 qoj-cli.json</a>
      <button id="cli-revoke" type="button" class="button button-danger-outline" :disabled="busy || !enabled" @click="revoke">{{ activeAction === 'revoke' ? '正在撤销…' : '撤销配置' }}</button>
      <button id="cli-refresh" type="button" class="button button-outline" :disabled="busy" @click="loadStatus()">刷新状态</button>
    </div>
    <p v-if="enabled && !downloadURL" class="field-help">已有配置无法重新下载；如已遗失，请重新生成。</p>
    <p v-else-if="downloadURL" class="field-help">请在离开当前队伍或刷新页面前下载配置；之后如需再次下载，须重新生成。</p>
    <p class="field-help">在项目根目录放入配置文件，可运行：</p>
    <pre class="cli-examples"><code>python cli/submit.py --config qoj-cli.json A.cpp
python cli/printfile.py --config qoj-cli.json notes.txt</code></pre>
    <p class="field-help">命令行工具随项目发布包提供，安装到 PATH 后可直接使用 <code>submit</code> 和 <code>printfile</code>。</p>
  </section>
</template>

<style scoped>
.cli-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.cli-examples { max-width: 100%; overflow: auto; padding: 12px; border: 1px solid #dce1e5; border-radius: 4px; background: #f6f8fa; color: #32485b; font-size: 13px; }
.cli-actions [aria-disabled="true"] { opacity: .55; cursor: not-allowed; }
#cli-tools-panel .team-current-heading { flex-wrap: wrap; }
</style>
