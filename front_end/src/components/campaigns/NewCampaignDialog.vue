<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronRight, Layers, TriangleAlert } from 'lucide-vue-next'
import { useCampaignStore } from '@/stores/campaignStore'
import { useLensStore } from '@/stores/lensStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
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
import type {
  AgentEffort,
  CampaignAreasPreview,
  CampaignDTO,
  CampaignTemplate,
  LensDTO,
} from '@/types/api'

const props = defineProps<{
  open: boolean
  repositoryUid: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  created: [campaign: CampaignDTO]
}>()

const campaigns = useCampaignStore()
const lensStore = useLensStore()
const toast = useToast()

const template = ref<CampaignTemplate>('rotation')
const effort = ref<AgentEffort | 'default'>('default')
const k = ref(3)
const title = ref('')
const creating = ref(false)
const loadingLenses = ref(false)

/** Local lenses only — global lenses ride along with their sweep agents. */
const localLenses = ref<LensDTO[]>([])
const selectedKeys = ref<Set<string>>(new Set())
/** Focused template picks exactly one lens. */
const focusedKey = ref('')

/** Live partition preview — what the plan will look like, before creating. */
const areasPreview = ref<CampaignAreasPreview | null>(null)
const loadingAreas = ref(false)
const areasOpen = ref(false)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    template.value = 'rotation'
    effort.value = 'default'
    k.value = 3
    title.value = ''
    // Best-effort preview — creation works fine without it.
    areasPreview.value = null
    areasOpen.value = false
    loadingAreas.value = true
    campaigns
      .fetchAreas(props.repositoryUid)
      .then((p) => (areasPreview.value = p))
      .catch(() => (areasPreview.value = null))
      .finally(() => (loadingAreas.value = false))
    loadingLenses.value = true
    try {
      const all = await lensStore.fetchAll()
      localLenses.value = all.filter((l) => l.scope === 'local')
      // All enabled lenses on by default — matches the backend's "empty =
      // every enabled lens" planning behavior, made explicit.
      selectedKeys.value = new Set(
        localLenses.value.filter((l) => l.enabled).map((l) => l.key),
      )
      focusedKey.value = localLenses.value.find((l) => l.enabled)?.key ?? ''
    } catch (e) {
      toast.error('Couldn’t load lenses', e instanceof Error ? e.message : String(e))
    } finally {
      loadingLenses.value = false
    }
  },
)

function toggleLens(key: string) {
  const next = new Set(selectedKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedKeys.value = next
}

const lensKeys = computed(() =>
  template.value === 'focused'
    ? focusedKey.value
      ? [focusedKey.value]
      : []
    : localLenses.value.filter((l) => selectedKeys.value.has(l.key)).map((l) => l.key),
)

const areaCount = computed(() => areasPreview.value?.areas.length ?? 0)
const rotationCovers = computed(() => Math.min(Math.max(k.value || 0, 0), areaCount.value))

const previewSummary = computed(() => {
  const p = areasPreview.value
  if (!p) return ''
  const n = (x: number) => x.toLocaleString('en-US')
  const bits = [
    `Partitions into ${p.areas.length} area${p.areas.length === 1 ? '' : 's'}`,
    `${n(p.total_files)} files`,
  ]
  if (p.uncovered_files > 0) bits.push(`${n(p.uncovered_files)} uncovered`)
  return bits.join(' · ')
})

const canCreate = computed(() => {
  if (creating.value || !props.repositoryUid) return false
  if (template.value === 'focused') return !!focusedKey.value
  return lensKeys.value.length > 0
})

async function create() {
  if (!canCreate.value) return
  creating.value = true
  try {
    const campaign = await campaigns.create(props.repositoryUid, {
      template: template.value,
      lens_keys: lensKeys.value,
      effort: effort.value === 'default' ? '' : effort.value,
      k: template.value === 'rotation' ? k.value : undefined,
      title: title.value.trim() || undefined,
    })
    emit('created', campaign)
    emit('update:open', false)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t create campaign', msg)
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>New campaign</DialogTitle>
        <DialogDescription>
          Plan a bounded audit sweep of this repository. Nothing runs until you launch it.
        </DialogDescription>
      </DialogHeader>

      <div class="space-y-3">
        <div class="grid gap-3 md:grid-cols-2">
          <div class="space-y-1.5">
            <Label>Template</Label>
            <Select :model-value="template" @update:model-value="template = $event as CampaignTemplate">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="rotation">Rotation — k least-recently-covered areas</SelectItem>
                <SelectItem value="full">Full — all areas + global sweeps</SelectItem>
                <SelectItem value="focused">Focused — one lens everywhere</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="space-y-1.5">
            <Label>Effort</Label>
            <Select :model-value="effort" @update:model-value="effort = $event as AgentEffort | 'default'">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Default — areas normal, sweeps deep</SelectItem>
                <SelectItem value="short">Short</SelectItem>
                <SelectItem value="normal">Normal</SelectItem>
                <SelectItem value="deep">Deep</SelectItem>
                <SelectItem value="unlimited">Unlimited</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div v-if="template === 'rotation'" class="space-y-1.5">
          <Label for="campaign-k">Areas this pass (k)</Label>
          <Input id="campaign-k" v-model.number="k" type="number" min="1" class="max-w-32" />
        </div>

        <!-- Live partition preview — nothing is persisted until Create. -->
        <div class="rounded-md border bg-muted/30 px-3 py-2 text-sm">
          <div v-if="loadingAreas" class="text-muted-foreground">Sizing areas…</div>
          <div v-else-if="!areasPreview" class="text-muted-foreground">
            Area preview unavailable.
          </div>
          <Collapsible v-else v-model:open="areasOpen">
            <div class="flex flex-wrap items-center gap-2">
              <CollapsibleTrigger
                class="flex min-w-0 items-center gap-1.5 text-left hover:text-foreground"
              >
                <ChevronRight
                  class="h-3.5 w-3.5 shrink-0 transition-transform"
                  :class="{ 'rotate-90': areasOpen }"
                />
                <span class="truncate">{{ previewSummary }}</span>
              </CollapsibleTrigger>
              <Badge
                v-if="areasPreview.degraded"
                variant="warn"
                class="px-1.5 text-[10px]"
                :title="areasPreview.degraded"
              >
                <TriangleAlert class="h-3 w-3" /> degraded
              </Badge>
            </div>
            <p v-if="template === 'rotation' && areaCount" class="mt-1 pl-5 text-xs text-muted-foreground">
              Rotation covers {{ rotationCovers }} of {{ areaCount }} areas this pass.
            </p>
            <CollapsibleContent>
              <ul class="mt-2 max-h-48 space-y-0.5 overflow-y-auto pl-5">
                <li
                  v-for="(a, i) in areasPreview.areas"
                  :key="i"
                  class="flex items-baseline justify-between gap-3 text-xs"
                >
                  <span class="truncate" :title="a.scope_paths.join('\n')">{{ a.title }}</span>
                  <span class="shrink-0 tabular-nums text-muted-foreground">
                    {{ a.file_count ?? '—' }} files
                  </span>
                </li>
              </ul>
            </CollapsibleContent>
          </Collapsible>
        </div>

        <div class="space-y-1.5">
          <Label>{{ template === 'focused' ? 'Lens' : 'Lenses' }}</Label>
          <div class="max-h-56 overflow-y-auto rounded-md border">
            <div v-if="loadingLenses" class="p-3 text-sm text-muted-foreground">Loading…</div>
            <div v-else-if="!localLenses.length" class="p-3 text-sm text-muted-foreground">
              No local lenses available.
            </div>
            <template v-else-if="template === 'focused'">
              <button
                v-for="l in localLenses"
                :key="l.key"
                type="button"
                class="flex w-full items-start gap-2.5 px-3 py-2 text-left text-sm hover:bg-accent"
                :class="{ 'bg-accent': focusedKey === l.key }"
                @click="focusedKey = l.key"
              >
                <input
                  type="radio"
                  class="mt-1 h-4 w-4 cursor-pointer accent-primary"
                  :checked="focusedKey === l.key"
                  tabindex="-1"
                />
                <span class="min-w-0">
                  <span class="flex flex-wrap items-center gap-1.5 font-medium">
                    {{ l.title || l.key }}
                    <Badge v-if="!l.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                  </span>
                  <span class="block font-mono text-xs text-muted-foreground">{{ l.key }}</span>
                </span>
              </button>
            </template>
            <template v-else>
              <label
                v-for="l in localLenses"
                :key="l.key"
                class="flex cursor-pointer items-start gap-2.5 px-3 py-2 text-sm hover:bg-accent"
              >
                <input
                  type="checkbox"
                  class="mt-1 h-4 w-4 cursor-pointer accent-primary"
                  :checked="selectedKeys.has(l.key)"
                  @change="toggleLens(l.key)"
                />
                <span class="min-w-0">
                  <span class="flex flex-wrap items-center gap-1.5 font-medium">
                    {{ l.title || l.key }}
                    <Badge v-if="!l.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                  </span>
                  <span class="block font-mono text-xs text-muted-foreground">{{ l.key }}</span>
                </span>
              </label>
            </template>
          </div>
        </div>

        <div class="space-y-1.5">
          <Label for="campaign-title">Title (optional)</Label>
          <Input id="campaign-title" v-model="title" placeholder="What is this sweep for?" />
        </div>
      </div>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!canCreate" :loading="creating" @click="create">
          <Layers /> Create campaign
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
