<script setup lang="ts">
// Self-contained live chat against one run — the thread's conversation pane.
// Trimmed adaptation of RunDetailView's transcript + socket + composer wiring
// (no findings/files/raw tabs); the run swap on implement is handled by
// watching runUid.
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { CircleStop, SendHorizontal } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import RunTranscript from '@/components/runs/RunTranscript.vue'
import { useToast } from '@/composables/useToast'
import { useRunSocket, type RunSocketState } from '@/composables/useRunSocket'
import { acceptsFollowUp, runStatusLabel, runStatusVariant } from '@/lib/runStatus'
import { ApiError } from '@/services/api'
import { useRunStore } from '@/stores/runStore'
import type { AgentTodo, RunDTO, RunStatus, RunTranscriptEvent } from '@/types/api'

const props = defineProps<{
  /** Every run in the thread, oldest first — earlier runs render as the
   *  read-only head of ONE continuous conversation. */
  runUids: string[]
  /** The run currently accepting messages (composer + live socket). */
  runUid: string
}>()
const emit = defineEmits<{
  (e: 'turn-settled'): void
  (e: 'todos', todos: AgentTodo[]): void
}>()

const runs = useRunStore()
const toast = useToast()

const run = ref<RunDTO | null>(null)
/** Narrated feed by default; raw transcript on demand. */
const narrated = ref(true)
const events = ref<RunTranscriptEvent[]>([])
/** Read-only transcripts of the thread's EARLIER runs, stitched above the
 *  active run so the whole thread reads as one conversation. */
const priorEvents = ref<RunTranscriptEvent[]>([])

async function loadPriorRuns() {
  const prior = props.runUids.filter((uid) => uid !== props.runUid)
  const out: RunTranscriptEvent[] = []
  for (const uid of prior) {
    try {
      const chunk = await runs.getTranscript(uid, 0)
      out.push(...chunk.events)
      out.push({
        seq: 0,
        ts: '',
        turn: 0,
        type: 'system',
        text: 'conversation continues in a new workspace',
      })
    } catch {
      /* an expired/errored earlier run must not block the live conversation */
    }
  }
  priorEvents.value = out
}

const allEvents = computed(() => [...priorEvents.value, ...events.value])

// Agent working plan: mirror of the LAST TodoWrite call in the conversation.
const todos = computed<AgentTodo[]>(() => {
  for (let i = allEvents.value.length - 1; i >= 0; i--) {
    const e = allEvents.value[i]
    if (e.type !== 'tool_use' || e.name !== 'TodoWrite') continue
    try {
      const parsed = JSON.parse(e.input || '{}')
      const list = Array.isArray(parsed?.todos) ? parsed.todos : []
      return list
        .filter((t: unknown): t is Record<string, string> => typeof t === 'object' && t !== null)
        .map((t: Record<string, string>) => ({
          content: String(t.content ?? ''),
          status: (['pending', 'in_progress', 'completed'].includes(t.status)
            ? t.status
            : 'pending') as AgentTodo['status'],
          activeForm: t.activeForm ? String(t.activeForm) : undefined,
        }))
        .filter((t: AgentTodo) => t.content)
    } catch {
      return []
    }
  }
  return []
})
watch(todos, (v) => emit('todos', v), { immediate: true })
const lastSeq = ref(0)
const draft = ref('')
const awaitingReply = ref(false)
const streamingText = ref('')
const restPending = ref(false)
const interrupting = ref(false)

type Socket = ReturnType<typeof useRunSocket>
const socket = shallowRef<Socket | null>(null)
const wsState = computed<RunSocketState>(() => socket.value?.state.value ?? 'idle')

function setStatus(status: RunStatus) {
  if (run.value) run.value = { ...run.value, status }
}

let settling = false
async function refetchAfterTurn() {
  if (settling) return
  settling = true
  try {
    await refetchTranscriptFull()
    run.value = await runs.get(props.runUid)
    emit('turn-settled')
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
  if (event.type === 'user_message') {
    events.value = events.value.filter(
      (e) => !(e.seq === 0 && e.type === 'user_message' && e.text === event.text),
    )
  }
  events.value = [...events.value, event]
}

function openSocket() {
  socket.value?.close()
  socket.value = useRunSocket(props.runUid, {
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
      if (code === 4404) return
      toast.error('Not authorized', 'Your credentials were rejected. Sign in again and retry.')
    },
  })
  socket.value.connect()
}

async function refetchTranscriptFull() {
  const chunk = await runs.getTranscript(props.runUid, 0)
  events.value = chunk.events
  lastSeq.value = chunk.last_seq
}

async function load() {
  try {
    await loadPriorRuns()
    run.value = await runs.get(props.runUid)
    await refetchTranscriptFull()
    openSocket()
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t load conversation', msg)
  }
}

onMounted(load)
onBeforeUnmount(() => socket.value?.close())
watch(
  () => props.runUid,
  () => {
    // Run swap (implement/fix dispatched): the finished conversation moves
    // into the stitched read-only head; the pane stays ONE conversation.
    events.value = []
    lastSeq.value = 0
    streamingText.value = ''
    awaitingReply.value = false
    void load()
  },
)

const canCompose = computed(() => Boolean(run.value && acceptsFollowUp(run.value.status)))
const busy = computed(
  () => run.value?.status === 'running' || awaitingReply.value || restPending.value,
)
const unauthorized = computed(() => wsState.value === 'unauthorized')
const canSend = computed(
  () => canCompose.value && !unauthorized.value && !busy.value && draft.value.trim().length > 0,
)
const showWorking = computed(() => busy.value)
const statusLabel = computed(() => (run.value ? runStatusLabel(run.value) : ''))
const statusVariant = computed(() => {
  const v = runStatusVariant(run.value?.status || 'queued')
  if (v === 'danger') return 'destructive' as const
  if (v === 'default') return 'secondary' as const
  return v
})

const inputHint = computed(() => {
  if (!run.value) return ''
  if (unauthorized.value) return 'Not authorized for this run — sign in again, then retry.'
  if (busy.value) return 'The agent is working — interrupt to cut the turn short.'
  if (run.value.status === 'queued') return 'The workspace is being prepared…'
  const bits: string[] = []
  if (run.value.status === 'failed') bits.push('Run failed — replying retries in the same conversation.')
  else if (run.value.status === 'ended') bits.push('Run ended — replying reopens the conversation.')
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
  const text = draft.value.trim()
  draft.value = ''
  await sendText(text)
}

/** Programmatic send — used by the thread view to deliver question answers
 *  into the conversation. */
async function sendText(text: string) {
  if (!run.value || !text.trim()) return
  const optimistic = pushOptimisticUserEvent(text)

  if (socket.value && wsState.value === 'open' && socket.value.send(text)) {
    awaitingReply.value = true
    setStatus('running')
    return
  }

  restPending.value = true
  try {
    const result = await runs.sendMessage(props.runUid, text)
    if (result.error) toast.warn('Turn ended with an error', result.error)
    await refetchAfterTurn()
  } catch (e) {
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

defineExpose({ sendText })

async function interrupt() {
  if (!run.value || interrupting.value) return
  interrupting.value = true
  try {
    if (socket.value && wsState.value === 'open' && socket.value.sendInterrupt()) {
      // The socket's status frame confirms and settles the transcript.
    } else {
      run.value = await runs.interrupt(props.runUid)
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
</script>

<template>
  <Card class="flex min-h-0 flex-1 flex-col">
    <CardContent class="flex min-h-0 flex-1 flex-col p-0">
      <div class="flex items-center gap-2 border-b px-4 py-2">
        <Badge v-if="run" :variant="statusVariant" class="font-mono uppercase">
          {{ statusLabel }}
        </Badge>
        <Button
          v-if="run?.status === 'running'"
          variant="outline"
          size="sm"
          :loading="interrupting"
          @click="interrupt"
        >
          <CircleStop /> Interrupt
        </Button>
        <button
          type="button"
          class="ml-auto text-xs text-muted-foreground hover:text-foreground"
          @click="narrated = !narrated"
        >
          {{ narrated ? 'Raw transcript' : 'Narrated view' }}
        </button>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto">
        <RunTranscript
          :events="allEvents"
          :live="showWorking"
          :streaming-text="streamingText"
          :narrated="narrated"
        />
      </div>

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
          <Button class="shrink-0" :disabled="!canSend" :loading="restPending" @click="send">
            <SendHorizontal /> Send
          </Button>
        </div>
        <p class="text-[11px] text-muted-foreground">{{ inputHint }}</p>
      </div>
    </CardContent>
  </Card>
</template>
