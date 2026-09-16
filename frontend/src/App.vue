<script setup>
import { computed, onMounted, onUnmounted, provide, ref, watchEffect } from 'vue';
import { contestKey, createContestStore } from './composables/useContest.js';
import { duration, timeOfDay } from './lib/format.js';
import { verdictInfo } from './lib/verdicts.js';
import ParticipationView from './components/ParticipationView.vue';
import OverviewView from './components/OverviewView.vue';
import ProblemWorkspace from './components/ProblemWorkspace.vue';
import AttachmentsView from './components/AttachmentsView.vue';
import SubmissionTable from './components/SubmissionTable.vue';
import ScoreboardView from './components/ScoreboardView.vue';
import PrintingView from './components/PrintingView.vue';
import TeamView from './components/TeamView.vue';
import AccountDialog from './components/AccountDialog.vue';
import SubmitDialog from './components/SubmitDialog.vue';
import SubmissionSourceDialog from './components/SubmissionSourceDialog.vue';
import ConfirmDialog from './components/ConfirmDialog.vue';

const c = createContestStore();
provide(contestKey, c);
onMounted(c.start);
onUnmounted(c.dispose);
const tabs = [['participation', 'Contest', '参赛'], ['overview', 'Overview', '概览'], ['problems', 'Problemset', '题目'], ['attachments', 'Attachments', '附件'], ['submissions', 'Submissions', '提交'], ['scoreboard', 'Scoreboard', '榜单'], ['printing', 'Printing', '打印'], ['team', 'Team', '队伍']];
const title = computed(() => !c.state ? c.connectionError ? '暂时无法连接比赛' : '正在连接比赛…' : c.canRead ? c.state.contest?.title || `Contest #${c.state.contest?.id || ''}` : `Contest #${c.state.contest?.id || ''}`);
const submissions = computed(() => c.canRead && c.can('manage') ? [...(c.state?.submissions || [])].sort((a, b) => b.submitted_at - a.submitted_at || b.id - a.id) : []);
const pending = computed(() => submissions.value.filter(s => verdictInfo(s.verdict, s).cls === 'pending').length);
const refreshingSubmissions = ref(false);

async function refreshSubmissions() {
  if (!c.allowedTab('submissions') || c.teamChanging || refreshingSubmissions.value) return;
  const context = c.context;
  refreshingSubmissions.value = true;
  try {
    const result = await c.refreshState(true);
    if (context !== c.context) return;
    if (result) c.notify('提交记录已刷新。', 'success');
    else c.notify(c.scoreboardError || c.connectionError || '刷新提交记录失败，请稍后重试。', 'error');
  } finally { refreshingSubmissions.value = false; }
}

const remaining = computed(() => {
  const start = Number(c.part.started_at) || 0, end = Number(c.part.ends_at) || 0;
  const now = c.tick + c.clockOffset;
  return c.part.status === 'scheduled' && start ? Math.max(0, start - now) : end ? Math.max(0, end - now) : null;
});
const progress = computed(() => {
  const total = Number(c.part.ends_at) - Number(c.part.started_at);
  return c.canRead && total > 0 && remaining.value !== null ? Math.max(0, Math.min(100, remaining.value / total * 100)) : 0;
});
const status = computed(() => ({ unregistered: '尚未参赛', scheduled: '等待开赛', running: '比赛进行中', finished: '比赛已结束', unavailable: '状态待确认' })[c.part.status] || '状态待确认');
const teamNote = computed(() => {
  const team = c.state?.team;
  return c.revoked ? '这台电脑的队伍访问权限已被撤销。请离开队伍后，使用队长提供的邀请码重新加入。' : team?.role === 'member' ? `${team.name || '当前队伍'} · 队员模式：${c.canRead ? '可查看题面、榜单和打印文件。' : '等待队长完成参赛，开赛后显示比赛内容。'}` : team ? `${team.name || '当前队伍'} · 队长电脑：管理参赛和代码提交。` : '';
});
watchEffect(() => { document.title = `${title.value} · QOJ Contest`; });
</script>

<template>
  <a class="skip-link" href="#main">跳转到内容</a>
  <header class="topbar">
    <a class="brand" href="#participation" aria-label="QOJ 比赛入口">QOJ <span>Contest</span></a>
    <nav class="main-nav" aria-label="主导航">
      <a v-for="[tab, en, zh] in tabs" :key="tab" :href="`#${tab}`" :data-tab="tab" class="nav-tab" :class="{ active: c.tab === tab }" :aria-current="c.tab === tab ? 'page' : undefined" :hidden="!c.allowedTab(tab)"><span class="nav-label-en">{{ en }}</span><span class="nav-label-zh">{{ zh }}</span><span v-if="tab === 'submissions'" id="pending-badge" class="nav-count" :hidden="!pending">{{ pending }}</span></a>
    </nav>
    <div class="topbar-right">
      <span id="connection-indicator" class="connection" :class="c.connection"><span class="connection-dot"></span><span id="connection-label">{{ c.connection === 'online' ? 'Connected' : c.connection === 'offline' ? 'Disconnected' : 'Connecting' }}</span></span>
      <button type="button" class="button button-success nav-submit" id="submit-button" :disabled="!c.running || !c.problems.length || !c.can('submit')" :hidden="!c.can('submit')" :title="c.running ? '提交代码至 QOJ' : '请先在参赛入口开始比赛'" @click="c.openSubmission()"><span aria-hidden="true">↑</span> Submit</button>
      <button id="account-button" class="account-button" type="button" :hidden="!c.can('manage')" @click="c.openAccount()"><span id="account-label">{{ c.state?.user?.authenticated ? c.state.user.username || 'QOJ account' : 'Connect QOJ' }}</span></button>
    </div>
  </header>
  <div class="timer-progress" aria-hidden="true"><div id="timer-progress-bar" :style="{ width: `${progress}%` }"></div></div>
  <main id="main" tabindex="-1">
    <div id="notice" class="notice" :class="c.notice?.kind" role="status" :hidden="!c.notice"><span>{{ c.notice?.message }}</span><button type="button" aria-label="关闭通知" @click="c.notice = null">×</button></div>
    <div id="demo-banner" class="banner banner-demo" :hidden="!c.state?.demo"><strong>DEMO 演示模式</strong><span>题目与评测结果均为模拟数据，不会向 QOJ 提交代码。</span></div>
    <div id="contest-error" class="banner banner-error" :hidden="!(c.connectionError || c.canRead && c.state?.contest_error)">{{ c.connectionError || (c.canRead ? c.state?.contest_error : '') }}</div>
    <div id="team-access-note" class="banner team-access-note" role="status" :hidden="!teamNote"><span id="team-access-message">{{ teamNote }}</span><a href="#team" class="text-link">队伍设置 →</a></div>
    <div class="page-heading">
      <div class="contest-heading"><h1 id="contest-title">{{ title }}</h1><span id="contest-id" class="contest-id">{{ c.state?.contest?.id ? `CONTEST #${c.state.contest.id}` : 'VIRTUAL CONTEST' }}</span><a id="contest-link" class="text-link source-link" :href="`https://qoj.ac/contest/${encodeURIComponent(c.state?.contest?.id || '')}`" :hidden="!c.canRead || !c.can('manage')" target="_blank" rel="noopener noreferrer">QOJ 原比赛 ↗</a></div>
      <div class="contest-clock" aria-label="比赛计时"><span id="run-status" class="status-pill" :class="`status-${c.running ? 'running' : c.part.status === 'finished' ? 'finished' : 'neutral'}`">{{ status }}</span><span id="timer-label">{{ c.part.status === 'scheduled' ? 'WAITING FOR START' : c.part.status === 'finished' ? 'CONTEST FINISHED' : c.part.status === 'running' ? 'TIME REMAINING' : 'CONTEST NOT STARTED' }}</span><strong id="countdown" class="countdown" :class="{ urgent: c.canRead && c.part.status === 'running' && remaining > 0 && remaining < 900 }" aria-live="off">{{ remaining === null ? '--:--:--' : duration(remaining) }}</strong></div>
    </div>
    <div class="contest-meta"><span id="contest-format">{{ c.state?.demo ? '离线演示' : c.part.mode === 'official' ? 'QOJ 正式比赛' : 'QOJ Virtual Participation' }}</span><span id="timer-detail">{{ Number(c.part.started_at) ? `开始 ${timeOfDay(c.part.started_at)}${Number(c.part.ends_at) ? ` · 结束 ${timeOfDay(c.part.ends_at)}` : ''}` : '等待 QOJ 确认参赛时间' }}</span></div>
    <ParticipationView />
    <OverviewView />
    <ProblemWorkspace />
    <AttachmentsView />
    <section id="view-submissions" class="view" aria-label="提交记录" :hidden="c.tab !== 'submissions'">
      <section class="card table-card">
        <div class="section-heading">
          <h2>Submissions <span class="heading-note">本次比赛的提交</span></h2>
          <button id="submissions-refresh" type="button" class="button button-outline button-small"
                  title="评测结果自动更新，也可手动刷新" :aria-busy="refreshingSubmissions"
                  :disabled="refreshingSubmissions || !c.allowedTab('submissions') || c.teamChanging"
                  @click="refreshSubmissions">{{ refreshingSubmissions ? '正在刷新…' : '刷新提交记录' }}</button>
        </div>
        <div id="submissions-table" class="table-scroll"><SubmissionTable :submissions="submissions" /></div>
      </section>
    </section>
    <ScoreboardView />
    <PrintingView />
    <TeamView />
    <footer class="footer"><div><span><strong>QOJ Contest</strong> <span class="footer-separator">·</span> Independent contest interface</span><span class="footer-credit">Developed with ❤️ by <a href="https://github.com/minstdfx" target="_blank" rel="noopener noreferrer"><strong>minstdfx</strong></a> · Assisted by ChatGPT</span></div><span id="last-updated">{{ c.lastUpdated }}</span></footer>
  </main>
  <AccountDialog /><SubmitDialog /><SubmissionSourceDialog /><ConfirmDialog />
</template>
