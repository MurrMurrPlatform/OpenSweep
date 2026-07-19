<script setup lang="ts">
// Thread view — one conversation carrying a ticket through refine → plan →
// implement → review. Chat pane left (against the thread's active run),
// plan + timeline rail right.
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { Bot, Check, ChevronDown, Hammer, History, ScrollText, XCircle } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import ActionMenuBar from '@/components/workitem/ActionMenuBar.vue'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Dialog,
  DialogHeader,
  DialogScrollContent,
  DialogTitle,
} from '@/components/ui/dialog'
import { MarkdownView } from '@/components/ui/markdown'
import ThreadChat from '@/components/threads/ThreadChat.vue'
import ThreadTimeline from '@/components/threads/ThreadTimeline.vue'
import ConvergenceChecklist from '@/components/delivery/ConvergenceChecklist.vue'
import TestLocallyButton from '@/components/delivery/TestLocallyButton.vue'
import ContinueInTerminalButton from '@/components/runs/ContinueInTerminalButton.vue'
import VerdictCard from '@/components/delivery/VerdictCard.vue'
import RunFilesPanel from '@/components/runs/RunFilesPanel.vue'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useRunPolicyStore } from '@/stores/runPolicyStore'
import { useRunStore } from '@/stores/runStore'
import { useThreadStore } from '@/stores/threadStore'
import { isLiveRunStatus } from '@/lib/runStatus'
import type {
  PullRequestDTO,
  RunDTO,
  RunPolicyDTO,
  ThreadDetailDTO,
  ThreadPhase,
  VerdictDTO,
} from '@/types/api'

const route = useRoute()
const threads = useThreadStore()
const delivery = useDeliveryStore()
const runs = useRunStore()
const runPolicies = useRunPolicyStore()
const toast = useToast()

// Embeddable in WorkItemView: an explicit uid prop wins over the route param.
const props = defineProps<{ uid?: string }>()
const uid = computed(() => props.uid || String(route.params.uid))
const thread = ref<ThreadDetailDTO | null>(null)
const pr = ref<PullRequestDTO | null>(null)
const verdict = ref<VerdictDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

// ── Run parity: the thread IS a conversation over runs, so it gets the same
// surfaces the Run page has — a Files (diff) tab against the active run's
// workspace, and the policy / provider / usage facts of that run.
const activeTab = ref<'conversation' | 'files' | 'details'>('conversation')
const activeRun = ref<RunDTO | null>(null)
const runPolicy = ref<RunPolicyDTO | null>(null)
const filesTabOpened = ref(false)
const filesRefreshKey = ref(0)
const changedFilesCount = ref<number | null>(null)
watch(activeTab, (tab) => {
  if (tab === 'files') filesTabOpened.value = true
})
const filesTabLabel = computed(() =>
  changedFilesCount.value === null ? 'Files' : `Files (${changedFilesCount.value})`,
)
const isLiveRun = computed(() =>
  Boolean(activeRun.value && isLiveRunStatus(activeRun.value.status)),
)

async function loadActiveRun() {
  const runUid = thread.value?.active_run_uid || ''
  if (!runUid) {
    activeRun.value = null
    runPolicy.value = null
    return
  }
  try {
    activeRun.value = await runs.get(runUid)
  } catch {
    activeRun.value = null
  }
  const policyUid = activeRun.value?.run_policy_uid || ''
  if (!policyUid) {
    runPolicy.value = null
  } else if (runPolicy.value?.uid !== policyUid) {
    try {
      runPolicy.value = await runPolicies.get(policyUid)
    } catch {
      runPolicy.value = null
    }
  }
}

const policyLabel = computed(() => {
  if (!activeRun.value?.run_policy_uid) return 'system default'
  if (runPolicy.value) return `${runPolicy.value.name} (v${runPolicy.value.version})`
  return activeRun.value.run_policy_uid.slice(0, 8)
})

const policyLimits = computed(() => {
  const p = runPolicy.value
  if (!p) return ''
  const bits: string[] = []
  if (p.max_wall_seconds) bits.push(`${p.max_wall_seconds}s wall`)
  if (p.max_tokens) bits.push(`${p.max_tokens.toLocaleString()} tokens`)
  if (p.max_dollars) bits.push(`$${p.max_dollars}`)
  if (p.max_tool_turns) bits.push(`${p.max_tool_turns} tool turns`)
  if (p.local_only) bits.push('local-only')
  if (p.dry_run) bits.push('dry-run')
  return bits.join(' · ')
})

const usageSummary = computed(() => {
  const usage = (activeRun.value?.usage ?? {}) as Record<string, unknown>
  const bits: string[] = []
  const tokens = usage.tokens
  const dollars = usage.dollars
  if (typeof tokens === 'number' && tokens > 0) bits.push(`${tokens.toLocaleString()} tokens`)
  if (typeof dollars === 'number' && dollars > 0) bits.push(`$${dollars.toFixed(2)}`)
  return bits.join(' · ')
})

const PHASE_LABELS: Record<ThreadPhase, string> = {
  refining: 'Refining & planning',
  implementing: 'Implementing',
  in_review: 'In review',
  done: 'Done',
  abandoned: 'Abandoned',
}

const active = computed(
  () => thread.value && thread.value.phase !== 'done' && thread.value.phase !== 'abandoned',
)

// Timeline is a side-channel, not the main event: collapsed by default so
// the conversation gets the width.
const timelineOpen = ref(false)

// The plan only surfaces here while it needs a decision; approved plans live
// on the Ticket tab.
const planModalOpen = ref(false)
const planAwaitingApproval = computed(
  () =>
    Boolean(thread.value?.plan_text?.trim()) &&
    thread.value?.plan_state === 'drafted' &&
    thread.value?.phase === 'refining',
)

/** Latest delivery_blocked detail — shown until a PR exists (a successful
 *  push/PR supersedes any earlier failure). */
const deliveryBlocked = computed(() => {
  if (!thread.value || thread.value.pr_uid) return ''
  if (thread.value.phase !== 'implementing' && thread.value.phase !== 'in_review') return ''
  const events = thread.value.events ?? []
  const last = [...events].reverse().find((e) => e.type === 'delivery_blocked')
  return last ? String(last.detail ?? 'delivery failed') : ''
})

async function reload() {
  try {
    thread.value = await threads.getThread(uid.value)
    if (thread.value.pr_uid) {
      try {
        pr.value = await delivery.getPullRequest(thread.value.pr_uid)
        verdict.value = await delivery.getLatestVerdict(thread.value.pr_uid)
      } catch {
        pr.value = null
        verdict.value = null
      }
    }
    await loadActiveRun()
    error.value = null
    window.dispatchEvent(new CustomEvent('workitem:changed'))
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function onTurnSettled() {
  filesRefreshKey.value += 1 // the Files panel refetches the workspace diff
  void reload()
}

onMounted(reload)
watch(uid, () => {
  thread.value = null
  pr.value = null
  activeRun.value = null
  changedFilesCount.value = null
  loading.value = true
  void reload()
})

async function onApprovePlan() {
  try {
    await threads.approvePlan(uid.value)
    toast.success('Plan approved')
  } catch (e) {
    toast.error('Couldn’t approve plan', e instanceof ApiError ? e.detail : String(e))
  }
  await reload()
}

const implementing = ref(false)
async function onImplement() {
  if (implementing.value) return
  implementing.value = true
  try {
    await threads.implement(uid.value)
    toast.success('Implementation started', 'The conversation continues with the implement run.')
  } catch (e) {
    toast.error(
      'Couldn’t start implementation',
      e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e),
    )
  } finally {
    implementing.value = false
  }
  await reload()
}

async function onAbandon() {
  try {
    await threads.abandon(uid.value)
    toast.info('Thread abandoned')
  } catch (e) {
    toast.error('Couldn’t abandon thread', e instanceof ApiError ? e.detail : String(e))
  }
  await reload()
}

// ── Structured questions + agent task list ──────────────────────────────────

const chatRef = ref<InstanceType<typeof ThreadChat> | null>(null)

const openQuestions = computed(() =>
  (thread.value?.events ?? []).filter((e) => e.type === 'question' && e.status === 'open'),
)

/** Answered but not yet delivered — batch gating holds them until every
 *  open question is answered (or the user forces continue). */
const answeredWaiting = computed(
  () =>
    (thread.value?.events ?? []).filter(
      (e) => e.type === 'question' && e.status === 'answered' && !e.delivered_at,
    ).length,
)

const continuing = ref(false)
async function onContinueQuestions() {
  if (continuing.value) return
  continuing.value = true
  try {
    await threads.continueQuestions(uid.value)
    toast.info('Continuing', 'Answers delivered; unanswered questions were dismissed.')
  } catch (e) {
    toast.error('Couldn’t continue', e instanceof ApiError ? e.detail : String(e))
  } finally {
    continuing.value = false
  }
  await reload()
}

const answeringUid = ref<string | null>(null)
async function onAnswerQuestion(questionUid: string, _questionText: string, answer: string) {
  if (answeringUid.value) return
  answeringUid.value = questionUid
  try {
    // The backend records the answer, syncs the mirrored ticket comment,
    // and delivers it into the conversation (the same path comment replies
    // take) — the chat picks the turn up over its live socket.
    await threads.answerQuestion(uid.value, questionUid, answer)
  } catch (e) {
    toast.error('Couldn’t answer', e instanceof ApiError ? e.detail : String(e))
  } finally {
    answeringUid.value = null
  }
  await reload()
}

const fixing = ref(false)
async function onFix() {
  if (!thread.value?.pr_uid || fixing.value) return
  fixing.value = true
  try {
    await delivery.triggerFix(thread.value.pr_uid)
    toast.success('Fix round started', 'The conversation continues with the fix run.')
  } catch (e) {
    toast.error(
      'Couldn’t start fix',
      e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e),
    )
  } finally {
    fixing.value = false
  }
  await reload()
}
</script>

<template>
  <div class="flex h-full flex-col gap-4">
    <!-- Identity lives in WorkItemView's unified header — this bar is ONLY
         actions. Ticket/PR navigation is the tabs' job now. -->
    <ActionMenuBar v-if="thread">
      <Button
        v-if="thread.phase === 'refining'"
        size="sm"
        :loading="implementing"
        @click="onImplement"
      >
        <Hammer /> Implement
      </Button>
      <TestLocallyButton
        v-if="thread.branch || pr"
        :branch="pr?.head_ref || thread.branch"
        :pr-number="pr?.github_number ?? null"
      />
      <ContinueInTerminalButton
        v-if="thread.active_run_uid"
        :run-uid="thread.active_run_uid"
      />
      <Button v-if="active" size="sm" variant="ghost" @click="onAbandon">
        <XCircle /> Abandon
      </Button>
      <template #trailing>
        <Badge variant="secondary">{{ thread.progress?.label ?? PHASE_LABELS[thread.phase] }}</Badge>
        <Badge variant="outline" class="px-1.5 text-[10px]">plan: {{ thread.plan_state }}</Badge>
      </template>
    </ActionMenuBar>

    <!-- Delivery failures (push rejected, PR open failed) are otherwise
         invisible: the thread just sits in `implementing` with changed files
         and no PR. Say WHY, loudly. -->
    <div
      v-if="deliveryBlocked"
      class="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
    >
      <strong>Couldn’t deliver the work:</strong> {{ deliveryBlocked }}
      <span class="block text-xs text-muted-foreground">
        The changes are safe in the workspace. Fix the cause (usually GitHub write access for the
        connected credential), then send any message in the conversation to retry the push.
      </span>
    </div>

    <div v-if="error" class="text-sm text-bad">{{ error }}</div>
    <div v-else-if="loading" class="text-sm text-muted-foreground">Loading thread…</div>

    <div v-else-if="thread" class="flex min-h-0 flex-1 flex-col gap-3">
      <!-- Sub-navigation: compact underline tabs + the timeline toggle on one
           quiet rule — no floating pill strip. -->
      <div class="flex items-end border-b">
        <Tabs v-model="activeTab">
          <TabsList class="-mb-px h-auto justify-start gap-1 rounded-none bg-transparent p-0">
            <TabsTrigger
              v-for="t in [
                { value: 'conversation', label: 'Conversation' },
                { value: 'files', label: filesTabLabel },
                { value: 'details', label: 'Details' },
              ]"
              :key="t.value"
              :value="t.value"
              class="rounded-none border-b-2 border-transparent bg-transparent px-3 py-1.5 text-sm text-muted-foreground shadow-none data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none"
            >
              {{ t.label }}
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <button
          type="button"
          class="ml-auto mb-1 inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          @click="timelineOpen = !timelineOpen"
        >
          <History class="size-3.5" />
          Timeline
          <span v-if="thread.events?.length" class="text-muted-foreground/70">{{ thread.events.length }}</span>
          <ChevronDown class="size-3 transition-transform" :class="timelineOpen ? 'rotate-180' : ''" />
        </button>
      </div>

      <!-- Plan gate: only surfaces while a drafted plan waits for approval —
           the approved plan's permanent home is the Ticket tab. -->
      <div
        v-if="planAwaitingApproval"
        class="flex items-center gap-2 rounded-md border border-primary/20 bg-primary/5 px-3 py-2 text-xs"
      >
        <ScrollText class="size-3.5 shrink-0 text-primary" />
        <span class="text-muted-foreground">
          The agent drafted a plan and is waiting for your approval — approving starts implementation.
        </span>
        <div class="ml-auto flex shrink-0 items-center gap-1.5">
          <Button size="sm" variant="ghost" class="h-6 text-xs" @click="planModalOpen = true">
            Read plan
          </Button>
          <Button size="sm" class="h-6 text-xs" @click="onApprovePlan">
            <Check /> Approve
          </Button>
        </div>
      </div>

      <div
        v-if="openQuestions.length"
        class="flex items-center gap-2 rounded-md border border-primary/20 bg-primary/5 px-3 py-2 text-xs"
      >
        <span class="text-muted-foreground">
          {{ openQuestions.length }} question{{ openQuestions.length === 1 ? '' : 's' }} waiting
          <template v-if="answeredWaiting"> · {{ answeredWaiting }} answered</template>
          — the agent resumes when all are answered.
        </span>
        <Button
          size="sm"
          variant="ghost"
          class="ml-auto h-6 text-xs"
          :loading="continuing"
          @click="onContinueQuestions"
        >
          Continue anyway
        </Button>
      </div>

      <div class="flex min-h-0 flex-1 gap-4">
        <section class="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
        <ThreadChat
          v-if="thread.active_run_uid"
          v-show="activeTab === 'conversation'"
          ref="chatRef"
          :run-uids="thread.runs.map((r) => r.uid)"
          :run-uid="thread.active_run_uid"
          :questions="openQuestions"
          :answering-uid="answeringUid"
          @turn-settled="onTurnSettled"
          @answer="onAnswerQuestion"
        />
        <p
          v-else-if="activeTab === 'conversation'"
          class="text-sm text-muted-foreground"
        >
          No conversation attached yet.
        </p>

        <RunFilesPanel
          v-if="filesTabOpened && thread.active_run_uid"
          v-show="activeTab === 'files'"
          :run-uid="thread.active_run_uid"
          :refresh-key="filesRefreshKey"
          :live="isLiveRun"
          @loaded="changedFilesCount = $event"
        />
        <p
          v-else-if="activeTab === 'files' && !thread.active_run_uid"
          class="text-sm text-muted-foreground"
        >
          No workspace yet — files appear once the conversation starts.
        </p>

        <div v-if="activeTab === 'details'" class="space-y-4 overflow-y-auto">
          <Card>
            <CardHeader class="p-4"><h2 class="text-sm font-semibold">Session facts</h2></CardHeader>
            <CardContent class="p-4 pt-0">
              <dl v-if="activeRun" class="grid grid-cols-[120px_1fr] gap-x-2 gap-y-1 text-xs">
                <dt class="text-muted-foreground">run</dt>
                <dd>
                  <RouterLink
                    :to="{ name: 'run-detail', params: { uid: activeRun.uid } }"
                    class="font-mono text-primary hover:underline"
                  >
                    {{ activeRun.uid.slice(0, 12) }}
                  </RouterLink>
                </dd>
                <dt class="text-muted-foreground">playbook</dt><dd>{{ activeRun.playbook }}</dd>
                <dt class="text-muted-foreground">executor</dt><dd>{{ activeRun.executor }}</dd>
                <template v-if="activeRun.provider_label || activeRun.provider_kind">
                  <dt class="text-muted-foreground">provider</dt>
                  <dd>
                    <div>{{ activeRun.provider_label || activeRun.provider_kind }}</div>
                    <div v-if="activeRun.provider_model" class="font-mono text-muted-foreground">
                      {{ activeRun.provider_model }}
                    </div>
                  </dd>
                </template>
                <dt class="text-muted-foreground">policy</dt>
                <dd :title="runPolicy?.description || ''">
                  {{ policyLabel }}
                  <span v-if="policyLimits" class="block text-muted-foreground">{{ policyLimits }}</span>
                </dd>
                <dt class="text-muted-foreground">turns</dt><dd>{{ activeRun.turns }}</dd>
                <template v-if="usageSummary">
                  <dt class="text-muted-foreground">usage</dt><dd>{{ usageSummary }}</dd>
                </template>
                <dt class="text-muted-foreground">workspace</dt>
                <dd class="font-mono">{{ activeRun.sandbox_uid || 'destroyed' }}</dd>
                <dt class="text-muted-foreground">branch</dt>
                <dd class="font-mono">{{ thread.branch || '—' }}</dd>
                <dt class="text-muted-foreground">started</dt><dd>{{ activeRun.started_at || '—' }}</dd>
                <dt class="text-muted-foreground">last activity</dt>
                <dd>{{ activeRun.last_activity_at || '—' }}</dd>
              </dl>
              <p v-else class="text-xs text-muted-foreground">No active run.</p>
            </CardContent>
          </Card>
          <Card v-if="thread.runs.length > 1">
            <CardHeader class="p-4"><h2 class="text-sm font-semibold">All runs in this thread</h2></CardHeader>
            <CardContent class="space-y-1 p-4 pt-0">
              <RouterLink
                v-for="r in thread.runs"
                :key="r.uid"
                :to="{ name: 'run-detail', params: { uid: r.uid } }"
                class="flex items-center gap-2 rounded-sm border bg-muted px-2 py-1.5 text-xs hover:border-primary/60"
              >
                <Bot class="size-3.5 text-muted-foreground" />
                <span class="font-mono">{{ r.uid.slice(0, 8) }}</span>
                <span>{{ r.playbook }}</span>
                <span class="ml-auto text-muted-foreground">{{ r.status }}</span>
              </RouterLink>
            </CardContent>
          </Card>
        </div>
        </section>

        <!-- Conversation rail: only convergence (when in review) and the
             timeline, only when opened — collapsed by default so the
             conversation gets the width. -->
        <aside
          v-if="activeTab === 'conversation' && (timelineOpen || (pr && thread.phase === 'in_review'))"
          class="w-80 shrink-0 space-y-4 overflow-y-auto"
        >
          <Card v-if="pr && thread.phase === 'in_review'">
            <CardContent class="space-y-3 p-4">
              <div class="flex items-center justify-between">
                <h3 class="text-sm font-semibold">Convergence</h3>
                <Button
                  v-if="verdict?.result === 'request_changes'"
                  size="sm"
                  variant="outline"
                  :loading="fixing"
                  @click="onFix"
                >
                  Run fix
                </Button>
              </div>
              <ConvergenceChecklist :convergence="pr.convergence" />
              <VerdictCard v-if="verdict" :verdict="verdict" :head-sha="pr.head_sha" />
            </CardContent>
          </Card>
          <ThreadTimeline v-if="timelineOpen" :events="thread.events" :runs="thread.runs" />
        </aside>
      </div>
    </div>

    <!-- Full-plan reader with the approval action — the drafted plan's only
         surface on this page (the approved plan lives on the Ticket tab). -->
    <Dialog v-model:open="planModalOpen">
      <DialogScrollContent class="max-w-3xl">
        <DialogHeader>
          <DialogTitle class="flex items-center gap-2">
            Plan
            <Badge :variant="thread?.plan_state === 'approved' ? 'success' : 'secondary'">
              {{ thread?.plan_state }}
            </Badge>
            <Button
              v-if="planAwaitingApproval"
              size="sm"
              class="ml-auto mr-6"
              @click="((planModalOpen = false), onApprovePlan())"
            >
              <Check /> Approve
            </Button>
          </DialogTitle>
        </DialogHeader>
        <MarkdownView :model-value="thread?.plan_text ?? ''" :preview-only="true" min-height="0" />
      </DialogScrollContent>
    </Dialog>
  </div>
</template>
