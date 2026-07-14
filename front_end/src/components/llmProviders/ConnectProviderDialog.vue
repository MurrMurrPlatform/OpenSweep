<script setup lang="ts">
import { computed, ref, watch } from 'vue'
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
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useLLMProviderStore } from '@/stores/llmProviderStore'
import { useToast } from '@/composables/useToast'
import { ChevronLeft, KeyRound, Server, TerminalSquare } from 'lucide-vue-next'
import type { LLMProvider, LLMProviderKindMeta } from '@/types/api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  /** A provider was created (health check runs in the background). */
  connected: []
}>()

const store = useLLMProviderStore()
const toast = useToast()

const selected = ref<LLMProviderKindMeta | null>(null)
const credential = ref('')
const baseUrl = ref('')
const model = ref('')
const submitting = ref(false)

watch(() => props.open, (val) => {
  if (!val) return
  selected.value = null
  store.fetchCatalog().catch(() => {})
})

/** Picker tiles: featured kinds in platform order; API-key kinds split off
 *  under their own divider. Everything else (aider, custom) is ops-only. */
const featured = computed(() =>
  store.catalog
    .filter((c) => (c.featured ?? 0) > 0)
    .sort((a, b) => (a.featured ?? 0) - (b.featured ?? 0)),
)
const agentTiles = computed(() => featured.value.filter((c) => !c.needs_api_key))
const apiTiles = computed(() => featured.value.filter((c) => c.needs_api_key))

function tileIcon(meta: LLMProviderKindMeta) {
  if (meta.needs_api_key) return KeyRound
  return meta.transport.startsWith('local CLI') ? TerminalSquare : Server
}

function pick(meta: LLMProviderKindMeta) {
  selected.value = meta
  credential.value = ''
  baseUrl.value = meta.default_base_url || ''
  model.value = meta.default_model || ''
}

/** Codex can fall back to the host ~/.codex bind-mount — token is optional. */
const credentialOptional = computed(() => selected.value?.kind === 'codex_subscription')

const canSubmit = computed(() => {
  const m = selected.value
  if (!m) return false
  if (m.needs_base_url && !baseUrl.value.trim()) return false
  if (m.needs_credential && !credentialOptional.value && !credential.value.trim()) return false
  return true
})

async function connect() {
  const m = selected.value
  if (!m) return
  submitting.value = true
  try {
    // Send only what the user actually provided — the backend fills label,
    // model, URL, and CLI wiring from the platform catalog.
    const payload: Partial<LLMProvider> & { credential_secret?: string } = { kind: m.kind }
    if (m.needs_base_url) payload.base_url = baseUrl.value.trim()
    if (model.value.trim()) payload.model = model.value.trim()
    if (credential.value.trim()) payload.credential_secret = credential.value.trim()
    const created = await store.create(payload)
    toast.success('Provider connected', created.label)
    emit('connected')
    emit('update:open', false)
    // Health check in the background — surface only problems.
    store.check(created.uid).then((p) => {
      if (p.last_health_status !== 'ok') {
        toast.warn(`${p.label} health check`, p.last_health_detail || p.last_health_status)
      }
    }).catch(() => {})
  } catch (e: any) {
    toast.error('Connect failed', e.detail || e.message)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <!-- Step 1 · pick what you use -->
      <template v-if="!selected">
        <DialogHeader>
          <DialogTitle>Connect a coding agent</DialogTitle>
          <DialogDescription>
            Pick what you already use — OpenSweep handles the wiring.
          </DialogDescription>
        </DialogHeader>

        <div class="flex max-h-[60vh] flex-col gap-4 overflow-y-auto">
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button
              v-for="meta in agentTiles"
              :key="meta.kind"
              type="button"
              class="card-interactive flex items-start gap-3 rounded-lg border bg-card p-3 text-left hover:border-primary/50"
              @click="pick(meta)"
            >
              <component :is="tileIcon(meta)" class="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span class="min-w-0">
                <span class="block font-medium">{{ meta.default_label || meta.display_name }}</span>
                <span class="block text-xs text-muted-foreground">{{ meta.tagline }}</span>
              </span>
            </button>
          </div>

          <template v-if="apiTiles.length">
            <div class="flex items-center gap-3">
              <div class="h-px flex-1 bg-border" />
              <span class="text-xs uppercase tracking-wider text-muted-foreground">Pay-per-token APIs</span>
              <div class="h-px flex-1 bg-border" />
            </div>
            <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <button
                v-for="meta in apiTiles"
                :key="meta.kind"
                type="button"
                class="card-interactive flex items-start gap-3 rounded-lg border bg-card p-3 text-left hover:border-primary/50"
                @click="pick(meta)"
              >
                <component :is="tileIcon(meta)" class="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                <span class="min-w-0">
                  <span class="block font-medium">{{ meta.default_label || meta.display_name }}</span>
                  <span class="block text-xs text-muted-foreground">{{ meta.tagline }}</span>
                </span>
              </button>
            </div>
          </template>
        </div>

        <DialogFooter>
          <Button variant="ghost" @click="emit('update:open', false)">Cancel</Button>
        </DialogFooter>
      </template>

      <!-- Step 2 · the one or two things we actually need -->
      <template v-else>
        <DialogHeader>
          <DialogTitle class="flex items-center gap-2">
            <button type="button" class="text-muted-foreground hover:text-foreground" @click="selected = null">
              <ChevronLeft class="h-5 w-5" />
              <span class="sr-only">Back to picker</span>
            </button>
            Connect {{ selected.default_label || selected.display_name }}
          </DialogTitle>
          <DialogDescription>{{ selected.tagline }}</DialogDescription>
        </DialogHeader>

        <div class="flex max-h-[60vh] flex-col gap-3 overflow-y-auto">
          <div v-if="selected.needs_base_url" class="flex flex-col gap-1.5">
            <Label for="connect-base-url">Server URL</Label>
            <Input id="connect-base-url" v-model="baseUrl" class="font-mono" />
            <span class="text-xs text-muted-foreground">
              Reachable from Docker — your host is <code>host.docker.internal</code>.
            </span>
          </div>

          <div v-if="selected.needs_base_url" class="flex flex-col gap-1.5">
            <Label for="connect-model">Model</Label>
            <Input id="connect-model" v-model="model" />
          </div>

          <div v-if="selected.needs_credential" class="flex flex-col gap-1.5">
            <Label for="connect-credential">
              {{ selected.credential_label || 'Credential' }}
              <span v-if="credentialOptional" class="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Textarea
              id="connect-credential"
              v-model="credential"
              :placeholder="selected.credential_placeholder"
              :rows="selected.kind === 'codex_subscription' ? 5 : 2"
              class="font-mono text-xs"
            />
            <div v-if="selected.setup_steps?.length" class="rounded-md bg-muted p-3 text-xs text-muted-foreground">
              <div class="mb-1 font-medium text-foreground">How to get this</div>
              <ol class="list-decimal space-y-1 pl-5">
                <li v-for="(step, i) in selected.setup_steps" :key="i">{{ step }}</li>
              </ol>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" @click="selected = null">Back</Button>
          <Button :disabled="!canSubmit || submitting" :loading="submitting" @click="connect">
            Connect
          </Button>
        </DialogFooter>
      </template>
    </DialogContent>
  </Dialog>
</template>
