<script setup lang="ts">
/**
 * Files tab of the run detail page — the shared FileChangesPanel wired to
 * GET /runs/{uid}/changes. Refetches whenever `refreshKey` bumps (turn
 * boundaries); a `runUid` change resets all panel state.
 */
import FileChangesPanel from '@/components/diff/FileChangesPanel.vue'
import { useRunStore } from '@/stores/runStore'

const props = defineProps<{
  runUid: string
  refreshKey?: number
  live?: boolean
}>()

const emit = defineEmits<{ loaded: [count: number] }>()

const runs = useRunStore()
</script>

<template>
  <FileChangesPanel
    :fetch="() => runs.getChanges(props.runUid)"
    :refresh-key="refreshKey"
    :reset-key="runUid"
    @loaded="emit('loaded', $event)"
  />
</template>
