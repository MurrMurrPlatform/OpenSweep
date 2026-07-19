<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agentStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { formatRelativeTime } from '@/lib/utils'
import type { AgentRevisionDTO } from '@/types/api'

const props = defineProps<{
  open: boolean
  agentUid: string
  /** True when the agent is a system row (revisions include org overrides). */
  system?: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  reverted: []
}>()

const agents = useAgentStore()
const toast = useToast()
const revisions = ref<AgentRevisionDTO[]>([])
const loading = ref(false)
const reverting = ref<number | null>(null)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    loading.value = true
    try {
      revisions.value = await agents.fetchRevisions(props.agentUid)
    } finally {
      loading.value = false
    }
  },
)

async function revert(rev: number) {
  if (reverting.value !== null) return
  reverting.value = rev
  try {
    await agents.revert(props.agentUid, rev)
    toast.success(`Reverted to revision ${rev}`)
    revisions.value = await agents.fetchRevisions(props.agentUid)
    emit('reverted')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Revert failed', msg)
  } finally {
    reverting.value = null
  }
}
</script>

<template>
  <Sheet :open="open" @update:open="emit('update:open', $event)">
    <SheetContent class="w-full overflow-y-auto sm:max-w-lg">
      <SheetHeader>
        <SheetTitle>Revision history</SheetTitle>
        <SheetDescription>
          Append-only — reverting copies an old revision into a new head.
        </SheetDescription>
      </SheetHeader>
      <div class="mt-4 space-y-3">
        <div v-if="loading" class="text-sm text-muted-foreground">Loading…</div>
        <div v-else-if="!revisions.length" class="text-sm text-muted-foreground">
          No revisions yet.
        </div>
        <div
          v-for="r in revisions"
          :key="r.uid"
          class="rounded-md border p-3"
        >
          <div class="flex items-center justify-between gap-2">
            <div class="flex items-center gap-2 text-sm font-medium">
              rev {{ r.rev }}
              <Badge v-if="r.org_uid" variant="info">Org override · {{ r.mode }}</Badge>
              <Badge v-else variant="outline">Base</Badge>
              <Badge v-if="!r.enabled" variant="secondary">disabled</Badge>
            </div>
            <Button
              v-if="r.org_uid"
              variant="outline"
              size="sm"
              :loading="reverting === r.rev"
              @click="revert(r.rev)"
            >
              Revert to
            </Button>
          </div>
          <div class="mt-1 text-xs text-muted-foreground">
            {{ r.created_at ? formatRelativeTime(r.created_at) : '' }}
          </div>
          <p v-if="r.body" class="mt-2 line-clamp-4 whitespace-pre-line text-xs">{{ r.body }}</p>
        </div>
      </div>
    </SheetContent>
  </Sheet>
</template>
