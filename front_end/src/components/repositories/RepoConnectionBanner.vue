<script setup lang="ts">
/**
 * Warns when a repository cannot deliver work, and offers the fix inline.
 *
 * A repo's git credential outlives its registration — tokens get rotated, App
 * installations get reinstalled — and when the connection it was registered
 * through goes away, the repo is left pointing at nothing. That used to surface
 * only as a failed push at the END of a run. This says it up front and links to
 * a working credential in two clicks.
 *
 * Silent when the state is `ok` or `unknown`: `unknown` means the check itself
 * could not be completed, and crying wolf on an inconclusive probe would teach
 * people to ignore the banner.
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { AlertTriangle, Link2, Plus, RefreshCw } from 'lucide-vue-next'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { ConnectionCandidateDTO, RepoConnectionDTO } from '@/types/api'

const props = defineProps<{ repoUid: string }>()
const emit = defineEmits<{ resolved: [] }>()

const store = useRepositoryStore()
const toast = useToast()
const router = useRouter()

function goToConnections() {
  fixOpen.value = false
  void router.push({ name: 'organization-settings' })
}

const connection = ref<RepoConnectionDTO | null>(null)
const loading = ref(false)
const fixOpen = ref(false)
const linking = ref<string | null>(null)

async function load() {
  if (!props.repoUid) return
  loading.value = true
  try {
    connection.value = await store.getConnection(props.repoUid)
  } catch {
    // Best-effort: a failed health check must not break the page it sits on.
    connection.value = null
  } finally {
    loading.value = false
  }
}

watch(() => props.repoUid, () => void load(), { immediate: true })

const broken = computed(
  () => connection.value?.state === 'disconnected' || connection.value?.state === 'denied',
)

const title = computed(() =>
  connection.value?.state === 'denied'
    ? 'This repository’s credential cannot push'
    : 'This repository is not connected to GitHub',
)

/** Only the ones that would actually change something. */
const linkable = computed<ConnectionCandidateDTO[]>(
  () => connection.value?.candidates.filter((c) => !c.is_current) ?? [],
)

function candidateLabel(c: ConnectionCandidateDTO) {
  const kind = c.kind === 'app' ? 'GitHub App' : 'Token'
  return `${kind} · ${c.account || c.uid.slice(0, 8)}`
}

async function link(c: ConnectionCandidateDTO) {
  if (linking.value) return
  linking.value = c.uid
  try {
    connection.value = await store.linkConnection(props.repoUid, c.uid)
    if (connection.value.state === 'ok') {
      fixOpen.value = false
      toast.success('Repository connected', `Delivering through ${candidateLabel(c)}.`)
      emit('resolved')
    } else {
      // Linked, but still not able to push — the credential is real, its
      // GitHub permissions are not. Keep the dialog open with the new reason.
      toast.warn('Linked, but still cannot push', connection.value.reason)
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Could not link that connection', msg)
  } finally {
    linking.value = null
  }
}
</script>

<template>
  <div
    v-if="broken"
    class="rounded-xl border border-destructive/40 bg-destructive/5 p-4"
    role="alert"
  >
    <div class="flex flex-wrap items-start gap-3">
      <AlertTriangle class="mt-0.5 size-5 shrink-0 text-destructive" />
      <div class="min-w-0 flex-1 space-y-1">
        <p class="text-sm font-medium text-foreground">{{ title }}</p>
        <p class="text-sm text-muted-foreground">{{ connection?.reason }}</p>
      </div>
      <div class="flex items-center gap-2">
        <Button variant="outline" size="sm" :loading="loading" @click="load()">
          <RefreshCw /> Re-check
        </Button>
        <Button size="sm" @click="fixOpen = true">
          <Link2 /> Fix connection
        </Button>
      </div>
    </div>
  </div>

  <Dialog v-model:open="fixOpen">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Connect this repository</DialogTitle>
        <DialogDescription>
          Pick the token or GitHub App installation this repository should deliver
          through. The credential is verified before it is saved.
        </DialogDescription>
      </DialogHeader>

      <p v-if="connection?.reason" class="rounded-lg bg-muted p-3 text-xs text-muted-foreground">
        {{ connection.reason }}
      </p>

      <div v-if="linkable.length" class="space-y-2">
        <button
          v-for="c in linkable"
          :key="c.uid"
          type="button"
          class="flex w-full items-center justify-between gap-3 rounded-lg border p-3 text-left transition-colors hover:bg-accent disabled:opacity-60"
          :disabled="!!linking"
          @click="link(c)"
        >
          <span class="min-w-0">
            <span class="block truncate text-sm font-medium">{{ candidateLabel(c) }}</span>
            <span v-if="c.installation_id" class="text-xs text-muted-foreground">
              installation {{ c.installation_id }}
            </span>
          </span>
          <Badge v-if="linking === c.uid" variant="secondary" class="px-1.5 text-[10px]">
            Linking…
          </Badge>
          <Link2 v-else class="size-4 shrink-0 text-muted-foreground" />
        </button>
      </div>
      <p v-else class="text-sm text-muted-foreground">
        No other credential is connected to this organization yet — add a GitHub token
        or install the GitHub App, then come back here to link it.
      </p>

      <DialogFooter class="sm:justify-between">
        <Button variant="outline" size="sm" @click="goToConnections()">
          <Plus /> Add a token or App
        </Button>
        <Button variant="ghost" size="sm" @click="fixOpen = false">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
