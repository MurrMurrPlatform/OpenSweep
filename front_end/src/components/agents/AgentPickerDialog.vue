<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agentStore'
import { useScheduledAgentStore } from '@/stores/scheduledAgentStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import type { AgentDTO, ComputeDial, ScheduledAgentDTO } from '@/types/api'

const props = defineProps<{
  open: boolean
  repositoryUid: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  created: [scheduledAgent: ScheduledAgentDTO]
}>()

const agents = useAgentStore()
const scheduled = useScheduledAgentStore()
const toast = useToast()

const search = ref('')
const selected = ref<AgentDTO | null>(null)
const available = ref<AgentDTO[]>([])
const loading = ref(false)
const creating = ref(false)

type TriggerMode = 'manual' | 'on-event' | 'cron'
const mode = ref<TriggerMode>('manual')
const cronExpr = ref('0 2 * * *')
const dial = ref<ComputeDial>('ask-before-run')

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    selected.value = null
    search.value = ''
    mode.value = 'manual'
    dial.value = 'ask-before-run'
    loading.value = true
    try {
      available.value = (await agents.fetchAll({ enabled_only: true })).filter(
        (a) => a.produces !== 'code-changes',
      )
    } finally {
      loading.value = false
    }
  },
)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return available.value
  return available.value.filter(
    (a) =>
      a.title.toLowerCase().includes(q) ||
      a.description.toLowerCase().includes(q) ||
      a.tags.some((t) => t.toLowerCase().includes(q)),
  )
})

const trigger = computed(() =>
  mode.value === 'cron'
    ? `cron:${cronExpr.value.trim()}`
    : mode.value === 'on-event'
      ? 'on-event'
      : '',
)

async function create() {
  if (!selected.value || creating.value) return
  if (mode.value === 'cron' && !cronExpr.value.trim()) {
    toast.error('Cron expression required')
    return
  }
  creating.value = true
  try {
    const sa = await scheduled.create({
      agent_uid: selected.value.uid,
      repository_uid: props.repositoryUid,
      trigger: trigger.value,
      compute_dial: dial.value,
    })
    toast.success('Agent scheduled', `“${sa.agent_title || sa.title}” added to this repository.`)
    emit('created', sa)
    emit('update:open', false)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t schedule agent', msg)
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>Add agent</DialogTitle>
        <DialogDescription>
          Pick an agent from the library and choose when it runs on this repository.
        </DialogDescription>
      </DialogHeader>

      <template v-if="!selected">
        <Input v-model="search" placeholder="Search agents…" autofocus />
        <div class="max-h-72 space-y-1 overflow-y-auto">
          <div v-if="loading" class="p-3 text-sm text-muted-foreground">Loading…</div>
          <button
            v-for="a in filtered"
            :key="a.uid"
            class="flex w-full items-start justify-between gap-3 rounded-md border p-3 text-left hover:bg-accent"
            @click="selected = a"
          >
            <span class="min-w-0">
              <span class="flex items-center gap-2 text-sm font-medium">
                {{ a.title }}
                <Badge v-if="a.provenance === 'system'" variant="outline">System</Badge>
              </span>
              <span class="mt-0.5 line-clamp-2 block text-xs text-muted-foreground">
                {{ a.description }}
              </span>
            </span>
            <ProducesBadge :produces="a.produces" />
          </button>
          <div v-if="!loading && !filtered.length" class="p-3 text-sm text-muted-foreground">
            No agents match. Create one in the Agent library first.
          </div>
        </div>
      </template>

      <template v-else>
        <div class="flex items-center justify-between rounded-md border p-3">
          <div class="flex items-center gap-2 text-sm font-medium">
            {{ selected.title }} <ProducesBadge :produces="selected.produces" />
          </div>
          <Button variant="ghost" size="sm" @click="selected = null">Change</Button>
        </div>
        <div class="grid gap-3 md:grid-cols-2">
          <div class="space-y-1.5">
            <Label>Trigger</Label>
            <Select :model-value="mode" @update:model-value="mode = $event as TriggerMode">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="manual">Manual — run only when triggered</SelectItem>
                <SelectItem value="on-event">On push</SelectItem>
                <SelectItem value="cron">Cron — on a schedule</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="space-y-1.5">
            <Label>Compute dial</Label>
            <Select :model-value="dial" @update:model-value="dial = $event as ComputeDial">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="disabled">Disabled</SelectItem>
                <SelectItem value="suggest">Suggest only</SelectItem>
                <SelectItem value="ask-before-run">Ask before run</SelectItem>
                <SelectItem value="auto-run-cheap">Auto-run on free compute</SelectItem>
                <SelectItem value="auto-run-any">Auto-run on any provider</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div v-if="mode === 'cron'" class="space-y-1.5">
          <Label>Crontab (5 fields, UTC)</Label>
          <Input v-model="cronExpr" placeholder="0 2 * * *" class="font-mono" />
        </div>
      </template>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!selected" :loading="creating" @click="create">
          Add to repository
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
