<script setup>
import { ref, watch } from 'vue';
import { useContest } from '../composables/useContest.js';
import BaseDialog from './BaseDialog.vue';
const c = useContest();
const busy = ref(false);
const error = ref('');
watch(() => c.confirmation, () => { error.value = ''; });
async function confirm() {
  if (busy.value) return;
  const dialog = c.confirmation;
  busy.value = true; error.value = '';
  try { await dialog.callback?.(); if (dialog === c.confirmation) c.confirmation.open = false; }
  catch (e) { if (dialog === c.confirmation && dialog.open) error.value = e.message; }
  finally { busy.value = false; }
}
</script>
<template>
  <BaseDialog id="confirm-dialog" v-model:open="c.confirmation.open" :prevent-close="busy" class="modal confirm-modal" aria-labelledby="confirm-title">
    <div class="modal-heading"><h2 id="confirm-title">{{ c.confirmation.title || '确认操作' }}</h2><button type="button" class="icon-button" data-close="confirm-dialog" aria-label="取消" :disabled="busy" @click="c.confirmation.open = false">×</button></div>
    <div class="modal-body"><p id="confirm-message">{{ c.confirmation.message }}</p><div id="confirm-error" class="inline-error" role="alert" :hidden="!error">{{ error }}</div></div>
    <div class="modal-footer"><button type="button" class="button button-outline" data-close="confirm-dialog" :disabled="busy" @click="c.confirmation.open = false">取消</button><button type="button" class="button button-primary" id="confirm-action" :disabled="busy" @click="confirm">{{ busy ? '处理中…' : '确认' }}</button></div>
  </BaseDialog>
</template>
