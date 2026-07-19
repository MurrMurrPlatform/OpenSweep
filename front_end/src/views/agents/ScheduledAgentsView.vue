<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Bot, Play, Plus, RefreshCw } from 'lucide-vue-next'
import { useScheduledAgentStore } from '@/stores/scheduledAgentStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import AgentPickerDialog from '@/components/agents/AgentPickerDialog.vue'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import { formatRelativeTime } from '@/lib/utils'
import type { ScheduledAgentDTO } from '@/types/api'

const router = useRouter()
const scheduled = useScheduledAgentStore()
const toast = useToast()
const { uid: repoUid } = useCurrentRepo()

const loading = ref(true)
const pickerOpen = ref(false)
const triggering = ref<string | null>(null)

onMounted(load)
watch(repoUid, load)

async function load() {
  if (!repoUid.value) return
  loading.value = true
  try {
    await scheduled.fetchAll(repoUid.value)
  } finally {
    loading.value = false
  }
}

function triggerLabel(s: ScheduledAgentDTO): string {
  if (s.trigger === 'on-event') return 'On push'
  if (s.trigger.startsWith('cron:')) return s.trigger.slice('cron:'.length)
  return 'Manual'
}

async function toggleEnabled(s: ScheduledAgentDTO) {
  try {
    await scheduled.update(s.uid, { enabled: !s.enabled })
  } catch (e) {
    toast.error('Couldn’t update', e instanceof Error ? e.message : String(e))
  }
}

async function runNow(s: ScheduledAgentDTO) {
  if (triggering.value) return
  triggering.value = s.uid
  try {
    const run = await scheduled.trigger(s.uid)
    toast.success('Run dispatched')
    router.push({ name: 'run-detail', params: { uid: run.uid } })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Trigger failed', msg)
  } finally {
    triggering.value = null
  }
}

function open(s: ScheduledAgentDTO) {
  router.push({ name: 'scheduled-agent-detail', params: { uid: s.uid } })
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Agents"
      subtitle="Agents bound to this repository — what runs automatically, when it fires, and with how much autonomy."
    >
      <Button variant="outline" size="sm" @click="load"><RefreshCw /> Refresh</Button>
      <Button size="sm" @click="pickerOpen = true"><Plus /> Add agent</Button>
    </PageHeader>

    <template v-if="loading">
      <Skeleton class="h-16" />
      <Skeleton class="h-16" />
    </template>

    <Card v-else-if="!scheduled.list.length">
      <CardContent class="flex flex-col items-center gap-2 p-8 text-center">
        <Bot class="h-8 w-8 text-muted-foreground" />
        <p class="text-sm text-muted-foreground">
          No agents are bound to this repository yet. Add one from the library to
          schedule recurring audits, doc upkeep, or any saved prompt.
        </p>
        <Button size="sm" class="mt-2" @click="pickerOpen = true"><Plus /> Add agent</Button>
      </CardContent>
    </Card>

    <Card v-else>
      <CardContent class="p-0">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b text-left text-xs uppercase text-muted-foreground">
              <th class="px-4 py-2 font-medium">Agent</th>
              <th class="px-4 py-2 font-medium">Trigger</th>
              <th class="px-4 py-2 font-medium">Dial</th>
              <th class="px-4 py-2 font-medium">Last dispatched</th>
              <th class="px-4 py-2 font-medium">Enabled</th>
              <th class="px-4 py-2"></th>
            </tr>
          </thead>
          <tbody class="stagger-children">
            <tr
              v-for="s in scheduled.list"
              :key="s.uid"
              class="cursor-pointer border-b last:border-0 hover:bg-accent/50"
              @click="open(s)"
            >
              <td class="px-4 py-2.5">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="font-medium">{{ s.title || s.agent_title }}</span>
                  <ProducesBadge :produces="s.agent_produces" />
                  <Badge v-if="s.provenance === 'system'" variant="outline">Seeded</Badge>
                </div>
              </td>
              <td class="px-4 py-2.5 font-mono text-xs">{{ triggerLabel(s) }}</td>
              <td class="px-4 py-2.5 text-xs text-muted-foreground">{{ s.compute_dial }}</td>
              <td class="px-4 py-2.5 text-xs text-muted-foreground">
                {{ s.last_scheduled_at ? formatRelativeTime(s.last_scheduled_at) : '—' }}
              </td>
              <td class="px-4 py-2.5" @click.stop>
                <Switch :model-value="s.enabled" @update:model-value="toggleEnabled(s)" />
              </td>
              <td class="px-4 py-2.5 text-right" @click.stop>
                <Button
                  variant="ghost"
                  size="sm"
                  :loading="triggering === s.uid"
                  @click="runNow(s)"
                >
                  <Play /> Run now
                </Button>
              </td>
            </tr>
          </tbody>
        </table>
      </CardContent>
    </Card>

    <AgentPickerDialog
      v-if="repoUid"
      v-model:open="pickerOpen"
      :repository-uid="repoUid"
      @created="load"
    />
  </div>
</template>
