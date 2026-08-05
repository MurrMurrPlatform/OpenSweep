<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Activity, Bot, CircleStop, MessagesSquare, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { useRunStore } from '@/stores/runStore'
import { useCurrentUserStore } from '@/stores/currentUserStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useActiveRuns } from '@/composables/useActiveRuns'
import { useNowTicker } from '@/composables/useNowTicker'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PLAYBOOK_TO_PRODUCES, producesLabel } from '@/lib/produces'
import {
  formatDuration,
  queuePosition,
  runElapsedMs,
  runPhase,
  runPhaseLabel,
  runStatusLabel,
  runStatusVariant,
} from '@/lib/runStatus'
import { formatRelativeTime } from '@/lib/utils'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import { ErrorState } from '@/components/ui/error-state'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import type { ProducesKind, RunDTO } from '@/types/api'

/** runStatusVariant predates the shadcn Badge tones (danger/default gone). */
function statusBadgeVariant(run: RunDTO): BadgeVariants['variant'] {
  const v = runStatusVariant(run.status, run)
  if (v === 'danger') return 'destructive'
  if (v === 'default') return 'secondary'
  return v
}

// Produces chips filter client-side via the playbook→produces display map —
// playbook stays internal machinery.
const PRODUCES_CHIPS: ProducesKind[] = ['findings', 'answer', 'review-verdict', 'code-changes', 'verification', 'documentation']

const router = useRouter()
const runs = useRunStore()
const toast = useToast()
const currentUser = useCurrentUserStore()
const { uid: repoUid } = useCurrentRepo()
const items = ref<RunDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const producesFilter = ref<ProducesKind | ''>('')
/** Platform admins only: also list @opensweep comment replies + chat sessions. */
const showAgentActivity = ref(false)
/** Drives the live elapsed column — one shared timer for every row. */
const now = useNowTicker()

/** `silent` skips the skeleton so the live heartbeat below never flickers the
 *  table the user is reading. */
async function reload({ silent = false } = {}) {
  if (!repoUid.value) return
  if (!silent) loading.value = true
  error.value = null
  try {
    items.value = await runs.fetchAll({
      repository_uid: repoUid.value,
      surface: showAgentActivity.value && currentUser.isPlatformAdmin ? 'all' : undefined,
    })
  } catch (e: unknown) {
    // A failed background refresh must not blank out a list that is on screen.
    if (!silent) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => void reload())
watch(repoUid, () => void reload())
watch(showAgentActivity, () => void reload())

// Liveness: /runs/active is polled for this repo whether or not anything is in
// flight, so a run dispatched from anywhere (a sweep, a PR review, another tab)
// shows up here — and its status keeps updating — without a Refresh click.
const { activeRuns, signature } = useActiveRuns(
  () => (repoUid.value ? { repository_uid: repoUid.value } : null),
  { pollWhenIdle: true },
)
// The heartbeat fires every tick; only re-fetch the list when the in-flight set
// actually changed, or while something is running (turns/last-activity move).
watch([signature, activeRuns], ([sig], [prevSig]) => {
  if (sig !== prevSig || activeRuns.value.length > 0) void reload({ silent: true })
})

function surfaceLabel(r: RunDTO): string {
  if (r.surface === 'comment') return 'comment reply'
  if (r.surface === 'chat') return 'chat bubble'
  return ''
}

/** Live-ticks while the run is in flight; the recorded duration once it isn't. */
function rowDuration(r: RunDTO): string {
  return formatDuration(runElapsedMs(r, now.value))
}

/** "#2 of 5" among the runs still ahead of it in the queue — null once dispatched. */
function rowQueuePosition(r: RunDTO): string | null {
  const pos = queuePosition(r, items.value)
  if (pos === null) return null
  const total = items.value.filter((x) => x.status === 'queued').length
  return `#${pos} of ${total}`
}

/** Only "starting" is worth calling out beside the status badge — "running"
 *  and "queued" already say themselves via the badge label. */
function rowPhaseSuffix(r: RunDTO): string | null {
  const phase = runPhase(r)
  return phase === 'starting' ? runPhaseLabel(phase) : null
}

const sorted = computed(() =>
  [...items.value]
    .filter(
      (r) => !producesFilter.value || PLAYBOOK_TO_PRODUCES[r.playbook] === producesFilter.value,
    )
    .sort((a, b) => {
      const ta = a.last_activity_at || a.updated_at || a.created_at || ''
      const tb = b.last_activity_at || b.updated_at || b.created_at || ''
      return tb.localeCompare(ta)
    }),
)

// ── Stop (settles a live run so it becomes deletable) ────────────────────────

/** Active = still schedulable/in-flight; `cancel` accepts only these. An
 *  awaiting_input run isn't active but still holds a workspace, so it needs
 *  `end` instead. Either way the run reaches a terminal status. */
function stoppable(r: RunDTO): boolean {
  return ['queued', 'running', 'paused_quota', 'awaiting_input'].includes(r.status)
}

// The confirm dialogs are driven by a separate `open` boolean, not by the
// target ref being non-null: AlertDialogAction closes the dialog (update:open
// fires) before the forwarded @click runs, so deriving `open` from the target
// and nulling it on close would wipe the target before confirmStop/confirmDelete
// could read it.
const stopTarget = ref<RunDTO | null>(null)
const stopOpen = ref(false)
const stoppingUid = ref<string | null>(null)

function requestStop(r: RunDTO) {
  stopTarget.value = r
  stopOpen.value = true
}

function stopVerb(r: RunDTO | null): 'end' | 'cancel' {
  return r?.status === 'awaiting_input' ? 'end' : 'cancel'
}

async function confirmStop() {
  const target = stopTarget.value
  if (!target || stoppingUid.value) return
  stoppingUid.value = target.uid
  try {
    const updated =
      stopVerb(target) === 'end' ? await runs.end(target.uid) : await runs.cancel(target.uid)
    // Patch the row in place so the delete button unlocks without a refetch.
    items.value = items.value.map((r) => (r.uid === updated.uid ? updated : r))
    toast.success('Run stopped', runStatusLabel(updated))
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      toast.warn('Can’t stop run', 'The run is no longer active — refresh the list.')
    } else {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
      toast.error('Couldn’t stop run', msg)
    }
  } finally {
    stoppingUid.value = null
  }
}

// ── Delete (active/awaiting-input runs must be cancelled or ended first) ─────

const deleteTarget = ref<RunDTO | null>(null)
const deleteOpen = ref(false)
const deleting = ref(false)

function requestDelete(r: RunDTO) {
  deleteTarget.value = r
  deleteOpen.value = true
}

function deletable(r: RunDTO): boolean {
  return !stoppable(r)
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target || deleting.value) return
  deleting.value = true
  try {
    await runs.remove(target.uid)
    items.value = items.value.filter((r) => r.uid !== target.uid)
    toast.success('Run deleted', target.title || target.uid.slice(0, 12))
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t delete run', msg)
  } finally {
    deleting.value = false
  }
}

// ── New chat dialog ──────────────────────────────────────────────────────────

const chatOpen = ref(false)
const chatTitle = ref('')
const chatPrompt = ref('')
const creating = ref(false)

watch(chatOpen, (open) => {
  if (open) {
    chatTitle.value = ''
    chatPrompt.value = ''
  }
})

async function startChat() {
  if (!repoUid.value || creating.value) return
  creating.value = true
  try {
    const run = await runs.createRun({
      repository_uid: repoUid.value,
      playbook: 'chat',
      title: chatTitle.value.trim() || undefined,
      prompt: chatPrompt.value.trim() || undefined,
    })
    chatOpen.value = false
    void router.push({ name: 'run-detail', params: { uid: run.uid } })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t start chat', msg)
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Runs"
      subtitle="Every agent conversation — reviews, fixes, chats, one list."
    >
      <Button variant="outline" size="sm" :disabled="loading" @click="reload()">
        <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
      </Button>
      <Button size="sm" @click="chatOpen = true">
        <Plus /> New chat
      </Button>
    </PageHeader>

    <!-- Playbook filter chips -->
    <div class="flex flex-wrap items-center gap-1.5">
      <Button
        :variant="producesFilter === '' ? 'default' : 'outline'"
        size="sm"
        class="rounded-full"
        @click="producesFilter = ''"
      >
        All
      </Button>
      <Button
        v-for="p in PRODUCES_CHIPS"
        :key="p"
        :variant="producesFilter === p ? 'default' : 'outline'"
        size="sm"
        class="rounded-full capitalize"
        @click="producesFilter = p"
      >
        {{ producesLabel(p) }}
      </Button>
      <Button
        v-if="currentUser.isPlatformAdmin"
        :variant="showAgentActivity ? 'default' : 'outline'"
        size="sm"
        class="ml-auto rounded-full"
        @click="showAgentActivity = !showAgentActivity"
      >
        <Bot /> Agent activity
      </Button>
    </div>

    <Card>
      <CardContent class="p-0">
        <!-- Loading -->
        <div v-if="loading" class="p-4 space-y-2">
          <Skeleton v-for="i in 6" :key="i" class="h-10" />
        </div>

        <!-- Error -->
        <div v-else-if="error" class="p-4">
          <ErrorState title="Couldn't load runs" :message="error" class="border-0">
            <Button variant="outline" size="sm" @click="reload()">Retry</Button>
          </ErrorState>
        </div>

        <!-- Empty -->
        <div v-else-if="sorted.length === 0" class="p-4">
          <EmptyState
            :icon="Activity"
            title="No runs yet"
            description="Start a chat, ask a question, or dispatch a review to populate this list."
            class="border-0"
          >
            <Button size="sm" @click="chatOpen = true">
              <Plus /> New chat
            </Button>
          </EmptyState>
        </div>

        <!-- Table -->
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th class="px-4 py-2 font-medium">Run</th>
                <th class="px-4 py-2 font-medium">Playbook</th>
                <th class="px-4 py-2 font-medium">Status</th>
                <th class="px-4 py-2 font-medium">Duration</th>
                <th class="px-4 py-2 font-medium">Turns</th>
                <th class="px-4 py-2 font-medium">Executor</th>
                <th class="px-4 py-2 font-medium">Last activity</th>
                <th class="px-4 py-2"><span class="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in sorted"
                :key="r.uid"
                class="cursor-pointer border-t border-border transition-colors hover:bg-accent"
                @click="$router.push({ name: 'run-detail', params: { uid: r.uid } })"
              >
                <td class="max-w-[280px] px-4 py-2">
                  <RouterLink
                    class="block truncate font-medium underline-offset-2 hover:underline"
                    :to="{ name: 'run-detail', params: { uid: r.uid } }"
                    @click.stop
                  >
                    {{ r.title || `Run ${r.uid.slice(0, 12)}` }}
                  </RouterLink>
                </td>
                <td class="px-4 py-2">
                  <ProducesBadge :produces="PLAYBOOK_TO_PRODUCES[r.playbook] ?? 'findings'" />
                  <Badge v-if="surfaceLabel(r)" variant="secondary" class="ml-1">
                    {{ surfaceLabel(r) }}
                  </Badge>
                </td>
                <td class="px-4 py-2">
                  <Badge :variant="statusBadgeVariant(r)">{{ runStatusLabel(r) }}</Badge>
                  <span v-if="rowQueuePosition(r)" class="ml-1.5 text-xs text-muted-foreground">{{ rowQueuePosition(r) }}</span>
                  <span v-if="rowPhaseSuffix(r)" class="ml-1.5 text-xs text-muted-foreground">{{ rowPhaseSuffix(r) }}</span>
                </td>
                <td class="whitespace-nowrap px-4 py-2 tabular-nums">{{ rowDuration(r) }}</td>
                <td class="px-4 py-2 tabular-nums">{{ r.turns }}</td>
                <td class="whitespace-nowrap px-4 py-2 font-mono">{{ r.executor }}</td>
                <td class="whitespace-nowrap px-4 py-2 text-muted-foreground">{{ formatRelativeTime(r.last_activity_at || r.updated_at) }}</td>
                <!-- Clicks anywhere in the actions cell stop here: a disabled
                     Button has `pointer-events: none`, so without this the
                     click would fall through to the row and navigate away. -->
                <td class="whitespace-nowrap px-2 py-2 text-right" @click.stop>
                  <Button
                    v-if="stoppable(r)"
                    variant="ghost"
                    size="icon-sm"
                    class="text-muted-foreground hover:text-destructive"
                    :loading="stoppingUid === r.uid"
                    :title="
                      stopVerb(r) === 'end'
                        ? 'End run — destroys the workspace, keeps the transcript'
                        : 'Cancel run — stops the in-flight turn'
                    "
                    @click="requestStop(r)"
                  >
                    <CircleStop />
                  </Button>
                  <!-- title lives on the wrapper: a disabled Button takes no
                       hover, so its own tooltip would never show. -->
                  <span
                    class="inline-block"
                    :title="deletable(r) ? 'Delete run' : 'Stop the run before deleting it'"
                  >
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      class="text-muted-foreground hover:text-destructive"
                      :disabled="!deletable(r)"
                      @click="requestDelete(r)"
                    >
                      <Trash2 />
                    </Button>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>

    <!-- New chat dialog -->
    <Dialog v-model:open="chatOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New chat</DialogTitle>
          <DialogDescription>
            Start a conversation with an agent in a fresh workspace clone of this repository.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-3">
          <div class="space-y-1.5">
            <Label for="chat-title">Title (optional)</Label>
            <Input id="chat-title" v-model="chatTitle" placeholder="What is this conversation about?" />
          </div>
          <div class="space-y-1.5">
            <Label for="chat-prompt">First message (optional)</Label>
            <Textarea
              id="chat-prompt"
              v-model="chatPrompt"
              :rows="3"
              placeholder="Ask about the code, or tell the agent what to do…"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="chatOpen = false">Cancel</Button>
          <Button size="sm" :disabled="!repoUid || creating" :loading="creating" @click="startChat">
            <MessagesSquare /> Start chat
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <AlertDialog v-model:open="stopOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {{ stopVerb(stopTarget) === 'end' ? 'End this run?' : 'Cancel this run?' }}
          </AlertDialogTitle>
          <AlertDialogDescription>
            <template v-if="stopVerb(stopTarget) === 'end'">
              “{{ stopTarget?.title || stopTarget?.uid.slice(0, 12) }}” closes and its workspace is
              destroyed. The transcript stays readable, and a follow-up message reopens it.
            </template>
            <template v-else>
              “{{ stopTarget?.title || stopTarget?.uid.slice(0, 12) }}” stops now and any in-flight
              turn is killed. Work the agent hasn’t finished is lost.
            </template>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep running</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmStop"
          >
            {{ stopVerb(stopTarget) === 'end' ? 'End run' : 'Cancel run' }}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this run?</AlertDialogTitle>
          <AlertDialogDescription>
            “{{ deleteTarget?.title || deleteTarget?.uid.slice(0, 12) }}” and its transcript are
            removed permanently. Findings the run produced are kept.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDelete"
          >
            Delete run
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
