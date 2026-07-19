<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Ban,
  Bot,
  CircleStop,
  ClipboardList,
  FolderArchive,
  GitPullRequest,
  OctagonX,
  RefreshCw,
  Save,
  SendHorizontal,
  SquareKanban,
  Wifi,
  WifiOff,
} from 'lucide-vue-next'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardHeader, CardContent } from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
import { Textarea } from '@/components/ui/textarea'
import RunTranscript from '@/components/runs/RunTranscript.vue'
import RunFilesPanel from '@/components/runs/RunFilesPanel.vue'
import CommentThread from '@/components/comments/CommentThread.vue'
import { useFindingStore } from '@/stores/findingStore'
import { useAgentStore } from '@/stores/agentStore'
import { useScheduledAgentStore } from '@/stores/scheduledAgentStore'
import SaveAsAgentDialog from '@/components/agents/SaveAsAgentDialog.vue'
import { useRunPolicyStore } from '@/stores/runPolicyStore'
import { useRunStore } from '@/stores/runStore'
import { useRunSocket, type RunSocketState } from '@/composables/useRunSocket'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { acceptsFollowUp, isLiveRunStatus, runStatusLabel, runStatusVariant } from '@/lib/runStatus'
import type {
  AgentDTO,
  FindingDTO,
  RunDTO,
  RunPolicyDTO,
  RunStatus,
  RunTranscriptEvent,
  ScheduledAgentDTO,
} from '@/types/api'

const route = useRoute()
const runs = useRunStore()
const agents = useAgentStore()
const scheduledAgents = useScheduledAgentStore()
const findings = useFindingStore()
const runPolicies = useRunPolicyStore()
const toast = useToast()

const uid = computed(() => String(route.params.uid))
const run = ref<RunDTO | null>(null)
const scheduledAgent = ref<ScheduledAgentDTO | null>(null)
const agent = ref<AgentDTO | null>(null)
const saveAgentOpen = ref(false)
const runFindings = ref<FindingDTO[]>([])
const runPolicy = ref<RunPolicyDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const rawArtifact = ref('')
const artifactError = ref('')
const activeTab = ref<'conversation' | 'files' | 'input' | 'raw'>('conversation')

// ── Files tab ────────────────────────────────────────────────────────────────
// Mounted lazily on first open, then kept alive with v-show so the panel's
// selection/expansion state survives tab switches. refreshKey bumps at every
// turn boundary so the diff follows the agent's work.
const filesTabOpened = ref(false)
const filesRefreshKey = ref(0)
const changedFilesCount = ref<number | null>(null)
watch(activeTab, (tab) => {
  if (tab === 'files') filesTabOpened.value = true
})
const filesTabLabel = computed(() =>
  changedFilesCount.value === null ? 'Files' : `Files (${changedFilesCount.value})`,
)

// ── Transcript ───────────────────────────────────────────────────────────────
// Initial load via GET /runs/{uid}/transcript; from then on the WebSocket
// streams every event live (the server tails the run's event file), so
// there is no polling loop.
const events = ref<RunTranscriptEvent[]>([])
const lastSeq = ref(0)

// ── Composer / turn state ────────────────────────────────────────────────────
const draft = ref('')
/** Turn accepted, first delta not in yet — shows the working indicator. */
const awaitingReply = ref(false)
/** Assistant tokens streamed over the WS for the in-flight turn. */
const streamingText = ref('')
/** In-flight blocking REST fallback call. */
const restPending = ref(false)
const interrupting = ref(false)
const ending = ref(false)
const cancelling = ref(false)
const rebuilding = ref(false)

// ── WebSocket lifecycle ──────────────────────────────────────────────────────

type Socket = ReturnType<typeof useRunSocket>
const socket = shallowRef<Socket | null>(null)
const wsState = computed<RunSocketState>(() => socket.value?.state.value ?? 'idle')

function setStatus(status: RunStatus) {
  if (run.value) run.value = { ...run.value, status }
}

let settling = false
/** Turn boundary observed on the socket — replace local state with the truth. */
async function refetchAfterTurn() {
  if (settling) return
  settling = true
  try {
    await refetchTranscriptFull()
    run.value = await runs.get(uid.value)
    await loadRunFindings()
    await loadArtifact()
    filesRefreshKey.value += 1 // the Files panel refetches the workspace diff
  } catch {
    /* transient — the next turn boundary catches up */
  } finally {
    settling = false
    streamingText.value = ''
  }
}

function pushEvent(event: RunTranscriptEvent) {
  if (event.seq) {
    if (events.value.some((e) => e.seq === event.seq)) return
    if (event.seq > lastSeq.value) lastSeq.value = event.seq
  }
  // The streamed user_message supersedes the optimistic local echo.
  if (event.type === 'user_message') {
    events.value = events.value.filter(
      (e) => !(e.seq === 0 && e.type === 'user_message' && e.text === event.text),
    )
  }
  events.value = [...events.value, event]
}

function openSocket() {
  socket.value?.close()
  socket.value = useRunSocket(uid.value, {
    afterSeq: () => lastSeq.value,
    onStatus: (status) => {
      setStatus(status)
      if (status !== 'running') {
        awaitingReply.value = false
        void refetchAfterTurn()
      }
    },
    onDelta: (text) => {
      awaitingReply.value = false
      streamingText.value += text
    },
    onEvent: (event) => {
      awaitingReply.value = false
      pushEvent(event)
      if (event.type === 'assistant_text') streamingText.value = ''
    },
    onMessageComplete: () => {
      awaitingReply.value = false
      streamingText.value = ''
      void refetchAfterTurn()
    },
    onError: (detail) => {
      awaitingReply.value = false
      toast.error('Run error', detail)
    },
    onUnavailable: () => {
      awaitingReply.value = false
      toast.warn('Live connection lost', 'Live updates stopped — retry the connection or reload.')
    },
    onUnauthorized: (code) => {
      awaitingReply.value = false
      if (code === 4404) return // run vanished — the REST 404 surfaces on reload
      toast.error('Not authorized', 'Your credentials were rejected. Sign in again and retry.')
    },
  })
  socket.value.connect()
}

// ── Data loading ─────────────────────────────────────────────────────────────

onMounted(load)
onBeforeUnmount(() => {
  socket.value?.close()
})
watch(uid, () => {
  events.value = []
  lastSeq.value = 0
  streamingText.value = ''
  awaitingReply.value = false
  changedFilesCount.value = null
  void load()
})

async function load() {
  loading.value = true
  error.value = null
  try {
    run.value = await runs.get(uid.value)
    await loadAgentContext()
    await loadRunFindings()
    await loadArtifact()
    await loadRunPolicy()
    await refetchTranscriptFull()
    openSocket()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function refetchTranscriptFull() {
  const chunk = await runs.getTranscript(uid.value, 0)
  events.value = chunk.events
  lastSeq.value = chunk.last_seq
}

function saveAgentPrefill() {
  const usage = asRecord(run.value?.usage)
  const input = asRecord(usage.input)
  const promptText = firstString(input.intent, agent.value?.prompt)
  return {
    title: run.value?.title || '',
    prompt: promptText || '',
    produces: 'findings' as const,
  }
}

async function loadAgentContext() {
  if (run.value?.scheduled_agent_uid) {
    if (scheduledAgent.value?.uid !== run.value.scheduled_agent_uid) {
      scheduledAgent.value = await scheduledAgents
        .get(run.value.scheduled_agent_uid)
        .catch(() => null)
    }
  } else {
    scheduledAgent.value = null
  }
  if (run.value?.agent_uid) {
    if (agent.value?.uid !== run.value.agent_uid) {
      agent.value = await agents.get(run.value.agent_uid).catch(() => null)
    }
  } else {
    agent.value = null
  }
}

async function loadArtifact() {
  if (!run.value?.raw_artifact_uri) {
    rawArtifact.value = ''
    artifactError.value = ''
    return
  }
  try {
    rawArtifact.value = (await runs.getArtifact(run.value.raw_artifact_uri)).text
    artifactError.value = ''
  } catch (e: unknown) {
    artifactError.value = e instanceof Error ? e.message : String(e)
  }
}

async function loadRunPolicy() {
  const policyUid = run.value?.run_policy_uid || ''
  if (!policyUid) {
    runPolicy.value = null
    return
  }
  if (runPolicy.value?.uid === policyUid) return
  try {
    runPolicy.value = await runPolicies.get(policyUid)
  } catch {
    runPolicy.value = null
  }
}

async function loadRunFindings() {
  if (!run.value) return
  runFindings.value = await findings.fetchAll({ source_run_uid: run.value.uid })
}

// ── Sending ──────────────────────────────────────────────────────────────────

// A queued CHAT run accepts a message too: the backend holds it until the
// background workspace clone finishes (first-message queueing).
const canCompose = computed(() =>
  Boolean(
    run.value &&
      (acceptsFollowUp(run.value.status) ||
        (run.value.playbook === 'chat' && run.value.status === 'queued')),
  ),
)
const busy = computed(
  () => run.value?.status === 'running' || awaitingReply.value || restPending.value,
)
const unauthorized = computed(() => wsState.value === 'unauthorized')
const workspaceGone = computed(() => Boolean(run.value && run.value.sandbox_uid === ''))
const canSend = computed(
  () => canCompose.value && !unauthorized.value && !busy.value && draft.value.trim().length > 0,
)

const inputHint = computed(() => {
  if (!run.value) return ''
  if (unauthorized.value) return 'Not authorized for this run — sign in again, then retry the connection.'
  if (busy.value) return 'The agent is working — interrupt to cut the turn short.'
  if (run.value.status === 'paused_quota') return runStatusLabel(run.value)
  if (run.value.status === 'queued') {
    return run.value.playbook === 'chat'
      ? 'The workspace is being prepared — your message starts the conversation as soon as it’s ready.'
      : 'The workspace is being prepared…'
  }
  const bits: string[] = []
  if (run.value.status === 'failed') bits.push('Run failed — replying retries in the same conversation.')
  else if (run.value.status === 'ended') bits.push('Run ended — replying reopens the conversation.')
  if (canCompose.value && workspaceGone.value) bits.push('workspace expired — sending will rebuild it first')
  bits.push('Enter to send · Shift+Enter for newline')
  return bits.join(' · ')
})

function pushOptimisticUserEvent(text: string): RunTranscriptEvent {
  const ev: RunTranscriptEvent = {
    seq: 0,
    ts: new Date().toISOString(),
    turn: (run.value?.turns ?? 0) + 1,
    type: 'user_message',
    text,
  }
  events.value = [...events.value, ev]
  return ev
}

async function send() {
  if (!canSend.value || !run.value) return
  if (unauthorized.value) {
    toast.error('Not authorized', 'Sign in again, then retry the connection.')
    return
  }
  const text = draft.value.trim()
  draft.value = ''
  const optimistic = pushOptimisticUserEvent(text)

  // Preferred path: stream over the socket.
  if (socket.value && wsState.value === 'open' && socket.value.send(text)) {
    awaitingReply.value = true
    setStatus('running')
    return
  }

  // Fallback: blocking REST — no deltas, just the indicator until the reply lands.
  restPending.value = true
  try {
    const result = await runs.sendMessage(uid.value, text)
    if (result.error) toast.warn('Turn ended with an error', result.error)
    await refetchAfterTurn()
  } catch (e) {
    // Undo the optimistic user entry and give the text back.
    events.value = events.value.filter((ev) => ev !== optimistic)
    draft.value = text
    if (e instanceof ApiError && e.status === 409) {
      toast.warn('Run is busy', 'Wait for the current turn to finish, or interrupt it.')
      setStatus('running')
    } else {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
      toast.error('Message failed', msg)
    }
  } finally {
    restPending.value = false
  }
}

async function interrupt() {
  if (!run.value || interrupting.value) return
  interrupting.value = true
  try {
    if (socket.value && wsState.value === 'open' && socket.value.sendInterrupt()) {
      // The socket's status frame confirms and settles the transcript.
    } else {
      run.value = await runs.interrupt(uid.value)
      await refetchTranscriptFull()
    }
    awaitingReply.value = false
    toast.info('Interrupted', 'The current turn was cut short.')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t interrupt', msg)
  } finally {
    interrupting.value = false
  }
}

async function endRun() {
  if (!run.value || ending.value) return
  ending.value = true
  try {
    run.value = await runs.end(uid.value)
    streamingText.value = ''
    awaitingReply.value = false
    toast.success('Run ended', 'Workspace destroyed — the transcript stays readable.')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t end run', msg)
  } finally {
    ending.value = false
  }
}

async function cancelRun() {
  if (!run.value || cancelling.value) return
  cancelling.value = true
  try {
    run.value = await runs.cancel(uid.value)
    streamingText.value = ''
    awaitingReply.value = false
    socket.value?.close()
    toast.info('Run cancelled', 'The run was stopped and marked cancelled.')
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      toast.warn('Can’t cancel', 'The run is no longer active.')
    } else {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
      toast.error('Couldn’t cancel run', msg)
    }
  } finally {
    cancelling.value = false
  }
}

async function rebuildWorkspace() {
  if (!run.value || rebuilding.value) return
  rebuilding.value = true
  try {
    run.value = await runs.recreateWorkspace(uid.value)
    toast.success('Workspace rebuilding', 'Recreated from the recorded spec.')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t rebuild workspace', msg)
  } finally {
    rebuilding.value = false
  }
}

// ── Presentation ─────────────────────────────────────────────────────────────

const toolResults = computed(() => {
  const value = run.value?.usage?.tool_results
  return Array.isArray(value) ? value : []
})

/** Which run policy governed this run — name+version when resolved, the uid
 *  stub when the policy node is gone, "system default" when none was set. */
const policyLabel = computed(() => {
  if (!run.value?.run_policy_uid) return 'system default'
  if (runPolicy.value) return `${runPolicy.value.name} (v${runPolicy.value.version})`
  return run.value.run_policy_uid.slice(0, 8)
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

const SUMMARY_SECTIONS = [
  { key: 'did', label: 'Did' },
  { key: 'succeeded', label: 'Succeeded' },
  { key: 'failed', label: 'Failed' },
  { key: 'skipped', label: 'Skipped' },
  { key: 'next_steps', label: 'Next steps' },
] as const

const summaryText = computed(() => run.value?.summary?.text || '')

const summarySections = computed(() =>
  SUMMARY_SECTIONS.map((s) => ({
    ...s,
    items: (run.value?.summary?.[s.key] || []).filter(Boolean),
  })).filter((s) => s.items.length),
)

const hasSummary = computed(() => Boolean(summaryText.value) || summarySections.value.length > 0)

const inputText = computed(() => {
  if (!run.value) return ''
  const usage = asRecord(run.value.usage)
  const capturedInput = firstString(
    usage.rendered_instruction,
    usage.instruction,
    usage.input_prompt,
    usage.prompt,
  )
  const capturedSystem = firstString(usage.rendered_system_prompt, usage.system_prompt)
  const input = asRecord(usage.input)
  const intent = firstString(input.intent, agent.value?.prompt)
  const target = Object.keys(run.value.target || {}).length
    ? run.value.target
    : scheduledAgent.value?.target

  const lines = [
    `run_uid: ${run.value.uid}`,
    `playbook: ${run.value.playbook}`,
    `repository_uid: ${run.value.repository_uid}`,
    `executor: ${run.value.executor}`,
    `mode: ${run.value.execution_mode}`,
    '',
    '# Intent',
    intent || '(not recorded)',
    '',
    '# Target',
    JSON.stringify(target || {}, null, 2),
  ]
  if (capturedSystem) lines.push('', '# System prompt', capturedSystem)
  if (capturedInput) lines.push('', '# Rendered instruction', capturedInput)
  return lines.join('\n')
})

/** runStatusVariant predates the shadcn Badge tones (danger/default gone). */
const statusBadgeVariant = computed<BadgeVariants['variant']>(() => {
  const v = runStatusVariant(run.value?.status || 'queued')
  if (v === 'danger') return 'destructive'
  if (v === 'default') return 'secondary'
  return v
})

/** "Paused (quota) — retry N ~time" for quota-paused runs, readable status otherwise. */
const statusLabel = computed(() => (run.value ? runStatusLabel(run.value) : ''))

/** Active = still schedulable/in-flight — the only states cancel accepts. */
const isActiveRun = computed(() =>
  Boolean(run.value && ['queued', 'running', 'paused_quota'].includes(run.value.status)),
)
const isLiveRun = computed(() => Boolean(run.value && isLiveRunStatus(run.value.status)))
const showWorking = computed(
  () => isLiveRun.value || awaitingReply.value || restPending.value,
)

const connectionChip = computed<
  { label: string; variant: BadgeVariants['variant']; retry: boolean } | null
>(() => {
  switch (wsState.value) {
    case 'open':
      return { label: 'live', variant: 'success', retry: false }
    case 'connecting':
      return { label: 'connecting…', variant: 'default', retry: false }
    case 'reconnecting':
      return { label: 'reconnecting…', variant: 'warn', retry: false }
    case 'unavailable':
      return { label: 'offline · REST fallback', variant: 'destructive', retry: true }
    case 'unauthorized':
      return { label: 'unauthorized', variant: 'destructive', retry: true }
    default:
      return null
  }
})

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !run">
      <Skeleton class="h-16 w-1/2" />
      <Skeleton class="h-96" />
      <Skeleton class="h-40" />
    </template>

    <ErrorState v-else-if="error && !run" title="Couldn't load run" :message="error">
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <template v-else-if="run">
      <PageHeader :title="run.title || `Run ${run.uid.slice(0, 12)}`">
        <template #breadcrumb>
          <div class="flex flex-wrap items-center gap-2 mb-1">
            <Badge class="uppercase px-1.5 text-[10px]">{{ run.playbook }}</Badge>
            <Badge variant="outline" class="px-1.5 text-[10px]">
              <Bot class="h-3 w-3" /> {{ run.executor }}
            </Badge>
            <Badge v-if="run.provider_label || run.provider_kind" variant="outline" class="px-1.5 text-[10px]" title="LLM provider">
              <Bot class="h-3 w-3" /> {{ run.provider_label || run.provider_kind }}
              <span v-if="run.provider_model" class="font-mono">· {{ run.provider_model }}</span>
            </Badge>
            <Badge v-if="run.sandbox_uid" variant="outline" class="px-1.5 text-[10px]" title="Workspace">
              <FolderArchive class="h-3 w-3" />
              <span class="font-mono">{{ run.sandbox_uid.slice(0, 8) }}</span>
            </Badge>
            <Badge v-else variant="outline" class="px-1.5 text-[10px]" title="Workspace destroyed or expired">
              <FolderArchive class="h-3 w-3" /> no workspace
            </Badge>
            <RouterLink
              v-if="run.linked_ticket_uid"
              :to="{ name: 'ticket-detail', params: { uid: run.linked_ticket_uid } }"
            >
              <Badge variant="info" class="px-1.5 text-[10px]">
                <SquareKanban class="h-3 w-3" />
                <span class="font-mono">{{ run.linked_ticket_uid.slice(0, 8) }}</span>
              </Badge>
            </RouterLink>
            <RouterLink
              v-if="run.linked_pr_uid"
              :to="{ name: 'pull-request-detail', params: { uid: run.linked_pr_uid } }"
            >
              <Badge variant="info" class="px-1.5 text-[10px]">
                <GitPullRequest class="h-3 w-3" />
                <span class="font-mono">{{ run.linked_pr_uid.slice(0, 8) }}</span>
              </Badge>
            </RouterLink>
            <RouterLink
              v-if="run.linked_finding_uid"
              :to="{ name: 'finding-detail', params: { uid: run.linked_finding_uid } }"
            >
              <Badge variant="info" class="px-1.5 text-[10px]">
                <ClipboardList class="h-3 w-3" />
                <span class="font-mono">{{ run.linked_finding_uid.slice(0, 8) }}</span>
              </Badge>
            </RouterLink>
            <Badge v-if="connectionChip" :variant="connectionChip.variant" class="px-1.5 text-[10px]">
              <Wifi v-if="!connectionChip.retry" class="h-3 w-3" />
              <WifiOff v-else class="h-3 w-3" />
              {{ connectionChip.label }}
            </Badge>
            <button
              v-if="connectionChip?.retry && !unauthorized"
              class="text-xs text-primary hover:underline"
              @click="socket?.reconnect()"
            >
              Retry
            </button>
          </div>
        </template>

        <div class="flex flex-wrap items-center gap-2">
          <Badge :variant="statusBadgeVariant" class="font-mono uppercase">{{ statusLabel }}</Badge>
          <Button
            v-if="!isLiveRun && !run.agent_uid"
            variant="outline"
            size="sm"
            @click="saveAgentOpen = true"
          >
            <Save /> Save as agent
          </Button>
          <Button
            v-if="run.status === 'running'"
            variant="outline"
            size="sm"
            :loading="interrupting"
            @click="interrupt"
          >
            <CircleStop /> Interrupt
          </Button>
          <Button
            v-if="canCompose && workspaceGone"
            variant="outline"
            size="sm"
            :loading="rebuilding"
            @click="rebuildWorkspace"
          >
            <RefreshCw /> Rebuild workspace
          </Button>
          <Button
            v-if="isActiveRun"
            variant="destructive"
            size="sm"
            :loading="cancelling"
            @click="cancelRun"
          >
            <Ban /> Cancel run
          </Button>
          <Button
            v-if="run.status === 'awaiting_input'"
            variant="destructive"
            size="sm"
            :loading="ending"
            @click="endRun"
          >
            <OctagonX /> End run
          </Button>
        </div>
      </PageHeader>

      <p class="text-muted-foreground text-sm">
        Trigger: {{ run.trigger }} · Triggered by: {{ run.triggered_by || '—' }}
        <template v-if="agent">
          · Agent:
          <router-link
            :to="{ name: 'agent-detail', params: { uid: agent.uid } }"
            class="text-primary hover:underline"
          >{{ agent.title }}</router-link>
        </template>
        <template v-if="scheduledAgent">
          · Schedule:
          <router-link
            :to="{ name: 'scheduled-agent-detail', params: { uid: scheduledAgent.uid } }"
            class="text-primary hover:underline"
          >{{ scheduledAgent.title || scheduledAgent.agent_title }}</router-link>
        </template>
      </p>

      <section class="space-y-3">
        <Tabs v-model="activeTab">
          <TabsList class="max-w-full overflow-x-auto">
            <TabsTrigger value="conversation">Conversation</TabsTrigger>
            <TabsTrigger value="files">{{ filesTabLabel }}</TabsTrigger>
            <TabsTrigger value="input">Input</TabsTrigger>
            <TabsTrigger value="raw">Raw</TabsTrigger>
          </TabsList>
        </Tabs>

        <Card v-if="activeTab === 'conversation'">
          <CardContent class="p-0">
            <RunTranscript :events="events" :live="showWorking" :streaming-text="streamingText" />

            <!-- Composer -->
            <div class="shrink-0 border-t p-4 space-y-1.5">
              <div class="flex items-end gap-2">
                <Textarea
                  v-model="draft"
                  :rows="2"
                  class="resize-none"
                  :disabled="!canCompose"
                  :placeholder="canCompose ? 'Message the agent…' : 'The agent is working…'"
                  @keydown.enter.exact.prevent="send"
                />
                <Button
                  class="shrink-0"
                  :disabled="!canSend"
                  :loading="restPending"
                  @click="send"
                >
                  <SendHorizontal /> Send
                </Button>
              </div>
              <p class="text-[11px] text-muted-foreground">{{ inputHint }}</p>
            </div>
          </CardContent>
        </Card>

        <RunFilesPanel
          v-if="filesTabOpened"
          v-show="activeTab === 'files'"
          :run-uid="uid"
          :refresh-key="filesRefreshKey"
          :live="isLiveRun"
          @loaded="changedFilesCount = $event"
        />

        <div v-if="activeTab === 'input'" class="terminal-shell">
          <div class="terminal-topbar">
            <span>run input</span>
            <span>{{ run.title || scheduledAgent?.title || agent?.title || run.playbook }}</span>
          </div>
          <pre class="terminal-output">{{ inputText }}</pre>
        </div>

        <div v-if="activeTab === 'raw'" class="terminal-shell">
          <div class="terminal-topbar">
            <span>raw transcript</span>
            <span>{{ run.raw_artifact_uri || 'no artifact' }}</span>
          </div>
          <div v-if="artifactError" class="text-sm text-bad p-3">{{ artifactError }}</div>
          <pre v-else-if="rawArtifact" class="terminal-output">{{ rawArtifact }}</pre>
          <div v-else class="text-muted-foreground text-sm p-3">Transcript will appear when the run writes an artifact.</div>
        </div>
      </section>

      <Card v-if="hasSummary">
        <CardHeader class="p-4"><h2 class="font-semibold text-sm">Run summary</h2></CardHeader>
        <CardContent class="p-4 pt-0">
          <p v-if="summaryText" class="text-sm whitespace-pre-wrap">{{ summaryText }}</p>
          <div
            v-if="summarySections.length"
            class="mt-3 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3"
          >
            <div v-for="section in summarySections" :key="section.key">
              <h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                {{ section.label }}
              </h3>
              <ul class="mt-1 text-xs space-y-1 list-disc pl-4">
                <li v-for="(item, i) in section.items" :key="i">{{ item }}</li>
              </ul>
            </div>
          </div>
        </CardContent>
      </Card>

      <section class="grid grid-cols-1 xl:grid-cols-[360px_1fr] gap-4">
        <Card>
          <CardHeader class="p-4"><h2 class="font-semibold text-sm">Run facts</h2></CardHeader>
          <CardContent class="p-4 pt-0">
            <dl class="text-xs grid grid-cols-[110px_1fr] gap-x-2 gap-y-1">
              <dt class="text-muted-foreground">playbook</dt><dd>{{ run.playbook }}</dd>
              <dt class="text-muted-foreground">policy</dt>
              <dd :title="runPolicy?.description || ''">
                {{ policyLabel }}
                <span v-if="policyLimits" class="block text-muted-foreground">{{ policyLimits }}</span>
              </dd>
              <dt class="text-muted-foreground">turns</dt><dd>{{ run.turns }}</dd>
              <template v-if="run.provider_label || run.provider_kind">
                <dt class="text-muted-foreground">provider</dt>
                <dd>
                  <div>{{ run.provider_label || run.provider_kind }}</div>
                  <div v-if="run.provider_model" class="text-muted-foreground font-mono">{{ run.provider_model }}</div>
                </dd>
              </template>
              <dt class="text-muted-foreground">started</dt><dd>{{ run.started_at || '—' }}</dd>
              <dt class="text-muted-foreground">last activity</dt><dd>{{ run.last_activity_at || '—' }}</dd>
              <dt class="text-muted-foreground">ended</dt><dd>{{ run.ended_at || '—' }}</dd>
              <dt class="text-muted-foreground">duration</dt><dd>{{ run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : '—' }}</dd>
              <dt class="text-muted-foreground">workspace</dt><dd class="font-mono">{{ run.sandbox_uid || 'destroyed' }}</dd>
              <dt class="text-muted-foreground">parse</dt><dd>{{ run.parse_status }}</dd>
            </dl>
          </CardContent>
        </Card>
        <Card>
          <CardHeader class="p-4"><h2 class="font-semibold text-sm">Output refs</h2></CardHeader>
          <CardContent class="p-4 pt-0">
            <ul v-if="run.output_refs.length" class="text-xs space-y-1">
              <li v-for="ref in run.output_refs" :key="ref" class="font-mono break-all">{{ ref }}</li>
            </ul>
            <div v-else class="text-xs text-muted-foreground">No refs yet.</div>
          </CardContent>
        </Card>
      </section>

      <Card>
        <CardHeader class="p-4 flex-row items-center justify-between">
          <h2 class="font-semibold text-sm">Findings from this run</h2>
          <span class="text-xs text-muted-foreground">{{ runFindings.length }} linked</span>
        </CardHeader>
        <CardContent class="p-4 pt-0">
          <div v-if="runFindings.length" class="space-y-2">
            <RouterLink
              v-for="finding in runFindings"
              :key="finding.uid"
              :to="{ name: 'finding-detail', params: { uid: finding.uid } }"
              class="card-interactive block rounded-sm border bg-muted p-3 hover:border-primary/60"
            >
              <div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>{{ finding.kind }}</span>
                <span>{{ finding.severity }}</span>
                <span>{{ finding.status }}</span>
              </div>
              <div class="mt-1 text-sm font-medium">{{ finding.title }}</div>
              <div v-if="finding.why_it_matters" class="mt-1 text-xs text-muted-foreground line-clamp-2">
                {{ finding.why_it_matters }}
              </div>
            </RouterLink>
          </div>
          <div v-else class="text-xs text-muted-foreground">
            No findings are linked to this run yet.
          </div>
        </CardContent>
      </Card>

      <Card v-if="toolResults.length">
        <CardHeader class="p-4"><h2 class="font-semibold text-sm">Agent activity</h2></CardHeader>
        <CardContent class="p-4 pt-0">
          <div class="space-y-2">
            <div v-for="(t, i) in toolResults" :key="i" class="rounded-sm bg-muted p-2 text-xs overflow-x-auto">
              <div class="font-mono text-muted-foreground">{{ t.tool || `tool-${i + 1}` }}</div>
              <pre class="mt-1 whitespace-pre-wrap">{{ JSON.stringify(t.result || t.error || t, null, 2) }}</pre>
            </div>
          </div>
        </CardContent>
      </Card>

      <div v-if="run.error" class="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
        <strong>Error:</strong> {{ run.error }}
      </div>

      <CommentThread
        subject-type="run"
        :subject-uid="run.uid"
        :repository-uid="run.repository_uid"
        title="Discussion"
      />
      <SaveAsAgentDialog v-model:open="saveAgentOpen" :prefill="saveAgentPrefill()" />
    </template>
  </div>
</template>

<style scoped>
.terminal-shell {
  background: #0b0f14;
  border: 1px solid #243244;
  border-radius: 6px;
  overflow: hidden;
}

.terminal-topbar {
  align-items: center;
  background: #111827;
  color: #9ca3af;
  display: flex;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  justify-content: space-between;
  padding: 8px 10px;
  text-transform: uppercase;
}

.terminal-output {
  color: #d1fae5;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  margin: 0;
  max-height: 520px;
  min-height: 320px;
  overflow: auto;
  padding: 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
