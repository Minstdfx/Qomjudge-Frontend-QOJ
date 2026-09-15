<script setup>
import { color, foreground } from '../lib/format.js';
defineProps({ problem: { type: Object, default: () => ({ label: '?' }) } });
</script>
<template><span class="problem-letter" :style="{ background: color(problem.color), color: foreground(problem.color) }">{{ problem.label }}</span></template>
