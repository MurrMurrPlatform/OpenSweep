<script setup lang="ts">
/**
 * Workspace shell — header, tab nav, and the shared run heartbeat for the
 * Overview / Activity / Settings tabs rendered in the nested RouterView.
 *
 * Owns the ONE useActiveRuns() instance (it polls per-instance; a copy per tab
 * would multiply the /runs/active traffic) and the Start-a-sweep dialog, and
 * provides both to tabs via workspaceContext.
 */
import { computed, provide, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  BookOpen,
  History,
  LayoutGrid,
  MessagesSquare,
  Search,
  Settings2,
  Sparkles,
} from 'lucide-vue-next'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useActiveRuns } from '@/composables/useActiveRuns'
import { PageHeader } from '@/components/ui/page-header'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import StartSweepDialog from '@/components/repositories/StartSweepDialog.vue'
import RepoConnectionBanner from '@/components/repositories/RepoConnectionBanner.vue'
import ActiveRunChip from '@/components/runs/ActiveRunChip.vue'
import { WorkspaceCtxKey } from './workspaceContext'

const route = useRoute()
const router = useRouter()
const { uid: repoUid, slug: repoSlug, repo } = useCurrentRepo()

// In-flight runs anywhere in this repo — the header chip, and the liveness
// heartbeat every tab watches via the provided `signature`.
const { activeRun, activeRuns, signature, noteDispatched } = useActiveRuns(
  () => (repoUid.value ? { repository_uid: repoUid.value } : null),
  { pollWhenIdle: true },
)

const sweepOpen = ref(false)

function ask() {
  if (!repoSlug.value) return
  router.push({ name: 'ask', params: { repoSlug: repoSlug.value } })
}

function onSweepDispatched(runUid: string) {
  // Bumps the signature, which is what tabs watch for silent refreshes.
  noteDispatched({ run_uid: runUid })
}

provide(WorkspaceCtxKey, {
  repo,
  repoUid,
  repoSlug,
  activeRun,
  activeRuns,
  signature,
  openSweep: () => {
    sweepOpen.value = true
  },
})

const tabs = computed(() => [
  { name: 'workspace-home', label: 'Overview', icon: LayoutGrid },
  { name: 'workspace-activity', label: 'Activity', icon: History },
  { name: 'workspace-settings', label: 'Settings', icon: Settings2 },
])
</script>

<template>
  <div class="space-y-4">
    <template v-if="!repo">
      <Skeleton class="h-12 w-72" />
      <Skeleton class="h-32" />
      <Skeleton class="h-40" />
    </template>

    <template v-else>
      <PageHeader :title="repo.name" :subtitle="repo.description || undefined">
        <template #breadcrumb>
          <div class="text-xs text-muted-foreground uppercase font-mono mb-1">github · {{ repo.slug }}</div>
        </template>
        <ActiveRunChip v-if="activeRun" :run="activeRun" />
        <Button variant="outline" size="sm" @click="ask">
          <MessagesSquare />
          Ask
        </Button>
        <Button as="router-link" :to="{ name: 'documentation', params: { repoSlug: repo.slug } }" variant="outline" size="sm">
          <BookOpen />
          Documentation
        </Button>
        <Button as="router-link" :to="{ name: 'areas', params: { repoSlug: repo.slug } }" variant="outline" size="sm">
          <Search />
          Areas
        </Button>
        <Button size="sm" @click="sweepOpen = true">
          <Sparkles />
          Start a sweep
        </Button>
      </PageHeader>

      <!-- Delivery credentials outlive registration (rotated tokens, reinstalled
           Apps), and a repo left pointing at a connection that is gone can no
           longer push. Say so here rather than at the end of a run. -->
      <RepoConnectionBanner v-if="repoUid" :repo-uid="repoUid" />

      <!-- Underline tabs, attached to a full-width rule (WorkItemView pattern) -->
      <nav class="flex gap-0.5 border-b" aria-label="Workspace sections">
        <RouterLink
          v-for="tab in tabs"
          :key="tab.name"
          :to="{ name: tab.name, params: { repoSlug: repo.slug } }"
          class="-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors"
          :class="
            route.name === tab.name
              ? 'border-primary font-medium text-foreground'
              : 'border-transparent text-muted-foreground hover:border-border hover:text-foreground'
          "
        >
          <component :is="tab.icon" class="size-3.5" /> {{ tab.label }}
        </RouterLink>
      </nav>

      <RouterView />

      <StartSweepDialog
        v-model:open="sweepOpen"
        :repository-uid="repoUid"
        @dispatched="onSweepDispatched"
      />
    </template>
  </div>
</template>
