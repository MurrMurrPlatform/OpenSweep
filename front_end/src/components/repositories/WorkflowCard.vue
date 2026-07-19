<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Workflow } from 'lucide-vue-next'
import { useWorkflowStore } from '@/stores/workflowStore'
import { useAgentStore } from '@/stores/agentStore'
import { useLLMProviderStore } from '@/stores/llmProviderStore'
import { useRunPolicyStore } from '@/stores/runPolicyStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import type { ReviewDepth, WorkflowConfig, WorkflowStage, WorkflowStageConfig } from '@/types/api'

// reka SelectItem values can't be empty strings — the "no selection / default"
// choice for prompt/provider/policy uses this sentinel and is translated back
// to '' at the read/write boundary via v-model helpers below.
const NONE = '__none__'

interface Props {
  repositoryUid: string
}
const props = defineProps<Props>()

const workflow = useWorkflowStore()
const agents = useAgentStore()
const llmProviders = useLLMProviderStore()
const runPolicies = useRunPolicyStore()
const toast = useToast()

const config = ref<WorkflowConfig | null>(null)
const loading = ref(true)
const saving = ref(false)
const loadError = ref<string | null>(null)

// Editable copies — the form owns these, `config` mirrors the server.
const form = ref<Record<WorkflowStage, WorkflowStageConfig>>({} as Record<WorkflowStage, WorkflowStageConfig>)

const STAGE_ORDER: WorkflowStage[] = ['ask', 'analysis', 'discover', 'review', 'fix', 'implement', 'verify', 'document']

const STAGE_HELP: Record<WorkflowStage, string> = {
  ask: 'Guidance appended to sweep and ask runs.',
  analysis: 'Overrides for whole-repo deep-scan runs. Prompt optional — empty keeps the built-in plan → sweep → synthesize scan. Empty policy → the deep effort policy.',
  discover: 'Guidance appended to sweep and ask runs.',
  review: 'Guidance appended to PR review runs.',
  fix: 'Guidance appended to PR fix runs.',
  implement: 'Guidance appended to ticket implement runs.',
  verify: 'Guidance appended to finding verification runs.',
  document: 'Guidance appended to docs and memories upkeep runs.',
}

const AUTO_HELP: Partial<Record<WorkflowStage, string>> = {
  review: 'Auto-review PRs on open/sync.',
  fix: 'Auto-dispatch a fix run when a review requests changes (bounded by max fix rounds).',
  verify: 'Challenge every blocking review verdict with a skeptic run before it drives the fix loop.',
}

/** The depth dial is consumed by auto reviews; manual triggers pick their own. */
const DEPTH_STAGES: WorkflowStage[] = ['review']

const DEPTH_OPTIONS = [
  { label: 'Quick — top 5, blocking only', value: 'quick' },
  { label: 'Normal — everything defensible', value: 'normal' },
  { label: 'Deep — exhaustive, all lenses', value: 'deep' },
]

function hydrate(c: WorkflowConfig) {
  config.value = c
  const next = {} as Record<WorkflowStage, WorkflowStageConfig>
  for (const stage of STAGE_ORDER) {
    const s = c.stages[stage]
    next[stage] = {
      agent_uid: s?.agent_uid ?? '',
      auto: s?.auto ?? false,
      depth: (s?.depth ?? 'normal') as ReviewDepth,
      provider_uid: s?.provider_uid ?? '',
      model: s?.model ?? '',
      max_wall_seconds: s?.max_wall_seconds ?? 0,
      run_policy_uid: s?.run_policy_uid ?? '',
    }
  }
  form.value = next
}

async function load() {
  loading.value = true
  loadError.value = null
  try {
    const [c] = await Promise.all([
      workflow.fetchForRepo(props.repositoryUid),
      agents.fetchAll({ enabled_only: true }),
      llmProviders.fetchAll(),
      runPolicies.fetchAll(),
    ])
    hydrate(c)
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.repositoryUid, () => void load())

const promptOptions = computed(() => [
  { label: 'No guidance (structural intent only)', value: NONE },
  // Playbook base agents (opensweep://agent/<key>) are the instructions
  // layer of every run already — assigning one as stage guidance would
  // duplicate it.
  ...agents.list
    .filter((a) => a.enabled && !a.source_url.startsWith('opensweep://agent/'))
    .map((a) => ({ label: a.title, value: a.uid })),
])

function isKnownPrompt(uid: string): boolean {
  return uid !== '' && promptOptions.value.some((o) => o.value === uid)
}

const providerOptions = computed(() => [
  { label: 'Default provider (active chain)', value: NONE },
  ...llmProviders.list
    .filter((p) => p.enabled)
    .map((p) => ({ label: `${p.label}${p.model ? ` — ${p.model}` : ''}`, value: p.uid })),
])

const policyOptions = computed(() => [
  { label: 'Default policy (effort / system default)', value: NONE },
  ...runPolicies.list.map((p) => ({ label: p.name || p.uid, value: p.uid })),
])

/** reka model-value <-> stored uid: '' stored, sentinel shown at the boundary. */
const toSelect = (uid: string) => (uid === '' ? NONE : uid)
const fromSelect = (v: unknown) => (v === NONE ? '' : String(v))

const dirty = computed(() => {
  const c = config.value
  if (!c) return false
  return STAGE_ORDER.some((stage) => {
    const server = c.stages[stage]
    const local = form.value[stage]
    if (!server || !local) return false
    return (
      local.agent_uid !== server.agent_uid ||
      local.auto !== server.auto ||
      local.depth !== server.depth ||
      local.provider_uid !== (server.provider_uid ?? '') ||
      local.model !== (server.model ?? '') ||
      Number(local.max_wall_seconds || 0) !== (server.max_wall_seconds ?? 0) ||
      local.run_policy_uid !== (server.run_policy_uid ?? '')
    )
  })
})

function isAutoStage(stage: WorkflowStage): boolean {
  return config.value?.auto_stages.includes(stage) ?? false
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    const stages = {} as Record<WorkflowStage, WorkflowStageConfig>
    for (const stage of STAGE_ORDER) {
      stages[stage] = {
        agent_uid: form.value[stage].agent_uid,
        auto: form.value[stage].auto,
        depth: form.value[stage].depth,
        provider_uid: form.value[stage].provider_uid,
        model: form.value[stage].model.trim(),
        max_wall_seconds: Math.max(0, Math.floor(Number(form.value[stage].max_wall_seconds) || 0)),
        run_policy_uid: form.value[stage].run_policy_uid,
      }
    }
    hydrate(await workflow.update(props.repositoryUid, { stages }))
    toast.success('Workflow saved')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save workflow', msg)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Card>
    <CardHeader class="flex-col gap-3 sm:flex-row sm:items-start sm:justify-between space-y-0">
      <div>
        <CardTitle class="flex items-center gap-2 text-base">
          <Workflow class="h-4 w-4 text-muted-foreground" /> Workflow
        </CardTitle>
        <div class="text-xs text-muted-foreground mt-0.5">
          Each stage defaults to its seeded “OpenSweep default” guidance agent —
          edit those in the Agent library, or swap in another agent per stage here.
          Per stage you can also pin an LLM provider, override its model, set a
          wall-clock ceiling, and choose a run policy (its full ceiling bundle);
          empty/0 inherit the platform defaults.
        </div>
      </div>
      <Button
        size="sm"
        class="shrink-0"
        :disabled="loading || !config || !dirty"
        :loading="saving"
        @click="save"
      >
        Save
      </Button>
    </CardHeader>
    <CardContent>
      <div v-if="loading" class="text-sm text-muted-foreground">Loading workflow…</div>
      <div v-else-if="loadError" class="text-sm text-muted-foreground">
        Couldn’t load the workflow: {{ loadError }}
        <Button variant="outline" size="sm" class="ml-2" @click="load">Retry</Button>
      </div>
      <div v-else-if="config" class="space-y-4 text-sm">
        <div
          v-for="stage in STAGE_ORDER"
          :key="stage"
          class="grid gap-3 lg:grid-cols-[8rem_1fr_auto] lg:items-start"
        >
          <div>
            <div class="text-xs font-medium text-foreground capitalize">{{ stage }}</div>
            <p class="text-xs text-muted-foreground">{{ STAGE_HELP[stage] }}</p>
          </div>
          <div class="space-y-2 min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <Select
                :model-value="toSelect(form[stage].agent_uid)"
                @update:model-value="form[stage].agent_uid = fromSelect($event)"
              >
                <SelectTrigger class="flex-1 min-w-40">
                  <SelectValue
                    :placeholder="isKnownPrompt(form[stage].agent_uid) ? undefined : 'No guidance (structural intent only)'"
                  />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in promptOptions" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
                </SelectContent>
              </Select>
              <Select
                v-if="DEPTH_STAGES.includes(stage)"
                :model-value="form[stage].depth"
                @update:model-value="form[stage].depth = $event as ReviewDepth"
              >
                <SelectTrigger
                  class="w-full shrink-0 sm:w-56"
                  title="Depth used by automatic (webhook) reviews. Manual reviews choose per run."
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in DEPTH_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <Select
                :model-value="toSelect(form[stage].provider_uid)"
                @update:model-value="form[stage].provider_uid = fromSelect($event)"
              >
                <SelectTrigger
                  class="flex-1 min-w-40"
                  title="Pin this stage's runs to a specific LLM provider. Default follows the active provider chain."
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in providerOptions" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
                </SelectContent>
              </Select>
              <Input
                v-model="form[stage].model"
                class="w-full shrink-0 sm:w-44"
                placeholder="Model (provider default)"
                title="Model override for this stage's runs. Empty uses the provider's own model."
              />
              <Input
                :model-value="form[stage].max_wall_seconds || ''"
                type="number"
                min="0"
                step="60"
                class="w-full shrink-0 sm:w-32"
                placeholder="Wall s"
                title="Wall-clock ceiling in seconds for this stage's runs (60–21600). 0 inherits the run policy's ceiling. Applies to local providers too when set."
                @update:model-value="form[stage].max_wall_seconds = Math.max(0, Math.floor(Number($event) || 0))"
              />
            </div>
            <div class="flex items-center gap-2">
              <Select
                :model-value="toSelect(form[stage].run_policy_uid)"
                @update:model-value="form[stage].run_policy_uid = fromSelect($event)"
              >
                <SelectTrigger
                  class="flex-1"
                  title="Run policy for this stage — its full ceiling bundle (dollars, wall time, tool turns, files). Default follows the agent's effort, then the system default. An explicit wall seconds above still overrides this policy's wall ceiling."
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in policyOptions" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div v-if="isAutoStage(stage)" class="flex items-center gap-2">
            <p class="max-w-52 text-xs text-muted-foreground lg:text-right">{{ AUTO_HELP[stage] }}</p>
            <Switch v-model="form[stage].auto" />
          </div>
          <div v-else class="hidden lg:block" />
        </div>
      </div>
    </CardContent>
  </Card>
</template>
