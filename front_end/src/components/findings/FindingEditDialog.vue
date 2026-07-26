<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useFindingStore } from '@/stores/findingStore'
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
  FindingKind,
  FindingSize,
  Severity,
  UpdateFindingRequest,
} from '@/types/api'

interface Props {
  open: boolean
  /** Edit mode: the finding to PATCH. Null + createRepositoryUid → create mode. */
  finding?: FindingDTO | null
  /** Create mode: file a brand-new finding against this repository. */
  createRepositoryUid?: string
  /** Create mode: kind the form resets to (e.g. 'feature-idea' on the Ideas page). */
  defaultKind?: FindingKind
}
const props = withDefaults(defineProps<Props>(), {
  finding: null,
  createRepositoryUid: '',
  defaultKind: 'defect',
})
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: [finding: FindingDTO] }>()

const isCreate = computed(() => !props.finding)

const store = useFindingStore()
const toast = useToast()

const title = ref('')
const kind = ref<FindingKind>('defect')
const severity = ref<Severity>('medium')
const size = ref<FindingSize>('medium')
const subtype = ref('')
const description = ref('')
const rootCause = ref('')
const whyItMatters = ref('')
const suggestedFix = ref('')
const pathsText = ref('')
const tagsText = ref('')
const saving = ref(false)

const KIND_OPTIONS = [
  { label: 'Defect', value: 'defect' },
  { label: 'Improvement', value: 'improvement' },
  { label: 'Gap', value: 'gap' },
  { label: 'Proposal', value: 'proposal' },
  { label: 'Observation', value: 'observation' },
  { label: 'Feature idea', value: 'feature-idea' },
]
const SEVERITY_OPTIONS = [
  { label: 'Low', value: 'low' },
  { label: 'Medium', value: 'medium' },
  { label: 'High', value: 'high' },
  { label: 'Critical', value: 'critical' },
]
const SIZE_OPTIONS = [
  { label: 'Trivial', value: 'trivial' },
  { label: 'Small', value: 'small' },
  { label: 'Medium', value: 'medium' },
  { label: 'Large', value: 'large' },
]

watch(
  () => props.open,
  (open) => {
    if (!open) return
    const f = props.finding
    if (f) {
      title.value = f.title
      kind.value = f.kind
      severity.value = f.severity
      size.value = f.size
      subtype.value = f.subtype || ''
      description.value = f.description || ''
      rootCause.value = f.root_cause || ''
      whyItMatters.value = f.why_it_matters || ''
      suggestedFix.value = f.suggested_fix || ''
      pathsText.value = (f.affected_paths || []).join('\n')
      tagsText.value = (f.tags || []).join(', ')
    } else {
      title.value = ''
      kind.value = props.defaultKind
      severity.value = 'medium'
      size.value = 'medium'
      subtype.value = ''
      description.value = ''
      rootCause.value = ''
      whyItMatters.value = ''
      suggestedFix.value = ''
      pathsText.value = ''
      tagsText.value = ''
    }
  },
)

const canSave = computed(() =>
  Boolean(title.value.trim() && !saving.value && (props.finding || props.createRepositoryUid)),
)

function parsedPaths(): string[] {
  return pathsText.value
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
}

function parsedTags(): string[] {
  return tagsText.value
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  try {
    const fields: UpdateFindingRequest = {
      title: title.value.trim(),
      kind: kind.value,
      severity: severity.value,
      size: size.value,
      subtype: subtype.value.trim(),
      description: description.value,
      root_cause: rootCause.value,
      why_it_matters: whyItMatters.value,
      suggested_fix: suggestedFix.value,
      affected_paths: parsedPaths(),
      tags: parsedTags(),
    }
    let saved: FindingDTO
    if (props.finding) {
      saved = await store.update(props.finding.uid, fields)
      toast.success('Finding updated', saved.title)
    } else {
      saved = await store.create({ ...fields, repository_uid: props.createRepositoryUid, title: title.value.trim() })
      toast.success('Finding filed', saved.title)
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
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>{{ isCreate ? 'File a finding' : 'Edit finding' }}</DialogTitle>
        <DialogDescription>
          {{ isCreate
            ? 'Manually record a finding for this workspace — filed as executor “manual”.'
            : 'Correct the narrative and classification. Status changes go through the triage buttons; machine provenance stays untouched.' }}
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="space-y-3">
        <div class="space-y-1">
          <Label>Title</Label>
          <Input v-model="title" placeholder="Short, specific summary" />
        </div>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div class="space-y-1">
            <Label>Kind</Label>
            <Select :model-value="kind" @update:model-value="kind = $event as FindingKind">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem v-for="o in KIND_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="space-y-1">
            <Label>Severity</Label>
            <Select :model-value="severity" @update:model-value="severity = $event as Severity">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem v-for="o in SEVERITY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="space-y-1">
            <Label>Size</Label>
            <Select :model-value="size" @update:model-value="size = $event as FindingSize">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem v-for="o in SIZE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div class="space-y-1">
          <Label>Subtype</Label>
          <Input v-model="subtype" placeholder="Optional grouping key (e.g. n-plus-one)" />
        </div>
        <div class="space-y-1">
          <Label>Description</Label>
          <Textarea v-model="description" :rows="4" placeholder="What is wrong, where, and how it manifests — markdown supported." />
        </div>
        <div class="space-y-1">
          <Label>Root cause</Label>
          <Textarea v-model="rootCause" :rows="3" placeholder="Why the problem exists (mechanism, not symptom)." />
        </div>
        <div class="space-y-1">
          <Label>Why it matters</Label>
          <Textarea v-model="whyItMatters" :rows="2" placeholder="Impact / consequences." />
        </div>
        <div class="space-y-1">
          <Label>Suggested fix</Label>
          <Textarea v-model="suggestedFix" :rows="3" placeholder="How to remedy it — markdown supported." />
        </div>
        <div class="space-y-1">
          <Label>Affected paths (one per line)</Label>
          <Textarea v-model="pathsText" :rows="3" placeholder="src/foo.ts:42  ·  src/bar.py#L10-L20" />
        </div>
        <div class="space-y-1">
          <Label>Tags (comma-separated)</Label>
          <Input v-model="tagsText" placeholder="e.g. security, flaky-test" />
        </div>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" @click="emit('update:open', false)">Cancel</Button>
        <Button :disabled="!canSave" :loading="saving" @click="save">
          {{ isCreate ? 'File finding' : 'Save changes' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
