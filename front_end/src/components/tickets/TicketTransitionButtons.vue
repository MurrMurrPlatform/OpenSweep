<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowLeft, ArrowRight, Check, ChevronDown, Trash2 } from 'lucide-vue-next'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
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
import { TRANSITIONS, type TicketTransition } from '@/components/tickets/ticketMeta'
import type { TicketDTO } from '@/types/api'

interface Props {
  ticket: TicketDTO
  /** Backlog-only delete button (the API rejects deletes elsewhere). */
  showDelete?: boolean
}
const props = withDefaults(defineProps<Props>(), { showDelete: true })
const emit = defineEmits<{ updated: [ticket: TicketDTO]; deleted: [uid: string] }>()

const store = useTicketStore()
const toast = useToast()

const busy = ref<string | null>(null)
const approveOpen = ref(false)
const deleteOpen = ref(false)

const transitions = computed<TicketTransition[]>(() => TRANSITIONS[props.ticket.status] ?? [])

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

function remove() {
  if (busy.value) return
  deleteOpen.value = true
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
</script>

<template>
  <div v-if="transitions.length || (showDelete && ticket.status === 'backlog')">
    <!-- Gate 1 (Approve) stays a first-class button — it is THE human gate.
         Everything else folds into one calm "Move" menu. -->
    <div class="flex items-center gap-1.5">
      <Button
        v-if="transitions.some((t) => t.kind === 'gate')"
        size="sm"
        :disabled="!!busy"
        @click="approveOpen = true"
      >
        <Check /> Approve
      </Button>
      <DropdownMenu v-if="transitions.some((t) => t.kind !== 'gate') || (showDelete && ticket.status === 'backlog')">
        <DropdownMenuTrigger as-child>
          <Button variant="ghost" size="sm" :disabled="!!busy" :loading="!!busy && busy !== 'delete'">
            Move <ChevronDown class="size-3.5 opacity-60" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-52">
          <template v-for="t in transitions" :key="t.to">
            <DropdownMenuItem v-if="t.kind !== 'gate'" @select="transition(t)">
              <ArrowRight v-if="t.kind === 'forward'" />
              <ArrowLeft v-else />
              {{ t.label }}
            </DropdownMenuItem>
          </template>
          <template v-if="showDelete && ticket.status === 'backlog'">
            <DropdownMenuSeparator v-if="transitions.some((t) => t.kind !== 'gate')" />
            <DropdownMenuItem class="text-destructive focus:text-destructive" @select="remove">
              <Trash2 /> Delete ticket
            </DropdownMenuItem>
          </template>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>

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
  </div>
</template>
