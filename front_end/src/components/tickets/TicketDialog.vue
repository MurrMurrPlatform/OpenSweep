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
import type {
  FindingDTO,
  RepositoryDTO,
  Severity,
  TicketDTO,
  TicketPriority,
  TicketSize,
  UpdateTicketRequest,
} from '@/types/api'

interface Props {
  open: boolean
  repositories: RepositoryDTO[]
  /** Edit mode: PATCH this ticket instead of creating one. */
  ticket?: TicketDTO | null
  /** Subticket mode: preseed parent_ticket_uid + lock the repository. */
  parent?: TicketDTO | null
  /** Promote mode: prefill from this finding + record it as the ticket origin. */
  finding?: FindingDTO | null
  /** Create mode: preselect this repository (e.g. active board filter). */
  defaultRepositoryUid?: string
}
const props = withDefaults(defineProps<Props>(), {
  ticket: null,
  parent: null,
  finding: null,
  defaultRepositoryUid: '',
})
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: [ticket: TicketDTO] }>()

const store = useTicketStore()
const toast = useToast()

const repositoryUid = ref('')
const title = ref('')
const description = ref('')
const priority = ref<TicketPriority>('medium')
const size = ref<TicketSize>('')
const acText = ref('')
const labelsText = ref('')
const saving = ref(false)

const isEdit = computed(() => Boolean(props.ticket))
const isPromote = computed(() => !isEdit.value && Boolean(props.finding))
const repoLocked = computed(() => isEdit.value || Boolean(props.parent) || isPromote.value)

const repoOptions = computed(() => props.repositories.map((r) => ({ label: r.name, value: r.uid })))

const PRIORITY_OPTIONS = [
  { label: 'Low', value: 'low' },
  { label: 'Medium', value: 'medium' },
  { label: 'High', value: 'high' },
  { label: 'Urgent', value: 'urgent' },
]
const SIZE_OPTIONS = [
  { label: 'Trivial', value: 'trivial' },
  { label: 'Small', value: 'small' },
  { label: 'Medium', value: 'medium' },
  { label: 'Large', value: 'large' },
]

// SelectItem values cannot be empty strings — use a sentinel for "Unsized".
const UNSIZED = 'none'
const sizeModel = computed(() => (size.value === '' ? UNSIZED : size.value))
function onSizeChange(value: string) {
  size.value = (value === UNSIZED ? '' : value) as TicketSize
}

const SEVERITY_TO_PRIORITY: Record<Severity, TicketPriority> = {
  low: 'low',
  medium: 'medium',
  high: 'high',
  critical: 'urgent',
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    if (props.ticket) {
      repositoryUid.value = props.ticket.repository_uid
      title.value = props.ticket.title
      description.value = props.ticket.description
      priority.value = props.ticket.priority
      size.value = props.ticket.size
      acText.value = props.ticket.acceptance_criteria.join('\n')
      labelsText.value = props.ticket.labels.join(', ')
    } else if (props.finding) {
      const f = props.finding
      repositoryUid.value = f.repository_uid
      title.value = f.title
      description.value = [
        f.description,
        f.root_cause ? `Root cause:\n${f.root_cause}` : '',
        f.why_it_matters,
        f.suggested_fix ? `Suggested fix:\n${f.suggested_fix}` : '',
      ]
        .filter(Boolean)
        .join('\n\n')
      priority.value = SEVERITY_TO_PRIORITY[f.severity] || 'medium'
      size.value = (SIZE_OPTIONS.some((o) => o.value === f.size) ? f.size : '') as TicketSize
      // Editable defaults — review runs verify each criterion against the PR,
      // so a promoted ticket should never start without any.
      const topPath = (f.affected_paths || [])[0]
      acText.value = [
        topPath
          ? `The problem no longer occurs at ${topPath}`
          : 'The problem described in the origin finding no longer occurs',
        f.suggested_fix ? 'The suggested fix (or an equivalent remedy) is implemented' : '',
        'A regression test covers this case',
      ]
        .filter(Boolean)
        .join('\n')
      labelsText.value = (f.tags || []).join(', ')
    } else {
      repositoryUid.value = props.parent?.repository_uid || props.defaultRepositoryUid || ''
      if (!repositoryUid.value && repoOptions.value.length === 1) {
        repositoryUid.value = repoOptions.value[0].value
      }
      title.value = ''
      description.value = ''
      priority.value = 'medium'
      size.value = ''
      acText.value = ''
      labelsText.value = ''
    }
  },
)

const canSave = computed(() => Boolean(repositoryUid.value && title.value.trim() && !saving.value))

function parsedAc(): string[] {
  return acText.value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

function parsedLabels(): string[] {
  return labelsText.value
    .split(',')
    .map((l) => l.trim())
    .filter(Boolean)
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  try {
    let saved: TicketDTO
    if (props.ticket) {
      const req: UpdateTicketRequest = {
        title: title.value.trim(),
        description: description.value,
        acceptance_criteria: parsedAc(),
        labels: parsedLabels(),
        priority: priority.value,
        size: size.value,
      }
      saved = await store.updateTicket(props.ticket.uid, req)
      toast.success('Ticket updated', saved.title)
    } else {
      saved = await store.createTicket({
        repository_uid: repositoryUid.value,
        title: title.value.trim(),
        description: description.value || undefined,
        acceptance_criteria: parsedAc().length ? parsedAc() : undefined,
        labels: parsedLabels().length ? parsedLabels() : undefined,
        priority: priority.value,
        size: size.value || undefined,
        origin: isPromote.value ? 'finding' : undefined,
        origin_finding_uid: isPromote.value ? props.finding!.uid : undefined,
        parent_ticket_uid: props.parent?.uid || undefined,
      })
      toast.success(
        isPromote.value ? 'Finding promoted to ticket' : props.parent ? 'Subticket created' : 'Ticket created',
        saved.title,
      )
    }
    emit('saved', saved)
    emit('update:open', false)
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Save failed', msg)
  } finally {
    saving.value = false
  }
}

const dialogTitle = computed(() => {
  if (isEdit.value) return 'Edit ticket'
  if (isPromote.value) return 'Promote finding to ticket'
  return props.parent ? 'New subticket' : 'New ticket'
})
const dialogDescription = computed(() => {
  if (isEdit.value) return 'Update the ticket fields. Status changes go through the pipeline buttons.'
  if (isPromote.value)
    return 'Prefilled from the finding, which stays linked as the ticket origin. New tickets start in Backlog until approved (Gate 1).'
  if (props.parent) return `Child of “${props.parent.title}”. New tickets start in Backlog until approved (Gate 1).`
  return 'New tickets start in Backlog. Nothing implements until a human approves it into Todo (Gate 1).'
})
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>{{ dialogTitle }}</DialogTitle>
        <DialogDescription>{{ dialogDescription }}</DialogDescription>
      </DialogHeader>

      <DialogBody class="space-y-3">
        <div class="space-y-1.5">
          <Label for="ticket-repo">Repository</Label>
          <Select
            :model-value="repositoryUid"
            :disabled="repoLocked"
            @update:model-value="repositoryUid = $event as string"
          >
            <SelectTrigger id="ticket-repo" class="w-full">
              <SelectValue placeholder="Select a repository…" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in repoOptions" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div class="space-y-1.5">
          <Label for="ticket-title">Title</Label>
          <Input id="ticket-title" v-model="title" placeholder="What needs to happen?" @keydown.enter="save" />
        </div>
        <div class="space-y-1.5">
          <Label for="ticket-desc">Description</Label>
          <Textarea id="ticket-desc" v-model="description" :rows="4" placeholder="Context, scope, links — markdown supported." />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <Label for="ticket-priority">Priority</Label>
            <Select :model-value="priority" @update:model-value="priority = $event as TicketPriority">
              <SelectTrigger id="ticket-priority" class="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="o in PRIORITY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="space-y-1.5">
            <Label for="ticket-size">Size</Label>
            <Select :model-value="sizeModel" @update:model-value="onSizeChange($event as string)">
              <SelectTrigger id="ticket-size" class="w-full">
                <SelectValue placeholder="Unsized" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem :value="UNSIZED">Unsized</SelectItem>
                <SelectItem v-for="o in SIZE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div class="space-y-1.5">
          <Label for="ticket-ac">Acceptance criteria (one per line)</Label>
          <Textarea id="ticket-ac" v-model="acText" :rows="4" placeholder="Each line becomes one checkable criterion." />
        </div>
        <div class="space-y-1.5">
          <Label for="ticket-labels">Labels (comma-separated)</Label>
          <Input id="ticket-labels" v-model="labelsText" placeholder="e.g. tech-debt, api" />
        </div>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!canSave" :loading="saving" @click="save">
          {{ isEdit ? 'Save changes' : parent ? 'Create subticket' : 'Create ticket' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
