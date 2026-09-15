<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { getProblemDetails } from '../composables/problemDetails.js';
import BaseDialog from './BaseDialog.vue';

const c = useContest();
const problemId = ref('');
const source = ref('');
const filename = ref('');
const language = ref('');
const languages = ref([]);
const loading = ref(false);
const sending = ref(false);
const available = ref(false);
const error = ref('');
const fileInput = ref(null);
let draftScope = c.context;
let detailController = null;
let submitController = null;
let detailRequest = 0;
let fileRequest = 0;
let submitRequest = 0;
const allowed = computed(() => c.canRead && c.can('submit'));
const canSend = computed(() => allowed.value && c.running && available.value && !loading.value && !sending.value);
const sizeLabel = computed(() => `${source.value.length.toLocaleString('zh-CN')} 字符`);

function draftKey(id) { return `qoj-vp:${draftScope}:draft:${id}`; }
function saveDraft() {
  if (!problemId.value) return;
  try { sessionStorage.setItem(draftKey(problemId.value), JSON.stringify({ source: source.value, language: language.value, filename: filename.value })); } catch { /* A draft must never prevent submission. */ }
}
function readDraft(id) {
  try {
    const value = JSON.parse(sessionStorage.getItem(draftKey(id)) || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch { return {}; }
}
function cancelRequests() {
  detailRequest += 1; fileRequest += 1; submitRequest += 1;
  detailController?.abort(); detailController = null;
  submitController?.abort(); submitController = null;
  loading.value = false; sending.value = false;
}
function clearSensitive() {
  saveDraft(); cancelRequests();
  draftScope = c.context;
  problemId.value = ''; source.value = ''; filename.value = ''; language.value = ''; languages.value = [];
  available.value = false; error.value = '';
  if (fileInput.value) fileInput.value.value = '';
  c.submitOpen = false; c.submitTarget = null;
}

async function prepare(id) {
  if (!allowed.value || !c.submitOpen || !c.byId(id)) return false;
  saveDraft();
  detailController?.abort();
  const currentController = new AbortController(); detailController = currentController;
  const currentRequest = ++detailRequest;
  const context = c.context;
  problemId.value = String(id);
  const draft = readDraft(id);
  source.value = typeof draft.source === 'string' ? draft.source : '';
  filename.value = typeof draft.filename === 'string' ? draft.filename : '';
  language.value = ''; languages.value = [];
  if (fileInput.value) fileInput.value.value = '';
  available.value = false; loading.value = true; error.value = '';
  const stale = () => currentRequest !== detailRequest || currentController.signal.aborted || context !== c.context || !c.submitOpen || !allowed.value;
  try {
    const detail = await getProblemDetails(c, id, { signal: currentController.signal });
    if (stale()) return false;
    languages.value = (Array.isArray(detail.languages) ? detail.languages : [])
      .filter((item) => item && ['string', 'number'].includes(typeof item.value) && typeof item.label === 'string')
      .map((item) => ({ value: String(item.value), label: item.label }));
    language.value = languages.value.find((item) => item.value === String(draft.language))?.value || languages.value[0]?.value || '';
    available.value = !!detail.submission_available && !!languages.value.length;
    if (!available.value) error.value = detail.message || '当前题目暂时无法提交，请检查 QOJ 登录状态或比赛权限。';
    return true;
  } catch (cause) {
    if (!stale() && cause.name !== 'AbortError') error.value = cause.message;
    return false;
  } finally {
    if (currentRequest === detailRequest) { loading.value = false; detailController = null; }
  }
}

function changeProblem(event) {
  fileRequest += 1;
  prepare(event.target.value);
}
function guessLanguage(name) {
  const extension = name.split('.').pop().toLowerCase();
  const patterns = {
    cpp: /c\+\+|cpp/i, cc: /c\+\+|cpp/i, cxx: /c\+\+|cpp/i,
    c: /^(gnu\s*)?c(?:\d|\s|$)|clang\s*c\b/i, py: /python\s*3|pypy\s*3|python|pypy/i,
    java: /java/i, kt: /kotlin/i, rs: /rust/i, go: /go(?:lang)?\b/i, pas: /pascal/i,
    js: /javascript|node/i, cs: /c#|csharp/i, hs: /haskell/i, rb: /ruby/i,
  };
  const pattern = patterns[extension];
  const option = pattern && languages.value.find((item) => pattern.test(item.label));
  if (option) language.value = option.value;
}
async function readFile(event) {
  const file = event.target.files?.[0];
  if (!file || !allowed.value || sending.value) return;
  const currentRequest = ++fileRequest;
  const context = c.context;
  const stale = () => currentRequest !== fileRequest || context !== c.context || !allowed.value || !c.submitOpen;
  try {
    if (file.size > 256 * 1024) throw new Error('代码文件不能超过 256 KiB。');
    const text = await file.text();
    if (stale()) return;
    if (text.includes('\0')) throw new Error('请选择源代码文本文件，不要上传编译后的程序。');
    const stem = file.name.replace(/\.[^.]+$/, '').toLowerCase();
    const guessed = c.problems.find((problem) => String(problem.label).toLowerCase() === stem || String(problem.id) === stem);
    if (guessed && String(guessed.id) !== problemId.value) await prepare(guessed.id);
    if (stale()) return;
    source.value = text; filename.value = file.name; guessLanguage(file.name); saveDraft();
    if (available.value) error.value = '';
  } catch (cause) {
    if (!stale()) error.value = cause.message;
  } finally {
    if (fileInput.value && currentRequest === fileRequest) fileInput.value.value = '';
  }
}

async function send() {
  if (!canSend.value) return;
  if (!source.value.trim()) { error.value = '请先选择代码文件或粘贴源代码。'; return; }
  if (new TextEncoder().encode(source.value).byteLength > 256 * 1024) { error.value = '源代码不能超过 256 KiB。'; return; }
  if (source.value.includes('\0')) { error.value = '请选择源代码文本文件，不要上传编译后的程序。'; return; }
  saveDraft(); error.value = ''; sending.value = true;
  const currentRequest = ++submitRequest;
  const context = c.context;
  const currentController = new AbortController(); submitController = currentController;
  const demo = c.state?.demo;
  try {
    await c.api('/api/submit', { problem_id: Number(problemId.value), language: language.value, source: source.value, filename: filename.value || 'solution.txt' }, { signal: currentController.signal });
    if (currentRequest !== submitRequest || context !== c.context || !allowed.value) return;
    sending.value = false;
    c.submitOpen = false;
    c.navigate('submissions');
    c.notify(demo ? '模拟提交已创建。' : '代码已提交，评测结果将自动更新。', 'success');
    await c.refreshState(true);
  } catch (cause) {
    if (currentRequest === submitRequest && context === c.context && cause.name !== 'AbortError') error.value = cause.message;
  } finally {
    if (currentRequest === submitRequest) { sending.value = false; submitController = null; }
  }
}

function close() { if (!sending.value) c.submitOpen = false; }
watch(() => c.context, clearSensitive, { flush: 'sync' });
watch(allowed, (value) => { if (!value) clearSensitive(); }, { flush: 'sync' });
watch([() => c.submitOpen, () => c.submitTarget], () => {
  if (!c.submitOpen) { saveDraft(); cancelRequests(); return; }
  if (!allowed.value) { clearSensitive(); return; }
  const target = c.byId(c.submitTarget) || c.byId(c.problemId) || c.problems[0];
  if (target) prepare(target.id);
});
watch([source, language, filename], saveDraft);
window.addEventListener('beforeunload', saveDraft);
onBeforeUnmount(() => { saveDraft(); cancelRequests(); window.removeEventListener('beforeunload', saveDraft); });
</script>

<template>
  <BaseDialog id="submit-dialog" v-model:open="c.submitOpen" class="modal submit-modal" aria-labelledby="submit-dialog-title" :prevent-close="sending">
    <div class="modal-heading"><div><div class="eyebrow">SUBMIT SOLUTION</div><h2 id="submit-dialog-title">提交代码</h2></div><button type="button" class="icon-button" data-close="submit-dialog" aria-label="关闭提交对话框" :disabled="sending" @click="close">×</button></div>
    <form id="submit-form" @submit.prevent="send">
      <div class="modal-body">
        <div id="submit-demo-note" class="banner banner-demo" :hidden="!c.state?.demo">演示模式：仅生成模拟评测结果。</div>
        <div class="form-row">
          <div class="field"><label for="submit-problem">Problem <span>题目</span></label><select id="submit-problem" :value="problemId" name="problem_id" :disabled="sending" required @change="changeProblem"><option v-for="problem in (c.canRead ? c.problems : [])" :key="problem.id" :value="String(problem.id)">{{ problem.label }} — {{ problem.title }}</option></select></div>
          <div class="field"><label for="submit-language">Language <span>语言</span></label><select id="submit-language" v-model="language" name="language" :disabled="loading || !languages.length || sending" required><option v-if="!languages.length" value="">{{ loading ? '正在获取语言…' : '暂无可用语言' }}</option><option v-for="item in languages" :key="item.value" :value="item.value">{{ item.label }}</option></select></div>
        </div>
        <label class="file-picker" for="source-file"><span class="file-icon" aria-hidden="true">⌘</span><span><strong id="file-picker-label">{{ filename || '选择代码文件' }}</strong><small>支持 .cpp、.c、.py、.java 等文本文件，或在下方粘贴代码</small></span><span class="button button-outline button-small">浏览文件</span></label>
        <input id="source-file" ref="fileInput" class="sr-only" type="file" accept=".cpp,.cc,.cxx,.c,.py,.java,.kt,.rs,.go,.pas,.txt,.js,.cs,.hs,.rb" :disabled="sending" @change="readFile">
        <div class="code-label"><label for="source-code">Source code <span>源代码</span></label><span id="source-size" class="muted small">{{ sizeLabel }}</span></div>
        <textarea id="source-code" v-model="source" class="code-input" :data-filename="filename" spellcheck="false" autocapitalize="off" autocomplete="off" rows="13" placeholder="// Paste your solution here…" :disabled="sending" required></textarea>
        <div class="field-help">代码草稿仅保存在当前浏览器标签页。提交后可在 Submissions 中查看评测结果。</div>
        <div id="submit-error" class="inline-error" role="alert" :hidden="!error">{{ error }}</div>
      </div>
      <div class="modal-footer"><button type="button" class="button button-outline" data-close="submit-dialog" :disabled="sending" @click="close">取消</button><button id="send-button" type="submit" class="button button-success" :disabled="!canSend">{{ sending ? '正在提交…' : c.state?.demo ? '↑ 提交模拟评测' : '↑ 提交至 QOJ' }}</button></div>
    </form>
  </BaseDialog>
</template>
