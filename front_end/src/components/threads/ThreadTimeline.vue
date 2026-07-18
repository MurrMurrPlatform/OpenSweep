<script setup lang="ts">
import { computed } from 'vue'
import { Card, CardContent } from '@/components/ui/card'
import type { ThreadEventDTO, ThreadRunSummaryDTO } from '@/types/api'

const props = defineProps<{
  events: ThreadEventDTO[]
  runs: ThreadRunSummaryDTO[]
}>()

const LABELS: Record<string, string> = {
  run_attached: 'Agent session started',
  plan_drafted: 'Plan drafted',
  plan_edited: 'Plan edited by hand',
  plan_approved: 'Plan approved',
  pr_opened: 'Draft PR opened',
  merged: 'PR merged — thread done',
  review_verdict: 'Review verdict',
  finding_bound: 'Review finding',
  fix_started: 'Fix round started',
  question: 'Agent asked a question',
}

const items = computed(() =>
  [...props.events].reverse().map((e) => ({
    ts: e.ts,
    label:
      e.type === 'phase_changed'
        ? `Phase: ${String(e.frm)} → ${String(e.to)}`
        : e.type === 'review_verdict'
          ? `Review verdict: ${String(e.result ?? '')}`
          : (LABELS[e.type] ?? e.type),
  })),
)

function timeOf(ts: string): string {
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}
</script>

<template>
  <Card>
    <CardContent class="space-y-2 p-4">
      <h3 class="text-sm font-semibold">Timeline</h3>
      <ol class="space-y-1.5 text-xs text-muted-foreground">
        <li v-for="(item, i) in items" :key="i" class="flex justify-between gap-2">
          <span>{{ item.label }}</span>
          <span class="shrink-0 tabular-nums">{{ timeOf(item.ts) }}</span>
        </li>
        <li v-if="!items.length" class="italic">Nothing yet.</li>
      </ol>
    </CardContent>
  </Card>
</template>
