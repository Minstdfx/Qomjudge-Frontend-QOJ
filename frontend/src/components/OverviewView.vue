<script setup>
import { computed, ref } from 'vue';
import { useContest } from '../composables/useContest.js';
import { duration } from '../lib/format.js';
import { scoreboardResult } from '../lib/scoreboard.js';
import ProblemLetter from './ProblemLetter.vue';
import ScoreboardTable from './ScoreboardTable.vue';
import SubmissionTable from './SubmissionTable.vue';

const c = useContest();
const refreshingBoard = ref(false);
const refreshingContest = ref(false);
const result = computed(() => scoreboardResult(c));
const errorTitle = computed(() => `无法读取 ${c.state?.demo ? '演示榜单' : 'QOJ 榜单'}`);
const teamRow = computed(() => {
  if (!c.canRead || result.value.error) return null;
  return result.value.rows.find((row) => row.is_self === true)
    || (c.state?.demo && c.state.run ? { ...c.state.run, rank: '—',
      team_name: c.state.run.team_name || c.state.team?.name || 'My Team', is_self: true } : null);
});
const submissions = computed(() => [...(c.canRead ? c.state?.submissions || [] : [])]
  .sort((a, b) => Number(b.submitted_at) - Number(a.submitted_at) || Number(b.id) - Number(a.id)).slice(0, 10));
const solved = computed(() => !c.canRead ? '—' : `${result.value.error ? '—' : c.state?.run?.solved ?? '—'} / ${c.problems.length}`);
const penalty = computed(() => c.canRead && !result.value.error ? c.state?.run?.penalty ?? '—' : '—');
const problemRows = computed(() => c.problems.map((problem) => {
  const stat = c.statFor(problem.id);
  return { problem, stat, status: stat.solved ? 'accepted' : stat.pending ? 'pending'
    : Number(stat.wrong) > 0 ? 'wrong' : Number(stat.attempts) > 0 ? 'neutral' : 'empty' };
}));
const problemHref = (problem) => `#problems/${encodeURIComponent(problem.id)}`;

async function refreshBoard() {
  if (!c.canRead || refreshingBoard.value) return;
  refreshingBoard.value = true;
  try { await c.refreshState(true); }
  finally { refreshingBoard.value = false; }
}

async function refreshContest() {
  if (!c.can('manage') || refreshingContest.value) return;
  refreshingContest.value = true;
  try {
    await c.refreshContest();
    c.notify('比赛信息已刷新。', 'success');
  } catch (error) { c.notify(error.message, 'error'); }
  finally { refreshingContest.value = false; }
}

function finishDemo() {
  if (!c.state?.demo || !c.can('manage') || !c.running) return;
  c.confirm('结束本次离线演示？', '结束后将停止接受本次演示的新提交。', async () => {
    if (!c.state?.demo || !c.can('manage') || !c.running) return;
    await c.api('/api/finish', {});
    await c.refreshState(true);
    c.notify('本次离线演示已结束。', 'success');
  });
}
</script>

<template>
  <section id="view-overview" class="view" aria-label="比赛概览" :hidden="c.tab !== 'overview'">
    <section class="team-summary" aria-label="本队成绩">
      <div class="view-heading scoreboard-heading">
        <h2>Team overview <span class="heading-note">本队成绩</span></h2>
        <div class="scoreboard-actions">
          <button id="team-scoreboard-refresh" type="button" class="button button-outline button-small"
                  :hidden="!c.canRead" :disabled="refreshingBoard" @click="refreshBoard">{{ refreshingBoard ? '正在刷新…' : '刷新榜单' }}</button>
          <a href="#scoreboard" class="text-link" @click.prevent="c.navigate('scoreboard')">完整榜单 →</a>
        </div>
      </div>
      <div id="team-scoreboard" class="table-scroll">
        <template v-if="c.canRead">
          <div v-if="result.error" class="scoreboard-unavailable" role="status"><strong>{{ errorTitle }}</strong><p>{{ result.error }}</p></div>
          <ScoreboardTable v-else-if="teamRow" :rows="[teamRow]" />
          <p v-else class="empty-state">QOJ 榜单暂无本队记录。</p>
        </template>
      </div>
    </section>
    <div class="team-columns">
      <section class="card table-card submissions-panel">
        <div class="section-heading">
          <h2>Submissions <span class="heading-note">最近提交</span></h2>
          <a href="#submissions" class="text-link" @click.prevent="c.navigate('submissions')">查看全部 →</a>
        </div>
        <div id="overview-submissions" class="table-scroll"><SubmissionTable :submissions="submissions" /></div>
        <p class="panel-note">显示本次比赛在此界面提交的最近 10 条记录，评测结果自动更新。</p>
      </section>
      <aside class="team-sidebar" aria-label="比赛信息与题目">
        <section class="card info-panel">
          <div class="section-heading"><h2>Contest information <span class="heading-note">比赛信息</span></h2></div>
          <dl class="contest-details">
            <div><dt>Team / 队伍</dt><dd id="team-name-display">{{ (c.canRead ? c.state?.run?.team_name : '') || c.state?.team?.name || '尚未开始' }}</dd></div>
            <div><dt>Duration / 时长</dt><dd id="duration-display">{{ c.canRead ? duration(c.state?.contest?.duration_seconds) : '—' }}</dd></div>
            <div><dt>Problems / 题数</dt><dd id="problem-count">{{ c.problems.length }}</dd></div>
            <div><dt>Attachments / 附件</dt><dd><a href="#attachments" class="text-link" @click.prevent="c.navigate('attachments')">查看比赛附件 →</a></dd></div>
          </dl>
          <div id="run-metrics" class="metrics" :hidden="!c.canRead || !c.state?.run">
            <div><span>Solved</span><strong id="metric-solved">{{ solved }}</strong></div>
            <div><span>Penalty</span><strong id="metric-penalty">{{ penalty }}</strong></div>
            <div><span>Submissions</span><strong id="metric-submissions">{{ c.canRead ? (c.state?.submissions || []).length : '—' }}</strong></div>
          </div>
          <div class="info-actions">
            <button id="refresh-button" type="button" class="button button-outline button-small" :hidden="!c.can('manage')"
                    :disabled="refreshingContest" @click="refreshContest">{{ refreshingContest ? '正在刷新…' : '刷新比赛信息' }}</button>
            <button id="finish-button" class="button button-danger-outline button-small" type="button"
                    :hidden="!c.state?.demo || !c.running || !c.can('manage')" @click="finishDemo">结束离线演示</button>
          </div>
        </section>
        <section class="card table-card compact-problems">
          <div class="section-heading">
            <h2>Problemset <span class="heading-note">比赛题目</span></h2>
            <a href="#problems" class="text-link" @click.prevent="c.navigate('problems')">查看题面 →</a>
          </div>
          <div id="overview-problems" class="table-scroll">
            <table>
              <thead><tr><th class="problem-column">ID</th><th>Problem</th><th>Status</th></tr></thead>
              <tbody>
                <tr v-for="{ problem, stat, status } in problemRows" :key="problem.id">
                  <td><a :href="problemHref(problem)" :aria-label="`查看题目 ${problem.label}`" @click.prevent="c.navigate('problems', problem.id)"><ProblemLetter :problem="problem" /></a></td>
                  <td><a class="problem-name" :href="problemHref(problem)" @click.prevent="c.navigate('problems', problem.id)">{{ problem.title }}</a></td>
                  <td>
                    <span v-if="status === 'accepted'" class="verdict verdict-accepted">✓ Accepted</span>
                    <span v-else-if="status === 'pending'" class="verdict verdict-pending"><span class="pending-pulse">◷</span> Pending</span>
                    <span v-else-if="status === 'wrong'" class="verdict verdict-wrong">× Incorrect</span>
                    <span v-else-if="status === 'neutral'" class="verdict verdict-neutral">{{ stat.attempts }} {{ Number(stat.attempts) === 1 ? 'attempt' : 'attempts' }}</span>
                    <span v-else class="problem-stats-empty">—</span>
                  </td>
                </tr>
                <tr v-if="!problemRows.length" class="empty-row"><td colspan="3">暂无可用题目，请检查比赛 ID 与 QOJ 连接状态。</td></tr>
              </tbody>
            </table>
          </div>
        </section>
        <section class="rules-note">
          <h3>QOJ contest</h3>
          <p id="contest-rules-note">{{ c.state?.demo ? '离线演示使用模拟题目、评测和本地成绩。' : '参赛时间、成绩、罚时和封榜结果以 QOJ 为准。' }}</p>
        </section>
      </aside>
    </div>
  </section>
</template>
