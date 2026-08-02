<script setup lang="ts">
import SweepMark from './SweepMark.vue'

defineProps<{ class?: string }>()
</script>

<template>
  <span :class="['opensweep-logo', $props.class]" role="img" aria-label="OpenSweep">
    <SweepMark class="opensweep-logo-mark" aria-hidden="true" />
    <span class="opensweep-logo-wordmark">OpenSweep</span>
  </span>
</template>

<style scoped>
.opensweep-logo {
  display: inline-flex;
  align-items: center;
  gap: 0.625rem;
}
.opensweep-logo-mark {
  height: 100%;
  color: hsl(var(white));
}
.opensweep-logo-wordmark {
  font-size: 1.0625rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: hsl(var(--foreground));
  white-space: nowrap;
}
</style>
