<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CalendarClock } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { ComputeDial } from '@/types/api'

const props = withDefaults(
  defineProps<{
    /** "" manual | "on-event" | "cron:<expr>" */
    trigger: string
    computeDial: ComputeDial
    saving?: boolean
    /** Hide the on-push option (e.g. HealthView's scheduled-audit dialog). */
    hideOnEvent?: boolean
    hint?: string
  }>(),
  { saving: false, hideOnEvent: false, hint: '' },
)

const emit = defineEmits<{
  save: [payload: { trigger: string; compute_dial: ComputeDial }]
}>()

type TriggerMode = 'manual' | 'on-event' | 'cron'
const mode = ref<TriggerMode>('manual')
const cronExpr = ref('')
const dial = ref<ComputeDial>(props.computeDial)

const TRIGGER_OPTIONS = computed(() =>
  [
    { label: 'Manual — run only when triggered', value: 'manual' },
    { label: 'On push — when a push touches its target', value: 'on-event' },
    { label: 'Cron — on a schedule', value: 'cron' },
  ].filter((o) => !props.hideOnEvent || o.value !== 'on-event'),
)

const DIAL_OPTIONS = [
  { label: 'Disabled — never runs automatically', value: 'disabled' },
  { label: 'Suggest — surface as a candidate only', value: 'suggest' },
  { label: 'Ask before run', value: 'ask-before-run' },
  { label: 'Auto-run on free (local) compute', value: 'auto-run-cheap' },
  { label: 'Auto-run on any provider', value: 'auto-run-any' },
]

const CRON_PRESETS = [
  { label: 'Nightly at 02:00', expr: '0 2 * * *' },
  { label: 'Every 6 hours', expr: '0 */6 * * *' },
  { label: 'Weekly (Mon 06:00)', expr: '0 6 * * 1' },
]

function hydrate() {
  if (props.trigger.startsWith('cron:')) {
    mode.value = 'cron'
    cronExpr.value = props.trigger.slice('cron:'.length)
  } else if (props.trigger === 'on-event') {
    mode.value = 'on-event'
    cronExpr.value = ''
  } else {
    mode.value = 'manual'
    cronExpr.value = ''
  }
  dial.value = props.computeDial
}
watch(() => [props.trigger, props.computeDial], hydrate, { immediate: true })

const nextTrigger = computed(() =>
  mode.value === 'cron'
    ? `cron:${cronExpr.value.trim()}`
    : mode.value === 'on-event'
      ? 'on-event'
      : '',
)

const dirty = computed(
  () => nextTrigger.value !== props.trigger || dial.value !== props.computeDial,
)

const cronMissing = computed(() => mode.value === 'cron' && !cronExpr.value.trim())

function save() {
  if (cronMissing.value) return
  emit('save', { trigger: nextTrigger.value, compute_dial: dial.value })
}
</script>

<template>
  <Card>
    <CardHeader class="flex-row items-center justify-between gap-3 space-y-0 p-4">
      <CardTitle class="flex items-center gap-2 text-base">
        <CalendarClock class="h-4 w-4 text-muted-foreground" /> Schedule
      </CardTitle>
      <Button size="sm" :disabled="!dirty || cronMissing" :loading="saving" @click="save">
        Save
      </Button>
    </CardHeader>
    <CardContent class="space-y-4 p-4 pt-0">
      <div class="grid gap-3 md:grid-cols-2">
        <div class="space-y-1.5">
          <Label>Trigger</Label>
          <Select :model-value="mode" @update:model-value="mode = $event as TriggerMode">
            <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in TRIGGER_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div class="space-y-1.5">
          <Label>Compute dial</Label>
          <Select :model-value="dial" @update:model-value="dial = $event as ComputeDial">
            <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in DIAL_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div v-if="mode === 'cron'" class="space-y-1.5">
        <Label>Crontab (5 fields, UTC)</Label>
        <Input v-model="cronExpr" placeholder="0 2 * * *" class="font-mono" />
        <div class="flex flex-wrap gap-1.5 pt-0.5">
          <Button
            v-for="preset in CRON_PRESETS"
            :key="preset.expr"
            variant="outline"
            size="sm"
            @click="cronExpr = preset.expr"
          >
            {{ preset.label }}
          </Button>
        </div>
      </div>
      <p v-if="hint" class="text-xs text-muted-foreground">{{ hint }}</p>
      <p v-else class="text-xs text-muted-foreground">
        Cron runs dispatch on the beat scanner; on-push runs are gated by the compute
        dial — “auto-run on free compute” makes them cost nothing on a local provider.
      </p>
    </CardContent>
  </Card>
</template>
