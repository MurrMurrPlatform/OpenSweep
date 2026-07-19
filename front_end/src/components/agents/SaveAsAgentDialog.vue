<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agentStore'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { PRODUCES_OPTIONS } from '@/lib/produces'
import type { AgentDTO, AgentEffort, ProducesKind } from '@/types/api'

const props = defineProps<{
  open: boolean
  /** Prefill from the run / composed prompt. */
  prefill?: {
    title?: string
    description?: string
    prompt?: string
    produces?: ProducesKind
    effort?: AgentEffort
    tags?: string[]
  }
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  created: [agent: AgentDTO]
}>()

const agents = useAgentStore()
const toast = useToast()

const title = ref('')
const produces = ref<ProducesKind>('findings')
const saving = ref(false)

watch(
  () => props.open,
  (open) => {
    if (open) {
      title.value = props.prefill?.title ?? ''
      produces.value = props.prefill?.produces ?? 'findings'
    }
  },
)

async function save() {
  if (!title.value.trim() || saving.value) return
  saving.value = true
  try {
    const agent = await agents.create({
      title: title.value.trim(),
      description: props.prefill?.description ?? '',
      prompt: props.prefill?.prompt ?? '',
      produces: produces.value,
      default_effort: props.prefill?.effort ?? 'normal',
      tags: props.prefill?.tags ?? [],
    })
    toast.success('Agent saved', `“${agent.title}” added to your library.`)
    emit('created', agent)
    emit('update:open', false)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save agent', msg)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Save as Agent</DialogTitle>
        <DialogDescription>
          Saves this prompt as a reusable Agent in your library — schedule it on any
          repository from its Agents page.
        </DialogDescription>
      </DialogHeader>
      <div class="space-y-3">
        <div class="space-y-1.5">
          <Label for="agent-title">Name</Label>
          <Input
            id="agent-title"
            v-model="title"
            autofocus
            placeholder="Nightly security sweep"
            @keydown.enter="save"
          />
        </div>
        <div class="space-y-1.5">
          <Label>Produces</Label>
          <Select :model-value="produces" @update:model-value="produces = $event as ProducesKind">
            <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in PRODUCES_OPTIONS" :key="o.value" :value="o.value">
                {{ o.label }} — {{ o.description }}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!title.trim()" :loading="saving" @click="save">
          Save agent
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
