<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { priorityVariant } from '@/components/tickets/ticketMeta'
import type { TicketDTO, TicketPriority } from '@/types/api'

interface Props {
  open: boolean
  repositoryUid: string
  /** Tickets eligible for grouping (ungrouped, not done). */
  tickets: TicketDTO[]
}
const props = defineProps<Props>()
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: [parent: TicketDTO] }>()

const store = useTicketStore()
const toast = useToast()

const title = ref('')
const description = ref('')
const priority = ref<TicketPriority>('medium')
const memberUids = ref<Set<string>>(new Set())
const saving = ref(false)

const PRIORITY_OPTIONS = [
  { label: 'Low', value: 'low' },
  { label: 'Medium', value: 'medium' },
  { label: 'High', value: 'high' },
  { label: 'Urgent', value: 'urgent' },
]

watch(
  () => props.open,
  (open) => {
    if (!open) return
    title.value = ''
    description.value = ''
    priority.value = 'medium'
    memberUids.value = new Set()
  },
)

function toggle(uid: string) {
  const next = new Set(memberUids.value)
  if (next.has(uid)) next.delete(uid)
  else next.add(uid)
  memberUids.value = next
}

const canSave = computed(
  () => Boolean(title.value.trim()) && memberUids.value.size >= 2 && !saving.value,
)

async function save() {
  if (!canSave.value) return
  saving.value = true
  try {
    const parent = await store.createEpic({
      repository_uid: props.repositoryUid,
      title: title.value.trim(),
      description: description.value || undefined,
      member_ticket_uids: Array.from(memberUids.value),
      priority: priority.value,
    })
    toast.success('Tickets grouped', `${memberUids.value.size} subtickets under “${parent.title}”`)
    emit('saved', parent)
    emit('update:open', false)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Grouping failed', msg)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Group tickets</DialogTitle>
        <DialogDescription>
          Pick ≥2 tickets to epic under a new parent ticket, so one implement run can pick up the
          whole epic instead of one PR per ticket. Members keep their status; the parent starts in
          Backlog (Gate 1).
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="space-y-3">
        <div class="space-y-1.5">
          <Label for="group-title">Group title</Label>
          <Input id="group-title" v-model="title" placeholder="e.g. Auth hardening epic" @keydown.enter="save" />
        </div>
        <div class="space-y-1.5">
          <Label for="group-desc">Description</Label>
          <Textarea
            id="group-desc"
            v-model="description"
            :rows="2"
            placeholder="Why these belong together — markdown supported."
          />
        </div>
        <div class="space-y-1.5">
          <Label for="group-priority">Priority</Label>
          <Select :model-value="priority" @update:model-value="priority = $event as TicketPriority">
            <SelectTrigger id="group-priority" class="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in PRIORITY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div class="space-y-1.5">
          <Label>
            Members · {{ memberUids.size }} selected
            <span v-if="memberUids.size < 2" class="font-normal text-muted-foreground">(pick at least 2)</span>
          </Label>
          <div
            v-if="tickets.length === 0"
            class="rounded-md border p-3 text-sm text-muted-foreground"
          >
            No groupable tickets — only ungrouped, unfinished tickets can join a group.
          </div>
          <div
            v-else
            class="max-h-64 divide-y overflow-y-auto rounded-md border"
          >
            <label
              v-for="t in tickets"
              :key="t.uid"
              class="flex cursor-pointer items-start gap-2.5 px-3 py-2 text-sm hover:bg-accent"
            >
              <input
                type="checkbox"
                class="mt-1 h-4 w-4 cursor-pointer accent-primary"
                :checked="memberUids.has(t.uid)"
                @change="toggle(t.uid)"
              />
              <span class="min-w-0">
                <span class="block font-medium">{{ t.title || '(untitled)' }}</span>
                <span class="flex flex-wrap items-center gap-1.5 pt-0.5">
                  <Badge :variant="priorityVariant(t.priority)" class="px-1.5 text-[10px]">{{ t.priority }}</Badge>
                  <Badge variant="outline" class="px-1.5 text-[10px]">{{ t.status }}</Badge>
                  <Badge v-for="label in t.labels" :key="label" variant="secondary" class="px-1.5 text-[10px]">{{ label }}</Badge>
                </span>
              </span>
            </label>
          </div>
        </div>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!canSave" :loading="saving" @click="save">
          Group {{ memberUids.size >= 2 ? memberUids.size : '' }} tickets
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
