<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
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
import type { Repository } from '@/types/api'

const props = defineProps<{
  open: boolean
  repository?: Repository | null
  submitting?: boolean
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  submit: [value: Partial<Repository>]
}>()

const form = reactive({
  slug: '',
  name: '',
  description: '',
  default_branch: 'main',
  color_scheme: 'indigo',
  github_owner: '',
  github_repo: '',
})

function reset() {
  form.slug = ''
  form.name = ''
  form.description = ''
  form.default_branch = 'main'
  form.color_scheme = 'indigo'
  form.github_owner = ''
  form.github_repo = ''
}

watch(() => props.open, (val) => {
  if (!val) return
  if (props.repository) {
    const r = props.repository
    form.slug = r.slug
    form.name = r.name
    form.description = r.description ?? ''
    form.default_branch = r.default_branch ?? 'main'
    form.color_scheme = r.color_scheme ?? 'indigo'
    form.github_owner = r.github_owner ?? ''
    form.github_repo = r.github_repo ?? ''
  } else {
    reset()
  }
})

const colorOptions = [
  { value: 'indigo', label: 'Indigo' },
  { value: 'violet', label: 'Violet' },
  { value: 'emerald', label: 'Emerald' },
  { value: 'amber', label: 'Amber' },
  { value: 'rose', label: 'Rose' },
  { value: 'sky', label: 'Sky' },
]

const canSubmit = computed(() => {
  if (!form.slug.trim() || !form.name.trim()) return false
  return !!form.github_owner.trim() && !!form.github_repo.trim()
})

function onSubmit() {
  const payload: Partial<Repository> = {
    slug: form.slug.trim(),
    mode: 'github',
    name: form.name.trim(),
    description: form.description.trim(),
    default_branch: form.default_branch.trim() || 'main',
    color_scheme: form.color_scheme,
    github_owner: form.github_owner.trim(),
    github_repo: form.github_repo.trim(),
  }
  emit('submit', payload)
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>{{ repository ? `Edit repository · ${repository.name}` : 'Add repository' }}</DialogTitle>
        <DialogDescription>Connect a GitHub repo to OpenSweep.</DialogDescription>
      </DialogHeader>

      <DialogBody class="flex flex-col gap-3">
        <div class="flex flex-col gap-1">
          <Label for="repo-name">Name</Label>
          <Input id="repo-name" v-model="form.name" placeholder="My project" />
        </div>

        <div class="flex flex-col gap-1">
          <Label for="repo-slug">Slug</Label>
          <Input id="repo-slug" v-model="form.slug" :disabled="!!repository" placeholder="my-project" />
          <span class="text-muted-foreground text-xs">URL-safe, unique. Cannot be changed after creation.</span>
        </div>

        <div class="flex flex-col gap-1">
          <Label for="repo-owner">GitHub owner</Label>
          <Input id="repo-owner" v-model="form.github_owner" placeholder="my-org" />
        </div>
        <div class="flex flex-col gap-1">
          <Label for="repo-repo">GitHub repo</Label>
          <Input id="repo-repo" v-model="form.github_repo" placeholder="my-repo" />
        </div>

        <div class="flex flex-col gap-1">
          <Label for="repo-branch">Default branch</Label>
          <Input id="repo-branch" v-model="form.default_branch" placeholder="main" />
        </div>

        <div class="flex flex-col gap-1">
          <Label for="repo-desc">Description</Label>
          <Textarea id="repo-desc" v-model="form.description" :rows="2" />
        </div>

        <div class="flex flex-col gap-1">
          <Label>Color scheme</Label>
          <Select
            :model-value="form.color_scheme"
            @update:model-value="form.color_scheme = $event as string"
          >
            <SelectTrigger class="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in colorOptions" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </DialogBody>

      <DialogFooter>
        <Button variant="ghost" @click="emit('update:open', false)">Cancel</Button>
        <Button :disabled="!canSubmit || submitting" :loading="submitting" @click="onSubmit">
          {{ repository ? 'Save changes' : 'Add repository' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
