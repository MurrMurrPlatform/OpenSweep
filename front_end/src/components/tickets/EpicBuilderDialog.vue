<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Loader2 } from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { priorityVariant } from '@/components/tickets/ticketMeta'
import {
  ANY,
  PRIORITY_OPTIONS,
  SEVERITY_OPTIONS,
  SORT_OPTIONS,
  STATUS_OPTIONS,
  floorValue,
  toggleStatus,
} from '@/components/tickets/epicSelection'
import type {
  EpicDraftDTO,
  PlanEpicsPreview,
  PlanEpicsRequest,
  EpicComputedAxis,
  TicketPriority,
  TicketStatus,
} from '@/types/api'

interface Props {
  open: boolean
  repositoryUid: string
}
const props = defineProps<Props>()
/** `created` fires after epics were persisted so the board can reload. */
const emit = defineEmits<{ 'update:open': [value: boolean]; created: [] }>()

const store = useTicketStore()
const toast = useToast()

/**
 * The computable axes only. `root-cause`, `theme` and `co-change` are
 * deliberately absent: they need agent judgment and live behind "Suggest epics
 * (agent run)".
 */
type ComputableAxis = EpicComputedAxis

interface AxisOption {
  value: ComputableAxis
  label: string
  /** One line of plain English: what this axis actually groups by. */
  hint: string
  /** Why a run produced nothing — shown instead of an empty list. */
  empty: string
}

const AXIS_OPTIONS: AxisOption[] = [
  {
    value: 'area',
    label: 'Subsystem area',
    hint: 'tickets whose files sit in the same subsystem area',
    empty: 'No tickets share a subsystem area.',
  },
  {
    value: 'feature',
    label: 'Feature area',
    hint: 'tickets whose files sit in the same feature area',
    empty: 'No tickets share a feature area.',
  },
  {
    value: 'files',
    label: 'Overlapping files',
    hint: 'tickets whose findings touch overlapping files',
    empty: 'No tickets touch overlapping files.',
  },
  {
    value: 'lens',
    label: 'Finding lens',
    hint: 'tickets from the same lens — security, performance, …',
    empty: 'No tickets share a lens.',
  },
  {
    value: 'class',
    label: 'Finding class',
    hint: 'tickets of one finding class — the same kind of problem, one fix',
    empty: 'No tickets share a finding class.',
  },
  {
    value: 'linked',
    label: 'Linked findings',
    hint: 'tickets promoted from the same finding, or findings that cite each other',
    empty: 'No tickets share a linked finding.',
  },
]

// ── Controls ────────────────────────────────────────────────────────────────

const axis = ref<ComputableAxis>('area')
const minSeverity = ref<string>(ANY)
const minPriority = ref<string>(ANY)
const statuses = ref<TicketStatus[]>(['backlog', 'todo'])
const sort = ref('priority')

/** The numeric knobs live in one reactive object: `<script setup>` unwraps
 *  refs in the template, so a shared `setNumber(ref, …)` handler would receive
 *  the number rather than the ref it needs to write back to. */
type NumberField = 'limit' | 'maxEpics' | 'minMembers' | 'maxMembers'
const nums = reactive<Record<NumberField, number>>({
  limit: 20,
  maxEpics: 4,
  minMembers: 2,
  maxMembers: 6,
})

const axisMeta = computed(
  () => AXIS_OPTIONS.find((o) => o.value === axis.value) ?? AXIS_OPTIONS[0],
)

/**
 * Number fields keep their last valid value while the box is empty mid-edit —
 * writing `NaN` into the request would send garbage to the preview endpoint.
 * Bounds are validated (not clamped) so typing "12" into a field whose floor
 * is 2 isn't rewritten to "2" after the first keystroke.
 */
function setNumber(field: NumberField, raw: string | number) {
  const n = typeof raw === 'number' ? raw : Number.parseInt(raw, 10)
  if (Number.isNaN(n)) return
  nums[field] = Math.max(0, n)
}

/** Blocks preview and create, and is shown inline. '' = the form is usable. */
const validationError = computed(() => {
  if (!props.repositoryUid) return 'Pick a repository first.'
  if (!statuses.value.length) return 'Pick at least one status — otherwise nothing is eligible.'
  if (nums.minMembers < 2) return 'An epic needs at least 2 members.'
  if (nums.maxMembers < nums.minMembers) return 'Max members must be at least min members.'
  return ''
})

const request = computed<Omit<PlanEpicsRequest, 'dry_run'>>(() => ({
  repository_uid: props.repositoryUid,
  statuses: [...statuses.value],
  min_priority: floorValue(minPriority.value),
  min_severity: floorValue(minSeverity.value),
  limit: nums.limit,
  sort: sort.value,
  axis: axis.value,
  max_epics: nums.maxEpics,
  min_members: nums.minMembers,
  max_members: nums.maxMembers,
}))

// ── Live preview ────────────────────────────────────────────────────────────

const PREVIEW_DEBOUNCE_MS = 350

const preview = ref<PlanEpicsPreview | null>(null)
const previewError = ref<string | null>(null)
const previewing = ref(false)
const creating = ref(false)

let debounceTimer: ReturnType<typeof setTimeout> | null = null
/** Bumped by every dispatch and every cancel, so a late response that no
 *  longer matches is dropped instead of writing into a closed dialog. */
let previewSeq = 0

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

function cancelPreview() {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
    debounceTimer = null
  }
  previewSeq += 1
  previewing.value = false
}

async function runPreview() {
  const mine = ++previewSeq
  previewing.value = true
  try {
    const result = await store.previewEpics(request.value)
    if (mine !== previewSeq) return
    preview.value = result
    previewError.value = null
  } catch (e) {
    if (mine !== previewSeq) return
    preview.value = null
    previewError.value = errorText(e)
  } finally {
    if (mine === previewSeq) previewing.value = false
  }
}

function schedulePreview() {
  cancelPreview()
  if (!props.open || validationError.value) return
  // The spinner goes up on the keystroke, not on the request — otherwise the
  // panel shows a stale plan as if it were the answer for the new controls.
  previewing.value = true
  debounceTimer = setTimeout(() => {
    debounceTimer = null
    void runPreview()
  }, PREVIEW_DEBOUNCE_MS)
}

watch(request, schedulePreview)

watch(
  () => props.open,
  (open) => {
    cancelPreview()
    previewError.value = null
    if (!open) {
      preview.value = null
      return
    }
    // Opening is not an edit — answer immediately rather than after a beat.
    if (!validationError.value) void runPreview()
  },
)

onBeforeUnmount(cancelPreview)

// ── Rendering the plan ──────────────────────────────────────────────────────

const summary = computed(() => preview.value?.summary ?? null)
const drafts = computed(() => preview.value?.epics ?? [])

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

const summaryLine = computed(() => {
  const s = summary.value
  if (!s) return ''
  return `${plural(s.selected, 'ticket', 'tickets')} → ${plural(s.epics, 'epic', 'epics')}`
})

/**
 * Why the plan is empty, in the caller's terms. Zero epics is a normal
 * outcome for every axis, so it must not read as a failure.
 */
const emptyReason = computed(() => {
  const s = summary.value
  if (!s || s.epics > 0) return ''
  if (s.candidates === 0) return 'No tickets in this repository yet — nothing to epic.'
  if (s.selected === 0) {
    return `None of the ${plural(s.candidates, 'candidate ticket', 'candidate tickets')} match these filters — loosen severity, priority or status, or raise "top X".`
  }
  return `${axisMeta.value.empty} Nothing forms a group of ${nums.minMembers} or more on this axis.`
})

/**
 * `capped_from` is stamped on every survivor when `max epics` dropped
 * qualifying epics. Surfacing it is the difference between "that's all
 * there was" and "we kept the strongest 4 of 9".
 */
const cappedFrom = computed(() => {
  for (const d of drafts.value) {
    const raw = d.evidence?.capped_from
    if (typeof raw === 'number' && raw > drafts.value.length) return raw
  }
  return 0
})

/** Keys that only restate what the row already shows. */
const EVIDENCE_NOISE = new Set([
  'axis',
  'plan_uid',
  'member_count',
  'member_uids',
  'capped_from',
  'part',
  'of_parts',
])

/** Most identifying first — the winner is the evidence line. */
const EVIDENCE_PRIORITY = [
  'shared_paths',
  'area_key',
  'feature_key',
  'class',
  'lens',
  'shared_finding_uids',
]

function evidenceLine(draft: EpicDraftDTO): string {
  const evidence = draft.evidence
  if (!evidence || typeof evidence !== 'object') return ''
  const keys = Object.keys(evidence).filter((k) => !EVIDENCE_NOISE.has(k))
  keys.sort((a, b) => {
    const ai = EVIDENCE_PRIORITY.indexOf(a)
    const bi = EVIDENCE_PRIORITY.indexOf(b)
    return (ai === -1 ? EVIDENCE_PRIORITY.length : ai) - (bi === -1 ? EVIDENCE_PRIORITY.length : bi)
  })
  for (const key of keys) {
    const value = evidence[key]
    if (Array.isArray(value)) {
      const items = value.filter((v) => typeof v === 'string' || typeof v === 'number')
      if (!items.length) continue
      return items.length > 1 ? `${items[0]} +${items.length - 1}` : String(items[0])
    }
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number') return String(value)
  }
  return ''
}

/**
 * Backend titles read `"<label> · 3 tickets"`, and the row renders the member
 * count itself. Drop that one segment when it matches the real count — if the
 * server title format ever changes, nothing is stripped and the count merely
 * appears twice.
 */
function displayTitle(draft: EpicDraftDTO): string {
  const n = draft.member_ticket_uids.length
  return draft.title.replace(new RegExp(`\\s·\\s${n} tickets?(?=\\s|$)`), '')
}


// ── Create ──────────────────────────────────────────────────────────────────

const canCreate = computed(
  () => !validationError.value && !previewing.value && !creating.value && drafts.value.length > 0,
)

async function create() {
  if (!canCreate.value) return
  creating.value = true
  try {
    const result = await store.planEpics(request.value)
    const n = result.epics.length
    const left = result.summary.unplaced
    toast.success(
      `${plural(n, 'epic', 'epics')} proposed`,
      `${plural(result.summary.selected, 'ticket', 'tickets')} selected${left ? ` · ${left} left out` : ''} — approve them in Proposed epics.`,
    )
    emit('created')
    emit('update:open', false)
  } catch (e) {
    toast.error('Could not build epics', errorText(e))
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>Build epics by rule</DialogTitle>
        <DialogDescription>
          Take the top X tickets and cut them into Y epics on a computable axis — no agent, no
          run, answers immediately. Epics land as proposals; nothing changes until you approve
          them.
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="space-y-5">
        <!-- 1. Axis — what "belongs together" means for this plan -->
        <fieldset class="space-y-2">
          <legend class="text-sm font-medium">Group by</legend>
          <div class="grid gap-1.5 sm:grid-cols-2">
            <label
              v-for="opt in AXIS_OPTIONS"
              :key="opt.value"
              class="flex cursor-pointer items-start gap-2.5 rounded-md border p-2.5 transition-colors hover:bg-accent"
              :class="axis === opt.value ? 'border-primary bg-primary/5' : ''"
            >
              <input
                type="radio"
                name="epic-axis"
                class="mt-0.5 size-4 shrink-0 cursor-pointer accent-primary"
                :value="opt.value"
                :checked="axis === opt.value"
                @change="axis = opt.value"
              />
              <span class="min-w-0">
                <span class="block text-sm font-medium">{{ opt.label }}</span>
                <span class="block text-xs text-muted-foreground">{{ opt.hint }}</span>
              </span>
            </label>
          </div>
        </fieldset>

        <!-- 2. Selection — which tickets are eligible, and how many -->
        <div class="space-y-2">
          <h3 class="text-sm font-medium">Which tickets</h3>
          <div class="grid gap-3 sm:grid-cols-2">
            <div class="space-y-1.5">
              <Label for="epic-min-severity">Min severity</Label>
              <Select
                :model-value="minSeverity"
                @update:model-value="minSeverity = $event as string"
              >
                <SelectTrigger id="epic-min-severity" class="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem :value="ANY">Any severity</SelectItem>
                  <SelectItem v-for="s in SEVERITY_OPTIONS" :key="s" :value="s">
                    {{ s }} and above
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label for="epic-min-priority">Min priority</Label>
              <Select
                :model-value="minPriority"
                @update:model-value="minPriority = $event as string"
              >
                <SelectTrigger id="epic-min-priority" class="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem :value="ANY">Any priority</SelectItem>
                  <SelectItem v-for="p in PRIORITY_OPTIONS" :key="p" :value="p">
                    {{ p }} and above
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label for="epic-limit">Top X tickets</Label>
              <Input
                id="epic-limit"
                type="number"
                inputmode="numeric"
                min="0"
                :model-value="nums.limit"
                @update:model-value="setNumber('limit', $event)"
              />
              <p class="text-xs text-muted-foreground">0 takes every match.</p>
            </div>
            <div class="space-y-1.5">
              <Label for="epic-sort">Take the top by</Label>
              <Select :model-value="sort" @update:model-value="sort = $event as string">
                <SelectTrigger id="epic-sort" class="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">
                    {{ o.label }}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <fieldset class="space-y-1.5">
            <legend class="text-sm font-medium">Status</legend>
            <div class="flex flex-wrap gap-x-4 gap-y-2">
              <label
                v-for="opt in STATUS_OPTIONS"
                :key="opt.value"
                class="flex cursor-pointer items-center gap-2 text-sm"
              >
                <input
                  type="checkbox"
                  class="size-4 cursor-pointer accent-primary"
                  :checked="statuses.includes(opt.value)"
                  @change="statuses = toggleStatus(statuses, opt.value)"
                />
                {{ opt.label }}
              </label>
            </div>
          </fieldset>
        </div>

        <!-- 3. Partition — how the selection is cut up -->
        <div class="space-y-2">
          <h3 class="text-sm font-medium">How to cut them</h3>
          <div class="grid gap-3 sm:grid-cols-3">
            <div class="space-y-1.5">
              <Label for="epic-max-epics">…in Y runs</Label>
              <Input
                id="epic-max-epics"
                type="number"
                inputmode="numeric"
                min="0"
                :model-value="nums.maxEpics"
                @update:model-value="setNumber('maxEpics', $event)"
              />
              <p class="text-xs text-muted-foreground">0 keeps every epic.</p>
            </div>
            <div class="space-y-1.5">
              <Label for="epic-min-members">Min members</Label>
              <Input
                id="epic-min-members"
                type="number"
                inputmode="numeric"
                min="2"
                :model-value="nums.minMembers"
                @update:model-value="setNumber('minMembers', $event)"
              />
            </div>
            <div class="space-y-1.5">
              <Label for="epic-max-members">Max members</Label>
              <Input
                id="epic-max-members"
                type="number"
                inputmode="numeric"
                min="2"
                :model-value="nums.maxMembers"
                @update:model-value="setNumber('maxMembers', $event)"
              />
            </div>
          </div>
          <p class="text-xs text-muted-foreground">
            Groups larger than the maximum are split into near-equal epics, not truncated.
          </p>
        </div>

        <!-- 4. Live preview of the exact plan Create would persist -->
        <section aria-label="Plan preview" aria-live="polite" class="space-y-2 rounded-md border bg-muted/40 p-3">
          <div class="flex items-center gap-2">
            <h3 class="text-sm font-medium">Plan</h3>
            <Loader2 v-if="previewing" class="size-3.5 animate-spin text-muted-foreground" />
            <span v-if="previewing" class="text-xs text-muted-foreground">Previewing…</span>
          </div>

          <p v-if="validationError" class="text-sm text-muted-foreground">
            {{ validationError }}
          </p>

          <template v-else-if="previewError">
            <p class="text-sm text-destructive">Couldn't preview this plan — {{ previewError }}</p>
            <Button variant="outline" size="sm" @click="runPreview()">Retry</Button>
          </template>

          <p v-else-if="!summary" class="text-sm text-muted-foreground">
            Working out which tickets qualify…
          </p>

          <div v-else :class="previewing ? 'opacity-60 transition-opacity' : ''">
            <p class="text-sm font-medium tabular-nums">{{ summaryLine }}</p>
            <p class="text-xs text-muted-foreground">
              from {{ plural(summary.candidates, 'candidate', 'candidates') }}
              <template v-if="summary.unplaced">
                · <span class="text-warn">{{ summary.unplaced }} left out</span>
              </template>
            </p>
            <p v-if="cappedFrom" class="pt-1 text-xs text-muted-foreground">
              Capped at {{ nums.maxEpics }} — {{ cappedFrom }} epics qualified; the strongest were
              kept.
            </p>

            <p v-if="emptyReason" class="pt-1.5 text-sm text-muted-foreground">
              {{ emptyReason }}
            </p>

            <ul v-else class="mt-2 divide-y rounded-md border bg-background">
              <!-- Preview drafts are unpersisted: there is no `uid` to key on. -->
              <li v-for="(d, i) in drafts" :key="`${i}-${d.title}`" class="p-2">
                <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span class="min-w-0 flex-1 truncate text-sm font-medium">
                    {{ displayTitle(d) }}
                  </span>
                  <span class="shrink-0 whitespace-nowrap text-xs tabular-nums text-muted-foreground">
                    {{ plural(d.member_ticket_uids.length, 'ticket', 'tickets') }}
                  </span>
                  <Badge
                    :variant="priorityVariant(d.suggested_priority as TicketPriority)"
                    class="shrink-0 px-1.5 text-[10px]"
                  >
                    {{ d.suggested_priority }}
                  </Badge>
                </div>
                <p
                  v-if="evidenceLine(d)"
                  class="truncate font-mono text-xs text-muted-foreground"
                  :title="evidenceLine(d)"
                >
                  {{ evidenceLine(d) }}
                </p>
              </li>
            </ul>
          </div>
        </section>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!canCreate" :loading="creating" @click="create">
          Create {{ drafts.length ? plural(drafts.length, 'epic', 'epics') : 'epics' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
