<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Telescope } from 'lucide-vue-next'
import { useLensStore } from '@/stores/lensStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import type { LensDTO } from '@/types/api'

const lenses = useLensStore()
const toast = useToast()

const loading = ref(true)

onMounted(load)

async function load() {
  loading.value = true
  try {
    await lenses.fetchAll()
  } catch (e) {
    toast.error('Couldn’t load lenses', e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

const sections = computed(() => [
  {
    label: 'Local lenses',
    hint: 'Compose into per-area checklists inside campaign parts.',
    items: lenses.list.filter((l) => !l.global_agent_key),
  },
  {
    label: 'Global lenses',
    hint: 'Back whole-repo sweep agents for cross-cutting concerns.',
    items: lenses.list.filter((l) => !!l.global_agent_key),
  },
])

async function toggleEnabled(l: LensDTO) {
  try {
    await lenses.update(l.key, { enabled: !l.enabled })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t update lens', msg)
  }
}

// ── Edit sheet — title/body/tags/enabled are org-tunable; key/scope are not ──

const editing = ref<LensDTO | null>(null)
const editTitle = ref('')
const editBody = ref('')
const editTags = ref('')
const editEnabled = ref(true)
const saving = ref(false)

function openEditor(l: LensDTO) {
  editing.value = l
  editTitle.value = l.title
  editBody.value = l.body
  editTags.value = l.tags.join(', ')
  editEnabled.value = l.enabled
}

async function save() {
  if (!editing.value || saving.value) return
  saving.value = true
  try {
    await lenses.update(editing.value.key, {
      title: editTitle.value.trim(),
      body: editBody.value,
      tags: editTags.value.split(',').map((t) => t.trim()).filter(Boolean),
      enabled: editEnabled.value,
    })
    toast.success('Lens saved')
    editing.value = null
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save lens', msg)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Lens library"
      subtitle="The audit checklists campaigns sweep with — tune the prose and labels; keys and scopes stay platform-owned."
    >
      <Button variant="outline" size="sm" @click="load"><RefreshCw /> Refresh</Button>
    </PageHeader>

    <template v-if="loading">
      <Skeleton class="h-20" />
      <Skeleton class="h-20" />
      <Skeleton class="h-20" />
    </template>

    <Card v-else-if="!lenses.list.length">
      <CardContent class="flex flex-col items-center gap-2 p-8 text-center">
        <Telescope class="h-8 w-8 text-muted-foreground" />
        <p class="text-sm text-muted-foreground">
          No lenses yet — the platform seeds them; check back after the next sync.
        </p>
      </CardContent>
    </Card>

    <template v-else>
      <section v-for="section in sections" :key="section.label" class="space-y-2">
        <template v-if="section.items.length">
          <div>
            <h2 class="text-sm font-semibold">{{ section.label }}</h2>
            <p class="text-xs text-muted-foreground">{{ section.hint }}</p>
          </div>
          <div class="stagger-children space-y-2">
            <Card
              v-for="l in section.items"
              :key="l.key"
              class="cursor-pointer transition-colors hover:bg-accent/50"
              @click="openEditor(l)"
            >
              <CardContent class="flex items-start justify-between gap-4 p-4">
                <div class="min-w-0">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="font-medium">{{ l.title || l.key }}</span>
                    <Badge variant="outline" class="font-mono text-[10px]">{{ l.key }}</Badge>
                    <Badge :variant="l.global_agent_key ? 'info' : 'secondary'">{{ l.global_agent_key ? 'global' : 'local' }}</Badge>
                    <Badge v-if="l.global_agent_key" variant="outline" class="font-mono text-[10px]">
                      {{ l.global_agent_key }}
                    </Badge>
                  </div>
                  <p class="mt-1 line-clamp-2 text-sm text-muted-foreground">{{ l.body }}</p>
                  <div v-if="l.tags.length" class="mt-1.5 flex flex-wrap gap-1">
                    <Badge v-for="t in l.tags.slice(0, 6)" :key="t" variant="secondary" class="text-[10px]">
                      {{ t }}
                    </Badge>
                  </div>
                </div>
                <div @click.stop>
                  <Switch :model-value="l.enabled" @update:model-value="toggleEnabled(l)" />
                </div>
              </CardContent>
            </Card>
          </div>
        </template>
      </section>
    </template>

    <!-- Edit sheet -->
    <Sheet :open="!!editing" @update:open="(open) => { if (!open) editing = null }">
      <SheetContent side="right" class="flex w-full flex-col overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{{ editing?.title || editing?.key }}</SheetTitle>
          <SheetDescription>
            <span class="font-mono">{{ editing?.key }}</span> · {{ editing?.global_agent_key ? 'global' : 'local' }} lens
          </SheetDescription>
        </SheetHeader>

        <div v-if="editing" class="flex-1 space-y-3">
          <div class="space-y-1.5">
            <Label for="lens-title">Title</Label>
            <Input id="lens-title" v-model="editTitle" />
          </div>
          <div class="space-y-1.5">
            <Label for="lens-body">Body</Label>
            <Textarea id="lens-body" v-model="editBody" :rows="18" class="font-mono text-xs" />
          </div>
          <div class="space-y-1.5">
            <Label for="lens-tags">Tags (comma-separated)</Label>
            <Input id="lens-tags" v-model="editTags" placeholder="security, correctness" />
          </div>
          <div class="flex items-center justify-between rounded-md border p-3">
            <div>
              <p class="text-sm font-medium">Enabled</p>
              <p class="text-xs text-muted-foreground">
                Disabled lenses drop out of new campaign checklists.
              </p>
            </div>
            <Switch v-model="editEnabled" />
          </div>
        </div>

        <SheetFooter>
          <Button variant="ghost" size="sm" @click="editing = null">Cancel</Button>
          <Button size="sm" :loading="saving" @click="save">Save</Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  </div>
</template>
