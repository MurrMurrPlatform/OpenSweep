<script setup lang="ts">
/** The evidence rows behind a trust score — shared by the tooltip chip and the
 *  detail-page breakdown so both always tell the same story. */
import { Circle, Gauge, Repeat2, ShieldCheck, Wrench } from 'lucide-vue-next'
import type { TrustSignal, TrustSignalKey } from './findingMeta'

defineProps<{ signals: TrustSignal[] }>()

const KEY_ICON: Record<TrustSignalKey, typeof Circle> = {
  corroboration: Repeat2,
  tool: Wrench,
  verification: ShieldCheck,
  confidence: Gauge,
}

const TONE_CLASS: Record<TrustSignal['tone'], string> = {
  positive: 'text-good',
  negative: 'text-destructive',
  neutral: 'text-muted-foreground',
}
</script>

<template>
  <ul class="space-y-1.5 text-xs">
    <li v-for="s in signals" :key="s.key" class="flex items-start gap-2">
      <component :is="KEY_ICON[s.key]" class="mt-0.5 size-3 shrink-0" :class="TONE_CLASS[s.tone]" />
      <span class="min-w-0">
        <span class="font-medium" :class="TONE_CLASS[s.tone]">{{ s.label }}</span>
        <span class="ml-1 text-muted-foreground">{{ s.detail }}</span>
      </span>
    </li>
  </ul>
</template>
