<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
const props = defineProps({ open: Boolean, preventClose: Boolean });
const emit = defineEmits(['update:open', 'close']);
const dialog = ref(null);
let previousFocus;
function sync() {
  if (!dialog.value) return;
  if (props.open && !dialog.value.open) { previousFocus = document.activeElement; dialog.value.showModal(); }
  else if (!props.open && dialog.value.open) dialog.value.close();
}
function dismiss() { if (!props.preventClose) emit('update:open', false); }
function closed() {
  // A new opening can precede the native close event from the previous opening.
  if (dialog.value?.open) return;
  emit('update:open', false); emit('close');
  if (previousFocus?.isConnected) previousFocus.focus();
}
function backdrop(event) {
  if (event.target !== dialog.value) return;
  const r = dialog.value.getBoundingClientRect();
  if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dismiss();
}
watch(() => props.open, sync, { flush: 'post' });
onMounted(sync);
onBeforeUnmount(() => { if (dialog.value?.open) dialog.value.close(); });
</script>
<template><dialog ref="dialog" @cancel.prevent="dismiss" @close="closed" @click="backdrop"><slot /></dialog></template>
