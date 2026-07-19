<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { Activity, CalendarClock, Radar, Search } from 'lucide-vue-next'
import { useDocStore } from '@/stores/docStore'
import { useAnalysisStore, type AnalysisDTO } from '@/stores/analysisStore'
import { useScheduledAgentStore } from '@/stores/scheduledAgentStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { AnimatedNumber } from '@/components/ui/animated-number'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import type { ComputeDial, DocDTO, ScheduledAgentDTO, ScopeFreshnessDTO } from '@/types/api'

const docs = useDocStore()
const analyses = useAnalysisStore()
const scheduledAgents = useScheduledAgentStore()
const latestAnalysis = ref<AnalysisDTO | null>(null)
const toast = useToast()
const { uid: repoUid, slug: repoSlug } = useCurrentRepo()

const freshness = ref<ScopeFreshnessDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

// ── Audit dispatch (staleness-driven or whole repo, §F) ─────────────────────
const auditOpen = ref(false)
const auditing = ref(false)
/** true = pick the stalest / never-checked pages automatically. */
const auditAutoSelect = ref(true)
const auditLimit = ref('3')
const auditMaxFindings = ref('')
const auditIntent = ref('')
/** Non-empty = single-page audit launched from a table row. */
const auditDocUid = ref('')
const auditDocSlug = ref('')

function openAudit(doc?: DocDTO) {
  auditDocUid.value = doc?.uid ?? ''
  auditDocSlug.value = doc?.slug ?? ''
  auditAutoSelect.value = !doc
  auditOpen.value = true
}

async function dispatchAudit() {
  if (!repoUid.value || auditing.value) return
  auditing.value = true
  try {
    const budget = Number.parseInt(auditMaxFindings.value, 10)
    const limit = Number.parseInt(auditLimit.value, 10)
    const result = await docs.audit(
      repoUid.value,
      auditDocUid.value ? [auditDocUid.value] : [],
      {
        auto_select: !auditDocUid.value && auditAutoSelect.value,
        limit: Number.isFinite(limit) && limit > 0 ? limit : 3,
        custom_intent: auditIntent.value.trim() || undefined,
        max_findings: Number.isFinite(budget) && budget > 0 ? budget : undefined,
      },
    )
    auditOpen.value = false
    auditIntent.value = ''
    const picked = result.selected.length
      ? `picked: ${result.selected.map((s) => s.slug).join(', ')}`
      : null
    if (result.runs_dispatched.length === 0 && result.errors.length === 0) {
      toast.success('Nothing to audit', result.summary)
    } else if (result.errors.length) {
      toast.error('Audit partially dispatched', result.errors.join(' · '))
    } else {
      toast.success(result.summary, picked ?? undefined)
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t dispatch audit', msg)
  } finally {
    auditing.value = false
  }
}

// ── Deep scan (one long whole-repo sweep: plan → sweep → synthesize) ─────────
const deepScanOpen = ref(false)
const deepScanning = ref(false)
const deepScanIntent = ref('')
const deepScanMaxFindings = ref('')

async function dispatchDeepScan() {
  if (!repoUid.value || deepScanning.value) return
  deepScanning.value = true
  try {
    const budget = Number.parseInt(deepScanMaxFindings.value, 10)
    const result = await docs.deepScan(repoUid.value, {
      custom_intent: deepScanIntent.value.trim() || undefined,
      max_findings: Number.isFinite(budget) && budget > 0 ? budget : undefined,
    })
    deepScanOpen.value = false
    deepScanIntent.value = ''
    deepScanMaxFindings.value = ''
    if (result.errors.length) {
      toast.error('Deep scan not dispatched', result.errors.join(' · '))
    } else {
      toast.success(result.summary, 'One long run — findings land as it works through the repo.')
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t start deep scan', msg)
  } finally {
    deepScanning.value = false
  }
}

// ── Scheduled audits — edits the repo's seeded "Audit stale code" config ────
// (the seeded audit-stale ScheduledAgent binding)
const scheduleOpen = ref(false)
const scheduleLoading = ref(false)
const savingSchedule = ref(false)
const auditScheduleBinding = ref<ScheduledAgentDTO | null>(null)

type ScheduleMode = 'manual' | 'cron'
const scheduleMode = ref<ScheduleMode>('manual')
const cronExpr = ref('')
const dial = ref<ComputeDial>('ask-before-run')
const schedulePagesPerTick = ref('3')

const SCHEDULE_OPTIONS = [
  { label: 'Off — audit only when I click', value: 'manual' },
  { label: 'On a schedule (cron)', value: 'cron' },
]

const DIAL_OPTIONS = [
  { label: 'Disabled — kill switch, never runs', value: 'disabled' },
  { label: 'Ask before run', value: 'ask-before-run' },
  { label: 'Auto-run on free (local) compute', value: 'auto-run-cheap' },
  { label: 'Auto-run on any provider', value: 'auto-run-any' },
]

const CRON_PRESETS = [
  { label: 'Nightly at 02:00', expr: '0 2 * * *' },
  { label: 'Every 6 hours', expr: '0 */6 * * *' },
  { label: 'Weekly (Mon 06:00)', expr: '0 6 * * 1' },
]

const scheduleSummary = computed(() => {
  const sa = auditScheduleBinding.value
  if (!sa || !sa.trigger.startsWith('cron:')) return null
  if (sa.compute_dial === 'disabled') return 'scheduled · disabled'
  return `cron ${sa.trigger.slice('cron:'.length)}`
})

async function openSchedule() {
  if (!repoUid.value) return
  scheduleOpen.value = true
  scheduleLoading.value = true
  try {
    const all = await scheduledAgents.fetchAll(repoUid.value)
    const sa = all.find((s) => s.agent_key === 'audit-stale') ?? null
    auditScheduleBinding.value = sa
    if (sa?.trigger.startsWith('cron:')) {
      scheduleMode.value = 'cron'
      cronExpr.value = sa.trigger.slice('cron:'.length)
    } else {
      scheduleMode.value = 'manual'
      cronExpr.value = ''
    }
    dial.value = sa?.compute_dial ?? 'ask-before-run'
    const rawLimit = sa?.target?.limit
    schedulePagesPerTick.value = String(
      typeof rawLimit === 'number' && rawLimit > 0 ? rawLimit : 3,
    )
  } catch (e) {
    toast.error('Couldn’t load audit schedule', e instanceof Error ? e.message : String(e))
    scheduleOpen.value = false
  } finally {
    scheduleLoading.value = false
  }
}

async function saveSchedule() {
  if (!repoUid.value || savingSchedule.value) return
  if (scheduleMode.value === 'cron' && !cronExpr.value.trim()) {
    toast.error('Cron expression required', 'Pick a preset or enter a 5-field crontab.')
    return
  }
  savingSchedule.value = true
  try {
    const limit = Number.parseInt(schedulePagesPerTick.value, 10)
    const payload = {
      trigger: scheduleMode.value === 'cron' ? `cron:${cronExpr.value.trim()}` : '',
      compute_dial: dial.value,
      target: { limit: Number.isFinite(limit) && limit > 0 ? limit : 3 },
    }
    if (!auditScheduleBinding.value) {
      // The seeded binding is created on repo registration; if it is missing
      // the backend seeding hasn't run — surface that instead of guessing.
      throw new Error('Seeded "Audit stale code" binding not found for this repository')
    }
    auditScheduleBinding.value = await scheduledAgents.update(
      auditScheduleBinding.value.uid,
      payload,
    )
    scheduleOpen.value = false
    toast.success(
      scheduleMode.value === 'cron' ? 'Scheduled audits on' : 'Scheduled audits off',
      scheduleMode.value === 'cron'
        ? `cron ${cronExpr.value.trim()} · up to ${payload.target.limit} stalest pages per tick`
        : undefined,
    )
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save schedule', msg)
  } finally {
    savingSchedule.value = false
  }
}

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
  try {
    const [, fresh, latest] = await Promise.all([
      docs.fetchAll({ repository_uid: repoUid.value }),
      docs.fetchFreshness(repoUid.value),
      analyses.latestForRepo(repoUid.value).catch(() => null),
    ])
    freshness.value = fresh
    latestAnalysis.value = latest
    // Best-effort: the header chip showing the current audit schedule.
    try {
      const all = await scheduledAgents.fetchAll(repoUid.value)
      auditScheduleBinding.value = all.find((s) => s.agent_key === 'audit-stale') ?? null
    } catch {
      auditScheduleBinding.value = null
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)
watch(repoUid, reload)

interface FreshnessRow {
  doc: DocDTO
  entry: ScopeFreshnessDTO | null
}

/** One row per doc page, joined with its freshness stamp (scope_uid == doc.uid). */
const rows = computed<FreshnessRow[]>(() => {
  const byScope = new Map(freshness.value.map((f) => [f.scope_uid, f]))
  return docs.list
    .map((doc) => ({ doc, entry: byScope.get(doc.uid) || null }))
    .sort((a, b) => {
      // Stale and never-checked first — that's the work queue.
      const rank = (r: FreshnessRow) => (r.doc.stale ? 0 : !r.entry ? 1 : r.entry.code_changed_since ? 2 : 3)
      if (rank(a) !== rank(b)) return rank(a) - rank(b)
      return a.doc.slug.localeCompare(b.doc.slug)
    })
})

const summary = computed(() => ({
  total: rows.value.length,
  stale: rows.value.filter((r) => r.doc.stale).length,
  never: rows.value.filter((r) => !r.entry).length,
  fresh: rows.value.filter((r) => !r.doc.stale && r.entry && !r.entry.code_changed_since).length,
}))

function daysAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const ms = Date.now() - new Date(iso).getTime()
  const days = Math.floor(ms / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return '1 day ago'
  return `${days} days ago`
}

function outcomeVariant(outcome: string): BadgeVariants['variant'] {
  if (outcome === 'clean') return 'success'
  if (outcome === 'findings') return 'info'
  if (outcome === 'failed') return 'destructive'
  return 'secondary'
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Health"
      subtitle="Has each documentation page been looked at since the code it watches last changed? One recency stamp per page, derived from doc runs."
    >
      <div class="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" :disabled="!repoUid" @click="openSchedule">
          <CalendarClock />
          Scheduled audits
          <Badge v-if="scheduleSummary" class="ml-1 px-1.5 text-[10px]">{{ scheduleSummary }}</Badge>
        </Button>
        <RouterLink
          v-if="latestAnalysis"
          :to="{ name: 'analysis-detail', params: { uid: latestAnalysis.uid } }"
          class="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors hover:bg-accent"
          title="Open the latest deep-scan report"
        >
          Latest report
          <Badge v-if="latestAnalysis.health_grade" class="px-1.5 text-[10px]">{{ latestAnalysis.health_grade }}</Badge>
        </RouterLink>
        <Button variant="outline" size="sm" :disabled="!repoUid" @click="deepScanOpen = true">
          <Radar /> Deep scan
        </Button>
        <Button size="sm" :disabled="!repoUid" @click="openAudit()">
          <Search /> Audit
        </Button>
      </div>
    </PageHeader>

    <Dialog v-model:open="deepScanOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Deep scan — whole repository</DialogTitle>
          <DialogDescription>
            One long run that plans its own scan, then works through the whole codebase area by area — correctness, security, tests, simplifications, performance — filing findings as it goes. Best on a deep-effort provider; only one runs per repo at a time.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-4">
          <div class="space-y-1.5">
            <Label for="deep-budget">Finding budget (optional)</Label>
            <Input id="deep-budget" v-model="deepScanMaxFindings" type="number" min="1" max="200" placeholder="no cap" />
            <p class="text-xs text-muted-foreground">
              Cap total findings for the whole scan, ranked by severity × confidence. Empty = find everything defensible.
            </p>
          </div>
          <div class="space-y-1.5">
            <Label for="deep-focus">Focus (optional)</Label>
            <Textarea
              id="deep-focus"
              v-model="deepScanIntent"
              :rows="2"
              placeholder="e.g. weight the scan toward security and the multi-tenancy boundaries"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" @click="deepScanOpen = false">Cancel</Button>
          <Button :loading="deepScanning" @click="dispatchDeepScan">
            <Radar /> Start deep scan
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="scheduleOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Scheduled audits</DialogTitle>
          <DialogDescription>
            Automatically audit the pages that most need a look — never-checked first, then longest-stale. One scoped run per page.
          </DialogDescription>
        </DialogHeader>
        <div v-if="scheduleLoading" class="space-y-3">
          <Skeleton class="h-9" />
          <Skeleton class="h-9" />
        </div>
        <div v-else class="space-y-4">
          <div class="space-y-1.5">
            <Label>Trigger</Label>
            <Select
              :model-value="scheduleMode"
              @update:model-value="scheduleMode = $event as ScheduleMode"
            >
              <SelectTrigger class="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="o in SCHEDULE_OPTIONS" :key="o.value" :value="o.value">
                  {{ o.label }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <template v-if="scheduleMode === 'cron'">
            <div class="space-y-1.5">
              <Label for="cron-expr">Crontab (5 fields, UTC)</Label>
              <Input id="cron-expr" v-model="cronExpr" placeholder="0 2 * * *" class="font-mono" />
              <div class="flex flex-wrap gap-1.5 pt-0.5">
                <Button
                  v-for="preset in CRON_PRESETS"
                  :key="preset.expr"
                  variant="outline"
                  size="sm"
                  @click="cronExpr = preset.expr"
                >
                  {{ preset.label }}
                </Button>
              </div>
            </div>
            <div class="grid gap-3 sm:grid-cols-2">
              <div class="space-y-1.5">
                <Label for="pages-per-tick">Pages per tick</Label>
                <Input id="pages-per-tick" v-model="schedulePagesPerTick" type="number" min="1" max="20" />
              </div>
              <div class="space-y-1.5">
                <Label>Compute</Label>
                <Select
                  :model-value="dial"
                  @update:model-value="dial = $event as ComputeDial"
                >
                  <SelectTrigger class="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="o in DIAL_OPTIONS" :key="o.value" :value="o.value">
                      {{ o.label }}
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p class="text-xs text-muted-foreground">
              When everything is fresh, a tick dispatches nothing. “Auto-run on free compute”
              makes the whole loop cost nothing on a local provider.
            </p>
          </template>
        </div>
        <DialogFooter>
          <Button variant="ghost" @click="scheduleOpen = false">Cancel</Button>
          <Button :loading="savingSchedule" :disabled="scheduleLoading" @click="saveSchedule">
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="auditOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{{ auditDocUid ? `Audit ${auditDocSlug}` : 'Audit repository' }}</DialogTitle>
          <DialogDescription>
            {{ auditDocUid
              ? "One run scoped to this page's watched paths."
              : 'Dispatch scoped audit runs — one per selected documentation page.' }}
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-4">
          <template v-if="!auditDocUid">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <p class="text-sm font-medium">Auto-select stale pages</p>
                <p class="text-xs text-muted-foreground">
                  Pick the pages that most need a look: never-checked first, then longest-stale.
                  Off = one whole-repository run with no page scoping.
                </p>
              </div>
              <Switch v-model="auditAutoSelect" class="mt-0.5 shrink-0" />
            </div>
            <div v-if="auditAutoSelect" class="space-y-1.5">
              <Label for="audit-limit">Pages per audit</Label>
              <Input id="audit-limit" v-model="auditLimit" type="number" min="1" max="20" placeholder="3" />
              <p class="text-xs text-muted-foreground">Each selected page gets its own scoped run.</p>
            </div>
          </template>
          <div class="space-y-1.5">
            <Label for="audit-budget">Finding budget per run</Label>
            <Input id="audit-budget" v-model="auditMaxFindings" type="number" min="1" max="50" placeholder="no cap" />
            <p class="text-xs text-muted-foreground">
              Cap findings per run, ranked by severity × confidence. Empty = find everything defensible.
            </p>
          </div>
          <div class="space-y-1.5">
            <Label for="audit-focus">Focus (optional)</Label>
            <Textarea
              id="audit-focus"
              v-model="auditIntent"
              :rows="2"
              placeholder="e.g. focus on security of the auth flows"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" @click="auditOpen = false">Cancel</Button>
          <Button :loading="auditing" @click="dispatchAudit">
            <Search /> Dispatch audit
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <template v-if="loading">
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Skeleton v-for="i in 4" :key="i" class="h-[72px]" />
      </div>
      <Skeleton class="h-64" />
    </template>

    <ErrorState v-else-if="error" title="Couldn't load freshness" :message="error">
      <Button variant="outline" size="sm" @click="reload">Retry</Button>
    </ErrorState>

    <template v-else>
      <section class="grid grid-cols-2 gap-3 text-sm lg:grid-cols-4">
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Pages</div>
            <div class="mt-1 text-xl font-semibold tabular-nums"><AnimatedNumber :value="summary.total" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Fresh</div>
            <div class="mt-1 text-xl font-semibold tabular-nums text-good"><AnimatedNumber :value="summary.fresh" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Stale</div>
            <div class="mt-1 text-xl font-semibold tabular-nums text-warn"><AnimatedNumber :value="summary.stale" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Never checked</div>
            <div class="mt-1 text-xl font-semibold tabular-nums text-muted-foreground"><AnimatedNumber :value="summary.never" /></div>
          </CardContent>
        </Card>
      </section>

      <Card v-if="rows.length">
        <CardContent class="p-0">
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead class="bg-muted text-xs uppercase text-muted-foreground">
                <tr>
                  <th class="px-4 py-2 text-left font-medium">Page</th>
                  <th class="px-4 py-2 text-left font-medium">Stale</th>
                  <th class="px-4 py-2 text-left font-medium">Last reviewed</th>
                  <th class="px-4 py-2 text-left font-medium">Last checked</th>
                  <th class="px-4 py-2 text-left font-medium">Outcome</th>
                  <th class="px-4 py-2 text-left font-medium">Code changed since</th>
                  <th class="px-4 py-2 text-right font-medium"></th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in rows"
                  :key="row.doc.uid"
                  class="border-t border-border transition-colors hover:bg-accent"
                >
                  <td class="max-w-[320px] px-4 py-2">
                    <RouterLink
                      :to="{ name: 'documentation', params: { repoSlug: repoSlug || '' }, query: { doc: row.doc.slug } }"
                      class="block truncate font-medium hover:underline"
                    >
                      {{ row.doc.title || row.doc.slug }}
                    </RouterLink>
                    <span class="block truncate font-mono text-[10px] text-muted-foreground">{{ row.doc.slug }}</span>
                  </td>
                  <td class="px-4 py-2">
                    <Badge
                      v-if="row.doc.stale"
                      variant="warn"
                      :title="row.doc.stale_paths.join('\n')"
                    >stale</Badge>
                    <span v-else class="text-xs text-muted-foreground">—</span>
                  </td>
                  <td class="whitespace-nowrap px-4 py-2 text-muted-foreground">
                    {{ daysAgo(row.doc.last_reviewed_at) }}
                  </td>
                  <td class="whitespace-nowrap px-4 py-2 text-muted-foreground">
                    {{ row.entry ? daysAgo(row.entry.last_checked) : '—' }}
                  </td>
                  <td class="px-4 py-2">
                    <Badge v-if="row.entry" :variant="outcomeVariant(row.entry.outcome)">
                      {{ row.entry.outcome }}
                    </Badge>
                    <span v-else class="text-xs text-muted-foreground">—</span>
                  </td>
                  <td class="px-4 py-2">
                    <Badge v-if="!row.entry" variant="outline">never checked</Badge>
                    <Badge v-else-if="row.entry.code_changed_since" variant="warn">code changed since</Badge>
                    <Badge v-else variant="success">fresh</Badge>
                  </td>
                  <td class="px-4 py-2 text-right">
                    <Button variant="ghost" size="sm" title="Audit this page's watched paths" @click="openAudit(row.doc)">
                      <Search /> Audit
                    </Button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <Card v-else>
        <CardContent>
          <EmptyState
            :icon="Activity"
            title="Nothing to report"
            description="No documentation pages yet — generate docs or create a page to start building coverage."
            class="border-0"
          />
        </CardContent>
      </Card>
    </template>
  </div>
</template>
