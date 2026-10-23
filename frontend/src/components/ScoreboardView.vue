<script setup>
import { computed, ref } from 'vue';
import { useContest } from '../composables/useContest.js';
import { ccpcDisplay, scoreboardResult, scoreboardSourceLabel, scoreboardSourceURL } from '../lib/scoreboard.js';
import ScoreboardTable from './ScoreboardTable.vue';

const c = useContest();
const refreshing = ref(false);
const result = computed(() => scoreboardResult(c));
const ccpc = computed(() => result.value.error ? null : ccpcDisplay(c));
const sourceURL = computed(() => scoreboardSourceURL(c));
const sourceLabel = computed(() => scoreboardSourceLabel(c));
const errorTitle = computed(() => `无法读取 ${c.state?.demo ? '演示榜单' : 'QOJ 榜单'}`);
const firstBloodLegend = computed(() => c.canRead && !result.value.error && !c.state?.demo
  && ['qoj', 'qoj-external'].includes(c.part?.scoreboard_source));

async function refresh() {
  if (!c.canRead || refreshing.value) return;
  refreshing.value = true;
  try { await c.refreshState(true); }
  finally { refreshing.value = false; }
}
</script>

<template>
  <section id="view-scoreboard" class="view" aria-label="比赛榜单" :hidden="c.tab !== 'scoreboard'">
    <section class="card table-card">
      <div class="section-heading scoreboard-heading">
        <h2>Scoreboard <span id="scoreboard-source-label" class="heading-note">{{ sourceLabel }}</span><span v-if="ccpc" class="ccpc-mode-label">CCPC 动态榜单</span></h2>
        <div class="scoreboard-actions">
          <a id="scoreboard-original" class="text-link" target="_blank" rel="noopener noreferrer"
             :href="sourceURL" :hidden="!sourceURL">打开 QOJ 原榜 ↗</a>
          <button id="scoreboard-refresh" type="button" class="button button-outline button-small"
                  :hidden="!c.canRead" :disabled="refreshing" @click="refresh">{{ refreshing ? '正在刷新…' : '刷新榜单' }}</button>
        </div>
      </div>
      <p id="scoreboard-source-message" class="banner scoreboard-message"
         :class="result.error ? 'banner-error' : 'banner-info'" :role="result.error ? 'alert' : 'status'"
         :hidden="!c.canRead || (!result.error && !result.message)">
        <template v-if="c.canRead && result.error"><strong>{{ errorTitle }}</strong><span>{{ result.error }}</span></template>
        <template v-else-if="c.canRead">{{ result.message }}</template>
      </p>
      <section v-if="ccpc" id="ccpc-revealed-summary" class="ccpc-summary" aria-label="已揭晓题目与全场统计">
        <div class="ccpc-summary-heading">
          <h3>已揭晓题目 <span>{{ ccpc.revealed_problems.length }} / {{ ccpc.revealed_problems.length + ccpc.hidden_count }}</span></h3>
          <span class="muted small">通过队伍数 / 提交次数</span>
        </div>
        <div v-if="ccpc.revealed_problems.length" class="ccpc-revealed-problems">
          <a v-for="problem in ccpc.revealed_problems" :key="problem.id" class="ccpc-revealed-problem"
             :href="`#problems/${encodeURIComponent(problem.id)}`" :title="problem.title"
             :data-revealed-problem="problem.id" @click.prevent="c.navigate('problems', problem.id)">
            <strong>{{ problem.label }}</strong>
            <span class="ccpc-problem-counts">{{ problem.solved ?? '—' }} / {{ problem.submissions ?? '—' }}</span>
            <small>阈值 {{ problem.threshold }}</small>
          </a>
        </div>
        <p v-else class="ccpc-summary-note">尚无题目达到揭晓条件。</p>
        <p class="ccpc-summary-note">{{ ccpc.all_revealed ? '全部题号已揭晓。' : `还有 ${ccpc.hidden_count} 道题未揭晓，题号以 ? 显示。` }}成绩块按各队记录独立排列，颜色只表示评测状态。</p>
      </section>
      <div id="scoreboard-table" class="table-scroll" :hidden="!c.canRead || !!result.error">
        <ScoreboardTable v-if="c.canRead && !result.error" :rows="result.rows" />
      </div>
    </section>
    <div id="scoreboard-footer" class="scoreboard-footer" :hidden="!c.canRead || !!result.error">
      <div class="table-legend">
        <span class="muted small">{{ ccpc ? '已揭晓题汇总：通过队伍数 / 提交次数' : '题头：通过队伍数 / 提交次数' }}</span>
        <span class="legend-item"><i class="legend-dot accepted-dot"></i>Accepted</span>
        <span id="first-blood-legend" class="legend-item" title="QOJ 标记的首次通过 / 一血" :hidden="!firstBloodLegend"><i class="legend-dot first-blood-dot"></i>First to solve</span>
        <span class="legend-item"><i class="legend-dot wrong-dot"></i>Incorrect</span>
        <span class="legend-item"><i class="legend-dot pending-dot"></i>Pending / Frozen</span>
      </div>
      <p id="scoreboard-explanation" class="muted small">{{ ccpc ? c.state?.demo ? '当前显示离线演示的模拟成绩，题号按模拟通过队伍数逐步揭晓。' : '名次、通过数、罚时与封榜结果以 QOJ 为准。阈值计入当前 QOJ 榜单中的历史队伍；直接访问 QOJ 原榜仍可能看到题号。' : c.state?.demo ? '当前显示离线演示的模拟成绩。' : '名次、通过数、罚时及每题记录按 QOJ 原样显示，包括封榜中的 ? 标记。' }}</p>
    </div>
  </section>
</template>

<style scoped>
.ccpc-mode-label { display: inline-block; margin-left: 10px; padding: 3px 7px; border: 1px solid #bac7d2; border-radius: 3px; background: #edf3f7; color: #36516a; font-size: 11px; font-weight: 600; vertical-align: middle; }
.ccpc-summary { padding: 13px 15px; border-top: 1px solid #d4dae0; border-bottom: 1px solid #d4dae0; background: #fafcfd; }
.ccpc-summary-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 15px; margin-bottom: 10px; }
.ccpc-summary-heading h3 { margin: 0; font-size: 13px; }
.ccpc-summary-heading h3 span { margin-left: 6px; font-variant-numeric: tabular-nums; color: #53687b; }
.ccpc-revealed-problems { display: flex; flex-wrap: wrap; gap: 7px; }
.ccpc-revealed-problem { display: grid; grid-template-columns: auto auto; gap: 2px 12px; align-items: center; padding: 6px 10px; border: 1px solid #becbd5; border-radius: 3px; background: #fff; color: #344f65; text-decoration: none; }
.ccpc-revealed-problem strong { grid-row: span 2; font-size: 17px; }
.ccpc-problem-counts { font-variant-numeric: tabular-nums; font-size: 12px; }
.ccpc-revealed-problem small { color: #6b747c; font-size: 10px; }
.ccpc-summary-note { margin: 9px 0 0; font-size: 12px; line-height: 1.6; color: #61717e; }
#scoreboard-explanation { max-width: 620px; line-height: 1.5; }
</style>
