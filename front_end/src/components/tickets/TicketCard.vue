<script setup lang="ts">
// Board/list card for a ticket. One contextual primary action stays inline
// (Approve on backlog — Gate 1; Implement on todo/in-progress); every other
// transition plus Delete lives in the kebab menu and the right-click context
// menu so cards stay scannable.
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Check,
  GitPullRequest,
  Layers,
  MessagesSquare,
  MoreHorizontal,
  Trash2,
} from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatRelativeTime } from '@/lib/utils'
import TicketImplementButton from '@/components/tickets/TicketImplementButton.vue'
import TicketOriginBadge from '@/components/tickets/TicketOriginBadge.vue'
import {
  priorityVariant,
  threadPhaseLabel,
  threadPhaseVariant,
  transitionsFor,
  type TicketTransition,
} from '@/components/tickets/ticketMeta'
import type { ThreadDTO, TicketDTO } from '@/types/api'

interface Props {
  ticket: TicketDTO
  subticketCount?: number
  /**
   * This card is already rendered inside its own epic (the Subtickets list),
   * so the "in epic" chip would just repeat the surrounding context. On the
   * board, where members appear loose, the chip is the only thing explaining
   * why the card has no actions.
   */
  nestedInEpic?: boolean
  /**
   * The ticket's ACTIVE thread (non-terminal), when the surface has it —
   * powers the "what does this need from me" badges and swaps Implement for
   * "Continue in thread". Null/omitted = no thread signals on this card.
   */
  thread?: ThreadDTO | null
}
const props = withDefaults(defineProps<Props>(), {
  subticketCount: 0,
  nestedInEpic: false,
  thread: null,
})
const emit = defineEmits<{ updated: [ticket: TicketDTO]; deleted: [uid: string] }>()

const store = useTicketStore()
const toast = useToast()
const router = useRouter()

const busy = ref<string | null>(null)
const approveOpen = ref(false)
const deleteOpen = ref(false)
const archiveOpen = ref(false)

/** Archived cards are read-only history: no transitions, no dispatch. */
const isArchived = computed(() => props.ticket.archived)
const transitions = computed<TicketTransition[]>(() =>
  isArchived.value ? [] : transitionsFor(props.ticket),
)
/** Non-gate moves — the gate (Approve) gets the inline primary button. */
const menuTransitions = computed(() => transitions.value.filter((t) => t.kind !== 'gate'))
/**
 * An epic ships as ONE run and ONE PR against the parent, so the parent is the
 * only thing that passes Gate 1 — approving a member would make it separately
 * implementable and race the epic's own PR. The backend refuses it; the button
 * is hidden so the refusal never has to be discovered.
 */
const inEpic = computed(() => !!props.ticket.parent_ticket_uid)
const canApprove = computed(
  () => props.ticket.status === 'backlog' && !inEpic.value && !isArchived.value,
)
const canDelete = computed(() => props.ticket.status === 'backlog' && !isArchived.value)
// Epic members archive through their parent (mirrors the Gate-1 rule); the
// backend 409s, so the item is hidden rather than discovered.
const canArchive = computed(() => !inEpic.value && !isArchived.value)
const hasMenu = computed(
  () =>
    menuTransitions.value.length > 0 ||
    canApprove.value ||
    canDelete.value ||
    canArchive.value ||
    isArchived.value,
)

// ── Thread signals — what does this ticket need from me? ────────────────────

const activeThread = computed(() =>
  props.thread && props.thread.phase !== 'done' && props.thread.phase !== 'abandoned'
    ? props.thread
    : null,
)
/** One attention badge at most — open questions block the agent, so they
 *  outrank a plan waiting for review. */
const threadAttention = computed<{ label: string; variant: 'warn' | 'info'; title: string } | null>(() => {
  const t = activeThread.value
  if (!t) return null
  if (t.questions_open > 0)
    return {
      label: `${t.questions_open} question${t.questions_open === 1 ? '' : 's'} waiting`,
      variant: 'warn',
      title: 'The agent is blocked on your answers — open the thread to reply',
    }
  if (t.plan_state === 'drafted' && t.phase === 'refining')
    return {
      label: 'Plan ready to review',
      variant: 'info',
      title: 'The agent drafted a plan — approving it starts implementation',
    }
  return null
})

function openTicket() {
  void router.push({ name: 'ticket-detail', params: { uid: props.ticket.uid } })
}

async function transition(t: TicketTransition) {
  if (busy.value) return
  busy.value = t.to
  try {
    const updated = await store.setStatus(props.ticket.uid, t.to)
    approveOpen.value = false
    emit('updated', updated)
    toast.success(t.kind === 'gate' ? 'Ticket approved' : `Moved to ${t.to}`, updated.title)
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error(e instanceof ApiError && e.status === 409 ? 'Illegal transition' : 'Action failed', msg)
  } finally {
    busy.value = null
  }
}

async function confirmRemove() {
  deleteOpen.value = false
  busy.value = 'delete'
  try {
    await store.deleteTicket(props.ticket.uid)
    emit('deleted', props.ticket.uid)
    toast.success('Ticket deleted', props.ticket.title)
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Delete failed', msg)
  } finally {
    busy.value = null
  }
}

async function confirmArchive() {
  archiveOpen.value = false
  busy.value = 'archive'
  try {
    const updated = await store.archiveTicket(props.ticket.uid)
    emit('updated', updated)
    toast.success('Ticket archived', props.ticket.title)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error(e instanceof ApiError && e.status === 409 ? 'Can’t archive' : 'Archive failed', msg)
  } finally {
    busy.value = null
  }
}

async function unarchive() {
  if (busy.value) return
  busy.value = 'unarchive'
  try {
    const updated = await store.unarchiveTicket(props.ticket.uid)
    emit('updated', updated)
    toast.success('Ticket restored', `Back on the board in ${updated.status}`)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Unarchive failed', msg)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <ContextMenu>
    <ContextMenuTrigger as-child>
      <Card class="group/card space-y-2 p-3 transition-colors hover:border-primary/40">
        <div class="flex items-start justify-between gap-1">
          <RouterLink
            :to="{ name: 'ticket-detail', params: { uid: ticket.uid } }"
            class="block min-w-0 text-sm font-medium transition-colors hover:text-primary"
          >
            {{ ticket.title || '(untitled)' }}
          </RouterLink>
          <DropdownMenu v-if="hasMenu">
            <DropdownMenuTrigger as-child>
              <Button
                variant="ghost"
                size="icon-sm"
                class="-mr-1 -mt-1 size-6 shrink-0 text-muted-foreground sm:opacity-0 sm:transition-opacity sm:focus-visible:opacity-100 sm:group-hover/card:opacity-100 sm:data-[state=open]:opacity-100"
                :title="`Actions for “${ticket.title || 'untitled'}”`"
              >
                <MoreHorizontal class="!size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" class="w-48">
              <DropdownMenuItem @select="openTicket">
                <ArrowUpRight /> Open ticket
              </DropdownMenuItem>
              <DropdownMenuSeparator v-if="canApprove || menuTransitions.length" />
              <DropdownMenuItem v-if="canApprove" :disabled="!!busy" @select="approveOpen = true">
                <Check /> Approve — Gate 1
              </DropdownMenuItem>
              <DropdownMenuItem
                v-for="t in menuTransitions"
                :key="t.to"
                :disabled="!!busy"
                @select="transition(t)"
              >
                <ArrowRight v-if="t.kind === 'forward'" />
                <ArrowLeft v-else />
                {{ t.label }}
              </DropdownMenuItem>
              <template v-if="canArchive || isArchived || canDelete">
                <DropdownMenuSeparator />
                <DropdownMenuItem v-if="canArchive" :disabled="!!busy" @select="archiveOpen = true">
                  <Archive /> Archive…
                </DropdownMenuItem>
                <DropdownMenuItem v-if="isArchived" :disabled="!!busy" @select="unarchive">
                  <ArchiveRestore /> Unarchive
                </DropdownMenuItem>
                <DropdownMenuItem
                  v-if="canDelete"
                  class="text-destructive focus:text-destructive"
                  :disabled="!!busy"
                  @select="deleteOpen = true"
                >
                  <Trash2 /> Delete…
                </DropdownMenuItem>
              </template>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        <div class="flex flex-wrap items-center gap-1.5">
          <Badge :variant="priorityVariant(ticket.priority)" class="px-1.5 text-[10px]">{{ ticket.priority }}</Badge>
          <Badge v-if="ticket.size" variant="outline" class="px-1.5 text-[10px]">{{ ticket.size }}</Badge>
          <TicketOriginBadge :origin="ticket.origin" />
          <Badge v-if="isArchived" variant="outline" class="px-1.5 text-[10px] text-muted-foreground">
            <Archive class="size-3" /> archived
          </Badge>
          <!-- Thread signals: one attention badge (what needs YOU) + the
               phase chip — both deep-link into the thread. -->
          <template v-if="activeThread">
            <RouterLink
              v-if="threadAttention"
              :to="{ name: 'thread-detail', params: { uid: activeThread.uid } }"
              :title="threadAttention.title"
            >
              <Badge :variant="threadAttention.variant" class="px-1.5 text-[10px] hover:bg-accent">
                {{ threadAttention.label }}
              </Badge>
            </RouterLink>
            <RouterLink
              :to="{ name: 'thread-detail', params: { uid: activeThread.uid } }"
              title="Open the thread"
            >
              <Badge :variant="threadPhaseVariant(activeThread.phase)" class="px-1.5 text-[10px] hover:bg-accent">
                <MessagesSquare class="size-3" /> {{ threadPhaseLabel(activeThread.phase) }}
              </Badge>
            </RouterLink>
          </template>
          <Badge v-if="ticket.linked_pr_uids.length" variant="outline" class="px-1.5 text-[10px]">
            <GitPullRequest class="size-3" /> {{ ticket.linked_pr_uids.length }}
          </Badge>
          <Badge v-if="subticketCount > 0" variant="outline" class="px-1.5 text-[10px]">
            <Layers class="size-3" /> {{ subticketCount }} subticket{{ subticketCount === 1 ? '' : 's' }}
          </Badge>
          <RouterLink
            v-if="inEpic && !nestedInEpic"
            :to="{ name: 'ticket-detail', params: { uid: ticket.parent_ticket_uid } }"
            title="Ships inside its epic — approved and implemented there"
          >
            <Badge variant="secondary" class="px-1.5 text-[10px] hover:bg-accent">
              <Layers class="size-3" /> in epic
            </Badge>
          </RouterLink>
          <span
            v-if="ticket.created_at"
            class="ml-auto text-[10px] text-muted-foreground"
            :title="`Created ${new Date(ticket.created_at).toLocaleString()}`"
          >{{ formatRelativeTime(ticket.created_at) }}</span>
        </div>

        <div v-if="ticket.labels.length" class="flex flex-wrap gap-1">
          <Badge v-for="label in ticket.labels" :key="label" variant="secondary" class="px-1.5 text-[10px]">{{ label }}</Badge>
        </div>

        <!-- One contextual primary action; the rest lives in the menus. An
             epic member has neither — both its gate and its dispatch belong to
             the parent — so the row collapses rather than sitting empty.
             An active thread owns the ticket's work, so its card offers
             "Continue in thread" instead of the one-shot Implement (which the
             backend would 409 anyway). -->
        <div
          v-if="!isArchived && !inEpic && (canApprove || ticket.status === 'todo' || ticket.status === 'in-progress')"
          class="flex flex-wrap items-center gap-1.5 pt-1"
        >
          <Button v-if="canApprove" size="sm" :disabled="!!busy" @click="approveOpen = true">
            <Check /> Approve
          </Button>
          <Button
            v-else-if="activeThread"
            as-child
            variant="outline"
            size="sm"
          >
            <RouterLink :to="{ name: 'thread-detail', params: { uid: activeThread.uid } }">
              <MessagesSquare /> Continue in thread
            </RouterLink>
          </Button>
          <TicketImplementButton v-else compact :ticket="ticket" @updated="emit('updated', $event)" />
        </div>
      </Card>
    </ContextMenuTrigger>

    <ContextMenuContent class="w-48">
      <ContextMenuItem @select="openTicket">
        <ArrowUpRight /> Open ticket
      </ContextMenuItem>
      <ContextMenuSeparator v-if="canApprove || menuTransitions.length" />
      <ContextMenuItem v-if="canApprove" :disabled="!!busy" @select="approveOpen = true">
        <Check /> Approve — Gate 1
      </ContextMenuItem>
      <ContextMenuItem
        v-for="t in menuTransitions"
        :key="t.to"
        :disabled="!!busy"
        @select="transition(t)"
      >
        <ArrowRight v-if="t.kind === 'forward'" />
        <ArrowLeft v-else />
        {{ t.label }}
      </ContextMenuItem>
      <template v-if="canArchive || isArchived || canDelete">
        <ContextMenuSeparator />
        <ContextMenuItem v-if="canArchive" :disabled="!!busy" @select="archiveOpen = true">
          <Archive /> Archive…
        </ContextMenuItem>
        <ContextMenuItem v-if="isArchived" :disabled="!!busy" @select="unarchive">
          <ArchiveRestore /> Unarchive
        </ContextMenuItem>
        <ContextMenuItem
          v-if="canDelete"
          class="text-destructive focus:text-destructive"
          :disabled="!!busy"
          @select="deleteOpen = true"
        >
          <Trash2 /> Delete…
        </ContextMenuItem>
      </template>
    </ContextMenuContent>
  </ContextMenu>

  <!-- Gate 1 confirm -->
  <Dialog v-model:open="approveOpen">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Approve ticket</DialogTitle>
        <DialogDescription>
          Gate 1: approving moves this ticket from Backlog to Todo. Nothing implements without it.
        </DialogDescription>
      </DialogHeader>
      <p class="text-sm text-muted-foreground">{{ ticket.title }}</p>
      <DialogFooter>
        <Button variant="ghost" size="sm" @click="approveOpen = false">Cancel</Button>
        <Button
          size="sm"
          :loading="busy === 'todo'"
          @click="transition({ to: 'todo', label: 'Approve', kind: 'gate' })"
        >
          <Check /> Approve — move to Todo
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- Archive confirm -->
  <AlertDialog v-model:open="archiveOpen">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>Archive ticket</AlertDialogTitle>
        <AlertDialogDescription>
          Archive “{{ ticket.title }}”? It leaves the board but keeps its history —
          restore it any time from the archived list.
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Cancel</AlertDialogCancel>
        <AlertDialogAction @click="confirmArchive">
          Archive
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>

  <!-- Delete confirm -->
  <AlertDialog v-model:open="deleteOpen">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>Delete ticket</AlertDialogTitle>
        <AlertDialogDescription>
          Delete “{{ ticket.title }}”? Only backlog tickets can be deleted.
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Cancel</AlertDialogCancel>
        <AlertDialogAction
          class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          @click="confirmRemove"
        >
          Delete
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
</template>
