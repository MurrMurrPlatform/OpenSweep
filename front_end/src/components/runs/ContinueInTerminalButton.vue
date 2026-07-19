<script setup lang="ts">
// Hands the live agent conversation to the user's terminal: one paste either
// resumes the actual claude session (same workspace, full context) or starts a
// fresh one seeded by the OPENSWEEP_HANDOFF.md brief the backend just wrote.
import { ref } from 'vue'
import { Terminal } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { useToast } from '@/composables/useToast'
import { useRunStore } from '@/stores/runStore'
import { ApiError } from '@/services/api'

const props = defineProps<{ runUid: string }>()

const toast = useToast()
const runs = useRunStore()
const busy = ref(false)

async function takeover() {
  if (busy.value) return
  busy.value = true
  try {
    const h = await runs.handoff(props.runUid)
    if (h.mode === 'unavailable') {
      toast.error('Workspace is gone', h.reason)
      return
    }
    await navigator.clipboard.writeText(h.command)
    toast.success(
      'Terminal command copied',
      h.mode === 'resume'
        ? 'Paste it in your terminal — the agent session resumes with full context.'
        : 'Paste it in your terminal — a fresh session picks up from the handoff brief.',
    )
  } catch (e) {
    toast.error('Couldn’t prepare handoff', e instanceof ApiError ? e.detail : String(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Button variant="outline" size="sm" :loading="busy" @click="takeover">
    <Terminal /> Continue in terminal
  </Button>
</template>
