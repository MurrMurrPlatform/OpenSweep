<script setup lang="ts">
import type { HTMLAttributes } from "vue"
import { cn } from "@/lib/utils"

const props = defineProps<{
  class?: HTMLAttributes["class"]
}>()
</script>

<template>
  <!-- `pr-6` keeps a wrapping title clear of the close button; `shrink-0` stops
       the header collapsing when the body next to it scrolls. -->
  <div
    :class="cn('flex shrink-0 flex-col gap-y-1.5 pr-6 text-left', props.class)"
  >
    <slot />
  </div>
</template>
