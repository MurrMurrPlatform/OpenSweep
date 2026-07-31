<script setup lang="ts">
/**
 * "Needs attention" — the workspace Overview's next-actions panel, fed by
 * GET /repositories/{uid}/attention (a current-state rollup, priority-sorted
 * server-side: kill switch, PRs blocked on a human, broken runs, approvals,
 * backlog aggregates).
 *
 * Non-fatal by design: a fetch error hides the panel rather than breaking the
 * dashboard. `refresh-key` lets the parent ride an existing heartbeat (the
 * active-runs signature) for silent updates.
 */
import { computed, ref, watch } from 'vue'
import type { RouteLocationRaw } from 'vue-router'
import { CheckCircle2 } from 'lucide-vue-next'
import { useAttentionStore } from '@/stores/attentionStore'
import { formatRelativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import type { AttentionItemDTO } from '@/types/api'

const props = defineProps<{
  repositoryUid: string
  repoSlug: string
  /** Bump to refresh silently (parents pass the active-runs signature). */
  refreshKey?: string
}>()

const store = useAttentionStore()
const failed = ref(false)
const loaded = ref(false)

async function load({ silent = false } = {}) {
  const uid = props.repositoryUid
  if (!uid) return
  if (!silent) loaded.value = false
  try {
    await store.fetchFor(uid)
    failed.value = false
  } catch {
    failed.value = true
  }
  loaded.value = true
}

watch(() => props.repositoryUid, () => void load(), { immediate: true })
watch(() => props.refreshKey, () => void load({ silent: true }))

const items = computed(() => store.attention?.items ?? [])

/** Per-entity subjects resolve to flat detail routes (notificationLinks map). */
const DETAIL_ROUTES: Record<string, string> = {
  PullRequest: 'pull-request-detail',
  Thread: 'thread-detail',
  Run: 'run-detail',
  Finding: 'finding-detail',
}

/** Aggregate kinds point at the list surface where the queue lives. */
const AGGREGATE_ROUTES: Record<string, string> = {
  area_edits_pending: 'areas',
  stale_areas: 'areas',
  epic_proposals_pending: 'tickets',
  high_severity_findings: 'findings',
}

function linkFor(item: AttentionItemDTO): RouteLocationRaw | null {
  if (item.kind === 'kill_switch_engaged') {
    return { name: 'workspace-settings', params: { repoSlug: props.repoSlug } }
  }
  const detail = DETAIL_ROUTES[item.subject_type]
  if (detail && item.subject_uid) return { name: detail, params: { uid: item.subject_uid } }
  const list = AGGREGATE_ROUTES[item.kind]
  if (list) return { name: list, params: { repoSlug: props.repoSlug } }
  return null
}

function tone(item: AttentionItemDTO): BadgeVariants['variant'] {
  if (item.priority <= 3) return 'destructive'
  if (item.priority <= 6) return 'warn'
  return 'secondary'
}

/** Short badge label per kind — the row title carries the specifics. */
const KIND_LABELS: Record<string, string> = {
  kill_switch_engaged: 'halted',
  verdict_needs_human: 'verdict',
  pr_fix_rounds_exhausted: 'fix rounds',
  pr_waive_requested: 'waiver',
  pr_ci_red: 'CI',
  run_needs_attention: 'run',
  thread_plan_awaiting_approval: 'plan',
  thread_ready_for_review: 'review',
  area_edits_pending: 'areas',
  epic_proposals_pending: 'epics',
  high_severity_findings: 'findings',
  stale_areas: 'stale',
}
</script>

<template>
  <Card v-if="!failed">
    <CardHeader class="p-4 pb-0">
      <CardTitle class="text-base">Needs attention</CardTitle>
    </CardHeader>
    <CardContent class="p-0 pt-2">
      <div v-if="!loaded" class="space-y-2 p-4 pt-2">
        <Skeleton v-for="i in 3" :key="i" class="h-9" />
      </div>
      <div
        v-else-if="!items.length"
        class="flex items-center gap-2 px-4 pb-4 pt-1 text-sm text-good"
      >
        <CheckCircle2 class="size-4" />
        Nothing needs you right now.
      </div>
      <ul v-else class="divide-y divide-border">
        <li v-for="(item, i) in items" :key="`${item.kind}-${item.subject_uid}-${i}`">
          <component
            :is="linkFor(item) ? 'RouterLink' : 'div'"
            :to="linkFor(item) ?? undefined"
            class="flex flex-wrap items-center gap-2 px-4 py-2.5 text-sm"
            :class="linkFor(item) ? 'transition-colors hover:bg-accent' : ''"
          >
            <Badge :variant="tone(item)" class="whitespace-nowrap">
              {{ KIND_LABELS[item.kind] ?? item.kind }}
            </Badge>
            <span class="min-w-0 flex-1 basis-48 truncate font-medium">{{ item.title }}</span>
            <span v-if="item.description" class="hidden max-w-64 truncate text-xs text-muted-foreground sm:inline">
              {{ item.description }}
            </span>
            <span v-if="item.created_at" class="whitespace-nowrap text-xs text-muted-foreground">
              {{ formatRelativeTime(item.created_at) }}
            </span>
          </component>
        </li>
      </ul>
    </CardContent>
  </Card>
</template>
