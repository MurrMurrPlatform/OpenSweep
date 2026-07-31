<script setup lang="ts">
/**
 * Workspace Settings tab — repo info, the three configuration cards
 * (workflow, analyzers, merge policy), and the danger zone. Split out of the
 * old single-page dashboard so daily-use surfaces stay uncluttered.
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Trash2 } from 'lucide-vue-next'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import MergePolicyCard from '@/components/delivery/MergePolicyCard.vue'
import WorkflowCard from '@/components/repositories/WorkflowCard.vue'
import AnalyzersCard from '@/components/repositories/AnalyzersCard.vue'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { useWorkspaceCtx } from './workspaceContext'

const router = useRouter()
const { repo, repoUid } = useWorkspaceCtx()

// ── Danger zone: delete the workspace (OpenSweep data only, GitHub untouched) ─

const repos = useRepositoryStore()
const toast = useToast()
const deleteOpen = ref(false)
const deleting = ref(false)

async function confirmDelete() {
  const target = repo.value
  if (!target || deleting.value) return
  deleting.value = true
  try {
    await repos.remove(target.uid)
    toast.success('Repository deleted', target.name)
    void router.push({ name: 'repositories' })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t delete repository', msg)
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- Repository identity + connection facts, read-only. -->
    <Card v-if="repo">
      <CardHeader class="p-4 pb-0">
        <CardTitle class="text-base">Repository</CardTitle>
      </CardHeader>
      <CardContent class="grid grid-cols-1 gap-3 p-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <div class="text-xs uppercase tracking-wide text-muted-foreground">Name</div>
          <div class="mt-1 truncate">{{ repo.name }}</div>
        </div>
        <div>
          <div class="text-xs uppercase tracking-wide text-muted-foreground">GitHub</div>
          <div class="mt-1 truncate font-mono text-xs">
            <a
              v-if="repo.github_owner && repo.github_repo"
              :href="`https://github.com/${repo.github_owner}/${repo.github_repo}`"
              target="_blank"
              rel="noopener noreferrer"
              class="text-primary hover:underline"
            >{{ repo.github_owner }}/{{ repo.github_repo }}</a>
            <template v-else>—</template>
          </div>
        </div>
        <div>
          <div class="text-xs uppercase tracking-wide text-muted-foreground">Default branch</div>
          <div class="mt-1 font-mono">{{ repo.default_branch }}</div>
        </div>
        <div>
          <div class="text-xs uppercase tracking-wide text-muted-foreground">Kill switch</div>
          <div class="mt-1" :class="repo.kill_switch_active ? 'font-semibold text-destructive' : ''">
            {{ repo.kill_switch_active ? 'ENGAGED' : 'inactive' }}
          </div>
        </div>
      </CardContent>
    </Card>

    <!-- Per-stage prompt guidance + auto-review/auto-fix toggles. -->
    <WorkflowCard v-if="repoUid" :repository-uid="repoUid" />

    <!-- Static-analyzer mode (auto/custom/off) for review + fix runs. -->
    <AnalyzersCard v-if="repoUid" :repository-uid="repoUid" />

    <!-- Write-path guardrails: path denylist, clean-round gate, fix-round bound. -->
    <MergePolicyCard v-if="repoUid" :repository-uid="repoUid" />

    <!-- Danger zone -->
    <Card class="border-destructive/40">
      <CardHeader class="p-4 pb-0">
        <CardTitle class="text-base text-destructive">Danger zone</CardTitle>
      </CardHeader>
      <CardContent class="flex flex-wrap items-center justify-between gap-3 p-4">
        <p class="max-w-prose text-sm text-muted-foreground">
          Deleting this workspace permanently removes its OpenSweep data — findings, runs,
          docs, work items. The GitHub repository itself is untouched.
        </p>
        <Button variant="destructive" size="sm" :loading="deleting" @click="deleteOpen = true">
          <Trash2 /> Delete workspace
        </Button>
      </CardContent>
    </Card>

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this workspace?</AlertDialogTitle>
          <AlertDialogDescription>
            “{{ repo?.name }}” and everything OpenSweep knows about it — findings, runs, docs,
            work items — are removed permanently. The GitHub repository is untouched.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDelete"
          >
            Delete workspace
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
