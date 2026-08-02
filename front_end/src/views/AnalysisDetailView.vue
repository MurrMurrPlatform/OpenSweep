<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { Activity, FileSearch, HelpCircle, Sparkles } from 'lucide-vue-next'
import { useAnalysisStore, type AnalysisDTO } from '@/stores/analysisStore'
import { useFindingStore } from '@/stores/findingStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
import { EmptyState } from '@/components/ui/empty-state'
import { MarkdownView } from '@/components/ui/markdown'
import type { FindingDTO } from '@/types/api'

const route = useRoute()
const router = useRouter()
const analyses = useAnalysisStore()
const findings = useFindingStore()
const repositories = useRepositoryStore()
const toast = useToast()

// Per-question answer drafts, keyed by question uid.
const drafts = reactive<Record<string, string>>({})
const savingQ = ref<string | null>(null)
const refining = ref(false)

const hasAnswered = computed(() => (item.value?.questions ?? []).some((q) => q.status === 'answered'))

async function answerQuestion(qid: string) {
  if (!item.value || !(drafts[qid] || '').trim()) return
  savingQ.value = qid
  try {
    item.value = await analyses.answerQuestion(item.value.uid, qid, drafts[qid].trim())
    drafts[qid] = ''
  } catch (e) {
    toast.error('Couldn’t save answer', e instanceof ApiError ? e.detail : String(e))
  } finally {
    savingQ.value = null
  }
}

async function dismissQuestion(qid: string) {
  if (!item.value) return
  savingQ.value = qid
  try {
    item.value = await analyses.dismissQuestion(item.value.uid, qid)
  } catch (e) {
    toast.error('Couldn’t dismiss question', e instanceof ApiError ? e.detail : String(e))
  } finally {
    savingQ.value = null
  }
}

async function refine() {
  if (!item.value || refining.value) return
  refining.value = true
  try {
    const res = await analyses.refineWithAnswers(item.value.uid)
    toast.success('Refined scan dispatched', 'A new analysis will supersede this one.')
    router.push({ name: 'analysis-detail', params: { uid: res.analysis_uid } })
  } catch (e) {
    toast.error('Couldn’t refine', e instanceof ApiError ? e.detail : String(e))
  } finally {
    refining.value = false
  }
}

const item = ref<AnalysisDTO | null>(null)
const runFindings = ref<FindingDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

// Ordered narrative sections — known keys first, then any extra agent-authored
// keys, so the report reads top-to-bottom regardless of authoring order.
const SECTION_ORDER = [
  ['executive_summary', 'Executive summary'],
  ['repository_map', 'Repository map'],
  ['security_summary', 'Security'],
  ['performance_summary', 'Performance'],
  ['data_integrity_summary', 'Data integrity'],
  ['dependency_report', 'Dependencies'],
  ['test_gap_report', 'Test gaps'],
  ['observability_summary', 'Observability'],
  ['implementation_plan', 'Implementation plan'],
  ['top_changes', 'Top recommended changes'],
] as const

const orderedSections = computed<{ key: string; label: string; body: string }[]>(() => {
  const secs = item.value?.sections ?? {}
  const known = SECTION_ORDER.map(([k]) => k)
  const out: { key: string; label: string; body: string }[] = []
  for (const [key, label] of SECTION_ORDER) {
    if (secs[key]?.trim()) out.push({ key, label, body: secs[key] })
  }
  for (const [key, body] of Object.entries(secs)) {
    if (!known.includes(key as (typeof known)[number]) && body?.trim()) {
      out.push({ key, label: humanize(key), body })
    }
  }
  return out
})

const repoSlug = computed(() => {
  const uid = item.value?.repository_uid
  return uid ? repositories.find(uid)?.slug ?? '' : ''
})

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'] as const
const findingsBySeverity = computed(() => {
  const groups: Record<string, FindingDTO[]> = {}
  for (const f of runFindings.value) (groups[f.severity] ??= []).push(f)
  return SEVERITY_ORDER.filter((s) => groups[s]?.length).map((s) => ({ severity: s, items: groups[s] }))
})

function humanize(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function statusVariant(s: string): BadgeVariants['variant'] {
  if (s === 'complete') return 'success'
  if (s === 'in_progress') return 'info'
  // `incomplete` = the scan died before authoring a verdict, so the report is
  // partial. It fell through to the neutral 'secondary' badge, which reads as
  // "fine" for the one status that most needs to read as "do not trust this".
  if (s === 'incomplete') return 'destructive'
  if (s === 'superseded' || s === 'archived') return 'warn'
  return 'secondary'
}

function severityVariant(s: string): BadgeVariants['variant'] {
  if (s === 'critical' || s === 'high') return 'destructive'
  if (s === 'medium') return 'warn'
  if (s === 'low') return 'info'
  return 'secondary'
}

function coverageVariant(s: string): BadgeVariants['variant'] {
  if (s === 'examined') return 'success'
  if (s === 'partial') return 'warn'
  return 'secondary'
}

const GRADE_PCT: Record<string, number> = { A: 95, B: 80, C: 65, D: 50, F: 25 }

/** Fill % for a scorecard bar — prefer the numeric score, fall back to grade. */
function scorePct(row: { score: number | null; max: number; grade: string }): number {
  if (row.score != null && row.max) return Math.max(0, Math.min(100, (row.score / row.max) * 100))
  return GRADE_PCT[(row.grade || '').toUpperCase()] ?? 0
}

function scoreBarClass(row: { score: number | null; max: number; grade: string }): string {
  const pct = scorePct(row)
  if (pct >= 80) return 'bg-green-500'
  if (pct >= 60) return 'bg-yellow-500'
  if (pct >= 40) return 'bg-orange-500'
  return 'bg-red-500'
}

async function load() {
  const uid = String(route.params.uid || '')
  if (!uid) return
  loading.value = true
  error.value = null
  try {
    if (!repositories.loaded) await repositories.fetchAll().catch(() => {})
    const a = await analyses.get(uid)
    item.value = a
    for (const q of a.questions) if (drafts[q.uid] == null) drafts[q.uid] = ''
    runFindings.value = await findings
      .fetchAll({ source_run_uid: a.source_run_uid })
      .catch(() => [] as FindingDTO[])
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => route.params.uid, load)
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading">
      <Skeleton class="h-8 w-64" />
      <Skeleton class="h-48" />
      <Skeleton class="h-48" />
    </template>

    <ErrorState v-else-if="error" title="Couldn't load analysis" :message="error" />

    <template v-else-if="item">
      <PageHeader :title="item.title || 'Deep scan'" :subtitle="`Run ${item.source_run_uid.slice(0, 8)} · ${item.executor || 'agent'}`">
        <div class="flex flex-wrap items-center gap-2">
          <Badge v-if="item.health_grade" variant="secondary">Health {{ item.health_grade }}</Badge>
          <Badge :variant="statusVariant(item.status)">{{ item.status.replace('_', ' ') }}</Badge>
          <Badge v-if="item.confidence" variant="outline">confidence: {{ item.confidence }}</Badge>
        </div>
      </PageHeader>

      <p v-if="item.status === 'superseded'" class="text-xs text-muted-foreground">
        A newer analysis has superseded this one.
      </p>

      <!-- Scorecard -->
      <Card v-if="item.scorecard.length">
        <CardHeader class="p-4"><CardTitle class="text-base">Scorecard</CardTitle></CardHeader>
        <CardContent class="space-y-3 p-4 pt-0">
          <div v-for="row in item.scorecard" :key="row.dimension">
            <div class="flex items-baseline justify-between gap-3 text-sm">
              <span class="font-medium capitalize">{{ row.dimension.replace(/_/g, ' ') }}</span>
              <span class="shrink-0 text-xs text-muted-foreground">
                <template v-if="row.grade">{{ row.grade }}</template>
                <template v-if="row.score != null">{{ row.grade ? ' · ' : '' }}{{ row.score }}/{{ row.max || 100 }}</template>
              </span>
            </div>
            <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                class="h-full rounded-full transition-all"
                :class="scoreBarClass(row)"
                :style="{ width: scorePct(row) + '%' }"
              />
            </div>
            <p v-if="row.rationale" class="mt-1 text-xs text-muted-foreground">{{ row.rationale }}</p>
          </div>
        </CardContent>
      </Card>

      <!-- Narrative report -->
      <Card v-for="sec in orderedSections" :key="sec.key">
        <CardHeader class="p-4"><CardTitle class="text-base">{{ sec.label }}</CardTitle></CardHeader>
        <CardContent class="p-4 pt-0">
          <MarkdownView :model-value="sec.body" preview-only />
        </CardContent>
      </Card>

      <!-- Findings (joined by source_run_uid) -->
      <Card>
        <CardHeader class="p-4"><CardTitle class="text-base">Findings ({{ item.finding_count }})</CardTitle></CardHeader>
        <CardContent class="p-0">
          <div v-if="!runFindings.length" class="p-4">
            <EmptyState :icon="FileSearch" title="No findings" description="This scan filed no findings." class="border-0" />
          </div>
          <div v-else class="stagger-children">
            <div v-for="group in findingsBySeverity" :key="group.severity">
              <div class="bg-muted px-4 py-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {{ group.severity }} ({{ group.items.length }})
              </div>
              <RouterLink
                v-for="f in group.items"
                :key="f.uid"
                :to="{ name: 'finding-detail', params: { uid: f.uid } }"
                class="flex items-center gap-3 border-t px-4 py-2 text-sm hover:bg-accent"
              >
                <Badge :variant="severityVariant(f.severity)">{{ f.kind }}</Badge>
                <span class="min-w-0 flex-1 truncate">{{ f.title }}</span>
                <span v-if="f.affected_paths?.length" class="truncate font-mono text-[10px] text-muted-foreground">
                  {{ f.affected_paths[0] }}
                </span>
              </RouterLink>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Validation baseline -->
      <Card v-if="item.validation_baseline.length">
        <CardHeader class="p-4"><CardTitle class="text-base">Validation baseline</CardTitle></CardHeader>
        <CardContent class="p-0">
          <div class="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Check</TableHead>
                  <TableHead>Command</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>Details</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow v-for="(row, i) in item.validation_baseline" :key="i">
                  <TableCell class="font-medium">{{ row.check }}</TableCell>
                  <TableCell class="font-mono text-[11px] text-muted-foreground">{{ row.command }}</TableCell>
                  <TableCell>{{ row.result }}</TableCell>
                  <TableCell class="text-muted-foreground">{{ row.details }}</TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <!-- Coverage checklist -->
      <Card v-if="item.coverage.length">
        <CardHeader class="p-4"><CardTitle class="text-base">Coverage</CardTitle></CardHeader>
        <CardContent class="p-0">
          <div
            v-for="(row, i) in item.coverage"
            :key="i"
            class="flex items-center gap-3 border-t px-4 py-2 text-sm first:border-t-0"
          >
            <Badge :variant="coverageVariant(row.status)">{{ row.status }}</Badge>
            <span class="font-medium">{{ row.area }}</span>
            <span v-if="row.note" class="text-muted-foreground">— {{ row.note }}</span>
          </div>
        </CardContent>
      </Card>

      <!-- Strengths -->
      <Card v-if="item.strengths.length">
        <CardHeader class="p-4"><CardTitle class="text-base">Strengths</CardTitle></CardHeader>
        <CardContent class="space-y-2 p-4 pt-0">
          <div v-for="(row, i) in item.strengths" :key="i">
            <p class="text-sm font-medium">{{ row.title }}</p>
            <p v-if="row.detail" class="text-sm text-muted-foreground">{{ row.detail }}</p>
          </div>
        </CardContent>
      </Card>

      <!-- Unresolved questions — answer inline, then refine with answers -->
      <Card v-if="item.questions.length">
        <CardHeader class="flex-row items-center justify-between gap-3 space-y-0 p-4">
          <CardTitle class="text-base">Unresolved questions ({{ item.open_question_count }} open)</CardTitle>
          <Button
            v-if="item.status !== 'superseded'"
            size="sm"
            :loading="refining"
            :disabled="!hasAnswered"
            :title="hasAnswered ? 'Re-scan, incorporating your answers' : 'Answer at least one question first'"
            @click="refine"
          >
            <Sparkles /> Refine with answers
          </Button>
        </CardHeader>
        <CardContent class="space-y-3 p-4 pt-0">
          <div v-for="q in item.questions" :key="q.uid" class="rounded-md border p-3">
            <div class="flex items-start gap-2">
              <HelpCircle class="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
              <div class="min-w-0 flex-1">
                <p class="text-sm font-medium">{{ q.question }}</p>
                <p v-if="q.why_it_matters" class="mt-0.5 text-xs text-muted-foreground">{{ q.why_it_matters }}</p>

                <div v-if="q.status === 'answered'" class="mt-1.5 rounded bg-muted p-2 text-sm">
                  <span class="text-muted-foreground">Answer:</span> {{ q.answer }}
                </div>

                <div v-else-if="q.status === 'open'" class="mt-2 space-y-2">
                  <Textarea v-model="drafts[q.uid]" :rows="2" placeholder="Answer this so the next scan can use it…" />
                  <div class="flex gap-2">
                    <Button
                      size="sm"
                      :loading="savingQ === q.uid"
                      :disabled="!(drafts[q.uid] || '').trim()"
                      @click="answerQuestion(q.uid)"
                    >
                      Save answer
                    </Button>
                    <Button variant="ghost" size="sm" :disabled="savingQ === q.uid" @click="dismissQuestion(q.uid)">
                      Dismiss
                    </Button>
                  </div>
                </div>
              </div>
              <Badge :variant="q.status === 'answered' ? 'success' : q.status === 'dismissed' ? 'secondary' : 'info'">
                {{ q.status }}
              </Badge>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Limitations -->
      <Card v-if="item.limitations">
        <CardHeader class="p-4"><CardTitle class="text-base">Analysis limitations</CardTitle></CardHeader>
        <CardContent class="p-4 pt-0"><MarkdownView :model-value="item.limitations" preview-only /></CardContent>
      </Card>
    </template>

    <EmptyState v-else :icon="Activity" title="Analysis not found" description="This analysis may have been removed." />
  </div>
</template>
