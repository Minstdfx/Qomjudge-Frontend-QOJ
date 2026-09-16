<script setup>
import { computed, ref } from 'vue';
import { useContest } from '../composables/useContest.js';
import { scoreboardResult, scoreboardSourceLabel, scoreboardSourceURL } from '../lib/scoreboard.js';
import ScoreboardTable from './ScoreboardTable.vue';

const c = useContest();
const refreshing = ref(false);
const result = computed(() => scoreboardResult(c));
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
        <h2>Scoreboard <span id="scoreboard-source-label" class="heading-note">{{ sourceLabel }}</span></h2>
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
      <div id="scoreboard-table" class="table-scroll" :hidden="!c.canRead || !!result.error">
        <ScoreboardTable v-if="c.canRead && !result.error" :rows="result.rows" />
      </div>
    </section>
    <div id="scoreboard-footer" class="scoreboard-footer" :hidden="!c.canRead || !!result.error">
      <div class="table-legend">
        <span class="muted small">题头：通过队伍数 / 提交次数</span>
        <span class="legend-item"><i class="legend-dot accepted-dot"></i>Accepted</span>
        <span id="first-blood-legend" class="legend-item" title="QOJ 标记的首次通过 / 一血" :hidden="!firstBloodLegend"><i class="legend-dot first-blood-dot"></i>First to solve</span>
        <span class="legend-item"><i class="legend-dot wrong-dot"></i>Incorrect</span>
        <span class="legend-item"><i class="legend-dot pending-dot"></i>Pending / Frozen</span>
      </div>
      <p id="scoreboard-explanation" class="muted small">{{ c.state?.demo ? '当前显示离线演示的模拟成绩。' : '名次、通过数、罚时及每题记录按 QOJ 原样显示，包括封榜中的 ? 标记。' }}</p>
    </div>
  </section>
</template>
