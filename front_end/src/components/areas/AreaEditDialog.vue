<script setup lang="ts">
import { ref, watch } from 'vue'
import { Check } from 'lucide-vue-next'
import { useAreaStore } from '@/stores/areaStore'
import { useToast } from '@/composables/useToast'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import type { AreaDTO, AreaKind } from '@/types/api'

const props = defineProps<{
  open: boolean
  area: AreaDTO | null
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  /** The PATCH landed — the updated area, for the caller to refetch/merge. */
  saved: [area: AreaDTO]
}>()

const areaStore = useAreaStore()
const toast = useToast()

const saving = ref(false)
const draftTitle = ref('')
const draftKind = ref<AreaKind>('subsystem')
const draftScopePaths = ref('')
const draftSpec = ref('')
const draftEnabled = ref(true)

watch(
  () => props.open,
  (open) => {
    const a = props.area
    if (!open || !a) return
    draftTitle.value = a.title
    draftKind.value = a.kind
    draftScopePaths.value = a.scope_paths.join('\n')
    draftSpec.value = a.spec
    draftEnabled.value = a.enabled
  },
)

async function saveEdit() {
  const target = props.area
  if (!target || saving.value) return
  saving.value = true
  try {
    const { area, warnings } = await areaStore.patchArea(target.uid, {
      title: draftTitle.value,
      kind: draftKind.value,
      scope_paths: draftScopePaths.value.split('\n').map((p) => p.trim()).filter(Boolean),
      spec: draftSpec.value,
      enabled: draftEnabled.value,
    })
    emit('update:open', false)
    if (warnings.length) {
      // Warnings are advisory partition drift notes — the save DID land.
      toast.warn(`Saved ${area.key} with warnings`, warnings.join('; '))
    } else {
      toast.success('Area saved', area.key)
    }
    emit('saved', area)
  } catch (e: unknown) {
    toast.error('Save failed', e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Edit area</DialogTitle>
        <DialogDescription>
          <span class="font-mono">{{ area?.key }}</span> — a human edit counts as a review and clears staleness.
        </DialogDescription>
      </DialogHeader>
      <div class="space-y-3">
        <div class="grid gap-3 md:grid-cols-2">
          <div class="space-y-1.5">
            <Label>Title</Label>
            <Input v-model="draftTitle" placeholder="Area title" />
          </div>
          <div class="space-y-1.5">
            <Label>Kind</Label>
            <Select :model-value="draftKind" @update:model-value="draftKind = $event as AreaKind">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="subsystem">subsystem — exclusive partition leaf</SelectItem>
                <SelectItem value="feature">feature — cross-cutting spec overlay</SelectItem>
                <SelectItem value="ignore">ignore — not auditable (spec says why)</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div class="space-y-1.5">
          <Label>Scope paths (one per line)</Label>
          <Textarea
            v-model="draftScopePaths"
            :rows="3"
            placeholder="backend/delivery/&#10;backend/webhooks.py"
            class="font-mono text-xs"
          />
        </div>
        <div class="space-y-1.5">
          <Label>Spec (markdown — what to check here)</Label>
          <Textarea v-model="draftSpec" :rows="8" placeholder="What matters in this area…" class="font-mono text-xs" />
        </div>
        <div class="flex items-center gap-2">
          <Switch :model-value="draftEnabled" @update:model-value="draftEnabled = $event" />
          <Label>Enabled — disabled areas are ignored by planning</Label>
        </div>
      </div>
      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :loading="saving" @click="saveEdit">
          <Check /> Save
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
