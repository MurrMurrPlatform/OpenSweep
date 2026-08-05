<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  Dialog,
  DialogBody,
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
import type { LLMProvider, LLMProviderEndpointPreset, LLMProviderKindMeta } from '@/types/api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  /** A provider was created (health check runs in the background). */
  connected: []
}>()

const store = useLLMProviderStore()
const toast = useToast()

/** One picker tile: a plain kind (Claude Code), or a kind + endpoint preset
 *  (opencode × OMLX / LM Studio / Ollama / Azure Foundry / generic API). */
interface Tile {
  key: string
  label: string
  tagline: string
  needsApiKey: boolean
  transport: string
  meta: LLMProviderKindMeta
  preset: LLMProviderEndpointPreset | null
}

const selected = ref<Tile | null>(null)
const credential = ref('')
const baseUrl = ref('')
const model = ref('')
/** Empty = platform default (100). Only relevant once more than one provider
 *  is connected — lower runs first when the active provider is unusable. */
const fallbackPriority = ref('')
const submitting = ref(false)

watch(() => props.open, (val) => {
  if (!val) return
  selected.value = null
  fallbackPriority.value = ''
  store.fetchCatalog().catch(() => {})
})

/** Picker tiles: featured kinds in platform order, kinds with endpoint
 *  presets flattened to one tile per preset. API-key tiles split off under
 *  their own divider. */
const tiles = computed<Tile[]>(() =>
  store.catalog
    .filter((c) => (c.featured ?? 0) > 0)
    .sort((a, b) => (a.featured ?? 0) - (b.featured ?? 0))
    .flatMap((meta): Tile[] => {
      const presets = meta.endpoint_presets
      if (!presets?.length) {
        return [{
          key: meta.kind,
          label: meta.default_label || meta.display_name,
          tagline: meta.tagline || '',
          needsApiKey: !!meta.needs_api_key,
          transport: meta.transport,
          meta,
          preset: null,
        }]
      }
      return presets.map((preset) => ({
        key: `${meta.kind}:${preset.key}`,
        label: preset.label,
        tagline: preset.tagline || meta.tagline || '',
        needsApiKey: !!preset.needs_api_key,
        transport: meta.transport,
        meta,
        preset,
      }))
    }),
)
const agentTiles = computed(() => tiles.value.filter((t) => !t.needsApiKey))
const apiTiles = computed(() => tiles.value.filter((t) => t.needsApiKey))

function tileIcon(tile: Tile) {
  if (tile.needsApiKey) return KeyRound
  return tile.transport.startsWith('local CLI') ? TerminalSquare : Server
}

function pick(tile: Tile) {
  selected.value = tile
  credential.value = ''
  baseUrl.value = tile.preset ? tile.preset.base_url : (tile.meta.default_base_url || '')
  model.value = (tile.preset?.default_model ?? tile.meta.default_model) || ''
}

/** The API key is required for hosted presets (they answer 401 without one);
 *  everywhere else the catalog decides (opencode: optional — no key needed
 *  for a local server). */
const credentialOptional = computed(() => {
  const t = selected.value
  if (!t) return false
  if (t.preset) return !t.preset.needs_api_key
  return t.meta.credential_optional === true
})

const setupSteps = computed(
  () => selected.value?.preset?.setup_steps ?? selected.value?.meta.setup_steps ?? [],
)

const canSubmit = computed(() => {
  const t = selected.value
  if (!t) return false
  if (t.meta.needs_base_url && !baseUrl.value.trim()) return false
  if (t.meta.needs_credential && !credentialOptional.value && !credential.value.trim()) return false
  return true
})

async function connect() {
  const t = selected.value
  if (!t) return
  submitting.value = true
  try {
    // Send only what the user actually provided — the backend fills label,
    // model, URL, and CLI wiring from the platform catalog. A preset tile
    // additionally pins its label and endpoint-specific extra_args (e.g.
    // Azure's @ai-sdk/openai package override).
    const payload: Partial<LLMProvider> & { credential_secret?: string } = { kind: t.meta.kind }
    if (t.preset) payload.label = t.preset.label
    if (t.meta.needs_base_url) payload.base_url = baseUrl.value.trim()
    if (model.value.trim()) payload.model = model.value.trim()
    if (t.preset?.extra_args) payload.extra_args = t.preset.extra_args
    if (credential.value.trim()) payload.credential_secret = credential.value.trim()
    const priority = Number.parseInt(fallbackPriority.value, 10)
    if (Number.isFinite(priority) && priority > 0) payload.fallback_priority = priority
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

        <DialogBody class="flex flex-col gap-4">
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button
              v-for="tile in agentTiles"
              :key="tile.key"
              type="button"
              class="card-interactive flex items-start gap-3 rounded-lg border bg-card p-3 text-left hover:border-primary/50"
              @click="pick(tile)"
            >
              <component :is="tileIcon(tile)" class="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span class="min-w-0">
                <span class="block font-medium">{{ tile.label }}</span>
                <span class="block text-xs text-muted-foreground">{{ tile.tagline }}</span>
              </span>
            </button>
          </div>

          <template v-if="apiTiles.length">
            <div class="flex items-center gap-3">
              <div class="h-px flex-1 bg-border" />
              <span class="text-xs uppercase tracking-wider text-muted-foreground">Hosted APIs (via opencode)</span>
              <div class="h-px flex-1 bg-border" />
            </div>
            <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <button
                v-for="tile in apiTiles"
                :key="tile.key"
                type="button"
                class="card-interactive flex items-start gap-3 rounded-lg border bg-card p-3 text-left hover:border-primary/50"
                @click="pick(tile)"
              >
                <component :is="tileIcon(tile)" class="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                <span class="min-w-0">
                  <span class="block font-medium">{{ tile.label }}</span>
                  <span class="block text-xs text-muted-foreground">{{ tile.tagline }}</span>
                </span>
              </button>
            </div>
          </template>
        </DialogBody>

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
            Connect {{ selected.label }}
          </DialogTitle>
          <DialogDescription>{{ selected.tagline }}</DialogDescription>
        </DialogHeader>

        <DialogBody class="flex flex-col gap-3">
          <div v-if="selected.meta.needs_base_url" class="flex flex-col gap-1.5">
            <Label for="connect-base-url">Server URL</Label>
            <Input id="connect-base-url" v-model="baseUrl" class="font-mono" />
            <span class="text-xs text-muted-foreground">
              Reachable from Docker — your host is <code>host.docker.internal</code>.
            </span>
          </div>

          <div v-if="selected.meta.needs_base_url" class="flex flex-col gap-1.5">
            <Label for="connect-model">Model</Label>
            <Input id="connect-model" v-model="model" />
          </div>

          <div v-if="selected.meta.needs_credential" class="flex flex-col gap-1.5">
            <Label for="connect-credential">
              {{ selected.preset ? 'API key' : (selected.meta.credential_label || 'Credential') }}
              <span v-if="credentialOptional" class="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Textarea
              id="connect-credential"
              v-model="credential"
              :placeholder="selected.meta.credential_placeholder"
              :rows="2"
              class="font-mono text-xs"
            />
            <div v-if="setupSteps.length" class="rounded-md bg-muted p-3 text-xs text-muted-foreground">
              <div class="mb-1 font-medium text-foreground">How to get this</div>
              <ol class="list-decimal space-y-1 pl-5">
                <li v-for="(step, i) in setupSteps" :key="i">{{ step }}</li>
              </ol>
            </div>
          </div>

          <div class="flex flex-col gap-1.5">
            <Label for="connect-fallback-priority">
              Fallback priority <span class="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Input id="connect-fallback-priority" v-model="fallbackPriority" type="number" min="1" class="max-w-32" placeholder="100" />
            <span class="text-xs text-muted-foreground">
              When another connected provider is quota-exhausted or unusable, providers with a
              lower number take over first. Leave blank for the platform default.
            </span>
          </div>
        </DialogBody>

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
