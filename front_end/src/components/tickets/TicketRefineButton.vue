<script setup lang="ts">
import { ref } from 'vue'
import { Wand2 } from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Button } from '@/components/ui/button'
import type { TicketDTO } from '@/types/api'

interface Props {
  ticket: TicketDTO
  /** Board-card variant: smaller icon. */
  compact?: boolean
}
const props = withDefaults(defineProps<Props>(), { compact: false })

const store = useTicketStore()
const toast = useToast()

const refining = ref(false)

// Read-only triage: the agent studies the code, sharpens the ticket's title /
// description / acceptance criteria and attaches an implementation plan via the
// platform tools. Available at any status — it never writes code.
async function dispatch() {
  if (refining.value) return
  refining.value = true
  try {
    const run = await store.refineTicket(props.ticket.uid)
    const runUid = typeof run.run_uid === 'string' ? run.run_uid : ''
    toast.success(
      'Refine run dispatched',
      runUid ? `run ${runUid.slice(0, 8)} · ${props.ticket.title}` : props.ticket.title,
      runUid ? { label: 'View run', to: { name: 'run-detail', params: { uid: runUid } } } : undefined,
    )
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t refine', msg)
  } finally {
    refining.value = false
  }
}
</script>

<template>
  <Button variant="ghost" size="sm" :loading="refining" @click="dispatch">
    <Wand2 /> Refine
  </Button>
</template>
