<script setup lang="ts">
/**
 * Horizontal action menu for work-item pages (ticket / thread / PR tabs).
 * One calm bar under the unified header: primary action(s) left, contextual
 * chips right, everything the same height. Views fill it via slots.
 */
</script>

<template>
  <div
    class="flex min-h-11 flex-wrap items-center gap-1.5 rounded-lg border bg-card/60 px-2 py-1.5"
  >
    <slot />
    <div v-if="$slots.trailing" class="ml-auto flex flex-wrap items-center gap-1.5">
      <slot name="trailing" />
    </div>
  </div>
</template>
