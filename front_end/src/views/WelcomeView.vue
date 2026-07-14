<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Toaster } from '@/components/ui/sonner'
import ConnectProviderDialog from '@/components/llmProviders/ConnectProviderDialog.vue'
import ConnectRepositoryDialog from '@/components/repositories/ConnectRepositoryDialog.vue'
import { useCurrentUserStore } from '@/stores/currentUserStore'
import { useOrganizationStore } from '@/stores/organizationStore'
import { useLLMProviderStore } from '@/stores/llmProviderStore'
import { useGithubAppStore } from '@/stores/githubAppStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import OpenSweepLogo from '@/components/branding/OpenSweepLogo.vue'
import {
  Building2, Check, Cpu, ExternalLink, FolderGit2, Github, RefreshCw,
} from 'lucide-vue-next'

/**
 * Self-contained onboarding wizard. Every step happens on this page —
 * nothing navigates into the app, so the router guard keeps unfinished
 * users here and the flow survives round-trips to GitHub (new tabs come
 * back to /welcome and the step state re-derives from backend status).
 * Only "Finish setup" marks the user onboarded, and it stays disabled until
 * the required steps (org name, LLM provider, GitHub) are all done.
 */
const currentUser = useCurrentUserStore()
const org = useOrganizationStore()
const llm = useLLMProviderStore()
const githubApp = useGithubAppStore()
const repos = useRepositoryStore()
const toast = useToast()
const router = useRouter()

// ── Step 1 · organization name ─────────────────────────────────────────────
const orgName = ref('')
const savingName = ref(false)
const nameSaved = ref(false)
const profileOrgName = ref('')

/** Provisioning names fresh orgs "<name>'s organization" (backend
 *  personal_org_name()) — that default doesn't count as a chosen name, but a
 *  previously saved custom name survives reloads. */
const defaultOrgName = computed(() => {
  const base = currentUser.displayName.trim() || (currentUser.email.split('@')[0] || '').trim()
  return base ? `${base}'s organization` : 'My organization'
})
const nameDone = computed(() =>
  nameSaved.value || (!!profileOrgName.value && profileOrgName.value !== defaultOrgName.value),
)

async function saveName() {
  savingName.value = true
  try {
    await org.rename(orgName.value.trim())
    nameSaved.value = true
    toast.success('Organization named', orgName.value.trim())
  } catch (e: any) {
    toast.error('Rename failed', e.detail || e.message)
  } finally {
    savingName.value = false
  }
}

// ── Step 2 · LLM provider ──────────────────────────────────────────────────
const providerDialogOpen = ref(false)
const providerDone = computed(() => llm.status?.configured || llm.list.length > 0)

function openProviderDialog() {
  providerDialogOpen.value = true
}

// ── Step 3 · GitHub ────────────────────────────────────────────────────────
// Self-serve default: paste a fine-grained access token. When the operator
// has provisioned the GitHub App (scripts/github-app-setup.sh), installing
// it on an account is the alternative path.
const refreshing = ref(false)
const githubDone = computed(() => githubApp.status.connected)
const appConfigured = computed(() => !!(githubApp.status.slug || githubApp.status.app_id))
const githubToken = ref('')
const addingToken = ref(false)

async function addGithubToken() {
  const value = githubToken.value.trim()
  if (!value) return
  addingToken.value = true
  try {
    const conn = await githubApp.addPatConnection(value)
    githubToken.value = ''
    toast.success('GitHub connected', conn.account ? `Authenticated as ${conn.account}.` : undefined)
  } catch (e: any) {
    toast.error('Token rejected', e.detail || e.message)
  } finally {
    addingToken.value = false
  }
}

function openInstallations() {
  if (githubApp.status.install_url) window.open(githubApp.status.install_url, '_blank')
}

// ── Step 4 · first repository ──────────────────────────────────────────────
const connectRepoOpen = ref(false)
const repoDone = computed(() => repos.list.length > 0)

function onRepoConnected() {
  repos.fetchAll().catch(() => {})
}

// ── Shared status / resume ─────────────────────────────────────────────────
/** Re-derive every step's done-state from the backend. Best-effort: a failed
 *  probe leaves that step undone rather than breaking the wizard. */
async function refreshStatuses() {
  refreshing.value = true
  await Promise.allSettled([
    llm.fetchStatus(),
    llm.fetchAll(),
    githubApp.fetchStatus(),
    repos.fetchAll(),
  ])
  refreshing.value = false
}

/** GitHub steps finish in other tabs — refresh when the user comes back. */
function onWindowFocus() {
  refreshStatuses()
}

onMounted(async () => {
  window.addEventListener('focus', onWindowFocus)

  refreshStatuses()
  try {
    const profile = await currentUser.loadProfile()
    orgName.value = profile.org.name
    profileOrgName.value = profile.org.name
  } catch {
    // Backend unreachable — the input just starts empty.
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('focus', onWindowFocus)
})

// ── Finish ─────────────────────────────────────────────────────────────────
const finishing = ref(false)

/** Steps 1–3 are hard requirements — an org without a name, provider, or
 *  GitHub connection can't do anything useful in the app. Step 4 stays
 *  optional. */
const missing = computed(() => {
  const m: string[] = []
  if (!nameDone.value) m.push('name your organization')
  if (!providerDone.value) m.push('connect an LLM provider')
  if (!githubDone.value) m.push('connect GitHub')
  return m
})
const canFinish = computed(() => missing.value.length === 0)

// ── Stepper presentation state ─────────────────────────────────────────────
const stepDone = computed(() => [nameDone.value, providerDone.value, githubDone.value, repoDone.value])
const requiredDone = computed(() => stepDone.value.slice(0, 3).filter(Boolean).length)
/** The first incomplete step gets the highlighted "you are here" treatment. */
const activeStep = computed(() => stepDone.value.findIndex((done) => !done))

type StepState = 'done' | 'active' | 'pending'
function stepState(i: number): StepState {
  if (stepDone.value[i]) return 'done'
  return i === activeStep.value ? 'active' : 'pending'
}
const bubbleClass: Record<StepState, string> = {
  done: 'border-transparent bg-good/15 text-good',
  active: 'border-transparent bg-primary text-primary-foreground shadow-sm',
  pending: 'border-border bg-card text-muted-foreground',
}
const cardClass: Record<StepState, string> = {
  done: '',
  active: 'border-primary/40 ring-1 ring-primary/20',
  pending: 'opacity-80',
}

/** Mark onboarded, land in the workspace list. Until this succeeds the
 *  router guard keeps every navigation on /welcome. */
async function finish() {
  if (!canFinish.value) return
  finishing.value = true
  try {
    await currentUser.setOnboarded(true)
  } catch (e: any) {
    toast.error('Could not save', e.detail || e.message)
    finishing.value = false
    return
  }
  finishing.value = false
  router.push('/repositories')
}
</script>

<template>
  <div class="min-h-screen overflow-auto bg-muted/40 text-foreground">
    <div class="stagger-children mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10 sm:px-6 sm:py-14">
      <div class="flex flex-col items-center gap-4 text-center">
        <OpenSweepLogo class="h-7" />
        <div>
          <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">Let's set up your workspace</h1>
          <p class="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
            Name your organization, connect an LLM provider and GitHub, and
            you're ready to review code. You can leave and come back —
            progress is picked up where you left off.
          </p>
        </div>
        <!-- Required-steps progress -->
        <div class="w-full max-w-xs">
          <div class="h-1 overflow-hidden rounded-full bg-border">
            <div
              class="h-full rounded-full bg-primary transition-all duration-500"
              :style="{ width: `${(requiredDone / 3) * 100}%` }"
            />
          </div>
          <p class="mt-1.5 text-xs text-muted-foreground">
            {{ requiredDone }} of 3 required steps done
          </p>
        </div>
      </div>

      <ol class="flex flex-col">
        <!-- 1 · Organization name -->
        <li class="flex gap-3 sm:gap-4">
          <div class="flex flex-col items-center">
            <div
              class="flex size-8 shrink-0 items-center justify-center rounded-full border text-sm font-semibold transition-colors"
              :class="bubbleClass[stepState(0)]"
            >
              <Check v-if="nameDone" class="size-4" />
              <template v-else>1</template>
            </div>
            <div class="w-px grow bg-border" />
          </div>
          <Card class="mb-4 min-w-0 flex-1 transition-colors" :class="cardClass[stepState(0)]">
            <CardHeader class="p-4">
              <div class="flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 text-sm font-semibold">
                  <Building2 class="size-4 text-muted-foreground" /> Name your organization
                </div>
                <Badge v-if="nameDone" variant="success"><Check class="size-3" /> Done</Badge>
              </div>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <div class="flex flex-wrap items-center gap-2">
                <Input v-model="orgName" class="min-w-0 flex-1" placeholder="Organization name" />
                <Button class="shrink-0" :loading="savingName" :disabled="!orgName.trim()" @click="saveName">
                  Save
                </Button>
              </div>
            </CardContent>
          </Card>
        </li>

        <!-- 2 · LLM provider -->
        <li class="flex gap-3 sm:gap-4">
          <div class="flex flex-col items-center">
            <div
              class="flex size-8 shrink-0 items-center justify-center rounded-full border text-sm font-semibold transition-colors"
              :class="bubbleClass[stepState(1)]"
            >
              <Check v-if="providerDone" class="size-4" />
              <template v-else>2</template>
            </div>
            <div class="w-px grow bg-border" />
          </div>
          <Card class="mb-4 min-w-0 flex-1 transition-colors" :class="cardClass[stepState(1)]">
            <CardHeader class="p-4">
              <div class="flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 text-sm font-semibold">
                  <Cpu class="size-4 text-muted-foreground" /> Connect an LLM provider
                </div>
                <Badge v-if="providerDone" variant="success"><Check class="size-3" /> Done</Badge>
              </div>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <p class="min-w-0 flex-1 basis-56 text-sm text-muted-foreground">
                  <template v-if="providerDone">
                    {{ llm.list.length }} provider{{ llm.list.length === 1 ? '' : 's' }} configured.
                    You can fine-tune them later under Admin → LLM providers.
                  </template>
                  <template v-else-if="currentUser.isAdmin">
                    Runs execute on your organization's own LLM provider — bring a
                    Claude/OpenAI subscription, an API key, or a local model.
                  </template>
                  <template v-else>
                    Runs execute on your organization's own LLM provider. Ask an
                    organization admin to configure one.
                  </template>
                </p>
                <Button
                  v-if="currentUser.isAdmin"
                  class="shrink-0"
                  variant="outline"
                  @click="openProviderDialog"
                >
                  <Cpu /> {{ providerDone ? 'Add another' : 'Add provider' }}
                </Button>
              </div>
            </CardContent>
          </Card>
        </li>

        <!-- 3 · GitHub App -->
        <li class="flex gap-3 sm:gap-4">
          <div class="flex flex-col items-center">
            <div
              class="flex size-8 shrink-0 items-center justify-center rounded-full border text-sm font-semibold transition-colors"
              :class="bubbleClass[stepState(2)]"
            >
              <Check v-if="githubDone" class="size-4" />
              <template v-else>3</template>
            </div>
            <div class="w-px grow bg-border" />
          </div>
          <Card class="mb-4 min-w-0 flex-1 transition-colors" :class="cardClass[stepState(2)]">
            <CardHeader class="p-4">
              <div class="flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 text-sm font-semibold">
                  <Github class="size-4 text-muted-foreground" /> Connect GitHub
                </div>
                <Badge v-if="githubDone" variant="success"><Check class="size-3" /> Done</Badge>
              </div>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <!-- Connected (token and/or App): next step is picking repos. -->
              <div v-if="githubDone" class="flex flex-wrap items-center justify-between gap-3">
                <p class="min-w-0 flex-1 basis-56 text-sm text-muted-foreground">
                  GitHub is connected<template v-if="githubApp.status.pat_connections?.length">
                    via access token<template v-if="githubApp.status.pat_connections![0].account">
                      (<span class="font-medium text-foreground">{{ githubApp.status.pat_connections![0].account }}</span>)</template></template><template v-else-if="appConfigured">
                    via the <span class="font-medium text-foreground">{{ githubApp.status.slug || `#${githubApp.status.app_id}` }}</span> App</template>.
                  Pick repositories in the next step; manage connections under Settings → GitHub.
                </p>
                <Button
                  v-if="githubApp.status.install_url"
                  class="shrink-0"
                  variant="outline"
                  @click="openInstallations"
                >
                  <ExternalLink /> Install the App on an account
                </Button>
              </div>

              <!-- Not connected: paste a token (self-serve), or install the App. -->
              <div v-else class="space-y-3">
                <p class="text-sm text-muted-foreground">
                  Paste a
                  <a
                    href="https://github.com/settings/personal-access-tokens/new"
                    target="_blank"
                    rel="noopener"
                    class="text-primary hover:underline"
                  >fine-grained access token</a>
                  scoped to the repositories OpenSweep should see (Contents + Pull requests
                  read/write). It's stored encrypted and never shown again.
                </p>
                <div class="flex flex-wrap items-center gap-2">
                  <Input
                    v-model="githubToken"
                    type="password"
                    autocomplete="off"
                    class="min-w-0 flex-1 basis-56 font-mono"
                    placeholder="github_pat_…"
                    @keydown.enter="addGithubToken"
                  />
                  <Button class="shrink-0" :loading="addingToken" :disabled="!githubToken.trim()" @click="addGithubToken">
                    <Github /> Connect token
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    class="shrink-0"
                    :loading="refreshing"
                    @click="refreshStatuses"
                  >
                    <RefreshCw />
                  </Button>
                </div>
                <p v-if="appConfigured" class="text-xs text-muted-foreground">
                  Prefer the GitHub App? It's configured for this instance —
                  <button class="text-primary hover:underline" @click="openInstallations">install it on your account</button>
                  instead (adds automatic webhooks).
                </p>
                <p v-else-if="currentUser.isPlatformAdmin" class="text-xs text-muted-foreground">
                  Tip: <code class="rounded bg-muted px-1 py-0.5">GITHUB_TOKEN</code> in the server env
                  auto-connects on your first login, and
                  <code class="rounded bg-muted px-1 py-0.5">scripts/github-app-setup.sh</code>
                  provisions the full GitHub App (webhooks + per-repo tokens).
                </p>
              </div>
            </CardContent>
          </Card>
        </li>

        <!-- 4 · First repository -->
        <li class="flex gap-3 sm:gap-4">
          <div class="flex flex-col items-center">
            <div
              class="flex size-8 shrink-0 items-center justify-center rounded-full border text-sm font-semibold transition-colors"
              :class="bubbleClass[stepState(3)]"
            >
              <Check v-if="repoDone" class="size-4" />
              <template v-else>4</template>
            </div>
          </div>
          <Card class="min-w-0 flex-1 transition-colors" :class="cardClass[stepState(3)]">
            <CardHeader class="p-4">
              <div class="flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 text-sm font-semibold">
                  <FolderGit2 class="size-4 text-muted-foreground" /> Register your first repository
                  <span class="font-normal text-muted-foreground">— optional</span>
                </div>
                <Badge v-if="repoDone" variant="success"><Check class="size-3" /> Done</Badge>
              </div>
            </CardHeader>
            <CardContent class="p-4 pt-0">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <p class="min-w-0 flex-1 basis-56 text-sm text-muted-foreground">
                  <template v-if="repoDone">
                    {{ repos.list.length }} workspace{{ repos.list.length === 1 ? '' : 's' }} connected — you're ready to go.
                  </template>
                  <template v-else>
                    Pick a repo the App can access and register it as a OpenSweep workspace.
                  </template>
                </p>
                <Button class="shrink-0" variant="outline" @click="connectRepoOpen = true">
                  <FolderGit2 /> Connect a repository
                </Button>
              </div>
            </CardContent>
          </Card>
        </li>
      </ol>

      <div class="flex flex-col items-center gap-2">
        <Button size="lg" class="w-full sm:w-auto sm:min-w-56" :loading="finishing" :disabled="!canFinish" @click="finish">
          Finish setup
        </Button>
        <p v-if="!canFinish" class="text-xs text-muted-foreground">
          Still to do: {{ missing.join(' · ') }}
        </p>
      </div>
    </div>

    <ConnectProviderDialog v-model:open="providerDialogOpen" />

    <ConnectRepositoryDialog
      v-model:open="connectRepoOpen"
      @connected="onRepoConnected"
    />

    <!-- /welcome renders outside ShellLayout, so it hosts its own Toaster. -->
    <Toaster :rich-colors="true" close-button />
  </div>
</template>
