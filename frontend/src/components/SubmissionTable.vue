<script setup>
import { computed } from 'vue';
import { useContest } from '../composables/useContest.js';
import { duration, timeOfDay, safeQojURL } from '../lib/format.js';
import { verdictInfo, submissionMessage } from '../lib/verdicts.js';
import ProblemLetter from './ProblemLetter.vue';

const props = defineProps({ submissions: { type: Array, default: () => [] } });
const c = useContest();
const rows = computed(() => (c.canRead && c.can('manage') ? props.submissions : []).filter((s) => s && typeof s === 'object').map((s) => ({
  submission: s,
  problem: c.byId(s.problem_id) || { id: s.problem_id, label: s.label || '?', title: '' },
  verdict: verdictInfo(s.verdict, s),
  message: submissionMessage(s),
  sourceAvailable: /^[1-9]\d*$/.test(String(s.id)),
  url: safeQojURL(s.qoj_url, s.qoj_id),
})));
</script>

<template>
  <table class="submissions-table">
    <thead><tr><th>ID</th><th>Elapsed</th><th>Problem</th><th class="hide-mobile">Language</th><th>Result / 评测结果</th><th class="cell-right">操作</th></tr></thead>
    <tbody>
      <tr v-for="{ submission: s, problem, verdict, message, sourceAvailable, url } in rows" :key="s.id" :data-submission-id="s.id">
        <td class="submission-id">{{ s.id }}</td>
        <td class="submission-time" :title="timeOfDay(s.submitted_at)">{{ duration(s.elapsed_seconds) }}</td>
        <td><a :href="`#problems/${encodeURIComponent(problem.id)}`" :title="problem.title" @click.prevent="c.navigate('problems', problem.id)"><ProblemLetter :problem="problem" /></a></td>
        <td class="hide-mobile">{{ s.language }}</td>
        <td class="submission-result"><span :class="['verdict', `verdict-${verdict.cls}`, { 'verdict-final': verdict.cls !== 'pending' }]"><span :class="{ 'pending-pulse': verdict.cls === 'pending' }">{{ verdict.icon }}</span> {{ verdict.label }}</span><span v-if="message" class="submission-message">{{ message }}</span></td>
        <td class="cell-right"><div class="submission-actions"><button v-if="sourceAvailable" type="button" class="button button-outline button-small" :data-submission-source="s.id" :aria-label="`查看提交 ${s.id} 的代码`" @click="c.openSource(s.id)">查看代码</button><a v-if="url" class="text-link" :href="url" target="_blank" rel="noopener noreferrer">#{{ s.qoj_id || '查看' }} ↗</a><span v-else-if="!sourceAvailable" class="muted">—</span></div></td>
      </tr>
      <tr v-if="!rows.length" class="empty-row"><td colspan="6">No submissions</td></tr>
    </tbody>
  </table>
</template>
