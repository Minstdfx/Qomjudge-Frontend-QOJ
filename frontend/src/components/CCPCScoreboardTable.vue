<script setup>
import { computed } from 'vue';
import { useContest } from '../composables/useContest.js';

const props = defineProps({ rows: { type: Array, default: () => [] } });
const c = useContest();
const tableRows = computed(() => !c.canRead ? [] : props.rows.map((row) => {
  const cells = (row.ccpc_cells || []).map((cell) => {
    const firstBlood = cell.status === 'accepted' && cell.first_blood === true
      && ['qoj', 'qoj-external'].includes(c.part?.scoreboard_source);
    const label = cell.revealed ? cell.label : '?';
    const detail = `${cell.revealed ? `题目 ${label}` : '题号尚未揭晓'}：${cell.display}${firstBlood ? '\nFirst to solve / 一血' : ''}`;
    return { ...cell, label, firstBlood, detail };
  });
  return { row, accepted: cells.filter((cell) => cell.status === 'accepted'),
    others: cells.filter((cell) => cell.status !== 'accepted') };
}));
const problemHref = (cell) => `#problems/${encodeURIComponent(cell.problem_id)}`;
</script>

<template>
  <table v-if="c.canRead" class="scoreboard-table ccpc-scoreboard-table" aria-label="CCPC 动态榜单">
    <thead>
      <tr><th>Rank</th><th>Team</th><th>Solved</th><th>Penalty</th><th class="ccpc-results-heading">解题记录 <span>各队独立排列</span></th></tr>
    </thead>
    <tbody>
      <tr v-for="({ row, accepted, others }, index) in tableRows" :key="index" :class="{ 'self-row': row.is_self === true }">
        <td>{{ row.rank ?? '—' }}</td>
        <td class="team-cell">{{ row.team_name ?? row.username ?? '—' }}<span v-if="row.is_self === true" class="team-self">YOU</span></td>
        <td class="score-cell">{{ row.solved ?? '—' }}</td>
        <td class="penalty-cell">{{ row.penalty ?? '—' }}</td>
        <td class="ccpc-results">
          <div class="ccpc-result-groups">
            <div v-for="(cells, group) in [accepted, others]" :key="group" class="ccpc-result-group"
                 :class="{ 'ccpc-other-results': group === 1 }" :hidden="!cells.length"
                 :aria-label="group === 0 ? '已通过，按通过时间排列' : '尚未通过'">
              <div v-for="cell in cells" :key="cell.key" class="ccpc-result"
                   :class="[`cell-${cell.status}`, { 'cell-first-blood': cell.firstBlood, 'ccpc-unrevealed': !cell.revealed }]"
                   :title="cell.detail" :aria-label="cell.detail" :data-revealed="String(cell.revealed)">
                <a v-if="cell.revealed" class="ccpc-problem-label" :href="problemHref(cell)"
                   @click.prevent="c.navigate('problems', cell.problem_id)">{{ cell.label }}</a>
                <strong v-else class="ccpc-problem-label" aria-label="题号尚未揭晓">?</strong>
                <span class="upstream-score">{{ cell.display }}</span>
              </div>
            </div>
            <span v-if="!accepted.length && !others.length" class="problem-stats-empty">尚未提交</span>
          </div>
        </td>
      </tr>
      <tr v-if="!tableRows.length" class="empty-row"><td colspan="5">暂无参赛队伍</td></tr>
    </tbody>
  </table>
</template>

<style scoped>
.ccpc-scoreboard-table { min-width: 700px; }
.ccpc-results-heading { text-align: left !important; text-transform: none !important; }
.ccpc-results-heading span { margin-left: 8px; color: #6b747c; font-size: 11px; font-weight: 400; }
.ccpc-results { min-width: 260px; max-width: 900px; text-align: left !important; }
.ccpc-result-groups, .ccpc-result-group { display: flex; flex-wrap: wrap; align-items: stretch; gap: 6px; }
.ccpc-result-groups { align-items: center; }
.ccpc-other-results { border-left: 1px solid #aab4bd; margin-left: 3px; padding-left: 9px; }
.ccpc-result-group:first-child[hidden] + .ccpc-other-results { border-left: 0; margin-left: 0; padding-left: 0; }
.ccpc-result { min-width: 63px; padding: 5px 8px; border: 1px solid #00000024; border-radius: 3px; text-align: center; }
.ccpc-problem-label { display: block; font-size: 15px; font-weight: 700; color: inherit; line-height: 1.3; }
a.ccpc-problem-label { text-decoration: underline; text-decoration-color: #00000050; text-underline-offset: 2px; }
.ccpc-result .upstream-score { font-size: 12px; font-variant-numeric: tabular-nums; }
.ccpc-result.cell-accepted { background: #88d970; color: #173c12; }
.ccpc-result.cell-first-blood { background: #278039; color: #fff; }
.ccpc-result.cell-pending { background: #9ecbf2; color: #153f66; }
.ccpc-result.cell-wrong { background: #f4aaa5; color: #6a201e; }
.ccpc-result.cell-neutral { background: #e8e9eb; color: #59616a; }
</style>
