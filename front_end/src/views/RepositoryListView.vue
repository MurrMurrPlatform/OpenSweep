<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { PageHeader } from '@/components/ui/page-header'
import { EmptyState } from '@/components/ui/empty-state'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useGithubAppStore } from '@/stores/githubAppStore'
import { useToast } from '@/composables/useToast'
import RepoCard from '@/components/repositories/RepoCard.vue'
import RepositoryFormDialog from '@/components/repositories/RepositoryFormDialog.vue'
import ConnectRepositoryDialog from '@/components/repositories/ConnectRepositoryDialog.vue'
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
import { Plus, FolderGit2, Github, BarChart3 } from 'lucide-vue-next'
import type { Repository } from '@/types/api'

const repos = useRepositoryStore()
const githubApp = useGithubAppStore()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const dialogOpen = ref(false)
const connectOpen = ref(false)
const editing = ref<Repository | null>(null)
const submitting = ref(false)
const deleteOpen = ref(false)
const pendingDelete = ref<Repository | null>(null)

onMounted(() => {
  if (!repos.loaded) repos.fetchAll()
  // Best-effort — the hint banner simply stays hidden if this fails.
  if (!githubApp.loaded) githubApp.fetchStatus().catch(() => {})
  // The /setup redirect passes install_error=state_reused when the signed
  // install nonce has already been consumed (double-click, expired link,
  // pre-upgrade state). Without a toast this looks identical to a success.
  const installError = route.query.install_error
  if (installError) {
    toast.error(
      'GitHub install link expired',
      'Click "Connect repository" and use the Install button again.',
    )
  }
  // /repositories?connect=1 (e.g. from GitHub settings) auto-opens the picker.
  if (route.query.connect === '1') {
    connectOpen.value = true
  }
  if (installError || route.query.connect === '1') {
    router.replace({ query: { ...route.query, connect: undefined, install_error: undefined } })
  }
})

function openConnect() {
  connectOpen.value = true
}

/** A repo was registered in the connect dialog — reload so its card appears. */
function onConnected() {
  repos.fetchAll().catch(() => {})
}

function openCreate() {
  editing.value = null
  dialogOpen.value = true
}

function openEdit(r: Repository) {
  editing.value = r
  dialogOpen.value = true
}

async function onSubmit(value: Partial<Repository>) {
  submitting.value = true
  try {
    if (editing.value) {
      await repos.update(editing.value.uid, value)
      toast.success('Repository updated')
    } else {
      await repos.create(value)
      toast.success('Repository added')
    }
    dialogOpen.value = false
  } catch (e: any) {
    toast.error('Save failed', e.detail || e.message)
  } finally {
    submitting.value = false
  }
}

function onDelete(r: Repository) {
  pendingDelete.value = r
  deleteOpen.value = true
}

async function confirmDelete() {
  const r = pendingDelete.value
  if (!r) return
  deleteOpen.value = false
  try {
    await repos.remove(r.uid)
    toast.success('Repository deleted')
  } catch (e: any) {
    toast.error('Delete failed', e.detail || e.message)
  }
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageHeader
      title="Repositories"
      subtitle="GitHub repos connected to OpenSweep."
    >
      <div class="flex items-center gap-2">
        <RouterLink :to="{ name: 'overview' }">
          <Button variant="outline" size="sm">
            <BarChart3 /> All-workspace overview
          </Button>
        </RouterLink>
        <Button variant="ghost" size="sm" @click="openCreate">
          <Plus /> Add manually
        </Button>
        <Button @click="openConnect">
          <Github /> Connect repository
        </Button>
      </div>
    </PageHeader>

    <div
      v-if="githubApp.loaded && !githubApp.status.connected"
      class="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-primary/30 bg-primary/10 px-4 py-3"
    >
      <div class="flex items-center gap-3 text-sm">
        <Github class="h-4 w-4 shrink-0 text-primary" />
        <span>
          <span class="font-medium text-primary">Connect GitHub</span>
          <span class="text-muted-foreground"> to pick repositories straight from your account — install the App once, then connect repos here with one click.</span>
        </span>
      </div>
      <RouterLink to="/settings/github">
        <Button variant="outline" size="sm">Set up</Button>
      </RouterLink>
    </div>

    <Card>
      <CardHeader>
        <CardTitle class="text-base">Connected repositories</CardTitle>
      </CardHeader>
      <CardContent>
        <div v-if="!repos.loaded" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Skeleton v-for="i in 6" :key="i" class="h-44" />
        </div>
        <div v-else-if="repos.list.length" class="stagger-children grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <RepoCard
            v-for="r in repos.list"
            :key="r.uid"
            :repo="r"
            class="card-interactive"
            @edit="openEdit"
            @delete="onDelete"
          />
        </div>
        <EmptyState
          v-else
          :icon="FolderGit2"
          title="No repositories yet"
          description="Connect a GitHub repo to start mapping it."
          class="border-0"
        >
          <div class="flex items-center gap-2">
            <Button @click="openConnect">
              <Github /> Connect repository
            </Button>
            <Button variant="ghost" size="sm" @click="openCreate">
              <Plus /> Add manually
            </Button>
          </div>
        </EmptyState>
      </CardContent>
    </Card>

    <ConnectRepositoryDialog
      v-model:open="connectOpen"
      @connected="onConnected"
    />

    <RepositoryFormDialog
      v-model:open="dialogOpen"
      :repository="editing"
      :submitting="submitting"
      @submit="onSubmit"
    />

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete repository</AlertDialogTitle>
          <AlertDialogDescription>
            Delete repository "{{ pendingDelete?.name }}"? This removes only the OpenSweep record; the underlying files / GitHub repo are untouched.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDelete"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
