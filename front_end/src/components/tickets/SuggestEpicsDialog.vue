<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
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
import {
  ANY,
  PRIORITY_OPTIONS,
  SEVERITY_OPTIONS,
  SORT_OPTIONS,
  STATUS_OPTIONS,
  floorValue,
  toggleStatus,
} from '@/components/tickets/epicSelection'
import type { EpicAgentAxis, SuggestEpicsRequest, TicketStatus } from '@/types/api'

interface Props {
  open: boolean
  repositoryUid: string
}
const props = defineProps<Props>()
const emit = defineEmits<{ 'update:open': [value: boolean]; dispatched: [] }>()

const store = useTicketStore()
const toast = useToast()

/**
 * The three axes a model is actually needed for. The six computable ones are
 * absent by design: the platform derives those exactly and instantly from the
 * rule builder, so asking a run for them costs a review for a grouping you
 * could have had for free.
 */
interface GoalOption {
  value: EpicAgentAxis
  label: string
  hint: string
}

const GOAL_OPTIONS: GoalOption[] = [
  {
    value: 'root-cause',
    label: 'Same root cause',
    hint: 'one defect showing up in several places — fix the cause once, every symptom goes',
  },
  {
    value: 'theme',
    label: 'Same feature or area',
    hint: 'one capability or surface: different fixes, but one coherent piece of work',
  },
  {
    value: 'co-change',
    label: 'Fixes touch the same code',
    hint: 'unrelated problems whose changes would collide on separate branches',
  },
]

const AGGRESSIVENESS_OPTIONS = [
  { value: 'conservative', label: 'Conservative', hint: 'up to 3 epics; only what is explicit in the code' },
  { value: 'balanced', label: 'Balanced', hint: 'up to 6 epics; anything it can point at code for' },
  { value: 'exhaustive', label: 'Exhaustive', hint: 'up to 10 epics; you are the filter, not its caution' },
]

// ── Controls ────────────────────────────────────────────────────────────────

const goals = ref<EpicAgentAxis[]>(['root-cause', 'theme'])
const aggressiveness = ref('balanced')
const minSeverity = ref<string>(ANY)
const minPriority = ref<string>(ANY)
const statuses = ref<TicketStatus[]>(['backlog', 'todo'])
const sort = ref('priority')

/** In a reactive object for the same reason as the rule builder: `<script
 *  setup>` unwraps refs in the template, so a shared setter would receive the
 *  number rather than the ref it must write back to. */
const nums = reactive({ limit: 40 })

function toggleGoal(goal: EpicAgentAxis) {
  const next = goals.value.includes(goal)
    ? goals.value.filter((g) => g !== goal)
    : [...goals.value, goal]
  // Fixed order so the prompt's grounds are listed the same way every run.
  goals.value = GOAL_OPTIONS.map((o) => o.value).filter((g) => next.includes(g))
}

function setLimit(raw: string | number) {
  const n = typeof raw === 'number' ? raw : Number.parseInt(raw, 10)
  if (Number.isNaN(n)) return
  nums.limit = Math.max(0, n)
}

const validationError = computed(() => {
  if (!props.repositoryUid) return 'Pick a repository first.'
  if (!goals.value.length) return 'Pick at least one thing to group on.'
  if (!statuses.value.length) return 'Pick at least one status — otherwise nothing is eligible.'
  return ''
})

const request = computed<SuggestEpicsRequest>(() => ({
  repository_uid: props.repositoryUid,
  goals: [...goals.value],
  aggressiveness: aggressiveness.value,
  statuses: [...statuses.value],
  min_priority: floorValue(minPriority.value),
  min_severity: floorValue(minSeverity.value),
  limit: nums.limit,
  sort: sort.value,
}))

// ── Dispatch ────────────────────────────────────────────────────────────────

const dispatching = ref(false)

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

// Reopening after a failed dispatch should not resume the spinner.
watch(
  () => props.open,
  (open) => {
    if (open) dispatching.value = false
  },
)

async function dispatch() {
  if (validationError.value || dispatching.value) return
  dispatching.value = true
  try {
    const result = await store.suggestEpics(request.value)
    const runUid = typeof result.run_uid === 'string' ? result.run_uid : ''
    const seeded = result.seeded_cluster_count ?? 0
    toast.success(
      'Grouping run dispatched',
      `Analyzing ${result.candidate_count ?? '?'} work items` +
        // Prior art is the difference between "it found nothing new" and "it
        // had nothing to work from" when the proposals come back thin.
        (seeded ? ` against ${seeded} computed grouping${seeded === 1 ? '' : 's'}` : ' from scratch') +
        `${runUid ? ` · run ${runUid.slice(0, 8)}` : ''} — proposals appear here for approval.`,
    )
    emit('dispatched')
    emit('update:open', false)
  } catch (e) {
    toast.error('Could not dispatch grouping run', errorText(e))
  } finally {
    dispatching.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>Suggest epics (agent run)</DialogTitle>
        <DialogDescription>
          A read-only run reads the code behind these work items and proposes which of them one
          implement run should fix together. It is shown the groupings the platform already
          computes, so it spends its judgment on what arithmetic cannot see. Nothing changes until
          you approve a proposal.
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="space-y-5">
        <!-- 1. Goals — what "belongs together" may mean for this run -->
        <fieldset class="space-y-2">
          <legend class="text-sm font-medium">Group on</legend>
          <div class="grid gap-1.5">
            <label
              v-for="opt in GOAL_OPTIONS"
              :key="opt.value"
              class="flex cursor-pointer items-start gap-2.5 rounded-md border p-2.5 transition-colors hover:bg-accent"
              :class="goals.includes(opt.value) ? 'border-primary bg-primary/5' : ''"
            >
              <input
                type="checkbox"
                class="mt-0.5 size-4 shrink-0 cursor-pointer accent-primary"
                :checked="goals.includes(opt.value)"
                @change="toggleGoal(opt.value)"
              />
              <span class="min-w-0">
                <span class="block text-sm font-medium">{{ opt.label }}</span>
                <span class="block text-xs text-muted-foreground">{{ opt.hint }}</span>
              </span>
            </label>
          </div>
          <p class="text-xs text-muted-foreground">
            Exact groupings by subsystem, feature, overlapping files or finding class need no
            agent — use “Build epics by rule…” for those, and they answer immediately.
          </p>
        </fieldset>

        <!-- 2. How readily to group -->
        <fieldset class="space-y-1.5">
          <legend class="text-sm font-medium">How readily</legend>
          <div class="grid gap-1.5 sm:grid-cols-3">
            <label
              v-for="opt in AGGRESSIVENESS_OPTIONS"
              :key="opt.value"
              class="flex cursor-pointer items-start gap-2 rounded-md border p-2 transition-colors hover:bg-accent"
              :class="aggressiveness === opt.value ? 'border-primary bg-primary/5' : ''"
            >
              <input
                type="radio"
                name="epic-aggressiveness"
                class="mt-0.5 size-4 shrink-0 cursor-pointer accent-primary"
                :value="opt.value"
                :checked="aggressiveness === opt.value"
                @change="aggressiveness = opt.value"
              />
              <span class="min-w-0">
                <span class="block text-sm font-medium">{{ opt.label }}</span>
                <span class="block text-xs text-muted-foreground">{{ opt.hint }}</span>
              </span>
            </label>
          </div>
          <p class="text-xs text-muted-foreground">
            A wrong epic costs one click to reject; a missed one costs a whole extra PR.
          </p>
        </fieldset>

        <!-- 3. Selection — which work items the run even sees -->
        <div class="space-y-2">
          <h3 class="text-sm font-medium">Which work items</h3>
          <div class="grid gap-3 sm:grid-cols-2">
            <div class="space-y-1.5">
              <Label for="suggest-min-severity">Min severity</Label>
              <Select
                :model-value="minSeverity"
                @update:model-value="minSeverity = $event as string"
              >
                <SelectTrigger id="suggest-min-severity" class="w-full">
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
              <Label for="suggest-min-priority">Min priority</Label>
              <Select
                :model-value="minPriority"
                @update:model-value="minPriority = $event as string"
              >
                <SelectTrigger id="suggest-min-priority" class="w-full">
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
              <Label for="suggest-limit">Top X work items</Label>
              <Input
                id="suggest-limit"
                type="number"
                inputmode="numeric"
                min="0"
                :model-value="nums.limit"
                @update:model-value="setLimit($event)"
              />
              <p class="text-xs text-muted-foreground">
                0 takes every match — on a large board that is a very long prompt.
              </p>
            </div>
            <div class="space-y-1.5">
              <Label for="suggest-sort">Take the top by</Label>
              <Select :model-value="sort" @update:model-value="sort = $event as string">
                <SelectTrigger id="suggest-sort" class="w-full">
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
          <p class="text-xs text-muted-foreground">
            Work items already inside an epic are never candidates — one item in two epics means
            two branches editing the same code.
          </p>
        </div>

        <p v-if="validationError" class="text-sm text-muted-foreground">{{ validationError }}</p>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button
          size="sm"
          :disabled="!!validationError || dispatching"
          :loading="dispatching"
          @click="dispatch"
        >
          Dispatch grouping run
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
