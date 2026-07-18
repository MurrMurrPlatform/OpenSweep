<script setup lang="ts">
// Thread view — one conversation carrying a ticket through refine → plan →
// implement → review. Chat pane left (against the thread's active run),
// plan + timeline rail right.
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { GitPullRequest, Hammer, MessagesSquare, XCircle } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { PageHeader } from '@/components/ui/page-header'
import AgentTodosPanel from '@/components/threads/AgentTodosPanel.vue'
import PlanPanel from '@/components/threads/PlanPanel.vue'
import ThreadChat from '@/components/threads/ThreadChat.vue'
import ThreadTimeline from '@/components/threads/ThreadTimeline.vue'
import ConvergenceChecklist from '@/components/delivery/ConvergenceChecklist.vue'
import TestLocallyButton from '@/components/delivery/TestLocallyButton.vue'
import VerdictCard from '@/components/delivery/VerdictCard.vue'
import { Card, CardContent } from '@/components/ui/card'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useThreadStore } from '@/stores/threadStore'
import type {
  AgentTodo,
  PullRequestDTO,
  ThreadDetailDTO,
  ThreadPhase,
  VerdictDTO,
} from '@/types/api'

const route = useRoute()
const threads = useThreadStore()
const delivery = useDeliveryStore()
const toast = useToast()

const uid = computed(() => String(route.params.uid))
const thread = ref<ThreadDetailDTO | null>(null)
const pr = ref<PullRequestDTO | null>(null)
const verdict = ref<VerdictDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

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
    error.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)
watch(uid, () => {
  thread.value = null
  pr.value = null
  loading.value = true
  void reload()
})

async function onSavePlan(text: string) {
  try {
    await threads.updatePlan(uid.value, text)
    toast.success('Plan saved')
  } catch (e) {
    toast.error('Couldn’t save plan', e instanceof ApiError ? e.detail : String(e))
  }
  await reload()
}

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
const todos = ref<AgentTodo[]>([])

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

// Re-sent with every message while planning: keeps the agent on the
// staged contract in follow-up turns (observed failure: it started
// implementing right after a question was answered). Implementation is
// authorized ONLY by the platform's GO message (rev2).
const PLANNING_REMINDER =
  '[Thread protocol reminder — PLANNING stage: do not edit files or commit; ' +
  'the platform will send an explicit GO message when implementation is approved. ' +
  'For now: ask the next question via opensweep_platform_ask_user, or update the ' +
  'ticket and submit the plan via opensweep_platform_submit_thread_plan, then stop and wait.]'

const protocolReminder = computed(() =>
  thread.value?.phase === 'refining' ? PLANNING_REMINDER : undefined,
)

async function onAnswerQuestion(questionUid: string, _questionText: string, answer: string) {
  try {
    // The backend records the answer, syncs the mirrored ticket comment,
    // and delivers it into the conversation (the same path comment replies
    // take) — the chat picks the turn up over its live socket.
    await threads.answerQuestion(uid.value, questionUid, answer)
  } catch (e) {
    toast.error('Couldn’t answer', e instanceof ApiError ? e.detail : String(e))
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
  <div class="flex h-full flex-col gap-4 p-4">
    <PageHeader title="Thread">
      <template #breadcrumb>
        <div class="mb-1 flex flex-wrap items-center gap-2">
          <MessagesSquare class="h-4 w-4 text-muted-foreground" />
          <Badge v-if="thread" variant="secondary">{{ PHASE_LABELS[thread.phase] }}</Badge>
          <Badge v-if="thread" variant="outline" class="px-1.5 text-[10px]">
            plan: {{ thread.plan_state }}
          </Badge>
        </div>
      </template>

      <div v-if="thread" class="flex flex-wrap items-center gap-2">
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
        <RouterLink
          v-if="thread.pr_uid"
          :to="{ name: 'pull-request-detail', params: { uid: thread.pr_uid } }"
        >
          <Button size="sm" variant="outline"><GitPullRequest /> Pull request</Button>
        </RouterLink>
        <RouterLink :to="{ name: 'ticket-detail', params: { uid: thread.subject_ticket_uid } }">
          <Button size="sm" variant="ghost">Ticket</Button>
        </RouterLink>
        <Button v-if="active" size="sm" variant="ghost" @click="onAbandon">
          <XCircle /> Abandon
        </Button>
      </div>
    </PageHeader>

    <div v-if="error" class="text-sm text-bad">{{ error }}</div>
    <div v-else-if="loading" class="text-sm text-muted-foreground">Loading thread…</div>

    <div v-else-if="thread" class="flex min-h-0 flex-1 gap-4">
      <section class="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
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
        <ThreadChat
          v-if="thread.active_run_uid"
          ref="chatRef"
          :run-uids="thread.runs.map((r) => r.uid)"
          :run-uid="thread.active_run_uid"
          :questions="openQuestions"
          :protocol-reminder="protocolReminder"
          @turn-settled="reload"
          @todos="todos = $event"
          @answer="onAnswerQuestion"
        />
        <p v-else class="text-sm text-muted-foreground">No conversation attached yet.</p>
      </section>

      <aside class="w-96 shrink-0 space-y-4 overflow-y-auto">
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
        <PlanPanel
          :plan-text="thread.plan_text"
          :plan-state="thread.plan_state"
          :editable="thread.phase === 'refining'"
          @save="onSavePlan"
          @approve="onApprovePlan"
        />
        <AgentTodosPanel :live="todos" :stored="thread.todos" :phase="thread.phase" />
        <ThreadTimeline :events="thread.events" :runs="thread.runs" />
      </aside>
    </div>
  </div>
</template>
