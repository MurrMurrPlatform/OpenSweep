<script setup lang="ts">
/**
 * The upkeep cells of one Areas row: files, spec, docs, audit coverage, and a
 * confirm-current action.
 *
 * Rendered inside a RouterLink row, so every interactive control must stop
 * propagation — otherwise clicking "Reviewed" also navigates to the detail
 * page.
 *
 * Three independent axes, deliberately not merged into one score: an area can
 * be freshly audited and still have a stale map, or a current map nobody has
 * ever audited. Collapsing them would hide exactly the case worth acting on.
 */
import { Check } from 'lucide-vue-next'
import { daysAgo } from '@/lib/utils'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { AreaHealthRowDTO } from '@/types/api'

const props = defineProps<{
  health: AreaHealthRowDTO | null
  /** Files under this row — a leaf's own count, or a grouping's rollup. */
  fileTotal?: number | null
  confirming?: boolean
  /** Hide the confirm action (groupings own no scope, so they never go stale). */
  confirmable?: boolean
}>()

const emit = defineEmits<{ confirm: [] }>()

const n = (x: number) => x.toLocaleString('en-US')

function outcomeVariant(outcome: string): BadgeVariants['variant'] {
  if (outcome === 'clean') return 'success'
  if (outcome === 'findings') return 'info'
  if (outcome === 'failed') return 'destructive'
  return 'secondary'
}

function docsTitle(h: AreaHealthRowDTO): string {
  return h.docs_stale
    ? `${h.docs_total} doc page(s) cover this area — ${h.docs_stale} need a review`
    : `${h.docs_total} doc page(s) cover this area`
}
</script>

<template>
  <div class="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
    <span v-if="props.fileTotal != null" class="tabular-nums" title="Files under this scope">
      {{ n(props.fileTotal) }} files
    </span>

    <template v-if="props.health">
      <Badge
        v-if="props.health.spec_state === 'missing'"
        variant="outline"
        class="px-1.5 text-[10px]"
        title="No spec — nothing says what to check here"
      >
        no spec
      </Badge>
      <Badge
        v-else-if="props.health.spec_state === 'stale'"
        variant="warn"
        class="px-1.5 text-[10px]"
        title="The spec was written against code that has since moved"
      >
        spec stale
      </Badge>

      <span
        v-if="props.health.docs_total"
        class="whitespace-nowrap tabular-nums"
        :title="docsTitle(props.health)"
      >
        {{ props.health.docs_total }} doc<span v-if="props.health.docs_total !== 1">s</span>
        <span v-if="props.health.docs_stale" class="text-warn">
          · {{ props.health.docs_stale }} stale
        </span>
      </span>

      <!-- Both badges live inside the v-if so the "never audited" v-else stays
           bound to `last_checked`. Vue pairs v-else with the nearest preceding
           v-if sibling, so a second v-if between them would steal it. -->
      <template v-if="props.health.last_checked">
        <Badge
          :variant="outcomeVariant(props.health.outcome)"
          class="px-1.5 text-[10px]"
          :title="`Last audited ${daysAgo(props.health.last_checked)}${props.health.revision ? ` at ${props.health.revision.slice(0, 7)}` : ''}`"
        >
          {{ props.health.outcome }} · {{ daysAgo(props.health.last_checked) }}
        </Badge>
        <Badge
          v-if="props.health.coverage_source !== 'reported'"
          variant="outline"
          class="px-1.5 text-[10px]"
          title="That run was aimed at these paths but did not report what it examined — treat the coverage as unverified"
        >
          scope only
        </Badge>
      </template>
      <Badge
        v-else
        variant="outline"
        class="px-1.5 text-[10px]"
        title="No audit has ever covered this area's paths"
      >
        never audited
      </Badge>

      <Button
        v-if="props.confirmable && props.health.stale"
        variant="ghost"
        size="sm"
        class="h-6 px-1.5 text-[10px]"
        :loading="props.confirming"
        title="I checked — the map is still right. Clears stale without inventing an edit."
        @click.stop.prevent="emit('confirm')"
      >
        <Check class="h-3 w-3" /> Reviewed
      </Button>
    </template>
  </div>
</template>
