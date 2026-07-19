<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  CheckCircle2,
  ClipboardList,
  ExternalLink,
  Gavel,
  Link2,
  ListChecks,
  MessagesSquare,
  MoreHorizontal,
  RefreshCw,
  RotateCcw,
  Search,
  SquareKanban,
  Target,
  Wrench,
} from 'lucide-vue-next'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { extractDispatchConflict, useActiveRuns } from '@/composables/useActiveRuns'
import { useDiscussions } from '@/composables/useDiscussions'
import { useDiscussInRun } from '@/composables/useDiscussInRun'
import { ApiError } from '@/services/api'
import ActionMenuBar from '@/components/workitem/ActionMenuBar.vue'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
import CiStateBadge from '@/components/delivery/CiStateBadge.vue'
import TestLocallyButton from '@/components/delivery/TestLocallyButton.vue'
import ConvergenceChecklist from '@/components/delivery/ConvergenceChecklist.vue'
import ResolutionCard from '@/components/delivery/ResolutionCard.vue'
import VerdictCard from '@/components/delivery/VerdictCard.vue'
import LinkTicketDialog from '@/components/tickets/LinkTicketDialog.vue'
import ActiveRunChip from '@/components/runs/ActiveRunChip.vue'
import DiscussionChip from '@/components/runs/DiscussionChip.vue'
import CommentThread from '@/components/comments/CommentThread.vue'
import type {
  FindingResolutionDTO,
  MergePolicyDTO,
  PullRequestDTO,
  ReviewDepth,
  VerdictDTO,
  VerdictResult,
} from '@/types/api'

const route = useRoute()
const delivery = useDeliveryStore()
const ticketStore = useTicketStore()
const toast = useToast()

const pr = ref<PullRequestDTO | null>(null)
const verdict = ref<VerdictDTO | null>(null)
const resolutions = ref<FindingResolutionDTO[]>([])
const policy = ref<MergePolicyDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const syncing = ref(false)
const recomputing = ref(false)
const reviewing = ref(false)
const fixing = ref(false)
const resettingFixRounds = ref(false)
const linkTicketOpen = ref(false)
/** Blocking-resolution finding_uids pre-selected to scope the next fix run. */
const fixSelection = ref<Set<string>>(new Set())

// ── Review options (depth dial + numeric budget, §B) ────────────────────────
const reviewDialogOpen = ref(false)
const reviewDepth = ref<ReviewDepth>('normal')
/** Empty = depth default (quick→5, normal/deep→uncapped). */
const reviewMaxFindings = ref('')
const reviewFull = ref(false)

const DEPTH_OPTIONS = [
  { label: 'Quick — top blocking issues only', value: 'quick' },
  { label: 'Normal — everything defensible', value: 'normal' },
  { label: 'Deep — exhaustive, lens by lens', value: 'deep' },
]

// ── Human verdict (maintainer records/overrides a SHA-bound verdict) ─────────
const verdictDialogOpen = ref(false)
const submittingVerdict = ref(false)
const verdictResult = ref<VerdictResult>('approve')
const verdictSha = ref('')
const verdictBlocking = ref('')

const VERDICT_OPTIONS = [
  { label: 'Approve — mergeable at this head', value: 'approve' },
  { label: 'Request changes — blocking work remains', value: 'request_changes' },
  { label: 'Needs human — escalate, auto-loop stops', value: 'needs_human' },
]

const DEPTH_HELP: Record<ReviewDepth, string> = {
  quick: 'Precision over recall: at most 5 findings (or your budget), blocking-severity only. What auto-reviews use.',
  normal: 'No cap — every issue the reviewer can defend with concrete evidence.',
  deep: 'All lenses (correctness, security, API, performance, tests, maintainability) with subagent fan-out. Always reviews the full diff.',
}

// Embeddable in WorkItemView: an explicit uid prop wins over the route param.
const props = defineProps<{ uid?: string }>()
const uid = computed(() => props.uid || String(route.params.uid))

// In-flight runs targeting this PR — surface a "view run" chip and gate the
// dispatch buttons per the overlap rules: a review is blocked only by another
// review, a fix only by another write run (implement/fix). Chat runs never
// gate anything; open discussions get their own non-blocking chip. Polls
// every ~5s while a run is live, stops on terminal.
const { workRuns, activeRun, noteDispatched } = useActiveRuns(() =>
  pr.value ? { pull_request_uid: pr.value.uid } : null,
)
const { discussions } = useDiscussions(() =>
  pr.value ? { linked_pr_uid: pr.value.uid } : null,
)
const { discussing, discuss: discussInRun } = useDiscussInRun(() =>
  pr.value
    ? {
        repository_uid: pr.value.repository_uid,
        title: `PR #${pr.value.github_number}: ${pr.value.title || '(untitled)'}`,
        linked_pr_uid: pr.value.uid,
      }
    : null,
)
const reviewInFlight = computed(() => workRuns.value.some((r) => r.playbook === 'review'))
const writeInFlight = computed(() =>
  workRuns.value.some((r) => r.playbook === 'implement' || r.playbook === 'fix'),
)

async function load() {
  loading.value = true
  error.value = null
  try {
    const [p, v, rs] = await Promise.all([
      delivery.getPullRequest(uid.value),
      delivery.getLatestVerdict(uid.value),
      delivery.fetchResolutions(uid.value),
    ])
    pr.value = p
    verdict.value = v
    resolutions.value = rs
    fixSelection.value = new Set()
    window.dispatchEvent(new CustomEvent('workitem:changed'))
    try {
      policy.value = await delivery.getMergePolicy(p.repository_uid)
    } catch {
      policy.value = null // fix-round bound stays unknown; the button still works
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(uid, () => void load())

/** Title of the bound ticket — makes the header chip readable, not just a uid. */
const ticketTitle = ref('')
watch(
  () => pr.value?.ticket_uid,
  async (ticketUid) => {
    ticketTitle.value = ''
    if (!ticketUid) return
    try {
      ticketTitle.value = (await ticketStore.getTicket(ticketUid)).title
    } catch {
      /* chip falls back to the short uid */
    }
  },
  { immediate: true },
)

const stateVariant = computed<BadgeVariants['variant']>(() => {
  switch (pr.value?.state) {
    case 'merged':
      return 'success'
    case 'open':
      return 'info'
    default:
      return 'default'
  }
})

function ciConclusionVariant(conclusion: string | null | undefined): BadgeVariants['variant'] {
  if (conclusion === 'success') return 'success'
  if (conclusion) return 'destructive'
  return 'warn'
}

const SEV_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 }
const sortedResolutions = computed(() =>
  [...resolutions.value].sort((a, b) => {
    if (a.blocking !== b.blocking) return a.blocking ? -1 : 1
    const sev = (SEV_RANK[a.finding_severity] ?? 9) - (SEV_RANK[b.finding_severity] ?? 9)
    if (sev !== 0) return sev
    return a.state.localeCompare(b.state)
  }),
)

// ── Fix runs (write path, bounded by MergePolicy.max_fix_rounds) ────────────

const blockingResolutions = computed(() => resolutions.value.filter((r) => r.blocking))

const fixRoundsExhausted = computed(
  () => policy.value !== null && (pr.value?.fix_rounds ?? 0) >= policy.value.max_fix_rounds,
)

const canDispatchFix = computed(
  () =>
    pr.value?.state === 'open' &&
    blockingResolutions.value.length > 0 &&
    !fixRoundsExhausted.value &&
    !writeInFlight.value,
)

const fixHint = computed(() => {
  if (writeInFlight.value) return 'A write run is already in flight for this PR — wait for it to finish.'
  if (fixRoundsExhausted.value) return 'Fix rounds exhausted — human required.'
  if (pr.value && pr.value.state !== 'open') return 'Fix runs only target open PRs.'
  if (blockingResolutions.value.length === 0) return 'Nothing blocking — nothing to fix.'
  return null
})

function toggleFixSelection(findingUid: string, value: boolean) {
  const next = new Set(fixSelection.value)
  if (value) next.add(findingUid)
  else next.delete(findingUid)
  fixSelection.value = next
}

async function dispatchFix() {
  if (!pr.value || fixing.value || !canDispatchFix.value) return
  fixing.value = true
  try {
    const scoped = blockingResolutions.value
      .map((r) => r.finding_uid)
      .filter((f) => fixSelection.value.has(f))
    const dispatch = await delivery.triggerFix(pr.value.uid, scoped.length ? scoped : undefined)
    const runUid = typeof dispatch.run_uid === 'string' ? dispatch.run_uid : ''
    toast.success(
      'Fix run dispatched',
      [
        runUid ? `run ${runUid.slice(0, 8)}` : null,
        scoped.length ? `scoped to ${scoped.length} finding${scoped.length === 1 ? '' : 's'}` : 'all blocking findings',
      ].filter(Boolean).join(' · '),
      runUid ? { label: 'View run', to: { name: 'run-detail', params: { uid: runUid } } } : undefined,
    )
    noteDispatched({
      run_uid: runUid || undefined,
      scheduled_agent_uid: typeof dispatch.scheduled_agent_uid === 'string' ? dispatch.scheduled_agent_uid : undefined,
      title: `Fix PR #${pr.value.github_number}`,
      playbook: 'fix',
    })
    fixSelection.value = new Set()
    // The dispatch bumps fix_rounds server-side — refresh the header quietly.
    try {
      pr.value = await delivery.getPullRequest(uid.value)
    } catch {
      /* keep the stale header rather than erroring the whole page */
    }
  } catch (e) {
    const conflict = extractDispatchConflict(e)
    if (conflict) {
      toast.error('Can’t dispatch fix run', conflict.message, {
        label: 'View blocking run',
        to: { name: 'run-detail', params: { uid: conflict.run_uid } },
      })
      noteDispatched(conflict)
      return
    }
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error(e instanceof ApiError && e.status === 409 ? 'Can’t dispatch fix run' : 'Fix run failed', msg)
  } finally {
    fixing.value = false
  }
}

async function resetFixRounds() {
  if (!pr.value || resettingFixRounds.value) return
  resettingFixRounds.value = true
  try {
    pr.value = await delivery.resetFixRounds(pr.value.uid)
    toast.success('Fix rounds reset', 'Automated fix runs are unlocked again for this PR.')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t reset fix rounds', msg)
  } finally {
    resettingFixRounds.value = false
  }
}

async function resync() {
  if (!pr.value || syncing.value) return
  syncing.value = true
  try {
    pr.value = await delivery.syncPullRequest(pr.value.repository_uid, pr.value.github_number)
    const [v, rs] = await Promise.all([
      delivery.getLatestVerdict(uid.value),
      delivery.fetchResolutions(uid.value),
    ])
    verdict.value = v
    resolutions.value = rs
    toast.success('PR re-synced', `head @${pr.value.head_sha.slice(0, 10)}`)
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Re-sync failed', msg)
  } finally {
    syncing.value = false
  }
}

async function recompute() {
  if (!pr.value || recomputing.value) return
  recomputing.value = true
  try {
    const state = await delivery.recompute(pr.value.uid)
    pr.value = { ...pr.value, converged: state.converged, convergence: state }
    toast.success(
      state.converged ? 'Converged' : 'Not converged',
      state.converged ? 'All four predicate conditions hold.' : state.reasons.join(' · '),
    )
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Recompute failed', msg)
  } finally {
    recomputing.value = false
  }
}

function openReviewDialog() {
  if (!pr.value || reviewing.value || reviewInFlight.value) return
  reviewDialogOpen.value = true
}

async function requestReview() {
  if (!pr.value || reviewing.value || reviewInFlight.value) return
  reviewing.value = true
  try {
    const budget = Number.parseInt(reviewMaxFindings.value, 10)
    const dispatch = await delivery.triggerReview(pr.value.uid, {
      depth: reviewDepth.value,
      full: reviewFull.value,
      max_findings: Number.isFinite(budget) && budget > 0 ? budget : undefined,
    })
    reviewDialogOpen.value = false
    toast.success(
      'Review run dispatched',
      [
        `run ${dispatch.run_uid.slice(0, 8)} @ ${dispatch.head_sha.slice(0, 10)}`,
        dispatch.depth ? `depth ${dispatch.depth}` : null,
        dispatch.incremental_from
          ? `incremental since ${dispatch.incremental_from.slice(0, 10)}`
          : null,
      ].filter(Boolean).join(' · '),
      { label: 'View run', to: { name: 'run-detail', params: { uid: dispatch.run_uid } } },
    )
    noteDispatched({
      run_uid: dispatch.run_uid,
      scheduled_agent_uid: dispatch.scheduled_agent_uid,
      title: `Review PR #${pr.value.github_number}`,
      playbook: 'review',
    })
  } catch (e) {
    const conflict = extractDispatchConflict(e)
    if (conflict) {
      reviewDialogOpen.value = false
      toast.error('Couldn’t request review', conflict.message, {
        label: 'View blocking run',
        to: { name: 'run-detail', params: { uid: conflict.run_uid } },
      })
      noteDispatched(conflict)
      return
    }
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t request review', msg)
  } finally {
    reviewing.value = false
  }
}

function onTicketLinked(updated: PullRequestDTO) {
  pr.value = updated
}

function openVerdictDialog() {
  if (!pr.value) return
  verdictSha.value = pr.value.head_sha || ''
  verdictResult.value = 'approve'
  verdictBlocking.value = ''
  verdictDialogOpen.value = true
}

async function submitVerdict() {
  if (!pr.value || submittingVerdict.value) return
  const sha = verdictSha.value.trim()
  if (sha.length < 7) {
    toast.warn('SHA required', 'Enter the commit SHA (at least 7 chars) this verdict is bound to.')
    return
  }
  submittingVerdict.value = true
  try {
    const blocking = Number.parseInt(verdictBlocking.value, 10)
    verdict.value = await delivery.submitVerdict(pr.value.uid, {
      sha,
      result: verdictResult.value,
      new_blocking_findings: Number.isFinite(blocking) && blocking > 0 ? blocking : 0,
    })
    verdictDialogOpen.value = false
    toast.success('Verdict recorded', `Bound to ${sha.slice(0, 10)} · ${verdictResult.value}`)
    // A fresh verdict recomputes convergence server-side — refresh the header.
    try {
      pr.value = await delivery.getPullRequest(uid.value)
    } catch {
      /* keep the stale header rather than erroring the whole page */
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t submit verdict', msg)
  } finally {
    submittingVerdict.value = false
  }
}

async function onResolutionUpdated(updated: FindingResolutionDTO) {
  resolutions.value = resolutions.value.map((r) => (r.uid === updated.uid ? updated : r))
  // Every ledger transition recomputes convergence server-side — refresh the PR view.
  try {
    pr.value = await delivery.getPullRequest(uid.value)
  } catch {
    /* keep the stale header rather than erroring the whole page */
  }
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !pr">
      <Skeleton class="h-12 w-2/3" />
      <div class="grid grid-cols-1 gap-4 items-start lg:grid-cols-[1fr_360px]">
        <div class="space-y-4">
          <Skeleton class="h-48" />
          <Skeleton class="h-48" />
        </div>
        <div class="space-y-4">
          <Skeleton class="h-40" />
          <Skeleton class="h-48" />
        </div>
      </div>
    </template>

    <ErrorState v-else-if="error && !pr" title="Couldn't load pull request" :message="error">
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <template v-else-if="pr">
      <!-- Identity (title, state, CI, branch) lives in WorkItemView's unified
           header — this bar is ONLY actions. -->
      <ActionMenuBar>
        <Button
          size="sm"
          :loading="reviewing"
          :disabled="pr.state !== 'open' || !pr.head_sha || reviewInFlight"
          :title="reviewInFlight ? 'A review run is already in flight for this PR' : undefined"
          @click="openReviewDialog"
        >
          <Search /> Request review
        </Button>
        <TestLocallyButton :branch="pr.head_ref" :pr-number="pr.github_number" />
        <Button variant="ghost" size="sm" :loading="discussing" @click="discussInRun">
          <MessagesSquare /> Discuss
        </Button>
        <Button v-if="pr.url" as="a" :href="pr.url" target="_blank" rel="noopener" variant="ghost" size="sm">
          <ExternalLink /> GitHub
        </Button>
        <button
          v-if="!pr.ticket_uid"
          type="button"
          class="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          @click="linkTicketOpen = true"
        >
          <Link2 class="size-3" /> Link ticket
        </button>
        <!-- Maintenance actions — needed rarely, kept out of the primary row. -->
        <DropdownMenu>
          <DropdownMenuTrigger as-child>
            <Button
              variant="ghost"
              size="icon-sm"
              class="size-8"
              title="More actions"
              :loading="syncing || recomputing"
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" class="w-48">
            <DropdownMenuItem :disabled="syncing" @select="resync">
              <RefreshCw /> Re-sync from GitHub
            </DropdownMenuItem>
            <DropdownMenuItem :disabled="recomputing" @select="recompute">
              <Target /> Recompute convergence
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <template #trailing>
          <span v-if="pr.author || pr.head_sha" class="text-xs text-muted-foreground">
            <template v-if="pr.author">{{ pr.author }}</template>
            <template v-if="pr.head_sha"> · head <span class="font-mono">{{ pr.head_sha.slice(0, 10) }}</span></template>
          </span>
          <DiscussionChip v-for="chat in discussions" :key="chat.uid" :run="chat" />
          <ActiveRunChip v-if="activeRun" :run="activeRun" />
        </template>
      </ActionMenuBar>

      <Dialog :open="reviewDialogOpen" @update:open="reviewDialogOpen = $event">
        <DialogContent class="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Request review</DialogTitle>
            <DialogDescription>
              One read-only run over this PR's diff, ending in a SHA-bound verdict.
            </DialogDescription>
          </DialogHeader>
          <div class="space-y-4">
            <div class="space-y-1.5">
              <Label>Depth</Label>
              <Select :model-value="reviewDepth" @update:model-value="reviewDepth = $event as ReviewDepth">
                <SelectTrigger class="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in DEPTH_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
                </SelectContent>
              </Select>
              <p class="text-xs text-muted-foreground">{{ DEPTH_HELP[reviewDepth] }}</p>
            </div>
            <div class="space-y-1.5">
              <Label>Finding budget</Label>
              <Input
                v-model="reviewMaxFindings"
                type="number"
                min="1"
                max="50"
                :placeholder="reviewDepth === 'quick' ? '5 (depth default)' : 'no cap (depth default)'"
              />
              <p class="text-xs text-muted-foreground">
                Cap on findings this run may file, ranked by severity × confidence. Leave empty for
                the depth's default.
              </p>
            </div>
            <div class="flex items-center justify-between gap-3" v-if="reviewDepth !== 'deep'">
              <div>
                <p class="text-xs font-medium text-foreground">Full review</p>
                <p class="text-xs text-muted-foreground">
                  Review the whole diff even when a prior verdict allows an incremental pass over
                  the new commits only.
                </p>
              </div>
              <Switch v-model="reviewFull" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" @click="reviewDialogOpen = false">Cancel</Button>
            <Button :loading="reviewing" @click="requestReview">
              <Search /> Dispatch review
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog :open="verdictDialogOpen" @update:open="verdictDialogOpen = $event">
        <DialogContent class="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Submit verdict</DialogTitle>
            <DialogDescription>
              Record or override a SHA-bound verdict by hand — recomputes convergence.
            </DialogDescription>
          </DialogHeader>
          <div class="space-y-4">
            <div class="space-y-1.5">
              <Label>Result</Label>
              <Select :model-value="verdictResult" @update:model-value="verdictResult = $event as VerdictResult">
                <SelectTrigger class="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in VERDICT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label>Head SHA</Label>
              <Input v-model="verdictSha" placeholder="commit sha this verdict is bound to" />
              <p class="text-xs text-muted-foreground">
                Defaults to the current head. A verdict only counts as fresh at the PR's head sha.
              </p>
            </div>
            <div class="space-y-1.5">
              <Label>New blocking findings</Label>
              <Input
                v-model="verdictBlocking"
                type="number"
                min="0"
                placeholder="0"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" @click="verdictDialogOpen = false">Cancel</Button>
            <Button :loading="submittingVerdict" @click="submitVerdict">
              <Gavel /> Submit verdict
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <div class="grid grid-cols-1 gap-4 items-start lg:grid-cols-[1fr_360px]">
        <!-- ── Main column ─────────────────────────────────────────────── -->
        <div class="space-y-4 min-w-0">
          <!-- Findings ledger / triage -->
          <Card>
            <CardHeader class="flex-col gap-3 sm:flex-row sm:items-center sm:justify-between space-y-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <ClipboardList class="h-4 w-4 text-muted-foreground" />
                Findings ledger
                <span class="text-muted-foreground text-xs font-normal">· {{ resolutions.length }}</span>
              </CardTitle>
              <div class="flex flex-wrap items-center gap-3">
                <span v-if="pr.convergence" class="text-xs text-muted-foreground">
                  {{ pr.convergence.counts.blocking }} blocking · {{ pr.convergence.counts.deferred }} deferred ·
                  {{ pr.convergence.counts.waived }} waived · {{ pr.convergence.counts.info }} info
                </span>
                <span class="text-xs" :class="fixRoundsExhausted ? 'text-destructive font-medium' : 'text-muted-foreground'">
                  Fix rounds: {{ pr.fix_rounds }}<template v-if="policy">/{{ policy.max_fix_rounds }}</template>
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  :disabled="!canDispatchFix"
                  :loading="fixing"
                  :title="fixHint ?? undefined"
                  @click="dispatchFix"
                >
                  <Wrench />
                  Dispatch fix run
                  <template v-if="fixSelection.size"> · {{ fixSelection.size }}</template>
                </Button>
              </div>
            </CardHeader>
            <CardContent class="p-0">
              <div
                v-if="fixHint"
                class="border-b px-5 py-2 text-xs"
                :class="fixRoundsExhausted ? 'text-destructive' : 'text-muted-foreground'"
              >
                {{ fixHint }}
                <template v-if="fixRoundsExhausted || pr.fix_rounds_exhausted">
                  This PR used all {{ policy?.max_fix_rounds }} automated fix rounds — triage the remaining
                  blocking findings yourself or raise the bound in the repository's merge policy.
                  <div class="mt-2">
                    <Button
                      variant="outline"
                      size="sm"
                      :loading="resettingFixRounds"
                      @click="resetFixRounds"
                    >
                      <RotateCcw /> Reset fix rounds
                    </Button>
                  </div>
                </template>
              </div>
              <div
                v-else-if="blockingResolutions.length"
                class="border-b px-5 py-2 text-xs text-muted-foreground"
              >
                Tick blocking findings to scope the fix run — leave everything unticked to target all of them.
              </div>
              <div v-if="resolutions.length === 0" class="p-5 text-sm text-muted-foreground">
                No findings are bound to this PR yet. Review runs bind their findings here as resolutions.
              </div>
              <div v-else class="divide-y divide-border">
                <ResolutionCard
                  v-for="r in sortedResolutions"
                  :key="r.uid"
                  :resolution="r"
                  :selectable="r.blocking && canDispatchFix"
                  :selected="fixSelection.has(r.finding_uid)"
                  @select="toggleFixSelection(r.finding_uid, $event)"
                  @updated="onResolutionUpdated"
                />
              </div>
            </CardContent>
          </Card>

          <!-- Latest verdict -->
          <Card>
            <CardHeader class="flex-row items-center justify-between gap-3 space-y-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <Gavel class="h-4 w-4 text-muted-foreground" />
                Latest verdict
              </CardTitle>
              <Button variant="outline" size="sm" @click="openVerdictDialog">
                <Gavel /> Submit verdict
              </Button>
            </CardHeader>
            <CardContent>
              <VerdictCard v-if="verdict" :verdict="verdict" :head-sha="pr.head_sha" />
              <div v-else class="text-sm text-muted-foreground">
                No verdict recorded yet. Request a review to get a SHA-bound verdict at the current head.
              </div>
            </CardContent>
          </Card>

          <!-- CI checks -->
          <Card>
            <CardHeader class="flex-row items-center justify-between gap-3 space-y-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <ListChecks class="h-4 w-4 text-muted-foreground" />
                CI checks
                <span class="text-muted-foreground text-xs font-normal">· {{ pr.ci_checks.length }}</span>
              </CardTitle>
              <CiStateBadge :state="pr.ci_state" />
            </CardHeader>
            <CardContent class="p-0">
              <div v-if="pr.ci_checks.length === 0" class="p-5 text-sm text-muted-foreground">
                No check runs on this head — an empty rollup is <em>not</em> green.
              </div>
              <div v-else class="overflow-x-auto">
                <table class="w-full text-sm">
                  <thead>
                    <tr class="text-left text-xs text-muted-foreground border-b">
                      <th class="py-2 px-5 font-medium">Check</th>
                      <th class="py-2 pr-3 font-medium">Status</th>
                      <th class="py-2 pr-5 font-medium">Conclusion</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-border">
                    <tr v-for="(check, idx) in pr.ci_checks" :key="`${check.name}-${idx}`">
                      <td class="py-2 px-5">
                        <a
                          v-if="check.url"
                          :href="check.url"
                          target="_blank"
                          rel="noopener"
                          class="text-primary hover:underline"
                        >
                          {{ check.name }}
                        </a>
                        <span v-else>{{ check.name }}</span>
                      </td>
                      <td class="py-2 pr-3 font-mono text-xs text-muted-foreground">{{ check.status || '—' }}</td>
                      <td class="py-2 pr-5">
                        <Badge :variant="ciConclusionVariant(check.conclusion)" class="px-1.5 text-[10px]">
                          {{ check.conclusion || 'pending' }}
                        </Badge>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </div>

        <!-- ── Sidebar ────────────────────────────────────────────────── -->
        <div class="space-y-4">
          <Card>
            <CardHeader class="flex-row items-center justify-between gap-3 space-y-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <Target class="h-4 w-4 text-muted-foreground" />
                Convergence
              </CardTitle>
              <Badge :variant="pr.converged ? 'success' : 'warn'" class="px-1.5 text-[10px]">
                {{ pr.converged ? 'converged' : 'not converged' }}
              </Badge>
            </CardHeader>
            <CardContent>
              <ConvergenceChecklist :convergence="pr.convergence" />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle class="flex items-center gap-2 text-base">
                <SquareKanban class="h-4 w-4 text-muted-foreground" />
                Ticket
              </CardTitle>
            </CardHeader>
            <CardContent>
              <RouterLink
                v-if="pr.ticket_uid"
                :to="{ name: 'ticket-detail', params: { uid: pr.ticket_uid } }"
                class="inline-flex max-w-full items-center gap-1.5 text-sm text-primary hover:underline"
              >
                <SquareKanban class="h-4 w-4 shrink-0" />
                <span class="min-w-0 truncate">{{ ticketTitle || pr.ticket_uid.slice(0, 8) }}</span>
              </RouterLink>
              <div v-else class="space-y-2">
                <p class="text-xs text-muted-foreground">
                  Not bound to a ticket yet — link the ticket this PR implements.
                </p>
                <Button variant="outline" size="sm" @click="linkTicketOpen = true">
                  <Link2 /> Link ticket
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle class="text-base">Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl class="text-xs grid grid-cols-[92px_1fr] gap-x-2 gap-y-1.5">
                <dt class="text-muted-foreground">head</dt>
                <dd class="font-mono break-all">{{ pr.head_sha || '—' }}</dd>
                <dt class="text-muted-foreground">branch</dt>
                <dd class="font-mono break-all">{{ pr.head_ref }} → {{ pr.base_ref }}</dd>
                <dt class="text-muted-foreground">base default</dt>
                <dd>{{ pr.base_is_default ? 'yes' : 'no' }}</dd>
                <dt class="text-muted-foreground">fix rounds</dt>
                <dd>{{ pr.fix_rounds }}<template v-if="policy"> / {{ policy.max_fix_rounds }}</template></dd>
                <template v-if="pr.last_synced_at">
                  <dt class="text-muted-foreground">last synced</dt>
                  <dd>{{ pr.last_synced_at }}</dd>
                </template>
                <template v-if="pr.updated_at">
                  <dt class="text-muted-foreground">updated</dt>
                  <dd>{{ pr.updated_at }}</dd>
                </template>
              </dl>
            </CardContent>
          </Card>
        </div>
      </div>

      <!-- Discussion -->
      <CommentThread subject-type="pull_request" :subject-uid="pr.uid" :repository-uid="pr.repository_uid" title="Discussion" />

      <LinkTicketDialog v-model:open="linkTicketOpen" :pr="pr" @linked="onTicketLinked" />
    </template>
  </div>
</template>
