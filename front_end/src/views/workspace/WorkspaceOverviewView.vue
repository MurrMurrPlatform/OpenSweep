<script setup lang="ts">
/**
 * Workspace Overview tab — the page onboarding lands on.
 *
 * Answers "what do I do here?" for a brand-new user (one obvious way to start
 * a sweep) and "what needs me / what happened since I last looked?" for
 * everyone else: the attention panel, at-a-glance numbers, and recent runs.
 * Refreshes ride the layout's /runs/active heartbeat (signature) instead of a
 * Refresh button.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { Activity, Github, MessagesSquare, Sparkles } from 'lucide-vue-next'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useMetricsStore } from '@/stores/metricsStore'
import { useRunStore } from '@/stores/runStore'
import { PLAYBOOK_TO_PRODUCES } from '@/lib/produces'
import { runStatusLabel, runStatusVariant } from '@/lib/runStatus'
import { formatRelativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { AnimatedNumber } from '@/components/ui/animated-number'
import AttentionPanel from '@/components/repositories/AttentionPanel.vue'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import { useWorkspaceCtx } from './workspaceContext'
import type { PullRequestDTO, RunDTO } from '@/types/api'

const router = useRouter()
const { repo, repoUid, repoSlug, activeRuns, signature, openSweep } = useWorkspaceCtx()
const metrics = useMetricsStore()
const delivery = useDeliveryStore()
const runStore = useRunStore()

function ask() {
  if (!repoSlug.value) return
  router.push({ name: 'ask', params: { repoSlug: repoSlug.value } })
}

// ── At-a-glance state ───────────────────────────────────────────────────────

const openPrs = ref<PullRequestDTO[]>([])
const recentRuns = ref<RunDTO[]>([])
const loadingPulse = ref(true)

/** Per-repo rollup (docs, open findings, high severity, runs/24h). */
const summary = computed(() => {
  const uid = repoUid.value
  if (!uid) return null
  return metrics.overview?.repositories.find((r) => r.repository_uid === uid) ?? null
})

/** Same "a human has to look at this" test the Queue board uses. */
function needsYou(pr: PullRequestDTO): boolean {
  const c = pr.convergence
  return (
    c?.verdict_result === 'needs_human' ||
    (c?.counts.blocking ?? 0) > 0 ||
    (pr.waive_requested_count ?? 0) > 0 ||
    pr.fix_rounds_exhausted === true
  )
}

const prsNeedingYou = computed(() => openPrs.value.filter((pr) => !pr.converged && needsYou(pr)).length)
const prsReady = computed(() => openPrs.value.filter((pr) => pr.converged).length)

/**
 * `silent` refreshes keep the rendered numbers on screen — the heartbeat must
 * never flash the page back to skeletons every five seconds.
 */
async function loadPulse({ silent = false } = {}) {
  const uid = repoUid.value
  if (!uid) return
  if (!silent) loadingPulse.value = true
  const [prs, runs] = await Promise.allSettled([
    delivery.fetchPullRequests({ state: 'open', repository_uid: uid }),
    runStore.query({ repository_uid: uid, limit: 6 }),
    metrics.fetchOverview(),
  ])
  if (repoUid.value !== uid) return // switched workspace mid-flight
  if (prs.status === 'fulfilled') openPrs.value = prs.value
  if (runs.status === 'fulfilled') recentRuns.value = runs.value
  loadingPulse.value = false
}

onMounted(() => void loadPulse())
watch(repoUid, () => void loadPulse())
// A run appearing, changing status or finishing is exactly when these numbers
// go stale — the layout's /runs/active poll is already running, so ride it.
watch(signature, () => void loadPulse({ silent: true }))

/** runStatusVariant predates the shadcn Badge tones (danger/default gone). */
function statusBadgeVariant(run: RunDTO): BadgeVariants['variant'] {
  const v = runStatusVariant(run.status, run)
  if (v === 'danger') return 'destructive'
  if (v === 'default') return 'secondary'
  return v
}

/**
 * Surface the backend's per-repo GitHub connection health — the webhook
 * handler flips this to `disconnected`/`suspended` when the App is
 * uninstalled or its access is revoked. Left unrendered, the user only
 * discovered the credential was gone by watching runs fail.
 */
const githubStatusLabel = computed(() => {
  switch (repo.value?.github_connection_status) {
    case 'disconnected': return 'GitHub disconnected'
    case 'suspended': return 'GitHub App suspended'
    case 'error': return 'GitHub connection error'
    default: return null
  }
})

/** Nothing has ever run here: the whole page becomes one call to action. */
const untouched = computed(
  () =>
    !loadingPulse.value &&
    recentRuns.value.length === 0 &&
    openPrs.value.length === 0 &&
    (summary.value?.docs ?? 0) === 0 &&
    (summary.value?.open_findings ?? 0) === 0,
)

const stats = computed(() => [
  {
    key: 'findings',
    label: 'Open findings',
    value: summary.value?.open_findings ?? 0,
    detail: `${summary.value?.high_severity_findings ?? 0} high/critical`,
    to: { name: 'findings', params: { repoSlug: repoSlug.value || '' } },
  },
  {
    key: 'prs',
    label: 'Open pull requests',
    value: openPrs.value.length,
    detail: prsNeedingYou.value
      ? `${prsNeedingYou.value} need you`
      : prsReady.value
        ? `${prsReady.value} ready to merge`
        : 'nothing waiting on you',
    to: { name: 'queue', params: { repoSlug: repoSlug.value || '' } },
  },
  {
    key: 'runs',
    label: 'Runs / 24h',
    value: summary.value?.runs_last_24h ?? 0,
    detail: activeRuns.value.length ? `${activeRuns.value.length} in flight` : 'none in flight',
    to: { name: 'runs', params: { repoSlug: repoSlug.value || '' } },
  },
  {
    key: 'docs',
    label: 'Doc pages',
    value: summary.value?.docs ?? 0,
    detail: 'the wiki agents read',
    to: { name: 'documentation', params: { repoSlug: repoSlug.value || '' } },
  },
])
</script>

<template>
  <div class="space-y-4">
    <!-- Cold workspace: one thing to do, not five cards of configuration. -->
    <EmptyState
      v-if="untouched"
      :icon="Sparkles"
      title="Nothing has swept this repository yet"
      description="A sweep sends agents over the code: it maps your subsystems and features, writes the wiki from what it finds, then audits for real problems. Everything on this page fills in from there."
    >
      <Button @click="openSweep">
        <Sparkles />
        Run your first sweep
      </Button>
      <Button variant="outline" @click="ask">
        <MessagesSquare />
        Ask a question instead
      </Button>
    </EmptyState>

    <template v-else>
      <!-- Surface a GitHub credential outage before the user notices runs
           failing. Backend flips repo.github_connection_status via webhooks
           on App uninstall / repo access revoke / installation suspend. -->
      <div
        v-if="githubStatusLabel"
        class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm"
      >
        <div class="flex items-center gap-3">
          <Github class="h-4 w-4 shrink-0 text-destructive" />
          <span>
            <span class="font-medium text-destructive">{{ githubStatusLabel }}</span>
            <span class="text-muted-foreground"> — OpenSweep can no longer talk to GitHub for this repo. Sweeps, audits and PR sync will fail until the App is reconnected.</span>
          </span>
        </div>
        <RouterLink to="/settings/github">
          <Button variant="outline" size="sm">Reconnect</Button>
        </RouterLink>
      </div>

      <!-- What needs a human, priority-sorted server-side. -->
      <AttentionPanel
        v-if="repoUid && repoSlug"
        :repository-uid="repoUid"
        :repo-slug="repoSlug"
        :refresh-key="signature"
      />

      <!-- KPI row -->
      <section class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <template v-if="loadingPulse && !summary">
          <Card v-for="i in 4" :key="`sk-${i}`">
            <CardContent class="p-4"><Skeleton class="h-10" /></CardContent>
          </Card>
        </template>
        <RouterLink v-for="s in stats" v-else :key="s.key" :to="s.to" class="block">
          <Card class="h-full transition-colors hover:bg-accent">
            <CardContent class="p-4">
              <div class="text-xs uppercase tracking-wide text-muted-foreground">{{ s.label }}</div>
              <div class="mt-1 text-2xl font-semibold tabular-nums"><AnimatedNumber :value="s.value" /></div>
              <div class="mt-1 truncate text-xs text-muted-foreground">{{ s.detail }}</div>
            </CardContent>
          </Card>
        </RouterLink>
      </section>

      <!-- Recent runs — live, no Refresh button -->
      <Card>
        <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0 p-4">
          <CardTitle class="text-base">Recent runs</CardTitle>
          <Button
            as="router-link"
            :to="{ name: 'runs', params: { repoSlug: repoSlug || '' } }"
            variant="ghost"
            size="sm"
          >
            All runs
          </Button>
        </CardHeader>
        <CardContent class="p-0">
          <div v-if="loadingPulse && !recentRuns.length" class="space-y-2 p-4">
            <Skeleton v-for="i in 3" :key="i" class="h-9" />
          </div>
          <EmptyState
            v-else-if="!recentRuns.length"
            :icon="Activity"
            title="No runs yet"
            description="Sweeps, audits and questions all land here as runs you can open and follow."
            class="rounded-none border-0"
          >
            <Button size="sm" @click="openSweep">
              <Sparkles /> Start a sweep
            </Button>
          </EmptyState>
          <ul v-else class="divide-y divide-border">
            <li v-for="r in recentRuns" :key="r.uid">
              <RouterLink
                :to="{ name: 'run-detail', params: { uid: r.uid } }"
                class="flex flex-wrap items-center gap-2 px-4 py-2.5 text-sm transition-colors hover:bg-accent"
              >
                <span class="min-w-0 flex-1 basis-48 truncate font-medium">
                  {{ r.title || `Run ${r.uid.slice(0, 12)}` }}
                </span>
                <ProducesBadge :produces="PLAYBOOK_TO_PRODUCES[r.playbook] ?? 'findings'" />
                <Badge :variant="statusBadgeVariant(r)">{{ runStatusLabel(r) }}</Badge>
                <span class="whitespace-nowrap text-xs text-muted-foreground">
                  {{ formatRelativeTime(r.last_activity_at || r.updated_at) }}
                </span>
              </RouterLink>
            </li>
          </ul>
        </CardContent>
      </Card>
    </template>

    <section class="grid gap-3 text-sm grid-cols-1 sm:grid-cols-3">
      <Card>
        <CardContent class="p-4">
          <div class="text-xs text-muted-foreground uppercase tracking-wide">Default branch</div>
          <div class="font-mono mt-1">{{ repo?.default_branch }}</div>
        </CardContent>
      </Card>
      <Card>
        <CardContent class="p-4">
          <div class="text-xs text-muted-foreground uppercase tracking-wide">GitHub</div>
          <div class="font-mono text-xs mt-1 truncate">
            <a
              v-if="repo?.github_owner && repo?.github_repo"
              :href="`https://github.com/${repo.github_owner}/${repo.github_repo}`"
              target="_blank"
              rel="noopener noreferrer"
              class="text-primary hover:underline"
            >{{ repo.github_owner }}/{{ repo.github_repo }}</a>
            <template v-else>—</template>
          </div>
          <div
            v-if="githubStatusLabel"
            class="mt-2 text-[10px] font-semibold uppercase tracking-wide text-destructive"
          >
            {{ githubStatusLabel }}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardContent class="p-4">
          <div class="text-xs text-muted-foreground uppercase tracking-wide">Kill switch</div>
          <div class="mt-1" :class="repo?.kill_switch_active ? 'text-destructive font-semibold' : ''">
            {{ repo?.kill_switch_active ? 'ENGAGED' : 'inactive' }}
          </div>
        </CardContent>
      </Card>
    </section>
  </div>
</template>
