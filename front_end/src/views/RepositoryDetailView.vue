<script setup lang="ts">
import { useRouter } from 'vue-router'
import { BookOpen, Search } from 'lucide-vue-next'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useActiveRuns } from '@/composables/useActiveRuns'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import MergePolicyCard from '@/components/delivery/MergePolicyCard.vue'
import WorkflowCard from '@/components/repositories/WorkflowCard.vue'
import AnalyzersCard from '@/components/repositories/AnalyzersCard.vue'
import ActiveRunChip from '@/components/runs/ActiveRunChip.vue'

const router = useRouter()
const { uid: repoUid, slug: repoSlug, repo } = useCurrentRepo()

// In-flight runs anywhere in this repo — surfaced as a chip in the header.
const { activeRun } = useActiveRuns(() =>
  repoUid.value ? { repository_uid: repoUid.value } : null,
)

function ask() {
  if (!repoSlug.value) return
  router.push({ name: 'ask', params: { repoSlug: repoSlug.value } })
}

function openDocs() {
  if (!repoSlug.value) return
  router.push({ name: 'documentation', params: { repoSlug: repoSlug.value } })
}
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
        <Button as="router-link" :to="{ name: 'documentation', params: { repoSlug: repo.slug } }" variant="outline" size="sm">
          <BookOpen />
          Documentation
        </Button>
        <Button as="router-link" :to="{ name: 'health', params: { repoSlug: repo.slug } }" variant="outline" size="sm">
          <Search />
          Health
        </Button>
        <ActiveRunChip v-if="activeRun" :run="activeRun" />
      </PageHeader>

      <!-- Twin entry points: the wiki vs. sharp triage. -->
      <section class="grid gap-3 sm:grid-cols-2">
        <button
          class="card-interactive rounded-lg border-2 border-primary bg-primary/10 p-4 text-left hover:bg-primary/15"
          @click="openDocs"
        >
          <div class="text-xs text-primary uppercase font-mono mb-1">Knowledge</div>
          <div class="font-semibold text-lg">Documentation</div>
          <div class="text-xs text-muted-foreground mt-1">
            The repository's wiki: generate pages from the code, review agent-proposed edits, sync back as a PR.
          </div>
        </button>
        <button
          class="card-interactive rounded-lg border bg-card p-4 text-left hover:bg-accent"
          @click="ask"
        >
          <div class="text-xs text-muted-foreground uppercase font-mono mb-1">Sharp triage</div>
          <div class="font-semibold text-lg">Ask a question</div>
          <div class="text-xs text-muted-foreground mt-1">
            Dispatch one targeted agent run. You provide the prompt, the executor digs in.
          </div>
        </button>
      </section>

      <section class="grid gap-3 text-sm grid-cols-1 sm:grid-cols-3">
        <Card>
          <CardContent class="p-4">
            <div class="text-xs text-muted-foreground uppercase tracking-wide">Default branch</div>
            <div class="font-mono mt-1">{{ repo.default_branch }}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-4">
            <div class="text-xs text-muted-foreground uppercase tracking-wide">GitHub</div>
            <div class="font-mono text-xs mt-1 truncate">
              <a
                v-if="repo.github_owner && repo.github_repo"
                :href="`https://github.com/${repo.github_owner}/${repo.github_repo}`"
                target="_blank"
                rel="noopener noreferrer"
                class="text-primary hover:underline"
              >{{ repo.github_owner }}/{{ repo.github_repo }}</a>
              <template v-else>—</template>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-4">
            <div class="text-xs text-muted-foreground uppercase tracking-wide">Kill switch</div>
            <div class="mt-1" :class="repo.kill_switch_active ? 'text-destructive font-semibold' : ''">
              {{ repo.kill_switch_active ? 'ENGAGED' : 'inactive' }}
            </div>
          </CardContent>
        </Card>
      </section>

      <!-- Per-stage prompt guidance + auto-review/auto-fix toggles. -->
      <WorkflowCard v-if="repoUid" :repository-uid="repoUid" />

      <!-- Static-analyzer mode (auto/custom/off) for review + fix runs. -->
      <AnalyzersCard v-if="repoUid" :repository-uid="repoUid" />

      <!-- Write-path guardrails: path denylist, clean-round gate, fix-round bound. -->
      <MergePolicyCard v-if="repoUid" :repository-uid="repoUid" />
    </template>
  </div>
</template>
