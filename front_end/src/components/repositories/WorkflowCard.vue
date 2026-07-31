<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronRight, ExternalLink, Workflow } from 'lucide-vue-next'
import { useWorkflowStore } from '@/stores/workflowStore'
import { useAgentStore } from '@/stores/agentStore'
import { useLLMProviderStore } from '@/stores/llmProviderStore'
import { useRunPolicyStore } from '@/stores/runPolicyStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
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
import type { AgentDTO, ReviewDepth, WorkflowConfig, WorkflowStage, WorkflowStageConfig } from '@/types/api'

// reka SelectItem values can't be empty strings — the "inherit the stage
// default" choice uses this sentinel and is translated back to '' at the
// read/write boundary via the helpers below.
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

// From fetchAll's return value, NOT agents.list — the store only caches
// unfiltered fetches, and this card asks for enabled_only.
const agentList = ref<AgentDTO[]>([])

// Editable copies — the form owns these, `config` mirrors the server.
const form = ref<Record<WorkflowStage, WorkflowStageConfig>>({} as Record<WorkflowStage, WorkflowStageConfig>)

// Per-stage disclosure: most users only touch review/fix, so stages start
// collapsed behind a one-line summary.
const expanded = ref<Record<string, boolean>>({})
const showGuidance = ref<Record<string, boolean>>({})

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
    const [c, fetchedAgents] = await Promise.all([
      workflow.fetchForRepo(props.repositoryUid),
      agents.fetchAll({ enabled_only: true }),
      llmProviders.fetchAll(),
      runPolicies.fetchAll(),
    ])
    agentList.value = fetchedAgents
    hydrate(c)
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.repositoryUid, () => void load())

// ── Guidance resolution (stored uid '' = inherit the seeded stage default) ──

function agentByUid(uid: string): AgentDTO | null {
  if (!uid) return null
  return agentList.value.find((a) => a.uid === uid) ?? null
}

function defaultAgent(stage: WorkflowStage): AgentDTO | null {
  return agentByUid(config.value?.default_agent_uids?.[stage] ?? '')
}

/** The agent whose body this stage's runs actually get: the pinned one, or
 *  the seeded default when the stage inherits. */
function effectiveAgent(stage: WorkflowStage): AgentDTO | null {
  return agentByUid(form.value[stage]?.agent_uid ?? '') ?? defaultAgent(stage)
}

/** First option is honest about what '' does: inherit the seeded default —
 *  "no guidance" only when that seeded prompt is deleted or disabled. */
function defaultOptionLabel(stage: WorkflowStage): string {
  const d = defaultAgent(stage)
  return d ? `Default — ${d.title}` : 'No guidance (structural intent only)'
}

const assignableAgents = computed(() =>
  // Playbook base agents (opensweep://agent/<key>) are the instructions
  // layer of every run already — assigning one as stage guidance would
  // duplicate it.
  agentList.value.filter((a) => a.enabled && !a.source_url.startsWith('opensweep://agent/')),
)

function promptOptions(stage: WorkflowStage) {
  return [
    { label: defaultOptionLabel(stage), value: NONE },
    ...assignableAgents.value.map((a) => ({ label: a.title, value: a.uid })),
  ]
}

/** A stored uid pointing at a deleted/disabled agent renders as a warning
 *  placeholder instead of silently looking like the default. */
function pinnedButUnknown(stage: WorkflowStage): boolean {
  const uid = form.value[stage]?.agent_uid ?? ''
  return uid !== '' && !agentByUid(uid)
}

function guidanceSummary(stage: WorkflowStage): string {
  const uid = form.value[stage]?.agent_uid ?? ''
  if (uid === '') {
    const d = defaultAgent(stage)
    return d ? `Default — ${d.title}` : 'No guidance'
  }
  return agentByUid(uid)?.title ?? 'Unknown agent (disabled or deleted)'
}

function overridesSummary(stage: WorkflowStage): string {
  const s = form.value[stage]
  if (!s) return ''
  const n = [s.provider_uid, s.model, s.run_policy_uid].filter(Boolean).length + (s.max_wall_seconds ? 1 : 0)
  return n ? `${n} override${n === 1 ? '' : 's'}` : ''
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

function stageDirty(stage: WorkflowStage): boolean {
  const server = config.value?.stages[stage]
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
}

const dirty = computed(() => !!config.value && STAGE_ORDER.some(stageDirty))

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
          Each stage's runs get its guidance prompt appended — by default the
          seeded “OpenSweep default” agent (edit those in the Agent library),
          or swap in another agent per stage here. Expand a stage to preview
          the guidance text, pin an LLM provider, override its model, set a
          wall-clock ceiling, or choose a run policy; empty/0 inherit the
          platform defaults.
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
    <CardContent class="p-0">
      <div v-if="loading" class="p-4 text-sm text-muted-foreground">Loading workflow…</div>
      <div v-else-if="loadError" class="p-4 text-sm text-muted-foreground">
        Couldn’t load the workflow: {{ loadError }}
        <Button variant="outline" size="sm" class="ml-2" @click="load">Retry</Button>
      </div>
      <div v-else-if="config" class="divide-y divide-border text-sm">
        <div v-for="stage in STAGE_ORDER" :key="stage">
          <!-- Summary row: the whole row toggles disclosure; the auto switch
               is the one control worth reaching without expanding. -->
          <div class="flex items-center gap-2 px-4 py-2.5">
            <button
              type="button"
              class="flex min-w-0 flex-1 items-center gap-2 text-left"
              @click="expanded[stage] = !expanded[stage]"
            >
              <ChevronRight
                class="size-3.5 shrink-0 text-muted-foreground transition-transform"
                :class="expanded[stage] ? 'rotate-90' : ''"
              />
              <span class="w-20 shrink-0 font-medium capitalize">
                {{ stage }}<span v-if="stageDirty(stage)" class="text-primary" title="Unsaved changes">*</span>
              </span>
              <span class="min-w-0 truncate text-muted-foreground">{{ guidanceSummary(stage) }}</span>
              <Badge v-if="DEPTH_STAGES.includes(stage)" variant="outline" class="shrink-0 capitalize">
                {{ form[stage].depth }}
              </Badge>
              <Badge v-if="overridesSummary(stage)" variant="secondary" class="shrink-0">
                {{ overridesSummary(stage) }}
              </Badge>
            </button>
            <div v-if="isAutoStage(stage)" class="flex shrink-0 items-center gap-1.5" :title="AUTO_HELP[stage]">
              <span class="text-xs text-muted-foreground">auto</span>
              <Switch v-model="form[stage].auto" />
            </div>
          </div>

          <!-- Expanded controls -->
          <div v-if="expanded[stage]" class="space-y-2 border-t border-border/60 bg-muted/30 px-4 py-3 lg:pl-10">
            <p class="text-xs text-muted-foreground">{{ STAGE_HELP[stage] }}</p>
            <div class="flex flex-wrap items-center gap-2">
              <Select
                :model-value="toSelect(form[stage].agent_uid)"
                @update:model-value="form[stage].agent_uid = fromSelect($event)"
              >
                <SelectTrigger class="flex-1 min-w-40">
                  <SelectValue
                    :placeholder="pinnedButUnknown(stage) ? 'Unknown agent (disabled or deleted)' : undefined"
                  />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in promptOptions(stage)" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
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

            <!-- Guidance preview: what this stage's runs actually receive. -->
            <div v-if="effectiveAgent(stage)" class="flex flex-wrap items-center gap-3 text-xs">
              <button
                type="button"
                class="text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                @click="showGuidance[stage] = !showGuidance[stage]"
              >
                {{ showGuidance[stage] ? 'Hide guidance text' : 'Show guidance text' }}
              </button>
              <RouterLink
                :to="{ name: 'agent-detail', params: { uid: effectiveAgent(stage)!.uid } }"
                class="inline-flex items-center gap-1 text-primary hover:underline"
              >
                Edit in library <ExternalLink class="size-3" />
              </RouterLink>
            </div>
            <pre
              v-if="showGuidance[stage] && effectiveAgent(stage)"
              class="max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md border bg-background p-3 font-sans text-xs text-muted-foreground"
            >{{ effectiveAgent(stage)!.prompt || '(this agent has an empty prompt body)' }}</pre>

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
        </div>
      </div>
    </CardContent>
  </Card>
</template>
