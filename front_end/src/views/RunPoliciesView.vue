<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Settings2 } from 'lucide-vue-next'
import { useRunPolicyStore } from '@/stores/runPolicyStore'
import { useToast } from '@/composables/useToast'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import type { RunPolicyDTO } from '@/types/api'

const policies = useRunPolicyStore()
const toast = useToast()
const items = ref<RunPolicyDTO[]>([])
const loading = ref(true)
const saving = ref(false)
const removingUid = ref<string | null>(null)
const deleteOpen = ref(false)
const pendingDeleteUid = ref<string | null>(null)
// null = create mode; a uid = editing that existing policy in place.
const editingUid = ref<string | null>(null)

// Ceiling fields are number | null — null means "no ceiling" (unlimited).
// An empty input coerces to null so editing an unlimited policy for an
// unrelated field never silently imposes a limit.
type PolicyForm = {
  name: string
  description: string
  max_dollars: number | ''
  max_tokens: number | ''
  max_wall_seconds: number | ''
  max_tool_turns: number | ''
  max_files_touched: number | ''
  cloud_allowed: boolean
  local_only: boolean
  dry_run: boolean
  warn_at_pct: number
  on_exceed: 'abort' | 'pause_for_approval'
}

const emptyForm = (): PolicyForm => ({
  name: '',
  description: '',
  max_dollars: 20,
  max_tokens: '',
  max_wall_seconds: 600,
  max_tool_turns: 40,
  max_files_touched: 40,
  cloud_allowed: false,
  local_only: true,
  dry_run: false,
  warn_at_pct: 80,
  on_exceed: 'abort',
})
const form = reactive<PolicyForm>(emptyForm())

/** Empty string / NaN → null (unlimited); otherwise the number. */
function numOrNull(v: number | ''): number | null {
  return v === '' || Number.isNaN(v) ? null : Number(v)
}

function resetForm() {
  Object.assign(form, emptyForm())
  editingUid.value = null
}

onMounted(async () => {
  loading.value = true
  try {
    items.value = await policies.fetchAll()
  } finally {
    loading.value = false
  }
})

function startEdit(p: RunPolicyDTO) {
  editingUid.value = p.uid
  Object.assign(form, {
    name: p.name,
    description: p.description ?? '',
    // Preserve null (unlimited) — don't fall back to a default that would
    // silently cap a previously-unlimited policy on the next save.
    max_dollars: p.max_dollars ?? '',
    max_tokens: p.max_tokens ?? '',
    max_wall_seconds: p.max_wall_seconds ?? '',
    max_tool_turns: p.max_tool_turns ?? '',
    max_files_touched: p.max_files_touched ?? '',
    cloud_allowed: p.cloud_allowed,
    local_only: p.local_only,
    dry_run: p.dry_run,
    warn_at_pct: p.warn_at_pct ?? 80,
    on_exceed: p.on_exceed,
  })
  if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function save() {
  if (saving.value || !form.name) return
  saving.value = true
  try {
    const payload = {
      name: form.name,
      description: form.description,
      max_dollars: numOrNull(form.max_dollars),
      max_tokens: numOrNull(form.max_tokens),
      max_wall_seconds: numOrNull(form.max_wall_seconds),
      max_tool_turns: numOrNull(form.max_tool_turns),
      max_files_touched: numOrNull(form.max_files_touched),
      cloud_allowed: form.cloud_allowed,
      local_only: form.local_only,
      dry_run: form.dry_run,
      warn_at_pct: form.warn_at_pct,
      on_exceed: form.on_exceed,
    }
    if (editingUid.value) {
      await policies.update(editingUid.value, payload)
      toast.success('Policy updated')
    } else {
      await policies.create({ ...payload, allowed_executors: [] })
      toast.success('Policy created')
    }
    items.value = await policies.fetchAll()
    resetForm()
  } catch (e: unknown) {
    const verb = editingUid.value ? 'Update' : 'Create'
    toast.error(`${verb} failed`, e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

function remove(uid: string) {
  pendingDeleteUid.value = uid
  deleteOpen.value = true
}

async function confirmRemove() {
  const uid = pendingDeleteUid.value
  if (!uid) return
  deleteOpen.value = false
  removingUid.value = uid
  try {
    await policies.remove(uid)
    items.value = items.value.filter((p) => p.uid !== uid)
    if (editingUid.value === uid) resetForm()
    toast.success('Policy deleted')
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
  } finally {
    removingUid.value = null
  }
}
</script>

<template>
  <div class="space-y-6">
    <PageHeader
      title="Run policies"
      subtitle="Ceilings + routing constraints that bound every InvestigationRun."
    />

    <Card>
      <CardHeader>
        <div class="flex items-center justify-between gap-2">
          <CardTitle class="text-base">{{ editingUid ? 'Edit policy' : 'Create policy' }}</CardTitle>
          <span v-if="editingUid" class="text-xs text-muted-foreground font-mono">{{ form.name }}</span>
        </div>
      </CardHeader>
      <CardContent class="space-y-4">
        <div class="grid gap-3 sm:grid-cols-3 text-sm">
          <div class="sm:col-span-1">
            <Label for="policy-name" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Name</Label>
            <Input id="policy-name" v-model="form.name" placeholder="default-conservative" />
          </div>
          <div class="sm:col-span-2">
            <Label for="policy-description" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Description</Label>
            <Input id="policy-description" v-model="form.description" placeholder="Short summary of intent" />
          </div>
          <div>
            <Label for="policy-dollars" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Max $ / run</Label>
            <Input id="policy-dollars" v-model.number="form.max_dollars" type="number" step="0.5" min="0" placeholder="∞ unlimited" />
          </div>
          <div>
            <Label for="policy-tokens" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Max tokens</Label>
            <Input id="policy-tokens" v-model.number="form.max_tokens" type="number" placeholder="∞ unlimited" />
          </div>
          <div>
            <Label for="policy-wall" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Max wall (s)</Label>
            <Input id="policy-wall" v-model.number="form.max_wall_seconds" type="number" placeholder="∞ unlimited" />
          </div>
          <div>
            <Label for="policy-turns" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Max turns</Label>
            <Input id="policy-turns" v-model.number="form.max_tool_turns" type="number" placeholder="∞ unlimited" />
          </div>
          <div>
            <Label for="policy-files" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">Max files</Label>
            <Input id="policy-files" v-model.number="form.max_files_touched" type="number" placeholder="∞ unlimited" />
          </div>
          <div class="sm:col-span-3 flex flex-wrap gap-6 rounded-md border bg-card px-3 py-3">
            <label class="flex items-center gap-2 text-sm">
              <Switch v-model="form.cloud_allowed" />
              cloud_allowed
            </label>
            <label class="flex items-center gap-2 text-sm">
              <Switch v-model="form.local_only" />
              local_only
            </label>
            <label class="flex items-center gap-2 text-sm">
              <Switch v-model="form.dry_run" />
              dry_run
            </label>
          </div>
          <div class="sm:col-span-3">
            <Label for="policy-on-exceed" class="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">On exceed</Label>
            <Select v-model="form.on_exceed">
              <SelectTrigger id="policy-on-exceed" class="w-full sm:w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="abort">abort</SelectItem>
                <SelectItem value="pause_for_approval">pause_for_approval</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <Button size="sm" :disabled="!form.name" :loading="saving" @click="save">
            {{ editingUid ? 'Save changes' : 'Create' }}
          </Button>
          <Button v-if="editingUid" variant="ghost" size="sm" :disabled="saving" @click="resetForm">
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>

    <Card>
      <CardHeader>
        <div class="flex items-center justify-between gap-2">
          <CardTitle class="text-base">Policies</CardTitle>
          <span class="text-xs text-muted-foreground">{{ items.length }}</span>
        </div>
      </CardHeader>
      <CardContent class="p-0">
        <div v-if="loading" class="p-4 space-y-2">
          <Skeleton v-for="i in 3" :key="i" class="h-16" />
        </div>
        <div v-else-if="items.length === 0" class="p-4">
          <EmptyState
            :icon="Settings2"
            title="No policies defined"
            description="Create a policy above to bound every InvestigationRun."
            class="border-0"
          />
        </div>
        <ul v-else class="stagger-children divide-y px-4">
          <li v-for="p in items" :key="p.uid" class="py-3 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div class="min-w-0">
              <div class="font-medium">
                {{ p.name }}
                <span class="text-muted-foreground font-mono text-xs ml-1">v{{ p.version }}</span>
              </div>
              <div class="text-xs text-muted-foreground">{{ p.description }}</div>
              <div class="text-xs text-muted-foreground font-mono mt-1 break-all">
                ${{ p.max_dollars ?? '∞' }} · tokens={{ p.max_tokens ?? '∞' }} ·
                wall={{ p.max_wall_seconds ?? '∞' }}s · turns={{ p.max_tool_turns ?? '∞' }} · files={{ p.max_files_touched ?? '∞' }} ·
                cloud={{ p.cloud_allowed }} · local_only={{ p.local_only }} · dry_run={{ p.dry_run }} · on_exceed={{ p.on_exceed }}
              </div>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <Button
                variant="secondary"
                size="sm"
                :disabled="saving && editingUid === p.uid"
                @click="startEdit(p)"
              >
                Edit
              </Button>
              <Button variant="destructive" size="sm" :loading="removingUid === p.uid" @click="remove(p.uid)">
                Delete
              </Button>
            </div>
          </li>
        </ul>
      </CardContent>
    </Card>

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete RunPolicy</AlertDialogTitle>
          <AlertDialogDescription>
            Delete this RunPolicy?
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmRemove"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
