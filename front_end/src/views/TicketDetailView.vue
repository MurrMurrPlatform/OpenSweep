<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  CheckCircle2,
  Circle,
  ClipboardList,
  FileText,
  GitPullRequest,
  Layers,
  ListChecks,
  MessagesSquare,
  Pencil,
  Plus,
  Search,
  Unlink,
} from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useThreadStore } from '@/stores/threadStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { useDiscussions } from '@/composables/useDiscussions'
import { useDiscussInRun } from '@/composables/useDiscussInRun'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
import { MarkdownView } from '@/components/ui/markdown'
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
import TicketCard from '@/components/tickets/TicketCard.vue'
import TicketDialog from '@/components/tickets/TicketDialog.vue'
import TicketOriginBadge from '@/components/tickets/TicketOriginBadge.vue'
import TicketStatusPipeline from '@/components/tickets/TicketStatusPipeline.vue'
import TicketTransitionButtons from '@/components/tickets/TicketTransitionButtons.vue'
import CommentThread from '@/components/comments/CommentThread.vue'
import DiscussionChip from '@/components/runs/DiscussionChip.vue'
import { STATUS_LABELS, priorityVariant, statusVariant } from '@/components/tickets/ticketMeta'
import type { TicketDTO } from '@/types/api'

const route = useRoute()
const router = useRouter()
const store = useTicketStore()
const repos = useRepositoryStore()
const toast = useToast()

const ticket = ref<TicketDTO | null>(null)
const children = ref<TicketDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const editOpen = ref(false)
const subticketOpen = ref(false)

// Open chat runs linked to this ticket — a non-blocking discussion chip.
const { discussions } = useDiscussions(() =>
  ticket.value ? { linked_ticket_uid: ticket.value.uid } : null,
)
const { discussing, discuss: discussInRun } = useDiscussInRun(() =>
  ticket.value
    ? {
        repository_uid: ticket.value.repository_uid,
        title: `Ticket: ${ticket.value.title || ticket.value.uid.slice(0, 8)}`,
        linked_ticket_uid: ticket.value.uid,
      }
    : null,
)

const uid = computed(() => String(route.params.uid))

async function load() {
  loading.value = true
  error.value = null
  try {
    const [detail] = await Promise.all([
      store.getTicket(uid.value),
      repos.loaded ? Promise.resolve(repos.list) : repos.fetchAll(),
    ])
    const { children: subtickets, ...ticketFields } = detail
    ticket.value = ticketFields
    children.value = subtickets
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(uid, () => void load())

const repoName = computed(() =>
  ticket.value ? repos.find(ticket.value.repository_uid)?.name ?? ticket.value.repository_uid.slice(0, 8) : '',
)

function onUpdated(updated: TicketDTO) {
  ticket.value = updated
}

function onDeleted() {
  const slug = ticket.value ? repos.find(ticket.value.repository_uid)?.slug : undefined
  if (slug) void router.push({ name: 'tickets', params: { repoSlug: slug } })
  else void router.push('/')
}

function onChildUpdated(updated: TicketDTO) {
  children.value = children.value.map((c) => (c.uid === updated.uid ? updated : c))
}

function onChildDeleted(childUid: string) {
  children.value = children.value.filter((c) => c.uid !== childUid)
}

function onSubticketCreated(created: TicketDTO) {
  children.value = [...children.value, created]
}

// ── Grouping — dissolve the group / leave the parent ────────────────────────

const ungrouping = ref(false)
const ungroupOpen = ref(false)

function ungroupAll() {
  if (!ticket.value || ungrouping.value) return
  ungroupOpen.value = true
}

async function confirmUngroupAll() {
  if (!ticket.value || ungrouping.value) return
  ungroupOpen.value = false
  ungrouping.value = true
  try {
    const result = await store.ungroupTicket(ticket.value.uid)
    children.value = []
    toast.success('Group dissolved', `${result.detached} subticket${result.detached === 1 ? '' : 's'} detached`)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Ungroup failed', msg)
  } finally {
    ungrouping.value = false
  }
}

const leavingGroup = ref(false)

async function leaveGroup() {
  if (!ticket.value || leavingGroup.value) return
  leavingGroup.value = true
  try {
    ticket.value = await store.removeFromGroup(ticket.value.uid)
    toast.success('Removed from group')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Remove from group failed', msg)
  } finally {
    leavingGroup.value = false
  }
}

function fmt(ts?: string | null): string {
  return ts ? new Date(ts).toLocaleString() : '—'
}

// ── Thread — the unified dev flow's conversation per ticket ─────────────────

const threadStore = useThreadStore()
const activeThreadUid = ref<string | null>(null)
const startingThread = ref(false)

watch(
  ticket,
  async (t) => {
    if (!t) return
    try {
      const existing = await threadStore.listThreads({ subject_ticket_uid: t.uid })
      activeThreadUid.value =
        existing.find((x) => x.phase !== 'done' && x.phase !== 'abandoned')?.uid ?? null
    } catch {
      activeThreadUid.value = null
    }
  },
  { immediate: true },
)

async function startThread() {
  if (!ticket.value || startingThread.value) return
  startingThread.value = true
  try {
    const targetUid =
      activeThreadUid.value ?? (await threadStore.createThread(ticket.value.uid)).uid
    void router.push({ name: 'thread-detail', params: { uid: targetUid } })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t start thread', msg)
  } finally {
    startingThread.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !ticket">
      <Skeleton class="h-12 w-2/3" />
      <div class="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px] items-start">
        <div class="space-y-4">
          <Skeleton class="h-48" />
          <Skeleton class="h-48" />
        </div>
        <div class="space-y-4">
          <Skeleton class="h-32" />
          <Skeleton class="h-64" />
        </div>
      </div>
    </template>

    <ErrorState v-else-if="error && !ticket" title="Couldn't load ticket" :message="error">
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <template v-else-if="ticket">
      <PageHeader :title="ticket.title || '(untitled)'">
        <template #breadcrumb>
          <div class="mb-1 flex flex-wrap items-center gap-2">
            <Badge :variant="statusVariant(ticket.status)" class="px-1.5 text-[10px]">{{ STATUS_LABELS[ticket.status] }}</Badge>
            <Badge :variant="priorityVariant(ticket.priority)" class="px-1.5 text-[10px]">{{ ticket.priority }}</Badge>
            <Badge v-if="ticket.size" variant="outline" class="px-1.5 text-[10px]">{{ ticket.size }}</Badge>
            <TicketOriginBadge :origin="ticket.origin" />
            <Badge v-for="label in ticket.labels" :key="label" variant="secondary" class="px-1.5 text-[10px]">{{ label }}</Badge>
            <span class="text-xs text-muted-foreground">{{ repoName }}</span>
          </div>
        </template>

        <div class="flex flex-wrap items-center gap-2">
          <DiscussionChip v-for="chat in discussions" :key="chat.uid" :run="chat" />
          <!-- The thread IS the refine→plan→implement flow; the old one-shot
               Refine/Implement buttons were removed to keep one path. -->
          <Button size="sm" :loading="startingThread" @click="startThread">
            <MessagesSquare /> {{ activeThreadUid ? 'Open thread' : 'Start thread' }}
          </Button>
          <Button variant="outline" size="sm" :loading="discussing" @click="discussInRun">
            <MessagesSquare /> Discuss
          </Button>
          <Button variant="outline" size="sm" @click="editOpen = true">
            <Pencil /> Edit
          </Button>
          <TicketTransitionButtons :ticket="ticket" @updated="onUpdated" @deleted="onDeleted" />
        </div>
      </PageHeader>

      <div class="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px] items-start">
        <!-- ── Main column ─────────────────────────────────────────────── -->
        <div class="min-w-0 space-y-4">
          <!-- Description -->
          <Card>
            <CardHeader class="p-6 pb-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <FileText class="size-4 text-muted-foreground" /> Description
              </CardTitle>
            </CardHeader>
            <CardContent class="p-6 pt-4">
              <MarkdownView
                v-if="ticket.description"
                :model-value="ticket.description"
                preview-only
              />
              <div v-else class="text-sm text-muted-foreground">No description yet — edit the ticket to add one.</div>
            </CardContent>
          </Card>

          <!-- Implementation plan (thread-authored ticket metadata) -->
          <Card v-if="ticket.plan?.markdown">
            <CardHeader class="p-6 pb-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <ListChecks class="size-4 text-muted-foreground" /> Plan
                <Badge
                  :variant="ticket.plan.state === 'approved' ? 'success' : 'secondary'"
                  class="px-1.5 text-[10px]"
                >
                  {{ ticket.plan.state }}
                </Badge>
                <RouterLink
                  v-if="ticket.plan.thread_uid"
                  :to="{ name: 'thread-detail', params: { uid: ticket.plan.thread_uid } }"
                  class="ml-auto text-xs font-normal text-primary hover:underline"
                >
                  Open thread →
                </RouterLink>
              </CardTitle>
            </CardHeader>
            <CardContent class="p-6 pt-4">
              <MarkdownView :model-value="ticket.plan.markdown" preview-only />
            </CardContent>
          </Card>

          <!-- Acceptance criteria -->
          <Card>
            <CardHeader class="p-6 pb-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <ListChecks class="size-4 text-muted-foreground" />
                Acceptance criteria
                <span class="text-xs font-normal text-muted-foreground">· {{ ticket.acceptance_criteria.length }}</span>
              </CardTitle>
            </CardHeader>
            <CardContent class="p-0 pt-2">
              <div v-if="ticket.acceptance_criteria.length === 0" class="px-6 pb-5 pt-3 text-sm text-muted-foreground">
                No acceptance criteria. Review runs verify each criterion against the PR — add some via Edit.
              </div>
              <ul v-else class="divide-y border-t">
                <li
                  v-for="(criterion, idx) in ticket.acceptance_criteria"
                  :key="idx"
                  class="flex items-start gap-2.5 px-6 py-2.5 text-sm"
                >
                  <CheckCircle2 v-if="ticket.status === 'done'" class="mt-0.5 size-4 shrink-0 text-good" />
                  <Circle v-else class="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <span>{{ criterion }}</span>
                </li>
              </ul>
            </CardContent>
          </Card>

          <!-- Subtickets -->
          <Card>
            <CardHeader class="flex-row items-center justify-between gap-2 p-6 pb-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <Layers class="size-4 text-muted-foreground" />
                Subtickets
                <span class="text-xs font-normal text-muted-foreground">· {{ children.length }}</span>
              </CardTitle>
              <div class="flex items-center gap-2">
                <Button
                  v-if="children.length"
                  variant="ghost"
                  size="sm"
                  :loading="ungrouping"
                  @click="ungroupAll"
                >
                  <Unlink /> Ungroup all
                </Button>
                <Button variant="outline" size="sm" @click="subticketOpen = true">
                  <Plus /> New subticket
                </Button>
              </div>
            </CardHeader>
            <CardContent class="p-6 pt-4">
              <div v-if="children.length === 0" class="text-sm text-muted-foreground">
                No subtickets. Split large tickets so each child converges on its own PR.
              </div>
              <div v-else class="space-y-2">
                <div v-for="child in children" :key="child.uid" class="relative">
                  <Badge variant="outline" class="absolute right-3 top-3 z-10 px-1.5 text-[10px]">
                    {{ STATUS_LABELS[child.status] }}
                  </Badge>
                  <TicketCard :ticket="child" @updated="onChildUpdated" @deleted="onChildDeleted" />
                </div>
              </div>
            </CardContent>
          </Card>

          <!-- Linked PRs -->
          <Card>
            <CardHeader class="p-6 pb-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <GitPullRequest class="size-4 text-muted-foreground" />
                Linked pull requests
                <span class="text-xs font-normal text-muted-foreground">· {{ ticket.linked_pr_uids.length }}</span>
              </CardTitle>
            </CardHeader>
            <CardContent class="p-0 pt-2">
              <div v-if="ticket.linked_pr_uids.length === 0" class="px-6 pb-5 pt-3 text-sm text-muted-foreground">
                No PRs yet. Implement-runs open one, or link an existing PR from its detail page.
              </div>
              <ul v-else class="divide-y border-t">
                <li v-for="prUid in ticket.linked_pr_uids" :key="prUid">
                  <RouterLink
                    :to="{ name: 'pull-request-detail', params: { uid: prUid } }"
                    class="flex items-center gap-2 px-6 py-2.5 text-sm transition-colors hover:text-primary"
                  >
                    <GitPullRequest class="size-4 text-muted-foreground" />
                    <span class="font-mono">{{ prUid.slice(0, 8) }}</span>
                  </RouterLink>
                </li>
              </ul>
            </CardContent>
          </Card>

          <!-- Linked findings -->
          <Card v-if="ticket.linked_finding_uids.length">
            <CardHeader class="p-6 pb-0">
              <CardTitle class="flex items-center gap-2 text-base">
                <ClipboardList class="size-4 text-muted-foreground" />
                Linked findings
                <span class="text-xs font-normal text-muted-foreground">· {{ ticket.linked_finding_uids.length }}</span>
              </CardTitle>
            </CardHeader>
            <CardContent class="p-0 pt-2">
              <ul class="divide-y border-t">
                <li v-for="findingUid in ticket.linked_finding_uids" :key="findingUid">
                  <RouterLink
                    :to="{ name: 'finding-detail', params: { uid: findingUid } }"
                    class="flex items-center gap-2 px-6 py-2.5 text-sm transition-colors hover:text-primary"
                  >
                    <ClipboardList class="size-4 text-muted-foreground" />
                    <span class="font-mono">{{ findingUid.slice(0, 8) }}</span>
                    <Badge v-if="findingUid === ticket.origin_finding_uid" variant="info" class="px-1.5 text-[10px]">origin</Badge>
                  </RouterLink>
                </li>
              </ul>
            </CardContent>
          </Card>

          <!-- Discussion -->
          <CommentThread subject-type="ticket" :subject-uid="ticket.uid" :repository-uid="ticket.repository_uid" title="Discussion" />
        </div>

        <!-- ── Sidebar ────────────────────────────────────────────────── -->
        <div class="space-y-4">
          <Card>
            <CardHeader class="p-6 pb-0">
              <CardTitle class="text-base">Pipeline</CardTitle>
            </CardHeader>
            <CardContent class="p-6 pt-4">
              <TicketStatusPipeline :status="ticket.status" />
            </CardContent>
          </Card>

          <Card>
            <CardHeader class="p-6 pb-0">
              <CardTitle class="text-base">Details</CardTitle>
            </CardHeader>
            <CardContent class="p-6 pt-4">
              <dl class="grid grid-cols-[92px_1fr] gap-x-2 gap-y-1.5 text-xs">
                <dt class="text-muted-foreground">repository</dt>
                <dd>{{ repoName }}</dd>
                <dt class="text-muted-foreground">origin</dt>
                <dd>{{ ticket.origin }}</dd>
                <template v-if="ticket.origin_finding_uid">
                  <dt class="text-muted-foreground">from finding</dt>
                  <dd>
                    <RouterLink
                      :to="{ name: 'finding-detail', params: { uid: ticket.origin_finding_uid } }"
                      class="inline-flex items-center gap-1 font-mono text-primary hover:underline"
                    >
                      <Search class="size-3" /> {{ ticket.origin_finding_uid.slice(0, 8) }}
                    </RouterLink>
                  </dd>
                </template>
                <template v-if="ticket.parent_ticket_uid">
                  <dt class="text-muted-foreground">parent</dt>
                  <dd class="flex items-center gap-1.5">
                    <RouterLink
                      :to="{ name: 'ticket-detail', params: { uid: ticket.parent_ticket_uid } }"
                      class="font-mono text-primary hover:underline"
                    >
                      {{ ticket.parent_ticket_uid.slice(0, 8) }}
                    </RouterLink>
                    <button
                      type="button"
                      class="inline-flex items-center gap-0.5 text-muted-foreground hover:text-destructive disabled:opacity-50"
                      :disabled="leavingGroup"
                      title="Remove from group"
                      @click="leaveGroup"
                    >
                      <Unlink class="size-3" /> leave
                    </button>
                  </dd>
                </template>
                <template v-if="ticket.assignee_uid">
                  <dt class="text-muted-foreground">assignee</dt>
                  <dd class="font-mono">{{ ticket.assignee_uid.slice(0, 8) }}</dd>
                </template>
                <dt class="text-muted-foreground">approved by</dt>
                <dd>{{ ticket.approved_by || '—' }}</dd>
                <dt class="text-muted-foreground">approved at</dt>
                <dd>{{ fmt(ticket.approved_at) }}</dd>
                <dt class="text-muted-foreground">done at</dt>
                <dd>{{ fmt(ticket.done_at) }}</dd>
                <dt class="text-muted-foreground">created</dt>
                <dd>{{ fmt(ticket.created_at) }}</dd>
                <dt class="text-muted-foreground">updated</dt>
                <dd>{{ fmt(ticket.updated_at) }}</dd>
              </dl>
            </CardContent>
          </Card>
        </div>
      </div>

      <!-- Edit -->
      <TicketDialog v-model:open="editOpen" :repositories="repos.list" :ticket="ticket" @saved="onUpdated" />
      <!-- New subticket (preseeded with parent_ticket_uid) -->
      <TicketDialog v-model:open="subticketOpen" :repositories="repos.list" :parent="ticket" @saved="onSubticketCreated" />

      <AlertDialog v-model:open="ungroupOpen">
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Detach subtickets</AlertDialogTitle>
            <AlertDialogDescription>
              Detach all {{ children.length }} subtickets from “{{ ticket.title }}”? The subtickets themselves are kept.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              @click="confirmUngroupAll"
            >
              Detach
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </template>
  </div>
</template>
