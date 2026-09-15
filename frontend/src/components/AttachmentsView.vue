<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { safeAttachmentURL } from '../lib/format.js';

const c = useContest();
const attachments = ref(null);
const status = ref('idle');
const error = ref('');
let requestId = 0;
let controller = null;
const allowed = computed(() => c.canRead && c.can('manage') && !c.teamChanging && !c.participationChanging);
const loading = computed(() => allowed.value && status.value === 'loading');
const loaded = computed(() => allowed.value && status.value === 'loaded');
const files = computed(() => {
  if (!allowed.value || !loaded.value) return [];
  return (attachments.value?.attachments || []).map((file, index) => {
    const external = file.external === true;
    const url = safeAttachmentURL(external ? file.url : file.download_url, external);
    return { ...file, key: file.id || `${index}-${file.name || ''}`, external, url,
      name: file.name || '未命名附件', host: external && url ? new URL(url).host : '' };
  });
});
const sourceURL = computed(() => {
  if (!allowed.value || c.state?.demo) return null;
  const contestId = String(c.state?.contest?.id || '');
  if (!/^[1-9][0-9]*$/.test(contestId)) return null;
  const value = attachments.value?.source_url || `/qoj/contest/${contestId}`;
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && !url.username && !url.password
      && [ `/qoj/contest/${contestId}`, `/qoj/contest/${contestId}/` ].includes(url.pathname)
      && !url.search && !url.hash ? url.href : null;
  } catch { return null; }
});

function invalidate() {
  requestId += 1;
  controller?.abort();
  controller = null;
  attachments.value = null;
  status.value = 'idle';
  error.value = '';
}

async function loadAttachments(refresh = false) {
  if (!allowed.value || status.value === 'loading' || (!refresh && status.value !== 'idle')) return;
  const request = ++requestId;
  const context = c.context;
  const activeController = new AbortController();
  controller?.abort();
  controller = activeController;
  status.value = 'loading';
  error.value = '';
  const current = () => request === requestId && context === c.context && allowed.value;
  try {
    const result = await c.api(`/api/attachments${refresh ? '?refresh=1' : ''}`, undefined, { signal: activeController.signal });
    if (!current()) return;
    if (!result || !Array.isArray(result.attachments) || result.attachments.some(file => !file || typeof file !== 'object' || Array.isArray(file))) {
      throw new Error('服务器返回了无法读取的附件列表。');
    }
    attachments.value = result;
    status.value = 'loaded';
  } catch (failure) {
    if (!current() || failure.name === 'AbortError') return;
    attachments.value = null;
    status.value = 'error';
    error.value = `无法获取比赛附件：${failure.message} 可刷新重试，或打开原比赛页面查看。`;
  } finally {
    if (request === requestId) controller = null;
  }
}

watch([() => c.context, allowed, () => c.resourceRevision], () => {
  invalidate();
  if (c.tab === 'attachments' && allowed.value) loadAttachments();
}, { immediate: true, flush: 'sync' });
watch(() => c.tab, tab => { if (tab === 'attachments') loadAttachments(); });
onBeforeUnmount(invalidate);
</script>

<template>
  <section id="view-attachments" class="view" aria-label="比赛附件" :hidden="c.tab !== 'attachments'">
    <section class="card table-card">
      <div class="section-heading attachments-heading"><h2>Attachments <span class="heading-note">比赛附件</span></h2><button id="attachments-refresh" type="button" class="button button-outline button-small" :disabled="!allowed || loading" @click="loadAttachments(true)">{{ loading ? '正在刷新…' : '刷新附件' }}</button></div>
      <p class="panel-note">下载题面合集、样例等比赛提供的文件。受限附件需要连接有访问权限的 QOJ 账号。</p>
      <div id="attachments-status" class="empty-state" role="status" :hidden="!loading">正在获取比赛附件…</div>
      <div id="attachments-error" class="inline-error" role="alert" :hidden="!allowed || !error">{{ allowed ? error : '' }}</div>
      <div id="attachments-empty" class="empty-state" :hidden="!loaded || !!files.length"><strong>暂未发现可下载附件</strong>比赛可能未提供附件，也可以在原比赛页面查看。</div>
      <div id="attachments-table" class="table-scroll" :hidden="!loaded || !files.length" :aria-busy="String(loading)">
        <table v-if="loaded && files.length" class="attachments-table">
          <thead><tr><th scope="col">Filename / 文件名</th><th scope="col">Type / 类型</th><th scope="col">Size / 大小</th><th scope="col" class="cell-right">操作</th></tr></thead>
          <tbody><tr v-for="file in files" :key="file.key">
            <td class="attachment-name">{{ file.name }}<span v-if="file.host" class="attachment-host">外部链接 · {{ file.host }}</span></td><td class="attachment-type">{{ file.type || '—' }}</td><td class="attachment-size">{{ file.size || '—' }}</td>
            <td class="cell-right"><a v-if="file.url" class="button button-outline button-small" :href="file.url" target="_blank" rel="noopener noreferrer" :download="file.external ? undefined : ''" :aria-label="file.external ? `打开外部链接：${file.name}` : `下载 ${file.name}`">{{ file.external ? '打开外部链接 ↗' : '下载' }}</a><span v-else class="muted small">链接不可用</span></td>
          </tr></tbody>
        </table>
      </div>
      <div class="attachments-footer"><span id="attachments-count" class="muted small" aria-live="polite">{{ loaded ? `共 ${files.length} 项附件` : '' }}</span><a id="attachments-source" class="text-link" :href="sourceURL || undefined" target="_blank" rel="noopener noreferrer" :hidden="!sourceURL">查看原比赛页面（代理） ↗</a></div>
    </section>
  </section>
</template>
