<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Trash2 } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agentStore'
import { useScheduledAgentStore } from '@/stores/scheduledAgentStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import CommentThread from '@/components/comments/CommentThread.vue'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import ScheduleEditor from '@/components/agents/ScheduleEditor.vue'
import type { AgentDTO, ComputeDial, RunDTO, ScheduledAgentDTO } from '@/types/api'

const route = useRoute()
const router = useRouter()
const scheduled = useScheduledAgentStore()
const agents = useAgentStore()
const toast = useToast()

const sa = ref<ScheduledAgentDTO | null>(null)
const agent = ref<AgentDTO | null>(null)
const runs = ref<RunDTO[]>([])
const loading = ref(true)
const triggering = ref(false)
const savingSchedule = ref(false)

/** target.limit of the seeded audit-stale binding — pages per tick. */
const auditLimit = computed(() => {
  const raw = sa.value?.target?.limit
  return typeof raw === 'number' && raw > 0 ? raw : 3
})

const scheduleHint = computed(() =>
  sa.value?.agent_key === 'audit-stale'
    ? `Each due tick audits the stalest / never-checked documentation pages (up to ${auditLimit.value} per tick, one scoped run each). “Disabled” on the compute dial is the kill switch even with a cron set.`
    : '',
)

onMounted(load)

async function load() {
  loading.value = true
  try {
    sa.value = await scheduled.get(String(route.params.uid))
    runs.value = await scheduled.fetchRuns(sa.value.uid)
    agent.value = await agents.get(sa.value.agent_uid).catch(() => null)
  } finally {
    loading.value = false
  }
}

async function saveSchedule(payload: { trigger: string; compute_dial: ComputeDial }) {
  if (!sa.value || savingSchedule.value) return
  savingSchedule.value = true
  try {
    sa.value = await scheduled.update(sa.value.uid, payload)
    toast.success('Schedule saved')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save schedule', msg)
  } finally {
    savingSchedule.value = false
  }
}

async function trigger() {
  if (!sa.value || triggering.value) return
  triggering.value = true
  try {
    await scheduled.trigger(sa.value.uid)
    runs.value = await scheduled.fetchRuns(sa.value.uid)
    toast.success('Run dispatched')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Trigger failed', msg)
  } finally {
    triggering.value = false
  }
}

async function remove() {
  if (!sa.value) return
  try {
    await scheduled.remove(sa.value.uid)
    toast.success('Scheduled agent removed')
    router.back()
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t remove', msg)
  }
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !sa">
      <Skeleton class="h-12 w-2/3" />
      <Skeleton class="h-24" />
      <Skeleton class="h-32" />
    </template>

    <template v-else-if="sa">
      <PageHeader :title="sa.title || sa.agent_title">
        <template #breadcrumb>
          <div class="mb-1 flex items-center gap-2">
            <ProducesBadge :produces="sa.agent_produces" />
            <Badge v-if="sa.provenance === 'system'" variant="outline">Seeded</Badge>
            <router-link
              :to="{ name: 'agent-detail', params: { uid: sa.agent_uid } }"
              class="text-xs text-primary hover:underline"
            >
              View agent in library →
            </router-link>
          </div>
        </template>
        <AlertDialog>
          <AlertDialogTrigger as-child>
            <Button variant="outline" size="sm"><Trash2 /> Remove</Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Remove this scheduled agent?</AlertDialogTitle>
              <AlertDialogDescription>
                The binding (schedule, dial, scope) is deleted. The agent stays in the
                library and past runs are kept.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction @click="remove">Remove</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        <Button size="sm" :loading="triggering" @click="trigger">Run now</Button>
      </PageHeader>

      <Card v-if="agent">
        <CardHeader class="p-4 pb-0">
          <CardTitle class="text-base">Agent prompt</CardTitle>
        </CardHeader>
        <CardContent class="p-4">
          <p class="line-clamp-6 whitespace-pre-line text-sm text-muted-foreground">
            {{ agent.prompt || agent.description || '(no prompt body — system instructions apply)' }}
          </p>
        </CardContent>
      </Card>

      <ScheduleEditor
        :trigger="sa.trigger"
        :compute-dial="sa.compute_dial"
        :saving="savingSchedule"
        :hint="scheduleHint"
        @save="saveSchedule"
      />

      <Card>
        <CardHeader class="p-4"><CardTitle class="text-base">Runs</CardTitle></CardHeader>
        <CardContent class="p-0">
          <ul v-if="runs.length" class="stagger-children divide-y px-4">
            <li v-for="r in runs" :key="r.uid" class="py-2 text-sm">
              <span class="font-mono uppercase">{{ r.status }}</span> · {{ r.executor }} ·
              <router-link
                :to="{ name: 'run-detail', params: { uid: r.uid } }"
                class="font-mono text-primary hover:underline"
              >
                {{ r.uid.slice(0, 8) }}
              </router-link>
              <span v-if="r.error" class="text-destructive"> · {{ r.error }}</span>
            </li>
          </ul>
          <div v-else class="p-4 text-sm text-muted-foreground">No runs yet.</div>
        </CardContent>
      </Card>

      <CommentThread
        subject-type="scheduled_agent"
        :subject-uid="sa.uid"
        :repository-uid="sa.repository_uid"
        title="Discussion"
      />
    </template>
  </div>
</template>
