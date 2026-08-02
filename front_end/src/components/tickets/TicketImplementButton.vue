<script setup lang="ts">
import { computed, ref } from 'vue'
import { Rocket } from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { extractDispatchConflict, useActiveRuns } from '@/composables/useActiveRuns'
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
import ActiveRunChip from '@/components/runs/ActiveRunChip.vue'
import type { TicketDTO } from '@/types/api'

interface Props {
  ticket: TicketDTO
  /** Board-card variant: outline + smaller icon instead of the primary button. */
  compact?: boolean
}
const props = withDefaults(defineProps<Props>(), { compact: false })
const emit = defineEmits<{ updated: [ticket: TicketDTO] }>()

const store = useTicketStore()
const toast = useToast()

const confirmOpen = ref(false)
const dispatching = ref(false)

// An epic member ships inside its parent's single run and PR, so it is never
// dispatched on its own — the backend 409s on it. Hiding the button everywhere
// this component is used keeps the two in step.
const eligible = computed(
  () =>
    !props.ticket.parent_ticket_uid &&
    (props.ticket.status === 'todo' || props.ticket.status === 'in-progress'),
)

/** Why this ticket cannot be implemented right now, in the user's terms.
 *  Rendering NOTHING for an ineligible ticket read as a broken page — the
 *  Implement button simply was not there and no surface said why. */
const blockedReason = computed(() => {
  if (props.ticket.parent_ticket_uid)
    return 'Part of an epic — implement the epic parent; its PR closes this one.'
  if (props.ticket.status === 'backlog')
    return 'Needs approval first (Gate 1): move it to Todo.'
  if (props.ticket.status === 'in-review') return 'Already in review.'
  if (props.ticket.status === 'done') return 'Already done.'
  return 'Not implementable in its current state.'
})

// In-flight WORK runs targeting this ticket replace the Implement button with
// a "view run" chip while one exists (polls ~5s, stops on terminal). Chat
// runs never gate this surface (useActiveRuns filters them out).
const { activeRun, hasActive, noteDispatched } = useActiveRuns(() => ({
  ticket_uid: props.ticket.uid,
}))

async function dispatch() {
  if (dispatching.value) return
  dispatching.value = true
  try {
    const run = await store.implementTicket(props.ticket.uid)
    confirmOpen.value = false
    const runUid = typeof run.run_uid === 'string' ? run.run_uid : ''
    toast.success(
      'Implement run dispatched',
      runUid ? `run ${runUid.slice(0, 8)} · ${props.ticket.title}` : props.ticket.title,
      runUid ? { label: 'View run', to: { name: 'run-detail', params: { uid: runUid } } } : undefined,
    )
    noteDispatched({
      run_uid: runUid || undefined,
      scheduled_agent_uid: typeof run.scheduled_agent_uid === 'string' ? run.scheduled_agent_uid : undefined,
      title: `Implement: ${props.ticket.title || props.ticket.uid.slice(0, 8)}`,
      playbook: 'implement',
    })
    // The dispatch moves the ticket along (and may link a PR) — refresh it.
    try {
      const detail = await store.getTicket(props.ticket.uid)
      emit('updated', detail)
    } catch {
      /* dispatch succeeded — a stale card beats a page error */
    }
  } catch (e) {
    const conflict = extractDispatchConflict(e)
    if (conflict) {
      confirmOpen.value = false
      toast.error('Can’t implement', conflict.message, {
        label: 'View blocking run',
        to: { name: 'run-detail', params: { uid: conflict.run_uid } },
      })
      noteDispatched(conflict)
      return
    }
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error(e instanceof ApiError && e.status === 409 ? 'Can’t implement' : 'Dispatch failed', msg)
  } finally {
    dispatching.value = false
  }
}
</script>

<template>
  <ActiveRunChip v-if="hasActive && activeRun" :run="activeRun" />
  <!-- Ineligible: say WHY rather than rendering nothing. A missing button is
       indistinguishable from a broken page. -->
  <Button
    v-else-if="!eligible"
    variant="outline"
    size="sm"
    disabled
    :title="blockedReason"
  >
    <Rocket /> Implement
  </Button>
  <template v-else>
    <Button
      :variant="compact ? 'outline' : 'default'"
      size="sm"
      :disabled="dispatching"
      @click="confirmOpen = true"
    >
      <Rocket /> Implement
    </Button>

    <Dialog v-model:open="confirmOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Dispatch one-shot implement run</DialogTitle>
          <DialogDescription>
            The quick, fire-and-forget path — no conversation, no plan step. Your next
            touchpoint is reviewing the draft PR.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-3 text-sm">
          <p class="font-medium">{{ ticket.title || '(untitled)' }}</p>
          <ul class="list-disc space-y-1 pl-5 text-muted-foreground">
            <li>Creates a <span class="font-mono">opensweep/…</span> branch for this ticket (an existing remote branch is adopted, not duplicated).</li>
            <li>An agent implements the acceptance criteria in an isolated write sandbox.</li>
            <li>The platform validates, pushes, and opens a <strong>draft PR</strong> linked back to this ticket.</li>
          </ul>
          <p class="text-xs text-muted-foreground">
            Prefer to steer the work — refine scope, review a plan, answer questions?
            Start a <strong>thread</strong> from the ticket page instead.
          </p>
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="confirmOpen = false">Cancel</Button>
          <Button size="sm" :loading="dispatching" @click="dispatch">
            <Rocket /> Dispatch implement run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </template>
</template>
