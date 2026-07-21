<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Check, ChevronDown, ChevronRight, X } from 'lucide-vue-next'
import { MarkdownView } from '@/components/ui/markdown'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { areaKindVariant } from '@/lib/areas'
import type { AreaEditDTO } from '@/types/api'

const props = defineProps<{
  edit: AreaEditDTO
  /** This edit is being resolved right now (spinner on Accept). */
  resolving?: boolean
  /** Another resolution (single or bulk) is in flight — lock the buttons. */
  disabled?: boolean
}>()

defineEmits<{ accept: []; reject: [] }>()

const specOpen = ref(false)

const heading = computed(() => props.edit.title || props.edit.key)
</script>

<template>
  <div class="border-b border-border p-4 last:border-b-0 space-y-2">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="font-mono text-sm font-medium">{{ edit.key }}</span>
          <Badge :variant="areaKindVariant(edit.kind)" class="px-1.5 text-[10px]">{{ edit.kind || 'subsystem' }}</Badge>
          <Badge v-if="!edit.area_uid" variant="info" class="px-1.5 text-[10px]">new area</Badge>
          <Badge v-if="edit.proposed_enabled === false" variant="destructive" class="px-1.5 text-[10px]">proposes retiring</Badge>
          <Badge v-else variant="warn" class="px-1.5 text-[10px]" title="Replaces the area's current spec">updates existing</Badge>
        </div>
        <div v-if="heading !== edit.key" class="text-sm">{{ heading }}</div>
        <div class="text-xs text-muted-foreground">
          <span v-if="edit.source_run_uid">
            run
            <RouterLink
              :to="{ name: 'run-detail', params: { uid: edit.source_run_uid } }"
              class="font-mono text-primary hover:underline"
            >{{ edit.source_run_uid.slice(0, 8) }}</RouterLink>
          </span>
          <span v-if="edit.created_at"> · {{ edit.created_at.slice(0, 10) }}</span>
        </div>
      </div>
      <div class="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          :disabled="disabled || resolving"
          @click="$emit('reject')"
        >
          <X /> Reject
        </Button>
        <Button
          size="sm"
          :loading="resolving"
          :disabled="disabled"
          @click="$emit('accept')"
        >
          <Check /> Accept
        </Button>
      </div>
    </div>

    <p v-if="edit.rationale" class="text-sm text-muted-foreground">{{ edit.rationale }}</p>

    <div v-if="edit.scope_paths.length" class="flex flex-wrap gap-1.5">
      <span
        v-for="path in edit.scope_paths"
        :key="path"
        class="rounded-full border border-border px-2.5 py-0.5 font-mono text-xs"
      >
        {{ path }}
      </span>
    </div>

    <!-- Collapsible spec preview -->
    <div v-if="edit.proposed_spec">
      <button
        type="button"
        class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        @click="specOpen = !specOpen"
      >
        <component :is="specOpen ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
        {{ edit.current_spec ? 'Proposed spec (replaces the current one)' : 'Proposed spec' }}
      </button>
      <div v-if="specOpen" class="mt-2 space-y-2">
        <div class="rounded-md border border-border p-3">
          <MarkdownView :model-value="edit.proposed_spec" preview-only />
        </div>
        <details v-if="edit.current_spec" class="rounded-md border border-border p-3">
          <summary class="cursor-pointer text-xs text-muted-foreground">Current spec (being replaced)</summary>
          <MarkdownView :model-value="edit.current_spec" preview-only class="mt-2" />
        </details>
      </div>
    </div>
  </div>
</template>
