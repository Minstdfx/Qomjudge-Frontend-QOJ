<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import { checkStatementPDF, fetchStatement, getProblemDetails, safeStatementPDF, statementSandbox } from '../composables/problemDetails.js';
import ProblemLetter from './ProblemLetter.vue';

const c = useContest();
const active = computed(() => c.canRead ? c.byId(c.problemId) || c.problems[0] || null : null);
const status = ref('idle');
const message = ref('从左侧列表选择题目，查看完整题面。');
const html = ref('');
const pdfURL = ref('');
const pdfs = ref([]);
const pdfOnly = ref(false);
const choice = ref('');
const frameKey = ref(0);
let loadedId = null;
let controller = null;
let request = 0;
const originalURL = computed(() => active.value && c.can('manage') ? `https://qoj.ac/contest/${encodeURIComponent(c.state?.contest?.id)}/problem/${encodeURIComponent(active.value.id)}` : null);
const openPDF = computed(() => (pdfs.value.find((pdf) => pdf.id === choice.value) || pdfs.value[0])?.url.href);

function clearStatement() {
  request += 1;
  controller?.abort(); controller = null;
  loadedId = null;
  html.value = ''; pdfURL.value = ''; pdfs.value = []; choice.value = ''; pdfOnly.value = false;
  frameKey.value += 1;
  status.value = 'idle';
  message.value = c.canRead ? '从左侧列表选择题目，查看完整题面。' : '确认已开赛后显示题面。';
}

async function loadStatement({ refresh = false, pdfId } = {}) {
  const problem = active.value;
  if (!c.canRead || !problem || c.tab !== 'problems') return;
  const id = String(problem.id);
  if (!refresh && pdfId === undefined && loadedId === id && ['loaded', 'loading'].includes(status.value)) return;
  controller?.abort();
  const currentController = new AbortController(); controller = currentController;
  const currentRequest = ++request;
  const context = c.context;
  loadedId = id;
  html.value = ''; pdfURL.value = ''; pdfs.value = [];
  status.value = 'loading'; message.value = '正在加载题面…'; frameKey.value += 1;
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; currentController.abort(); }, 45000);
  const stale = () => currentRequest !== request || context !== c.context || !c.canRead || currentController.signal.aborted;
  try {
    const detail = await getProblemDetails(c, id, { refresh, signal: currentController.signal });
    if (stale()) return;
    const candidates = (Array.isArray(detail.statement_pdfs) ? detail.statement_pdfs : [])
      .filter((pdf) => pdf && typeof pdf.id === 'string')
      .map((pdf, index) => ({ ...pdf, url: safeStatementPDF(pdf.url, id), name: pdf.name || `PDF 题面 ${index + 1}` }))
      .filter((pdf) => pdf.url);
    pdfs.value = candidates;
    pdfOnly.value = detail.statement_format === 'pdf';
    const selected = (pdfId === undefined ? null : candidates.find((pdf) => pdf.id === pdfId)) || (pdfOnly.value ? candidates[0] : null);
    if (pdfOnly.value && !selected) throw new Error('未找到可访问的 PDF 题面，请重新加载。');
    choice.value = selected?.id || '';
    if (selected) {
      await checkStatementPDF(selected.url, currentController.signal);
      if (stale()) return;
      // Only the dedicated, server-verified PDF endpoint gets a native viewer.
      // HTML is always placed in a separate sandboxed srcdoc document.
      pdfURL.value = selected.url.href;
      status.value = 'loaded'; message.value = '若内嵌 PDF 无法显示，可点击「单独打开 PDF」查看。';
    } else {
      if (!detail.statement_url) throw new Error(detail.message || '暂时无法获取题面，请在 QOJ 中查看原题。');
      const result = await fetchStatement(detail.statement_url, id, currentController.signal, candidates);
      if (stale()) return;
      html.value = result;
      status.value = 'loaded'; message.value = detail.message || '';
    }
  } catch (error) {
    if (currentRequest !== request || context !== c.context || !c.canRead || error.name === 'AbortError' && !timedOut) return;
    status.value = 'error'; c.detailsCache.delete(id);
    message.value = `${timedOut ? '题面加载超时，请检查网络后重试。' : error.message} ${c.can('manage') ? '可点击「重新加载」重试，或查看原题。' : '可点击「重新加载」重试，或联系队长检查比赛权限。'}`;
  } finally {
    clearTimeout(timeout);
    if (controller === currentController) controller = null;
  }
}

watch(() => c.context, clearStatement, { flush: 'sync' });
watch(() => c.canRead, (allowed) => { if (!allowed) clearStatement(); }, { flush: 'sync' });
watch([() => c.context, () => c.tab, () => active.value?.id, () => c.canRead], () => {
  if (c.tab === 'problems' && c.canRead) loadStatement();
}, { immediate: true });
watch(() => c.problemRevision || 0, () => {
  clearStatement();
  if (c.tab === 'problems' && c.canRead) loadStatement({ refresh: true });
});
onBeforeUnmount(clearStatement);
</script>

<template>
  <section id="view-problems" class="view" aria-label="题目" :hidden="c.tab !== 'problems'">
    <div class="problem-layout">
      <aside class="card problem-sidebar">
        <div class="section-heading"><h2>Problem set</h2></div>
        <div id="problem-list">
          <a v-for="problem in (c.canRead ? c.problems : [])" :key="problem.id" :href="`#problems/${encodeURIComponent(problem.id)}`" :class="['problem-item', { selected: String(active?.id) === String(problem.id) }]" :aria-current="String(active?.id) === String(problem.id) ? 'true' : undefined" @click.prevent="c.navigate('problems', problem.id)"><ProblemLetter :problem="problem" /><span class="problem-item-name">{{ problem.title }}</span><span v-if="c.statFor(problem.id).solved" class="problem-check" aria-label="已通过">✓</span></a>
          <p v-if="!c.canRead || !c.problems.length" class="empty-state">暂无题目</p>
        </div>
      </aside>
      <section class="card statement-card">
        <div class="section-heading statement-heading">
          <div><ProblemLetter id="statement-label" :problem="active || { label: '?' }" /><h2 id="statement-title">{{ active?.title || '选择一道题目' }}</h2></div>
          <div class="statement-actions">
            <a id="statement-original" class="text-link" :href="originalURL || undefined" target="_blank" rel="noopener noreferrer" :hidden="!originalURL">原题 ↗</a>
            <button id="statement-retry" type="button" class="button button-outline button-small" :hidden="!c.canRead" :disabled="!active" @click="loadStatement({ refresh: true, pdfId: choice })">重新加载</button>
            <button id="statement-submit" type="button" class="button button-success button-small" :hidden="!c.can('submit')" :disabled="!c.running || !active" @click="c.openSubmission(active?.id)">提交代码</button>
          </div>
        </div>
        <div id="statement-pdf-tools" class="statement-pdf-tools" :hidden="!c.canRead || !pdfs.length">
          <label for="statement-pdf-select">题面格式</label>
          <select id="statement-pdf-select" :value="choice" aria-label="选择网页或 PDF 题面" @change="loadStatement({ pdfId: $event.target.value })"><option v-if="!pdfOnly" value="">网页题面</option><option v-for="pdf in pdfs" :key="pdf.id" :value="pdf.id">{{ pdf.name }}</option></select>
          <a id="statement-pdf-open" class="text-link" :href="openPDF" target="_blank" rel="noopener noreferrer">单独打开 PDF ↗</a>
        </div>
        <p id="statement-message" :class="['statement-message', status === 'error' ? 'statement-error' : 'muted']" :role="status === 'error' ? 'alert' : 'status'" :hidden="!message">{{ message }}</p>
        <iframe id="statement-frame" :key="`${frameKey}:${pdfURL ? 'pdf' : 'html'}`" title="题目描述" referrerpolicy="no-referrer" :sandbox="pdfURL ? undefined : statementSandbox" :srcdoc="html || undefined" :src="pdfURL || undefined" :hidden="!c.canRead || status !== 'loaded'"></iframe>
      </section>
    </div>
  </section>
</template>
