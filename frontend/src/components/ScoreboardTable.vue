<script setup>
import { computed } from 'vue';
import { useContest } from '../composables/useContest.js';
import { scoreboardCell } from '../lib/scoreboard.js';
import ProblemLetter from './ProblemLetter.vue';

const props = defineProps({ rows: { type: Array, default: () => [] } });
const c = useContest();
const tableRows = computed(() => !c.canRead ? [] : props.rows.map((row) => ({
  row,
  cells: c.problems.map((problem) => ({
    id: problem.id,
    ...scoreboardCell(c, c.statFor(problem.id, row)),
  })),
})));
const problemHref = (problem) => `#problems/${encodeURIComponent(problem.id)}`;
</script>

<template>
  <table v-if="c.canRead" class="scoreboard-table">
    <thead>
      <tr>
        <th>Rank</th><th>Team</th><th>Solved</th><th>Penalty</th>
        <th v-for="problem in c.problems" :key="problem.id" class="scoreboard-problem">
          <a :href="problemHref(problem)" :title="problem.title" @click.prevent="c.navigate('problems', problem.id)">
            <ProblemLetter :problem="problem" />
          </a>
        </th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="({ row, cells }, index) in tableRows" :key="index" :class="{ 'self-row': row.is_self === true }">
        <td>{{ row.rank ?? '—' }}</td>
        <td class="team-cell">{{ row.team_name ?? row.username ?? '—' }}<span v-if="row.is_self === true" class="team-self">YOU</span></td>
        <td class="score-cell">{{ row.solved ?? '—' }}</td>
        <td class="penalty-cell">{{ row.penalty ?? '—' }}</td>
        <td v-for="cell in cells" :key="cell.id" class="scoreboard-problem"
            :class="[`cell-${cell.status}`, { 'cell-first-blood': cell.firstBlood }]"
            :title="cell.detail" :aria-label="cell.ariaLabel">
          <span v-if="!cell.demo" class="upstream-score">{{ cell.text }}</span>
          <template v-else-if="cell.hasResult">
            <strong>{{ cell.value }}</strong><small>{{ cell.attempts }} {{ cell.attempts === 1 ? 'try' : 'tries' }}</small>
          </template>
          <template v-else>&nbsp;</template>
        </td>
      </tr>
      <tr v-if="!tableRows.length" class="empty-row"><td :colspan="4 + c.problems.length">暂无参赛队伍</td></tr>
    </tbody>
  </table>
</template>
