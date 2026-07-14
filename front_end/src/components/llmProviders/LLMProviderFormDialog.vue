<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
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
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import type { LLMProvider, LLMProviderKindMeta } from '@/types/api'

// Edit-only: new providers go through ConnectProviderDialog. The kind is
// fixed after creation (delete + reconnect to switch); transport internals
// (CLI template, extra args, api-key env) are platform-owned and never shown.
const props = defineProps<{
  open: boolean
  provider: LLMProvider | null
  catalog: LLMProviderKindMeta[]
  submitting?: boolean
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  submit: [value: Partial<LLMProvider>]
}>()

const form = reactive({
  label: '',
  model: '',
  base_url: '',
  enabled: true,
  active: false,
  credential_secret: '',          // write-only; never pre-filled
  clear_credential: false,         // explicit "remove the stored secret" toggle
})

watch(() => props.open, (val) => {
  if (!val || !props.provider) return
  const p = props.provider
  form.label = p.label
  form.model = p.model || ''
  form.base_url = p.base_url || ''
  form.enabled = p.enabled
  form.active = p.active
  form.credential_secret = ''
  form.clear_credential = false
})

const meta = computed(() => props.catalog.find(c => c.kind === props.provider?.kind))

function onSubmit() {
  const payload: Partial<LLMProvider> & { credential_secret?: string } = {
    label: form.label,
    model: form.model,
    base_url: form.base_url,
    enabled: form.enabled,
    active: form.active,
  }
  // Only send credential_secret if the user typed something OR asked to clear.
  if (form.credential_secret) {
    payload.credential_secret = form.credential_secret
  } else if (form.clear_credential) {
    payload.credential_secret = ''
  }
  emit('submit', payload)
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Edit provider · {{ provider?.label }}</DialogTitle>
        <DialogDescription>
          {{ meta?.display_name || provider?.kind }}
        </DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-3 max-h-[60vh] overflow-y-auto -mx-6 px-6">
        <div class="flex flex-col gap-1.5">
          <Label for="provider-label">Label</Label>
          <Input id="provider-label" v-model="form.label" />
        </div>

        <div class="flex flex-col gap-1.5">
          <Label for="provider-model">Model</Label>
          <Input id="provider-model" v-model="form.model" :placeholder="meta?.default_model" />
        </div>

        <div v-if="meta?.needs_base_url" class="flex flex-col gap-1.5">
          <Label for="provider-base-url">Server URL</Label>
          <Input id="provider-base-url" v-model="form.base_url" class="font-mono" :placeholder="meta?.default_base_url" />
        </div>

        <!-- Credential section — kind-aware. -->
        <div v-if="meta?.needs_credential" class="rounded-md border bg-muted p-3 flex flex-col gap-2">
          <div class="flex items-center justify-between">
            <span class="font-semibold text-sm">{{ meta.credential_label || 'Credential' }}</span>
            <span class="text-xs flex items-center gap-2">
              <span
                :class="(provider?.has_credential_secret && !form.clear_credential) ? 'text-good' : 'text-muted-foreground'"
              >
                {{ (provider?.has_credential_secret && !form.clear_credential) ? '✓ set' : 'not set' }}
              </span>
              <button
                v-if="provider?.has_credential_secret"
                type="button"
                class="text-destructive underline"
                @click="form.clear_credential = !form.clear_credential; if (form.clear_credential) form.credential_secret = ''"
              >
                {{ form.clear_credential ? 'undo clear' : 'clear' }}
              </button>
            </span>
          </div>

          <Textarea
            v-model="form.credential_secret"
            :placeholder="provider?.has_credential_secret ? '— leave blank to keep current value —' : meta.credential_placeholder"
            :rows="provider?.kind === 'codex_subscription' ? 6 : 2"
            class="font-mono text-xs"
          />

          <details v-if="meta.setup_steps?.length" class="text-xs text-muted-foreground">
            <summary class="cursor-pointer hover:text-foreground">How to get this</summary>
            <ol class="mt-2 list-decimal pl-5 space-y-1">
              <li v-for="(step, i) in meta.setup_steps" :key="i">{{ step }}</li>
            </ol>
          </details>
        </div>

        <div class="flex items-center justify-between border rounded-md px-3 py-2 text-sm">
          <span>Enabled</span>
          <Switch v-model="form.enabled" />
        </div>

        <div class="flex items-center justify-between border rounded-md px-3 py-2 text-sm">
          <div>
            <div>Active provider</div>
            <div class="text-xs text-muted-foreground">Normal investigations use this provider automatically.</div>
          </div>
          <Switch v-model="form.active" />
        </div>
      </div>

      <DialogFooter>
        <Button variant="ghost" @click="emit('update:open', false)">Cancel</Button>
        <Button :disabled="!form.label.trim() || submitting" :loading="submitting" @click="onSubmit">
          Save changes
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
