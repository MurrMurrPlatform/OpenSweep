<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Bot, Plus, RefreshCw } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agentStore'
import { useToast } from '@/composables/useToast'
import { PageHeader } from '@/components/ui/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import type { AgentDTO } from '@/types/api'

const router = useRouter()
const agents = useAgentStore()
const toast = useToast()

const loading = ref(true)
const search = ref('')
const producesFilter = ref<string>('')
const provenanceFilter = ref<string>('')

onMounted(load)

async function load() {
  loading.value = true
  try {
    await agents.fetchAll()
  } finally {
    loading.value = false
  }
}

const PROVENANCE_CHIPS = [
  { label: 'All', value: '' },
  { label: 'System', value: 'system' },
  { label: 'Yours', value: 'user' },
  { label: 'Imported', value: 'imported' },
]

const producesChips = computed(() => {
  const kinds = new Set(agents.list.map((a) => a.produces))
  return ['', ...kinds]
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return agents.list.filter((a) => {
    if (producesFilter.value && a.produces !== producesFilter.value) return false
    if (provenanceFilter.value && a.provenance !== provenanceFilter.value) return false
    if (
      q &&
      !a.title.toLowerCase().includes(q) &&
      !a.description.toLowerCase().includes(q) &&
      !a.tags.some((t) => t.toLowerCase().includes(q))
    )
      return false
    return true
  })
})

async function toggleEnabled(a: AgentDTO) {
  try {
    await agents.update(a.uid, { enabled: !a.enabled })
  } catch (e) {
    toast.error('Couldn’t update agent', e instanceof Error ? e.message : String(e))
  }
}

function open(a: AgentDTO) {
  router.push({ name: 'agent-detail', params: { uid: a.uid } })
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Agent library"
      subtitle="Reusable agent definitions — prompts, what they produce, and your org's tuning of the system agents. Schedule any of them per repository."
    >
      <Button variant="outline" size="sm" @click="load"><RefreshCw /> Refresh</Button>
      <Button size="sm" @click="router.push({ name: 'agent-create' })">
        <Plus /> New agent
      </Button>
    </PageHeader>

    <div class="flex flex-wrap items-center gap-2">
      <Input v-model="search" placeholder="Search agents…" class="max-w-xs" />
      <div class="flex flex-wrap gap-1.5">
        <Button
          v-for="c in PROVENANCE_CHIPS"
          :key="c.value"
          :variant="provenanceFilter === c.value ? 'default' : 'outline'"
          size="sm"
          @click="provenanceFilter = c.value"
        >
          {{ c.label }}
        </Button>
      </div>
      <div class="flex flex-wrap gap-1.5">
        <Button
          v-for="p in producesChips"
          :key="p || 'all'"
          :variant="producesFilter === p ? 'secondary' : 'ghost'"
          size="sm"
          @click="producesFilter = p"
        >
          {{ p || 'Any output' }}
        </Button>
      </div>
    </div>

    <template v-if="loading">
      <Skeleton class="h-20" />
      <Skeleton class="h-20" />
      <Skeleton class="h-20" />
    </template>

    <Card v-else-if="!filtered.length">
      <CardContent class="flex flex-col items-center gap-2 p-8 text-center">
        <Bot class="h-8 w-8 text-muted-foreground" />
        <p class="text-sm text-muted-foreground">
          No agents match. Create one, or save a prompt as an agent from the Ask page.
        </p>
      </CardContent>
    </Card>

    <div v-else class="stagger-children space-y-2">
      <Card
        v-for="a in filtered"
        :key="a.uid"
        class="cursor-pointer transition-colors hover:bg-accent/50"
        @click="open(a)"
      >
        <CardContent class="flex items-start justify-between gap-4 p-4">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-medium">{{ a.title }}</span>
              <ProducesBadge :produces="a.produces" />
              <Badge v-if="a.provenance === 'system'" variant="outline">System</Badge>
              <Badge v-if="a.provenance === 'imported'" variant="secondary">Imported</Badge>
              <Badge v-if="a.has_org_override" variant="info">Org override</Badge>
            </div>
            <p class="mt-1 line-clamp-2 text-sm text-muted-foreground">{{ a.description }}</p>
            <div v-if="a.tags.length" class="mt-1.5 flex flex-wrap gap-1">
              <Badge v-for="t in a.tags.slice(0, 6)" :key="t" variant="secondary" class="text-[10px]">
                {{ t }}
              </Badge>
            </div>
          </div>
          <div @click.stop>
            <Switch :model-value="a.enabled" @update:model-value="toggleEnabled(a)" />
          </div>
        </CardContent>
      </Card>
    </div>
  </div>
</template>
