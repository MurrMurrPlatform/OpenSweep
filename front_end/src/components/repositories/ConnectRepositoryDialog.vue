<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { useGithubAppStore } from '@/stores/githubAppStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Check, ExternalLink, GitBranch, Github, Lock, Search } from 'lucide-vue-next'
import type { AvailableRepo, AvailableReposDTO, AvailableReposInstallation } from '@/types/api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  /** At least one repo was registered (incl. 409 already-registered) — refresh the list. */
  connected: []
}>()

const store = useGithubAppStore()
const toast = useToast()

const data = ref<AvailableReposDTO | null>(null)
const loading = ref(false)
const error = ref('')
const query = ref('')
/** `${installation_id}/${full_name}` of the row whose POST is in flight. */
const connectingKey = ref<string | null>(null)

watch(() => props.open, (val) => {
  if (val) {
    query.value = ''
    load()
  }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await store.fetchAvailableRepos()
  } catch (e: any) {
    data.value = null
    error.value = e.detail || e.message || 'Could not load repositories from GitHub.'
  } finally {
    loading.value = false
  }
}

const totalRepos = computed(() =>
  (data.value?.installations ?? []).reduce((n, inst) => n + inst.repos.length, 0),
)

/** Installations with repos filtered by the search query. Installations that
 *  end up empty are hidden unless they carry an error (which we still show). */
const filteredInstallations = computed(() => {
  const q = query.value.trim().toLowerCase()
  const installations = data.value?.installations ?? []
  return installations
    .map((inst) => ({
      ...inst,
      repos: q ? inst.repos.filter((r) => r.full_name.toLowerCase().includes(q)) : inst.repos,
    }))
    .filter((inst) => inst.repos.length > 0 || inst.error)
})

const noMatches = computed(() =>
  !!query.value.trim() && totalRepos.value > 0 && filteredInstallations.value.every((i) => !i.repos.length),
)

/** Stable group key — installation id, or the PAT connection uid. */
function groupKey(inst: AvailableReposInstallation): string {
  return inst.connection_uid || String(inst.id)
}

function rowKey(inst: AvailableReposInstallation, repo: AvailableRepo): string {
  return `${groupKey(inst)}/${repo.full_name}`
}

/** Flip the row in the source data (filteredInstallations spreads copies). */
function markRegistered(inst: AvailableReposInstallation, fullName: string) {
  const source = data.value?.installations.find((i) => groupKey(i) === groupKey(inst))
  const repo = source?.repos.find((r) => r.full_name === fullName)
  if (repo) repo.registered = true
}

/** App connected but installed nowhere yet — send them to GitHub to install it. */
function openInstall() {
  if (data.value?.install_url) window.open(data.value.install_url, '_blank')
}

async function connect(inst: AvailableReposInstallation, repo: AvailableRepo) {
  connectingKey.value = rowKey(inst, repo)
  try {
    await store.registerRepo({
      ...(inst.connection_uid
        ? { connection_uid: inst.connection_uid }
        : { installation_id: inst.id }),
      owner: repo.owner,
      name: repo.name,
    })
    markRegistered(inst, repo.full_name)
    toast.success('Repository connected', repo.full_name)
    emit('connected')
  } catch (e: any) {
    if (e instanceof ApiError && e.status === 409) {
      // Already registered (e.g. by someone else in parallel) — same outcome.
      markRegistered(inst, repo.full_name)
      toast.info('Already connected', `${repo.full_name} is already a OpenSweep workspace.`)
      emit('connected')
    } else {
      toast.error('Connect failed', e.detail || e.message)
    }
  } finally {
    connectingKey.value = null
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>Connect repository</DialogTitle>
        <DialogDescription>
          Pick a repo your GitHub connection can access and register it as a OpenSweep workspace.
        </DialogDescription>
      </DialogHeader>

    <!-- Loading -->
    <div v-if="loading" class="flex flex-col gap-2">
      <Skeleton class="h-9" />
      <Skeleton v-for="i in 4" :key="i" class="h-12" />
    </div>

    <!-- Error -->
    <ErrorState
      v-else-if="error"
      title="Could not load repositories"
      :message="error"
      class="py-8"
    >
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <!-- No GitHub access at all (no App, no token) -->
    <EmptyState
      v-else-if="data && !data.connected"
      :icon="Github"
      title="GitHub not connected"
      description="Connect GitHub first — add an access token (or install the GitHub App) under Settings → GitHub. That's what gives OpenSweep access to your repositories."
      class="border-0 py-8"
    >
      <RouterLink to="/settings/github" @click="emit('update:open', false)">
        <Button size="sm"><Github /> Set up GitHub</Button>
      </RouterLink>
    </EmptyState>

    <!-- Connected, but the App is installed nowhere yet -->
    <EmptyState
      v-else-if="data && data.installations.length === 0"
      :icon="Github"
      title="GitHub App not installed"
      description="The App is connected but not installed on any account yet. Install it and grant the repositories OpenSweep should see."
      class="border-0 py-8"
    >
      <Button v-if="data.install_url" size="sm" @click="openInstall">
        <ExternalLink /> Install GitHub App
      </Button>
    </EmptyState>

    <!-- No repos visible at all -->
    <EmptyState
      v-else-if="data && totalRepos === 0 && !data.installations.some((i) => i.error)"
      :icon="Github"
      title="No repositories available"
      description="The App has no repository access yet. Install it on an account and grant it the repos OpenSweep should see."
      class="border-0 py-8"
    >
      <a v-if="data.install_url" :href="data.install_url" target="_blank" rel="noopener">
        <Button size="sm"><ExternalLink /> Install the App</Button>
      </a>
    </EmptyState>

    <!-- Repo picker -->
    <!-- min-w-0: DialogContent is a grid — without it a long unbreakable
         repo/branch name widens this column past the dialog edge. -->
    <div v-else-if="data" class="flex min-w-0 flex-col gap-3">
      <div class="relative">
        <Search class="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input v-model="query" placeholder="Filter by owner/name…" class="pl-8" />
      </div>

      <div class="flex max-h-[50vh] flex-col gap-4 overflow-y-auto pr-1">
        <div v-if="noMatches" class="py-6 text-center text-sm text-muted-foreground">
          No repositories match “{{ query }}”.
        </div>

        <div v-for="inst in filteredInstallations" :key="groupKey(inst)" class="flex flex-col gap-1.5">
          <!-- Group header: an App installation or a token connection -->
          <div class="flex items-baseline justify-between gap-2 px-0.5">
            <div class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              {{ inst.account || (inst.connection_uid ? 'access token' : `installation #${inst.id}`) }}
              <span v-if="inst.connection_uid" class="ml-1 normal-case tracking-normal opacity-70">(token)</span>
            </div>
            <div class="text-xs text-muted-foreground">
              {{ inst.repos.length }} {{ inst.repos.length === 1 ? 'repository' : 'repositories' }}
            </div>
          </div>
          <div v-if="inst.error" class="break-words rounded-sm border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
            Could not list repositories for this connection: {{ inst.error }}
          </div>

          <!-- Repo rows -->
          <div
            v-for="repo in inst.repos"
            :key="repo.full_name"
            class="flex items-center justify-between gap-3 rounded-sm border px-3 py-2"
          >
            <div class="min-w-0">
              <div class="flex min-w-0 items-center gap-2">
                <span class="truncate font-mono text-sm">{{ repo.full_name }}</span>
                <Badge v-if="repo.private" variant="outline" class="shrink-0 px-1.5 text-[10px]">
                  <Lock class="h-3 w-3" /> private
                </Badge>
                <Badge v-if="repo.default_branch" variant="outline" class="min-w-0 max-w-40 px-1.5 text-[10px]">
                  <GitBranch class="h-3 w-3 shrink-0" />
                  <span class="truncate">{{ repo.default_branch }}</span>
                </Badge>
              </div>
              <div v-if="repo.description" class="mt-0.5 truncate text-xs text-muted-foreground">
                {{ repo.description }}
              </div>
            </div>
            <div class="shrink-0">
              <span
                v-if="repo.registered"
                class="inline-flex items-center gap-1 text-xs font-medium text-good"
              >
                <Check class="h-3.5 w-3.5" /> Connected
              </span>
              <Button
                v-else
                size="sm"
                :loading="connectingKey === rowKey(inst, repo)"
                :disabled="connectingKey !== null && connectingKey !== rowKey(inst, repo)"
                @click="connect(inst, repo)"
              >
                Connect
              </Button>
            </div>
          </div>
        </div>
      </div>

      <!-- Install-more footer -->
      <div class="border-t pt-3 text-xs text-muted-foreground">
        Don't see a repo?
        <a
          v-if="data.install_url"
          :href="data.install_url"
          target="_blank"
          rel="noopener"
          class="text-primary hover:underline"
        >
          Install the App on another account or grant more repositories →
        </a>
        <template v-else>
          Install the App on another account or grant it more repositories on GitHub.
        </template>
      </div>
    </div>

      <DialogFooter>
        <Button variant="ghost" @click="emit('update:open', false)">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
