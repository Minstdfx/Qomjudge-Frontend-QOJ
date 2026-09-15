<script setup>
import { ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import BaseDialog from './BaseDialog.vue';
const c = useContest();
const cookie = ref('');
const error = ref('');
const busy = ref('');
watch([() => c.context, () => c.accountOpen], () => {
  cookie.value = '';
  if (!busy.value) error.value = '';
}, { flush: 'sync' });
async function action(kind) {
  if (busy.value || c.accountChanging || !c.can('manage')) return;
  busy.value = kind; error.value = '';
  const payload = kind === 'connect' ? { cookie: cookie.value.trim().replace(/^cookie:\s*/i, '') } : {};
  try {
    const result = await c.runAccountAction(kind, payload);
    if (!result) return;
    cookie.value = '';
    if (!c.can('manage')) return;
    if (kind !== 'logout' && !c.state?.user?.authenticated) error.value = '尚未检测到 QOJ 登录状态。请完成登录，或使用 Cookie 连接。';
    else { c.accountOpen = false; c.notify(kind === 'logout' ? '已断开 QOJ 账号。' : 'QOJ 账号已连接。', kind === 'logout' ? 'info' : 'success'); }
  } catch (e) { if (c.accountOpen && c.can('manage')) error.value = e.message; }
  finally { busy.value = ''; }
}
</script>
<template>
  <BaseDialog id="account-dialog" v-model:open="c.accountOpen" :prevent-close="!!busy" class="modal account-modal" aria-labelledby="account-dialog-title">
    <div class="modal-heading"><div><div class="eyebrow">QOJ ACCOUNT</div><h2 id="account-dialog-title">连接你的 QOJ 账号</h2></div><button type="button" class="icon-button" data-close="account-dialog" aria-label="关闭账号对话框" :disabled="!!busy" @click="c.accountOpen = false">×</button></div>
    <div class="modal-body">
      <p id="account-status" class="muted">{{ c.state?.user?.authenticated ? `已连接账号：${c.state.user.username || 'QOJ 用户'}。提交将发送至此账号。` : '登录后，你的代码将通过此服务器提交至 QOJ。' }}</p>
      <a class="button button-primary full-width" href="/qoj/login" target="_blank" rel="noopener noreferrer">打开 QOJ 登录页面 ↗</a>
      <p class="field-help">在新页面完成登录后，点击下方「刷新登录状态」。如果登录页面无法使用，可导入 QOJ Cookie。</p>
      <button type="button" id="account-refresh" class="button button-outline full-width" :disabled="!!busy" @click="action('refresh')">{{ busy === 'refresh' ? '正在检查…' : '刷新登录状态' }}</button>
      <details class="cookie-details"><summary>使用 Cookie 连接</summary><p class="field-help">在浏览器登录 qoj.ac，打开开发者工具的 Network 面板，刷新页面，复制该站点请求头中的完整 Cookie 值。Cookie 等同登录凭证，请只连接你信任的服务器。</p>
        <form id="cookie-form" @submit.prevent="action('connect')"><label for="cookie-input">QOJ Cookie</label><textarea id="cookie-input" v-model="cookie" :disabled="!!busy" rows="4" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="复制 Cookie 值，不包含 Cookie: 前缀" required></textarea><button id="connect-button" type="submit" class="button button-primary full-width" :disabled="!!busy">{{ busy === 'connect' ? '正在连接…' : '连接账号' }}</button></form>
      </details>
      <div id="account-error" class="inline-error" role="alert" :hidden="!error">{{ error }}</div>
      <button id="logout-button" class="button button-danger-outline full-width" type="button" :hidden="!c.state?.user?.authenticated || c.state?.demo" :disabled="!!busy" @click="action('logout')">断开 QOJ 账号</button>
    </div>
  </BaseDialog>
</template>
