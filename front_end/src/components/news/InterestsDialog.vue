<script setup lang="ts">
import { ref, watch } from 'vue'
import { Sparkles } from 'lucide-vue-next'
import { useInterestStore } from '@/stores/interestStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
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
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import type { InterestDTO } from '@/types/api'

interface Props {
  open: boolean
  repositoryUid: string
}
const props = defineProps<Props>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()

const interests = useInterestStore()
const toast = useToast()

const items = ref<InterestDTO[]>([])
const loading = ref(false)
const saving = ref(false)
const removingUid = ref<string | null>(null)
const deleteOpen = ref(false)
const pendingDeleteUid = ref<string | null>(null)
// null = create mode; a uid = editing that existing interest in place.
const editingUid = ref<string | null>(null)

const title = ref('')
const details = ref('')
const enabled = ref(true)

function resetForm() {
  title.value = ''
  details.value = ''
  enabled.value = true
  editingUid.value = null
}

async function reload() {
  if (!props.repositoryUid) return
  loading.value = true
  try {
    items.value = await interests.fetchAll({ repository_uid: props.repositoryUid })
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error("Couldn't load interests", msg)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    resetForm()
    void reload()
  },
)

function startEdit(i: InterestDTO) {
  editingUid.value = i.uid
  title.value = i.title
  details.value = i.details
  enabled.value = i.enabled
}

async function save() {
  if (saving.value || !title.value.trim() || !props.repositoryUid) return
  saving.value = true
  try {
    if (editingUid.value) {
      const updated = await interests.update(editingUid.value, {
        title: title.value.trim(),
        details: details.value,
        enabled: enabled.value,
      })
      items.value = items.value.map((x) => (x.uid === updated.uid ? updated : x))
      toast.success('Interest updated')
    } else {
      const created = await interests.create({
        repository_uid: props.repositoryUid,
        title: title.value.trim(),
        details: details.value,
        enabled: enabled.value,
      })
      items.value = [created, ...items.value]
      toast.success('Interest added')
    }
    resetForm()
  } catch (e: unknown) {
    const verb = editingUid.value ? 'Update' : 'Create'
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error(`${verb} failed`, msg)
  } finally {
    saving.value = false
  }
}

/** Per-row toggle — applies immediately, no edit round-trip. */
async function toggleEnabled(i: InterestDTO, value: boolean) {
  try {
    const updated = await interests.update(i.uid, { enabled: value })
    items.value = items.value.map((x) => (x.uid === updated.uid ? updated : x))
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Toggle failed', msg)
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
    await interests.remove(uid)
    items.value = items.value.filter((x) => x.uid !== uid)
    if (editingUid.value === uid) resetForm()
    toast.success('Interest deleted')
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Delete failed', msg)
  } finally {
    removingUid.value = null
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Interests</DialogTitle>
        <DialogDescription>
          Topics the news-scan agent watches for this workspace — the scan prioritizes items matching enabled interests.
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="space-y-4">
        <!-- Inline CRUD form -->
        <div class="space-y-3 rounded-md border border-border bg-card p-3">
          <div class="text-xs font-semibold">
            {{ editingUid ? 'Edit interest' : 'Add interest' }}
          </div>
          <div class="space-y-1">
            <Label>Title</Label>
            <Input v-model="title" placeholder="e.g. Local-first sync engines" />
          </div>
          <div class="space-y-1">
            <Label>Details</Label>
            <Textarea v-model="details" :rows="3" placeholder="What exactly to watch for and why it matters here." />
          </div>
          <div class="flex items-center justify-between gap-3">
            <label class="flex items-center gap-2 text-sm">
              <Switch v-model="enabled" />
              Enabled
            </label>
            <div class="flex items-center gap-2">
              <Button v-if="editingUid" variant="ghost" size="sm" :disabled="saving" @click="resetForm">
                Cancel
              </Button>
              <Button size="sm" :disabled="!title.trim()" :loading="saving" @click="save">
                {{ editingUid ? 'Save changes' : 'Add' }}
              </Button>
            </div>
          </div>
        </div>

        <!-- List -->
        <div v-if="loading" class="space-y-2">
          <Skeleton v-for="i in 3" :key="i" class="h-12" />
        </div>
        <EmptyState
          v-else-if="items.length === 0"
          :icon="Sparkles"
          title="No interests yet"
          description="Add topics above — without interests the scan falls back to the repository's own stack."
        />
        <ul v-else class="divide-y divide-border">
          <li v-for="i in items" :key="i.uid" class="flex items-start gap-3 py-3">
            <Switch
              class="mt-0.5 shrink-0"
              :model-value="i.enabled"
              @update:model-value="toggleEnabled(i, $event)"
            />
            <div class="min-w-0 flex-1">
              <div class="text-sm font-medium" :class="{ 'text-muted-foreground': !i.enabled }">{{ i.title }}</div>
              <p v-if="i.details" class="text-xs text-muted-foreground whitespace-pre-line">{{ i.details }}</p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              <Button variant="secondary" size="sm" :disabled="saving && editingUid === i.uid" @click="startEdit(i)">
                Edit
              </Button>
              <Button variant="destructive" size="sm" :loading="removingUid === i.uid" @click="remove(i.uid)">
                Delete
              </Button>
            </div>
          </li>
        </ul>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Close</Button>
      </DialogFooter>
    </DialogContent>

    <!-- Teleported by default, so it overlays above this Dialog. -->
    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete interest</AlertDialogTitle>
          <AlertDialogDescription>Delete this interest?</AlertDialogDescription>
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
  </Dialog>
</template>
