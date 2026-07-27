<script setup lang="ts">
/**
 * The one place to start a sweep.
 *
 * The backend exposes /repositories/{uid}/sweep/{map-areas,generate-docs,
 * audit,deep-scan,generate-specs} but the UI historically scattered them over
 * three screens (Documentation, Health, Areas). This dialog collects them,
 * shows what each one costs BEFORE dispatch (GET /sweep/estimate), and makes
 * the area-map prerequisite an actionable step rather than a disabled button
 * with a tooltip.
 */
import { computed, ref, watch, type Component } from 'vue'
import { BookOpen, Layers, Radar, Search, Sparkles } from 'lucide-vue-next'
import { useAreaStore } from '@/stores/areaStore'
import { useDocStore, type SweepEstimate } from '@/stores/docStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'

type SweepKind = 'map-areas' | 'generate-docs' | 'audit' | 'deep-scan' | 'generate-specs'

interface Props {
  open: boolean
  repositoryUid: string | null
}
const props = defineProps<Props>()
const emit = defineEmits<{
  'update:open': [value: boolean]
  /** A run was dispatched — the parent refreshes its activity surfaces. */
  dispatched: [runUid: string]
}>()

const areaStore = useAreaStore()
const docs = useDocStore()
const toast = useToast()

const selected = ref<SweepKind>('map-areas')
const dispatching = ref(false)

// ── Preview: what this sweep costs, before spending it ──────────────────────

const estimate = ref<SweepEstimate | null>(null)
const estimateError = ref(false)
const loadingPreview = ref(false)
/** null = the area map hasn't been resolved yet — an unknown map never gates. */
const enabledAreas = ref<number | null>(null)

async function loadPreview() {
  const uid = props.repositoryUid
  if (!uid) return
  loadingPreview.value = true
  estimateError.value = false
  const [est, areas] = await Promise.allSettled([
    docs.sweepEstimate(uid),
    areaStore.fetchAreas(uid),
  ])
  if (props.repositoryUid !== uid) return // switched workspace mid-flight
  if (est.status === 'fulfilled') estimate.value = est.value
  else {
    estimate.value = null
    estimateError.value = true
  }
  enabledAreas.value =
    areas.status === 'fulfilled' ? areas.value.filter((a) => a.enabled).length : null
  loadingPreview.value = false
  selected.value = recommended.value
}

watch(
  () => props.open,
  (open) => {
    if (open) void loadPreview()
  },
)

// ── Gate: docs + specs hang off the area map ────────────────────────────────

/** Only gates once the area map is actually known to be empty. */
const areasMissing = computed(() => enabledAreas.value === 0)
const hasDocs = computed(() => (estimate.value?.docs ?? 0) > 0)

/** First useful sweep for the repo's current state — preselected on open. */
const recommended = computed<SweepKind>(() => {
  if (areasMissing.value) return 'map-areas'
  if (!hasDocs.value) return 'generate-docs'
  return 'audit'
})

interface SweepOption {
  kind: SweepKind
  label: string
  icon: Component
  produces: string
  description: string
  /** What it will spend, from GET /sweep/estimate. */
  cost: string
  blocked: boolean
}

const options = computed<SweepOption[]>(() => {
  const est = estimate.value
  const docRuns = est ? est.generate_docs_runs : null
  const auditRuns = est ? est.audit_runs_if_all_selected : null
  return [
    {
      kind: 'map-areas',
      label: 'Map the codebase',
      icon: Layers,
      produces: 'Area map',
      description:
        'One run reads the repository and proposes its subsystems and features. Everything else hangs off this map — start here.',
      cost: '1 run',
      blocked: false,
    },
    {
      kind: 'generate-docs',
      label: 'Generate documentation',
      icon: BookOpen,
      produces: 'Doc pages',
      description:
        'Writes the wiki from the code — one page per area, each watching the paths it describes.',
      cost: docRuns === null ? '—' : `${docRuns} run${docRuns === 1 ? '' : 's'}`,
      blocked: areasMissing.value,
    },
    {
      kind: 'generate-specs',
      label: 'Generate feature specs',
      icon: Sparkles,
      produces: 'Specs',
      description:
        'Drafts a spec for every feature that lacks one and refreshes stale ones, as edits you review.',
      cost: '1 run',
      blocked: areasMissing.value,
    },
    {
      kind: 'audit',
      label: 'Audit for problems',
      icon: Search,
      produces: 'Findings',
      description: hasDocs.value
        ? 'Scoped runs against the pages that most need a look — never-checked first, then longest-stale.'
        : 'One repository-wide run that files what it finds. Generate docs first to get cheaper, scoped audits.',
      cost: hasDocs.value
        ? `up to 3 runs${auditRuns !== null ? ` (${auditRuns} for every page)` : ''}`
        : '1 run',
      blocked: false,
    },
    {
      kind: 'deep-scan',
      label: 'Deep scan',
      icon: Radar,
      produces: 'Findings + report',
      description:
        'One long run that plans its own scan and works through the whole codebase area by area. The thorough, expensive option.',
      cost: '1 long run',
      blocked: false,
    },
  ]
})

const selectedOption = computed(
  () => options.value.find((o) => o.kind === selected.value) ?? options.value[0],
)

function pick(option: SweepOption) {
  if (option.blocked) return
  selected.value = option.kind
}

// ── Dispatch ────────────────────────────────────────────────────────────────

function close() {
  emit('update:open', false)
}

/** Dispatch endpoints answer 200-with-errors when they couldn't queue a run. */
function announce(runUid: string, title: string, summary?: string) {
  emit('dispatched', runUid)
  toast.success(
    title,
    summary || `run ${runUid.slice(0, 8)}`,
    { label: 'View run', to: { name: 'run-detail', params: { uid: runUid } } },
  )
  close()
}

async function start() {
  const uid = props.repositoryUid
  const option = selectedOption.value
  if (!uid || dispatching.value || !option || option.blocked) return
  dispatching.value = true
  try {
    switch (option.kind) {
      case 'map-areas': {
        const r = await areaStore.mapNow(uid)
        if (!r.run_uid) toast.error('Map areas failed', r.errors?.join(' · ') || r.summary)
        else announce(r.run_uid, 'Mapping the codebase', r.summary)
        break
      }
      case 'generate-docs': {
        const r = await docs.generate(uid)
        if (!r.run_uid) toast.error('Generate docs failed', r.errors?.join(' · ') || r.summary)
        else announce(r.run_uid, 'Generating documentation', r.summary)
        break
      }
      case 'generate-specs': {
        const r = await areaStore.generateSpecs(uid)
        if (!r.run_uid) toast.error('Generate specs failed', r.errors?.join(' · ') || r.summary)
        else announce(r.run_uid, 'Drafting feature specs', r.summary)
        break
      }
      case 'audit': {
        // No doc pages yet → one repository-wide run; auto-select needs pages
        // to choose between and would otherwise dispatch nothing.
        const r = await docs.audit(uid, [], { auto_select: hasDocs.value, limit: 3 })
        if (r.runs_dispatched.length) {
          announce(r.runs_dispatched[0], `Audit dispatched — ${r.runs_dispatched.length} run${r.runs_dispatched.length === 1 ? '' : 's'}`, r.summary)
        } else {
          toast.warn('Nothing to audit', r.errors.join(' · ') || r.summary)
        }
        break
      }
      case 'deep-scan': {
        const r = await docs.deepScan(uid)
        if (!r.run_uid) toast.error('Deep scan not dispatched', r.errors?.join(' · ') || r.summary)
        else announce(r.run_uid, 'Deep scan started', r.summary)
        break
      }
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      toast.warn('Already running', 'One sweep of this kind per repository at a time — open Runs to follow it.')
    } else {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
      toast.error('Couldn’t start the sweep', msg)
    }
  } finally {
    dispatching.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>Start a sweep</DialogTitle>
        <DialogDescription>
          A sweep is one dispatch of agents over this repository. Pick what you
          want out of it — the cost is shown before you spend it.
        </DialogDescription>
      </DialogHeader>

      <div class="max-h-[55vh] space-y-2 overflow-y-auto -mx-6 px-6">
        <template v-if="loadingPreview && !estimate">
          <Skeleton v-for="i in 4" :key="i" class="h-[68px]" />
        </template>

        <template v-else>
          <!-- The area map is the prerequisite, not a tooltip on a dead button. -->
          <div
            v-if="areasMissing"
            class="flex flex-wrap items-center gap-2 rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs"
          >
            <Layers class="size-4 shrink-0 text-primary" />
            <span class="min-w-0 flex-1">
              This repository has no area map yet. Docs and specs are generated one
              per area — map the codebase first.
            </span>
            <Button size="sm" variant="outline" class="shrink-0" @click="selected = 'map-areas'">
              Map first
            </Button>
          </div>

          <button
            v-for="option in options"
            :key="option.kind"
            type="button"
            :disabled="option.blocked"
            :class="[
              'w-full rounded-lg border p-3 text-left transition-colors',
              option.blocked
                ? 'cursor-not-allowed border-dashed opacity-60'
                : selected === option.kind
                  ? 'border-primary bg-primary/10'
                  : 'hover:bg-accent',
            ]"
            @click="pick(option)"
          >
            <div class="flex flex-wrap items-center gap-2">
              <component :is="option.icon" class="size-4 shrink-0 text-muted-foreground" />
              <span class="font-medium">{{ option.label }}</span>
              <Badge variant="secondary" class="px-1.5 text-[10px]">{{ option.produces }}</Badge>
              <span
                v-if="option.kind === recommended && !option.blocked"
                class="rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-primary"
              >
                recommended
              </span>
              <span class="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">
                {{ option.cost }}
              </span>
            </div>
            <p class="mt-1 text-xs text-muted-foreground">{{ option.description }}</p>
            <p v-if="option.blocked" class="mt-1 text-xs text-primary">
              Needs the area map — run “Map the codebase” first.
            </p>
          </button>

          <p v-if="estimate?.note" class="pt-1 text-xs text-muted-foreground">
            {{ estimate.note }}
          </p>
          <p v-else-if="estimateError" class="pt-1 text-xs text-muted-foreground">
            Couldn't load the cost estimate — the sweep still dispatches normally.
          </p>
        </template>
      </div>

      <DialogFooter>
        <Button variant="ghost" @click="close">Cancel</Button>
        <Button
          :disabled="!repositoryUid || !selectedOption || selectedOption.blocked"
          :loading="dispatching"
          @click="start"
        >
          <Sparkles v-if="!dispatching" />
          Start {{ selectedOption?.label.toLowerCase() ?? 'sweep' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
