<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Circle,
  Code2,
  FileSearch,
  Info,
  Lightbulb,
  Loader2,
  MessagesSquare,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Ticket,
  Wand2,
  XCircle,
} from 'lucide-vue-next'
import { useFindingStore } from '@/stores/findingStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { useDiscussions } from '@/composables/useDiscussions'
import { useDiscussInRun } from '@/composables/useDiscussInRun'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
import { MarkdownView } from '@/components/ui/markdown'
import CodeSnippetViewer from '@/components/code/CodeSnippetViewer.vue'
import CommentThread from '@/components/comments/CommentThread.vue'
import DiscussionChip from '@/components/runs/DiscussionChip.vue'
import TicketDialog from '@/components/tickets/TicketDialog.vue'
import FindingEditDialog from '@/components/findings/FindingEditDialog.vue'
import TrustSignalList from '@/components/findings/TrustSignalList.vue'
import {
  corroborationCount,
  severityVariant,
  statusVariant,
  trustPercent,
  trustSignals,
  trustTier,
  type VerificationOutcome,
} from '@/components/findings/findingMeta'
import { extractCodeIdentifiersFrom } from '@/lib/codeHints'
import { isLiveRunStatus, runStatusLabel, runStatusVariant } from '@/lib/runStatus'
import type {
  FindingDTO,
  RunDTO,
  TicketDTO,
} from '@/types/api'

const route = useRoute()
const router = useRouter()
const findings = useFindingStore()
const repositories = useRepositoryStore()
const toast = useToast()

const item = ref<FindingDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const transitioning = ref(false)

// Open chat runs linked to this finding — a non-blocking discussion chip.
const { discussions } = useDiscussions(() =>
  item.value ? { linked_finding_uid: item.value.uid } : null,
)
const { discussing, discuss: discussInRun } = useDiscussInRun(() => {
  if (!item.value) return null
  const title = (item.value.title || '').trim()
  return {
    repository_uid: item.value.repository_uid,
    title: `Finding: ${title ? title.slice(0, 120) : item.value.uid.slice(0, 8)}`,
    linked_finding_uid: item.value.uid,
  }
})
const verifying = ref(false)
const refining = ref(false)
const verifications = ref<RunDTO[]>([])
const verificationsLoading = ref(false)
const promoteOpen = ref(false)
const editOpen = ref(false)
let pollTimer: number | undefined

const searchHints = computed(() => {
  if (!item.value) return []
  return extractCodeIdentifiersFrom(
    item.value.title,
    item.value.description,
    item.value.root_cause,
    item.value.why_it_matters,
    item.value.suggested_fix,
    item.value.subtype,
  )
})

interface AffectedSlice {
  raw: string
  path: string
  startLine?: number
  endLine?: number
  highlightLines: number[]
}

function parseAffectedPath(raw: string): AffectedSlice {
  const fallback: AffectedSlice = { raw, path: raw, highlightLines: [] }
  // Accept `path:line`, `path:line-end`, `path:line:col`, `path#L42`, `path#L42-L60`
  const hashMatch = raw.match(/^(.+?)#L(\d+)(?:-L(\d+))?$/)
  if (hashMatch) {
    const start = parseInt(hashMatch[2], 10)
    const end = hashMatch[3] ? parseInt(hashMatch[3], 10) : undefined
    return buildSlice(raw, hashMatch[1], start, end)
  }
  const colonMatch = raw.match(/^(.+?):(\d+)(?:[-:](\d+))?$/)
  if (colonMatch) {
    const start = parseInt(colonMatch[2], 10)
    const maybeEnd = colonMatch[3] ? parseInt(colonMatch[3], 10) : undefined
    // If the third group looks like a column (small + close), treat as single line.
    const end = maybeEnd && maybeEnd > start ? maybeEnd : undefined
    return buildSlice(raw, colonMatch[1], start, end)
  }
  return fallback
}

function buildSlice(raw: string, path: string, start: number, end?: number): AffectedSlice {
  const safeStart = Number.isFinite(start) && start > 0 ? start : undefined
  const safeEnd = Number.isFinite(end || NaN) && (end as number) > 0 ? end : undefined
  const highlightLines = safeStart ? [safeStart] : []
  return {
    raw,
    path,
    startLine: safeStart ? Math.max(1, safeStart - 6) : undefined,
    endLine: safeEnd ? safeEnd + 6 : safeStart ? safeStart + 18 : undefined,
    highlightLines,
  }
}

const affectedSlices = computed<AffectedSlice[]>(() => {
  if (!item.value?.affected_paths?.length) return []
  return item.value.affected_paths.map(parseAffectedPath)
})

/** Legacy run-status tones (from lib/runStatus) → shadcn Badge variants. */
function toneToBadge(tone: 'success' | 'danger' | 'warn' | 'info' | 'default'): BadgeVariants['variant'] {
  if (tone === 'danger') return 'destructive'
  if (tone === 'default') return 'secondary'
  return tone
}

const kindIcon = computed(() => {
  if (!item.value) return Info
  switch (item.value.kind) {
    case 'defect':
      return AlertCircle
    case 'gap':
      return AlertTriangle
    case 'improvement':
      return Sparkles
    case 'proposal':
      return ArrowRight
    case 'feature-idea':
      return Lightbulb
    case 'observation':
    default:
      return Info
  }
})

const statusTone = computed<BadgeVariants['variant']>(() =>
  item.value ? statusVariant(item.value.status) : 'secondary',
)

onMounted(async () => {
  await load()
  if (!repositories.loaded) {
    try {
      await repositories.fetchAll()
    } catch {
      /* the promote dialog degrades to an empty repo select */
    }
  }
})

onBeforeUnmount(() => {
  if (pollTimer) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
})

async function load() {
  loading.value = true
  error.value = null
  try {
    item.value = await findings.get(String(route.params.uid))
    await reloadVerifications()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function reloadVerifications() {
  if (!item.value) return
  verificationsLoading.value = true
  try {
    verifications.value = await findings.listVerifications(item.value.uid)
    schedulePollIfNeeded()
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t load verifications', msg)
  } finally {
    verificationsLoading.value = false
  }
}

function schedulePollIfNeeded() {
  const live = verifications.value.some((r) => isLiveRunStatus(r.status))
  if (live && !pollTimer) {
    pollTimer = window.setInterval(pollVerifications, 2500)
  } else if (!live && pollTimer) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

async function pollVerifications() {
  if (!item.value) return
  try {
    verifications.value = await findings.listVerifications(item.value.uid)
    schedulePollIfNeeded()
  } catch {
    /* ignore */
  }
}

async function transition(action: 'dismiss' | 'acknowledge' | 'wontFix' | 'markFixed') {
  if (!item.value || transitioning.value) return
  const map = {
    dismiss: () => findings.dismiss(item.value!.uid),
    acknowledge: () => findings.acknowledge(item.value!.uid),
    wontFix: () => findings.wontFix(item.value!.uid),
    markFixed: () => findings.markFixed(item.value!.uid),
  }
  transitioning.value = true
  try {
    item.value = await map[action]()
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t update finding', msg)
  } finally {
    transitioning.value = false
  }
}

/** Promote flow: TicketDialog created the ticket — jump to it. */
function onPromoted(ticket: TicketDTO) {
  void router.push({ name: 'ticket-detail', params: { uid: ticket.uid } })
}

/** Edit flow: FindingEditDialog PATCHed the finding — refresh the view. */
function onEdited(finding: FindingDTO) {
  item.value = finding
}

async function launchVerification() {
  if (!item.value || verifying.value) return
  verifying.value = true
  try {
    const run = await findings.launchVerification(item.value.uid)
    verifications.value = [run, ...verifications.value]
    toast.success('Verification launched', `Run ${run.uid.slice(0, 8)} queued`)
    schedulePollIfNeeded()
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t launch verification', msg)
  } finally {
    verifying.value = false
  }
}

async function launchRefine() {
  if (!item.value || refining.value) return
  refining.value = true
  try {
    const run = await findings.launchRefine(item.value.uid)
    toast.success('Refine launched', `Run ${run.uid.slice(0, 8)} queued`, {
      label: 'View run',
      to: { name: 'run-detail', params: { uid: run.uid } },
    })
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t launch refine', msg)
  } finally {
    refining.value = false
  }
}

const verdictPattern = /verdict\s*[:\-]\s*(resolved-properly|partially-resolved|not-resolved|cannot-determine)/i

type VerdictKey = 'resolved-properly' | 'partially-resolved' | 'not-resolved' | 'cannot-determine'

interface VerdictInfo {
  key: VerdictKey
  label: string
  variant: BadgeVariants['variant']
  icon: typeof CheckCircle2
}

/** All text the agent left via complete_run — the mandated verdict line lands
 *  in summary.text, but scan the structured sections too. */
function summaryHaystack(run: RunDTO): string {
  const s = run.summary || {}
  return [
    s.text || '',
    ...(s.did || []),
    ...(s.succeeded || []),
    ...(s.failed || []),
    ...(s.skipped || []),
    ...(s.next_steps || []),
  ].join('\n')
}

function verdictFor(run: RunDTO): VerdictInfo | null {
  const match = summaryHaystack(run).match(verdictPattern)
  if (!match) return null
  const key = match[1].toLowerCase() as VerdictKey
  if (key === 'resolved-properly') return { key, label: 'Resolved properly', variant: 'success', icon: CheckCircle2 }
  if (key === 'partially-resolved') return { key, label: 'Partially resolved', variant: 'warn', icon: AlertTriangle }
  if (key === 'not-resolved') return { key, label: 'Not resolved', variant: 'destructive', icon: XCircle }
  return { key: 'cannot-determine', label: 'Cannot determine', variant: 'secondary', icon: Info }
}

function summaryFor(run: RunDTO): string {
  const s = run.summary || {}
  if (s.text?.trim()) return s.text.trim()
  const sections = [
    ['Did', s.did],
    ['Succeeded', s.succeeded],
    ['Failed', s.failed],
  ] as const
  const lines = sections
    .filter(([, items]) => items?.length)
    .map(([label, items]) => `${label}: ${items!.join('; ')}`)
  if (lines.length) return lines.join('\n')
  return run.error || ''
}

function runStatusBadge(run: RunDTO): { label: string; variant: BadgeVariants['variant'] } {
  // awaiting_input / ended both mean the verification turn finished OK.
  return { label: runStatusLabel(run), variant: toneToBadge(runStatusVariant(run.status, run)) }
}

const liveVerification = computed(() =>
  verifications.value.find((r) => isLiveRunStatus(r.status)) || null,
)

interface EnrichedVerification {
  run: RunDTO
  status: { label: string; variant: BadgeVariants['variant'] }
  verdict: VerdictInfo | null
  summary: string
}

const enrichedVerifications = computed<EnrichedVerification[]>(() =>
  verifications.value.map((run) => ({
    run,
    status: runStatusBadge(run),
    verdict: verdictFor(run),
    summary: summaryFor(run),
  })),
)

/** A verification run's verdict answers "was it fixed"; read here for what it
 *  implies about whether the finding was REAL in the first place. A skeptic
 *  that re-read the code and still saw the problem is independent evidence. */
const VERDICT_TO_TRUST: Record<VerdictKey, VerificationOutcome> = {
  'not-resolved': 'still-present',
  'partially-resolved': 'partly-present',
  'resolved-properly': 'resolved',
  'cannot-determine': 'inconclusive',
}

const verificationOutcome = computed<VerificationOutcome>(() => {
  if (liveVerification.value) return 'running'
  // Newest first — launchVerification prepends, and the API lists in that order.
  const latest = enrichedVerifications.value.find((e) => e.verdict)
  return latest?.verdict ? VERDICT_TO_TRUST[latest.verdict.key] : 'none'
})

const trustSignalRows = computed(() =>
  item.value ? trustSignals(item.value, verificationOutcome.value) : [],
)

watch(
  () => route.params.uid,
  async (uid) => {
    if (uid) await load()
  },
)
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !item">
      <Skeleton class="h-12 w-2/3" />
      <div class="grid items-start gap-6 lg:grid-cols-[1fr_320px]">
        <div class="space-y-4">
          <Skeleton class="h-32" />
          <Skeleton class="h-32" />
        </div>
        <div class="space-y-4">
          <Skeleton class="h-40" />
          <Skeleton class="h-48" />
        </div>
      </div>
    </template>

    <ErrorState v-else-if="error && !item" title="Couldn't load finding" :message="error">
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <template v-else-if="item">
      <PageHeader :title="item.title">
        <template #breadcrumb>
          <div class="mb-1 flex flex-wrap items-center gap-2">
            <Badge :variant="severityVariant(item.severity)">
              <component :is="kindIcon" class="h-3 w-3" />
              {{ item.severity }} · {{ item.kind }}
            </Badge>
            <Badge v-for="t in item.tags || []" :key="t" variant="outline">{{ t }}</Badge>
            <Badge variant="outline">size: {{ item.size }}</Badge>
            <Badge v-if="item.subtype" variant="outline">{{ item.subtype }}</Badge>
            <Badge :variant="statusTone">status: {{ item.status }}</Badge>
            <Badge :variant="trustTier(item.trust).variant">
              <ShieldCheck class="h-3 w-3" />
              trust {{ trustPercent(item.trust) }}%
            </Badge>
            <!-- Where this finding went. Set with status 'ticketed'; cleared
                 server-side if that ticket is deleted, so it never dangles. -->
            <RouterLink
              v-if="item.ticket_uid"
              :to="{ name: 'ticket-detail', params: { uid: item.ticket_uid } }"
              class="inline-flex items-center gap-1.5 text-xs text-primary hover:underline"
            >
              <Ticket class="h-3.5 w-3.5" />
              Became a ticket →
            </RouterLink>
          </div>
        </template>

        <div class="flex flex-wrap items-center gap-2">
          <DiscussionChip v-for="chat in discussions" :key="chat.uid" :run="chat" />
          <Button variant="outline" size="sm" @click="editOpen = true">
            <Pencil /> Edit
          </Button>
          <Button v-if="!item.ticket_uid" variant="outline" size="sm" @click="promoteOpen = true">
            <Ticket /> Promote to ticket
          </Button>
          <Button variant="outline" size="sm" :loading="discussing" @click="discussInRun">
            <MessagesSquare /> Discuss
          </Button>
          <Button variant="outline" size="sm" :loading="refining" @click="launchRefine">
            <Wand2 /> Refine
          </Button>
          <Button
            size="sm"
            :loading="verifying"
            :disabled="!!liveVerification"
            @click="launchVerification"
          >
            <ShieldCheck />
            {{ liveVerification ? 'Verification running…' : 'Launch verification run' }}
          </Button>
          <Button variant="outline" size="icon-sm" title="Reload verifications" @click="reloadVerifications" :disabled="verificationsLoading">
            <RefreshCw :class="{ 'animate-spin': verificationsLoading }" />
          </Button>
        </div>
      </PageHeader>

      <div class="grid items-start gap-6 lg:grid-cols-[1fr_320px]">
        <!-- ── Main column ─────────────────────────────────────────────── -->
        <div class="min-w-0 space-y-4">
          <Card v-if="item.description">
            <CardHeader class="p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <FileSearch class="h-4 w-4 text-muted-foreground" />
                Analysis
              </CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <MarkdownView :model-value="item.description" preview-only />
            </CardContent>
          </Card>

          <Card v-if="item.root_cause">
            <CardHeader class="p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <AlertCircle class="h-4 w-4 text-muted-foreground" />
                Root cause
              </CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <MarkdownView :model-value="item.root_cause" preview-only />
            </CardContent>
          </Card>

          <Card v-if="item.why_it_matters">
            <CardHeader class="p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <Info class="h-4 w-4 text-muted-foreground" />
                Why it matters
              </CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <MarkdownView :model-value="item.why_it_matters" preview-only />
            </CardContent>
          </Card>

          <Card v-if="item.suggested_fix">
            <CardHeader class="p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <Sparkles class="h-4 w-4 text-muted-foreground" />
                Suggested fix
              </CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <MarkdownView :model-value="item.suggested_fix" preview-only />
            </CardContent>
          </Card>

          <Card v-if="affectedSlices.length">
            <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0 p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <Code2 class="h-4 w-4 text-muted-foreground" />
                Affected code
                <span class="text-xs font-normal text-muted-foreground">
                  · {{ affectedSlices.length }} location{{ affectedSlices.length === 1 ? '' : 's' }}
                </span>
              </CardTitle>
              <span v-if="searchHints.length" class="text-xs text-muted-foreground">
                hint match: {{ searchHints.slice(0, 4).join(', ') }}{{ searchHints.length > 4 ? '…' : '' }}
              </span>
            </CardHeader>
            <CardContent class="space-y-3 p-4 pt-0">
              <CodeSnippetViewer
                v-for="slice in affectedSlices"
                :key="slice.raw"
                :repository-uid="item.repository_uid"
                :path="slice.path"
                :start-line="slice.startLine"
                :end-line="slice.endLine"
                :highlight-lines="slice.highlightLines"
                :search-hints="searchHints"
              />
            </CardContent>
          </Card>

          <!-- ── Verification panel ────────────────────────────────────── -->
          <Card>
            <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0 p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <ShieldCheck class="h-4 w-4 text-muted-foreground" />
                Verification runs
                <span class="text-xs font-normal text-muted-foreground">· {{ verifications.length }}</span>
              </CardTitle>
              <span v-if="liveVerification" class="inline-flex items-center gap-1 text-xs text-warn">
                <Loader2 class="h-3 w-3 animate-spin" /> {{ liveVerification.status }}
              </span>
            </CardHeader>
            <CardContent class="p-0">
              <div v-if="!verifications.length" class="flex items-start gap-3 p-5 text-sm text-muted-foreground">
                <FileSearch class="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  No verification has been launched yet. A verification run reads the affected paths
                  with the active LLM and produces a detailed pass/fail analysis you can use to
                  confidently mark this finding fixed.
                </div>
              </div>
              <ul v-else class="divide-y">
                <li
                  v-for="entry in enrichedVerifications"
                  :key="entry.run.uid"
                  class="grid grid-cols-[1fr_auto] items-start gap-3 p-4"
                >
                  <div class="min-w-0 space-y-1.5">
                    <div class="flex flex-wrap items-center gap-2 text-xs">
                      <RouterLink
                        :to="{ name: 'run-detail', params: { uid: entry.run.uid } }"
                        class="font-mono text-primary hover:underline"
                      >
                        {{ entry.run.uid.slice(0, 12) }}
                      </RouterLink>
                      <Badge :variant="entry.status.variant">
                        {{ entry.status.label }}
                      </Badge>
                      <Badge v-if="entry.verdict" :variant="entry.verdict.variant">
                        <component :is="entry.verdict.icon" class="h-3 w-3" />
                        {{ entry.verdict.label }}
                      </Badge>
                      <span class="text-muted-foreground">{{ entry.run.executor }}</span>
                      <span v-if="entry.run.duration_ms" class="text-muted-foreground">
                        · {{ (entry.run.duration_ms / 1000).toFixed(1) }}s
                      </span>
                      <span v-if="entry.run.started_at" class="text-muted-foreground">· {{ entry.run.started_at }}</span>
                    </div>
                    <p v-if="entry.summary" class="line-clamp-4 whitespace-pre-line text-sm text-muted-foreground">
                      {{ entry.summary }}
                    </p>
                    <p
                      v-else-if="isLiveRunStatus(entry.run.status)"
                      class="text-xs text-muted-foreground"
                    >
                      Agent is investigating… results will appear here when the run completes.
                    </p>
                    <p v-else-if="entry.run.error" class="text-xs text-destructive">{{ entry.run.error }}</p>
                  </div>
                  <RouterLink
                    :to="{ name: 'run-detail', params: { uid: entry.run.uid } }"
                    class="inline-flex items-center gap-1 whitespace-nowrap text-xs text-primary hover:underline"
                  >
                    Open run <ArrowRight class="h-3 w-3" />
                  </RouterLink>
                </li>
              </ul>
            </CardContent>
          </Card>

          <Card v-if="Object.keys(item.evidence).length">
            <CardHeader class="p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <Circle class="h-4 w-4 text-muted-foreground" />
                Evidence
              </CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <pre class="max-h-96 overflow-auto rounded-sm bg-muted p-3 text-xs">{{ JSON.stringify(item.evidence, null, 2) }}</pre>
            </CardContent>
          </Card>

        </div>

        <!-- ── Sidebar ────────────────────────────────────────────────── -->
        <div class="space-y-4">
          <!-- Why the score is what it is. A bare 0..1 float is not a triage
               input; the signals behind it are. -->
          <Card>
            <CardHeader class="flex-row items-center justify-between gap-2 space-y-0 p-4">
              <CardTitle class="flex items-center gap-2 text-base">
                <ShieldCheck class="h-4 w-4 text-muted-foreground" />
                Trust
              </CardTitle>
              <Badge :variant="trustTier(item.trust).variant">
                {{ trustTier(item.trust).label }} · {{ trustPercent(item.trust) }}%
              </Badge>
            </CardHeader>
            <CardContent class="space-y-3 p-4 pt-0">
              <p class="text-xs text-muted-foreground">
                Evidence the platform can check independently of what the model asserted.
              </p>
              <TrustSignalList :signals="trustSignalRows" />
              <p v-if="verificationOutcome === 'none'" class="text-xs text-muted-foreground">
                No verification run yet — launching one adds a second, skeptical opinion.
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader class="p-4">
              <CardTitle class="text-base">Triage</CardTitle>
            </CardHeader>
            <CardContent class="space-y-2 p-4 pt-0">
              <Button variant="outline" size="sm" class="w-full justify-start" :disabled="transitioning" @click="transition('acknowledge')">
                <CheckCircle2 /> Acknowledge
              </Button>
              <Button variant="outline" size="sm" class="w-full justify-start" :disabled="transitioning" @click="transition('markFixed')">
                <CheckCircle2 class="text-good" /> Mark fixed
              </Button>
              <Button variant="outline" size="sm" class="w-full justify-start" :disabled="transitioning" @click="transition('wontFix')">
                <XCircle /> Won't fix
              </Button>
              <Button variant="destructive" size="sm" class="w-full justify-start" :disabled="transitioning" @click="transition('dismiss')">
                <XCircle /> Dismiss
              </Button>
            </CardContent>
          </Card>

          <!-- Discussion -->
          <CommentThread subject-type="finding" :subject-uid="item.uid" :repository-uid="item.repository_uid" title="Discussion" />

          <Card>
            <CardHeader class="p-4">
              <CardTitle class="text-base">Provenance</CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <dl class="grid grid-cols-[88px_1fr] gap-x-2 gap-y-1.5 text-xs">
                <dt class="text-muted-foreground">executor</dt>
                <dd class="font-mono">{{ item.executor }}</dd>
                <dt class="text-muted-foreground">source</dt>
                <dd class="font-mono">{{ item.source_path }} · {{ item.parse_status }}</dd>
                <template v-if="item.source_run_uid">
                  <dt class="text-muted-foreground">run</dt>
                  <dd>
                    <RouterLink
                      :to="{ name: 'run-detail', params: { uid: item.source_run_uid } }"
                      class="font-mono text-primary hover:underline"
                    >
                      {{ item.source_run_uid.slice(0, 12) }}
                    </RouterLink>
                  </dd>
                </template>
                <template v-if="item.provider_label || item.provider_kind">
                  <dt class="text-muted-foreground">provider</dt>
                  <dd>
                    <div>{{ item.provider_label || item.provider_kind }}</div>
                    <div v-if="item.provider_model" class="font-mono text-muted-foreground">{{ item.provider_model }}</div>
                  </dd>
                </template>
                <template v-if="item.detected_by_tool">
                  <dt class="text-muted-foreground">analyzer</dt>
                  <dd class="font-mono break-all">
                    {{ item.detected_by_tool }}<span v-if="item.detected_by_rule"> · {{ item.detected_by_rule }}</span>
                  </dd>
                </template>
                <dt class="text-muted-foreground">confidence</dt>
                <dd>{{ trustPercent(item.confidence) }}% self-reported</dd>
                <template v-if="item.created_at">
                  <dt class="text-muted-foreground">created</dt>
                  <dd>{{ item.created_at }}</dd>
                </template>
                <template v-if="item.updated_at">
                  <dt class="text-muted-foreground">updated</dt>
                  <dd>{{ item.updated_at }}</dd>
                </template>
                <template v-if="item.last_confirmed_at">
                  <dt class="text-muted-foreground">confirmed</dt>
                  <dd>{{ item.last_confirmed_at }}</dd>
                </template>
                <template v-if="corroborationCount(item) > 1">
                  <dt class="text-muted-foreground">corroborated</dt>
                  <dd>
                    <RouterLink
                      v-for="uid in item.source_run_uids || []"
                      :key="uid"
                      :to="{ name: 'run-detail', params: { uid } }"
                      class="mr-2 inline-block font-mono text-primary hover:underline"
                    >
                      {{ uid.slice(0, 8) }}
                    </RouterLink>
                  </dd>
                </template>
              </dl>
            </CardContent>
          </Card>

          <Card v-if="item.affected_paths?.length">
            <CardHeader class="p-4">
              <CardTitle class="text-base">Affected paths</CardTitle>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <ul class="space-y-0.5 font-mono text-xs">
                <li v-for="p in item.affected_paths" :key="p" class="break-all">{{ p }}</li>
              </ul>
            </CardContent>
          </Card>
        </div>
      </div>

      <TicketDialog
        v-model:open="promoteOpen"
        :repositories="repositories.list"
        :finding="item"
        @saved="onPromoted"
      />

      <FindingEditDialog v-model:open="editOpen" :finding="item" @saved="onEdited" />
    </template>
  </div>
</template>
