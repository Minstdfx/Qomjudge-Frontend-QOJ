<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import BaseDialog from './BaseDialog.vue';

const c = useContest();
const source = ref('');
const filename = ref('');
const meta = ref('');
const message = ref('');
const error = ref('');
const loading = ref(false);
const loaded = ref(false);
const downloadURL = ref('');
const currentId = ref(null);
const codeInput = ref(null);
let controller = null;
let request = 0;
const allowed = computed(() => c.canRead && c.can('manage'));

function clear() {
  request += 1;
  controller?.abort(); controller = null;
  if (downloadURL.value) URL.revokeObjectURL(downloadURL.value);
  downloadURL.value = ''; source.value = ''; filename.value = ''; meta.value = '';
  message.value = ''; error.value = ''; loading.value = false; loaded.value = false; currentId.value = null;
}

function closeAndClear() {
  clear();
  c.sourceOpen = false;
  c.sourceId = null;
}

async function readSource() {
  const id = String(c.sourceId || '');
  const submission = (Array.isArray(c.state?.submissions) ? c.state.submissions : []).find((item) => String(item.id) === id);
  if (!c.sourceOpen || !allowed.value || !/^[1-9]\d*$/.test(id) || !submission) { closeAndClear(); return; }
  clear(); currentId.value = id;
  const currentRequest = request;
  const context = c.context;
  const currentController = new AbortController(); controller = currentController;
  loading.value = true;
  const stale = () => currentRequest !== request || currentController.signal.aborted || context !== c.context || !allowed.value || !c.sourceOpen;
  try {
    const result = await c.api(`/api/submissions/${encodeURIComponent(id)}/source`, undefined, { signal: currentController.signal });
    if (stale()) return;
    if (typeof result?.source !== 'string') throw new Error('服务器返回了无法读取的源代码。');
    filename.value = typeof result.filename === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.+\-]{0,199}$/.test(result.filename) ? result.filename : `submission-${id}.txt`;
    const reference = result.qoj_id ? `QOJ #${result.qoj_id}` : String(submission.verdict).toUpperCase() === 'FAILED' ? '未生成 QOJ 提交编号' : 'QOJ 编号待确认';
    meta.value = `${typeof result.language === 'string' ? result.language : submission.language || '未知语言'} · ${reference}`;
    source.value = result.source;
    downloadURL.value = URL.createObjectURL(new Blob([result.source], { type: 'text/plain;charset=utf-8' }));
    loaded.value = true;
  } catch (cause) {
    if (!stale() && cause.name !== 'AbortError') error.value = cause.message;
  } finally {
    if (currentRequest === request) { controller = null; loading.value = false; }
  }
}

async function copySource() {
  if (!allowed.value || !loaded.value || !codeInput.value) return;
  const currentRequest = request;
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(source.value); copied = true; }
  } catch { /* HTTP LAN pages fall back to selecting the readonly textarea. */ }
  if (currentRequest !== request || !c.sourceOpen || !allowed.value) return;
  if (!copied) {
    codeInput.value.focus(); codeInput.value.select();
    try { copied = document.execCommand('copy'); } catch { /* Keep the selection for manual copying. */ }
  }
  message.value = copied ? '代码已复制。' : '代码已选中，请使用复制快捷键复制。';
}

watch(() => c.context, closeAndClear, { flush: 'sync' });
watch(allowed, (value) => { if (!value) closeAndClear(); }, { flush: 'sync' });
watch([() => c.sourceOpen, () => c.sourceId], () => {
  if (c.sourceOpen) readSource();
  else clear();
}, { immediate: true });
onBeforeUnmount(clear);
</script>

<template>
  <BaseDialog id="submission-source-dialog" v-model:open="c.sourceOpen" class="modal submission-source-modal" aria-labelledby="submission-source-title">
    <div class="modal-heading"><h2 id="submission-source-title">{{ currentId ? `提交 #${currentId} 的代码` : '提交代码' }}</h2><button type="button" class="icon-button" data-close="submission-source-dialog" aria-label="关闭代码查看" @click="c.sourceOpen = false">×</button></div>
    <div class="modal-body">
      <p id="submission-source-loading" class="muted" role="status" :hidden="!loading">正在读取保存的代码…</p>
      <div class="submission-source-details"><strong id="submission-source-name">{{ filename }}</strong><span id="submission-source-meta" class="muted small">{{ meta }}</span></div>
      <textarea id="submission-source-code" ref="codeInput" class="code-input" aria-label="本条提交保存的源代码" readonly spellcheck="false" autocapitalize="off" wrap="off" rows="18" :hidden="!loaded" :value="source"></textarea>
      <p id="submission-source-status" class="field-help" role="status" aria-live="polite">{{ message }}</p>
      <div id="submission-source-error" class="inline-error" role="alert" :hidden="!error">{{ error }}</div>
    </div>
    <div class="modal-footer">
      <button id="submission-source-retry" type="button" class="button button-outline" :disabled="loading || !currentId" @click="readSource">重新读取</button>
      <a id="submission-source-download" class="button button-outline" :hidden="!downloadURL" :href="downloadURL || undefined" :download="downloadURL ? filename : undefined">下载代码</a>
      <button id="submission-source-copy" type="button" class="button button-primary" :disabled="!loaded" @click="copySource">复制代码</button>
    </div>
  </BaseDialog>
</template>
