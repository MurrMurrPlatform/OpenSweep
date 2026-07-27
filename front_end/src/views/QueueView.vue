<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { GitPullRequest, Plus, RefreshCw } from 'lucide-vue-next'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useActiveRuns } from '@/composables/useActiveRuns'
import { PageHeader } from '@/components/ui/page-header'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import PullRequestCard from '@/components/delivery/PullRequestCard.vue'
import SyncPrDialog from '@/components/delivery/SyncPrDialog.vue'
import type { PullRequestDTO } from '@/types/api'

const delivery = useDeliveryStore()
const repos = useRepositoryStore()
const { uid: repoUid, repo } = useCurrentRepo()

const prs = ref<PullRequestDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const syncOpen = ref(false)
const syncingRepo = ref(false)

/** `silent` skips the skeleton so the live heartbeat below never flickers the
 *  board the user is reading. */
async function reload({ silent = false } = {}) {
  if (!repoUid.value) return
  if (!silent) loading.value = true
  error.value = null
  try {
    // The list DTO carries waive_requested_count — no per-PR resolutions fetch.
    prs.value = await delivery.fetchPullRequests({ state: 'open', repository_uid: repoUid.value })
  } catch (e: unknown) {
    // A failed background refresh must not blank out a board that is on screen.
    if (!silent) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(repoUid, () => void reload(), { immediate: true })

// Review/fix runs are what move a PR between these columns. Ride the shared
// /runs/active heartbeat: when the in-flight set changes, the board is stale.
const { signature } = useActiveRuns(
  () => (repoUid.value ? { repository_uid: repoUid.value } : null),
  { pollWhenIdle: true },
)
watch(signature, () => void reload({ silent: true }))

/** 2-way reconcile with GitHub — imports PRs opened outside OpenSweep and
 *  drops externally merged/closed ones. */
async function syncFromGitHub() {
  if (!repoUid.value || syncingRepo.value) return
  syncingRepo.value = true
  error.value = null
  try {
    await delivery.syncRepository(repoUid.value)
    await reload()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    syncingRepo.value = false
  }
}

function needsYou(pr: PullRequestDTO): boolean {
  const c = pr.convergence
  return (
    c?.verdict_result === 'needs_human' ||
    (c?.counts.blocking ?? 0) > 0 ||
    (pr.waive_requested_count ?? 0) > 0 ||
    pr.fix_rounds_exhausted === true
  )
}

const ready = computed(() => prs.value.filter((pr) => pr.converged))
const needing = computed(() => prs.value.filter((pr) => !pr.converged && needsYou(pr)))
const running = computed(() => prs.value.filter((pr) => !pr.converged && !needsYou(pr)))

function repoName(pr: PullRequestDTO): string {
  if (repo.value && pr.repository_uid === repo.value.uid) return repo.value.name
  return repos.find(pr.repository_uid)?.name ?? pr.repository_uid.slice(0, 8)
}

interface QueueColumn {
  id: string
  title: string
  subtitle: string
  items: PullRequestDTO[]
  showReasons: boolean
  empty: string
}

const columns = computed<QueueColumn[]>(() => [
  {
    id: 'ready',
    title: 'Ready to merge',
    subtitle: 'Converged — CI green, verdict fresh, ledger clear.',
    items: ready.value,
    showReasons: false,
    empty: 'Nothing converged yet.',
  },
  {
    id: 'needs-you',
    title: 'Needs you',
    subtitle: 'Blocking triage, needs-human verdicts, waiver requests.',
    items: needing.value,
    showReasons: true,
    empty: 'Nothing needs a human right now.',
  },
  {
    id: 'running',
    title: 'Running / Waiting',
    subtitle: 'Pending CI or awaiting review.',
    items: running.value,
    showReasons: false,
    empty: 'No PRs in flight.',
  },
])
</script>

<template>
  <div class="space-y-4">
    <PageHeader title="Queue" subtitle="Every open PR, sorted by what it needs next.">
      <Button variant="outline" size="sm" :disabled="loading" @click="reload()">
        <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
      </Button>
      <Button variant="outline" size="sm" :disabled="syncingRepo" @click="syncFromGitHub">
        <RefreshCw :class="{ 'animate-spin': syncingRepo }" /> Sync from GitHub
      </Button>
      <Button size="sm" @click="syncOpen = true">
        <Plus /> Sync PR
      </Button>
    </PageHeader>

    <!-- Loading -->
    <div v-if="loading" class="grid grid-cols-1 gap-4 xl:grid-cols-3">
      <Card v-for="i in 3" :key="i">
        <CardContent class="space-y-3">
          <Skeleton class="h-4 w-1/3" />
          <Skeleton class="h-20" />
          <Skeleton class="h-20" />
        </CardContent>
      </Card>
    </div>

    <!-- Error -->
    <ErrorState v-else-if="error" title="Couldn't load the queue" :message="error">
      <Button variant="outline" size="sm" @click="reload()">Retry</Button>
    </ErrorState>

    <!-- Empty -->
    <EmptyState
      v-else-if="prs.length === 0"
      :icon="GitPullRequest"
      title="No open pull requests"
      description="PRs land here via webhooks and the periodic GitHub sweep. Pull them in now to get started."
    >
      <Button size="sm" :disabled="syncingRepo" @click="syncFromGitHub">
        <RefreshCw :class="{ 'animate-spin': syncingRepo }" /> Sync from GitHub
      </Button>
    </EmptyState>

    <!-- Columns -->
    <div v-else class="stagger-children grid grid-cols-1 items-start gap-4 xl:grid-cols-3">
      <Card v-for="col in columns" :key="col.id">
        <CardHeader class="p-4">
          <CardTitle class="text-base">
            {{ col.title }}
            <span class="font-normal text-muted-foreground">· {{ col.items.length }}</span>
          </CardTitle>
          <p class="text-xs text-muted-foreground">{{ col.subtitle }}</p>
        </CardHeader>
        <CardContent class="p-0">
          <div v-if="col.items.length === 0" class="p-4 text-sm text-muted-foreground">{{ col.empty }}</div>
          <div v-else class="divide-y divide-border">
            <PullRequestCard
              v-for="pr in col.items"
              :key="pr.uid"
              :pr="pr"
              :repo-name="repoName(pr)"
              :waive-requests="pr.waive_requested_count ?? 0"
              :show-reasons="col.showReasons"
            />
          </div>
        </CardContent>
      </Card>
    </div>

    <SyncPrDialog v-model:open="syncOpen" :repositories="repo ? [repo] : []" @synced="reload()" />
  </div>
</template>
