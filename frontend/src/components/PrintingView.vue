<script setup>
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { bytes, dateTime } from '../lib/format.js';

const c = useContest();
const printData = ref(null);
const loading = ref(false);
const uploading = ref(false);
const listError = ref('');
const uploadError = ref('');
const mode = ref('file');
const fileName = ref('notes.txt');
const content = ref('');
const selectedFile = shallowRef(null);
const fileInput = ref(null);
let listRequest = 0;
let uploadRequest = 0;
let lastLoaded = 0;
let listController = null;
let uploadController = null;
const allowed = computed(() => c.canRead && !!c.state?.run && !c.teamChanging && !c.participationChanging);
const canUpload = computed(() => allowed.value && c.can('print'));
const canAdd = computed(() => canUpload.value && c.running && !uploading.value);
const hasData = computed(() => allowed.value && !!printData.value);
const files = computed(() => allowed.value ? (printData.value?.files || []).map((file, index) => ({
  ...file, key: file.id || `${index}-${file.name || ''}`,
  name: file.name || '未命名文件',
  view: safePrintURL(file.view_url, 'view'), download: safePrintURL(file.download_url, 'download'),
})) : []);
const count = computed(() => hasData.value
  ? `本次比赛：${files.value.length} / 100 份 · ${bytes(files.value.reduce((total, file) => total + (Number(file.size) || 0), 0))} / 20 MiB`
  : '');
const uploadNote = computed(() => !allowed.value
  ? '开赛后可以添加打印文件；队员会看到队长在本次比赛中共享的文件。'
  : canUpload.value
    ? c.running
      ? '添加代码、文本或 PDF，供本队电脑查看、下载或打印。文件不会提交到 QOJ。'
      : '本次比赛已结束，仍可查看本次比赛的打印文件。'
    : '这里显示队长在本次比赛中共享的文件。打开「查看 / 打印」后可使用浏览器打印。');
const showEmpty = computed(() => (c.canRead && !c.state?.run)
  || (hasData.value && !files.value.length));

function safePrintURL(value, action) {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && !url.username && !url.password
      && new RegExp(`^/api/prints/[^/]+/${action}$`).test(url.pathname)
      && !url.search && !url.hash ? url.href : null;
  } catch { return null; }
}

function clearFile() {
  selectedFile.value = null;
  if (fileInput.value) fileInput.value.value = '';
}

function invalidateUpload() {
  uploadRequest += 1;
  uploadController?.abort();
  uploadController = null;
  uploading.value = false;
  uploadError.value = '';
  clearFile();
  content.value = '';
  fileName.value = 'notes.txt';
}

function cancelList() {
  listRequest += 1;
  listController?.abort();
  listController = null;
  loading.value = false;
  lastLoaded = 0;
}

function invalidate() {
  cancelList();
  invalidateUpload();
  printData.value = null;
  listError.value = '';
}

async function loadPrints(refresh = false) {
  if (!allowed.value || loading.value || (!refresh && Date.now() - lastLoaded < 3000)) return;
  const request = ++listRequest;
  const context = c.context;
  const activeController = new AbortController();
  listController?.abort();
  listController = activeController;
  loading.value = true;
  listError.value = '';
  const current = () => request === listRequest && context === c.context && allowed.value;
  try {
    const result = await c.api('/api/prints', undefined, { signal: activeController.signal });
    if (!current()) return;
    if (!result || !Array.isArray(result.files) || result.files.some(file => !file || typeof file !== 'object' || Array.isArray(file))) {
      throw new Error('服务器返回了无法读取的打印列表。');
    }
    printData.value = result;
  } catch (failure) {
    if (!current() || failure.name === 'AbortError') return;
    listError.value = `无法获取打印文件：${failure.message}`;
  } finally {
    if (request === listRequest) {
      loading.value = false;
      listController = null;
      lastLoaded = Date.now();
    }
  }
}

function selectFile(event) {
  selectedFile.value = event.target.files?.[0] || null;
  uploadError.value = '';
}

async function uploadPrint() {
  if (uploading.value) return;
  if (!canUpload.value) {
    uploadError.value = '打印文件由队长电脑添加。';
    return;
  }
  if (!c.running) {
    uploadError.value = '请先在参赛入口确认已开赛；比赛结束后不能继续添加打印文件。';
    return;
  }
  const request = ++uploadRequest;
  const context = c.context;
  const activeController = new AbortController();
  uploadController?.abort();
  uploadController = activeController;
  uploading.value = true;
  uploadError.value = '';
  const current = () => request === uploadRequest && context === c.context && canUpload.value && c.running;
  try {
    const maxSize = Math.min(2 * 1024 * 1024, Number(printData.value?.max_file_size) || 2 * 1024 * 1024);
    const supported = /\.(txt|cpp|cc|cxx|c|h|hpp|py|java|kt|rs|go|pas|js|cs|rb|md|pdf)$/i;
    let data;
    if (mode.value === 'text') {
      const name = fileName.value.trim();
      const text = content.value;
      if (!name || !supported.test(name) || /\.pdf$/i.test(name)) throw new Error('请填写文本或源代码文件名，例如 notes.txt 或 main.cpp。');
      if (!text.length) throw new Error('请先粘贴需要打印的代码或文本。');
      if (text.includes('\0')) throw new Error('打印文本不能包含空字符。');
      if (new TextEncoder().encode(text).length > maxSize) throw new Error('每份打印文件不能超过 2 MiB。');
      data = { name, content: text };
    } else {
      const file = selectedFile.value;
      if (!file) throw new Error('请先选择打印文件。');
      if (!supported.test(file.name)) throw new Error('请选择 UTF-8 文本、支持的源代码文件或 PDF。');
      if (!file.size) throw new Error('请选择非空文件。');
      if (file.size > maxSize) throw new Error('每份打印文件不能超过 2 MiB。');
      if (!/\.pdf$/i.test(file.name)) {
        let text;
        try { text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()); }
        catch { throw new Error('文本文件需要使用 UTF-8 编码，请转换编码后再上传。'); }
        if (text.includes('\0')) throw new Error('请选择文本或源代码文件，不要上传编译后的程序。');
      }
      data = new FormData();
      data.append('file', file);
    }
    // File reads cannot be aborted; check the original scope before the POST.
    if (!current()) return;
    await c.api('/api/prints', data, { signal: activeController.signal });
    if (!current()) return;
    clearFile();
    content.value = '';
    cancelList();
    c.notify('打印文件已添加，队员可在 Printing 页面查看。', 'success');
    await loadPrints(true);
  } catch (failure) {
    if (current() && failure.name !== 'AbortError') uploadError.value = failure.message;
  } finally {
    // An old request must not clear a new run's draft or uploading state.
    if (request === uploadRequest) {
      uploading.value = false;
      uploadController = null;
    }
  }
}

watch([() => c.context, allowed], () => {
  invalidate();
  if (c.tab === 'printing' && allowed.value) loadPrints();
}, { immediate: true, flush: 'sync' });
watch(canUpload, available => { if (!available) invalidateUpload(); }, { flush: 'sync' });
watch(() => c.running, running => { if (!running) invalidateUpload(); }, { flush: 'sync' });
watch(mode, () => { uploadError.value = ''; });
watch([() => c.tab, () => c.stateVersion], ([tab]) => {
  if (tab === 'printing') loadPrints();
}, { immediate: true });
onBeforeUnmount(invalidate);
</script>

<template>
  <section id="view-printing" class="view" aria-label="虚拟打印" :hidden="c.tab !== 'printing'">
    <section class="card table-card">
      <div class="section-heading"><h2>Printing <span class="heading-note">虚拟打印</span></h2><button id="prints-refresh" type="button" class="button button-outline button-small" :disabled="!allowed || loading" @click="loadPrints(true)">刷新列表</button></div>
      <p id="print-upload-note" class="panel-note">{{ uploadNote }}</p>
      <form id="print-upload-form" class="print-upload-form" :hidden="!canUpload" @submit.prevent="uploadPrint">
        <div class="print-mode-field"><label for="print-mode">添加方式</label><select id="print-mode" v-model="mode" :disabled="uploading || !canUpload"><option value="file">上传文件</option><option value="text">粘贴代码 / 文本</option></select></div>
        <div id="print-file-fields" :hidden="mode === 'text'"><label for="print-file">选择打印文件</label><input id="print-file" ref="fileInput" type="file" accept=".txt,.cpp,.cc,.cxx,.c,.h,.hpp,.py,.java,.kt,.rs,.go,.pas,.js,.cs,.rb,.md,.pdf" :disabled="uploading || !canUpload" @change="selectFile"></div>
        <div id="print-text-fields" :hidden="mode !== 'text'"><label for="print-name">文件名</label><input id="print-name" v-model="fileName" type="text" maxlength="180" placeholder="例如 main.cpp" :disabled="uploading || !canUpload"><label for="print-content">代码 / 文本内容</label><textarea id="print-content" v-model="content" class="code-input" rows="9" spellcheck="false" autocapitalize="off" placeholder="粘贴要发送给队员阅读、打印的代码或文本" :disabled="uploading || !canUpload"></textarea></div>
        <div class="print-upload-actions"><p class="field-help">支持 UTF-8 文本、常见源代码和 PDF；代码预览提供语法高亮与行号。每份文件最多 2 MiB，本次比赛最多 100 份、合计 20 MiB。文件仅在当前参赛记录内共享。</p><button id="print-upload-button" type="submit" class="button button-primary" :disabled="!canAdd">{{ uploading ? '正在添加…' : '添加打印文件' }}</button></div>
        <div id="print-upload-error" class="inline-error" role="alert" :hidden="!canUpload || !uploadError">{{ canUpload ? uploadError : '' }}</div>
      </form>
      <div id="prints-status" class="empty-state" role="status" :hidden="!allowed || !loading || hasData">正在获取打印文件…</div>
      <div id="prints-error" class="inline-error" role="alert" :hidden="!allowed || !listError">{{ allowed ? listError : '' }}</div>
      <div id="prints-empty" class="empty-state" :hidden="!showEmpty">{{ c.state?.run ? '本次比赛还没有打印文件。' : '尚未开赛，暂无打印文件。' }}</div>
      <div id="prints-table" class="table-scroll" :hidden="!allowed || !files.length" :aria-busy="String(loading)">
        <table v-if="allowed && files.length" class="prints-table">
          <thead><tr><th scope="col">Filename / 文件名</th><th scope="col">大小</th><th scope="col">添加时间</th><th scope="col" class="cell-right">操作</th></tr></thead>
          <tbody><tr v-for="file in files" :key="file.key">
            <td class="print-name">{{ file.name }}</td><td class="small">{{ bytes(file.size) }}</td><td class="small">{{ dateTime(file.created_at) }}</td>
            <td><div class="print-file-actions"><a v-if="file.view" class="button button-outline button-small" :href="file.view" target="_blank" rel="noopener noreferrer" :aria-label="`查看 / 打印 ${file.name}`">查看 / 打印</a><a v-if="file.download" class="button button-outline button-small" :href="file.download" target="_blank" rel="noopener noreferrer" download :aria-label="`下载打印文件 ${file.name}`">下载</a><span v-if="!file.view && !file.download" class="muted">链接不可用</span></div></td>
          </tr></tbody>
        </table>
      </div>
      <p id="prints-count" class="panel-note" aria-live="polite">{{ count }}</p>
    </section>
  </section>
</template>
