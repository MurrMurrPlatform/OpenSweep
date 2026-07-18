<script setup lang="ts">
import { ref, watch } from 'vue'
import { Check, Maximize2, Pencil } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog,
  DialogHeader,
  DialogScrollContent,
  DialogTitle,
} from '@/components/ui/dialog'
import MarkdownView from '@/components/ui/markdown/MarkdownView.vue'
import type { PlanState } from '@/types/api'

const props = defineProps<{
  planText: string
  planState: PlanState
  editable: boolean
}>()
const emit = defineEmits<{
  (e: 'save', text: string): void
  (e: 'approve'): void
}>()

const editing = ref(false)
const expanded = ref(false)
const draft = ref(props.planText)
watch(
  () => props.planText,
  (v) => {
    if (!editing.value) draft.value = v
  },
)

function save() {
  editing.value = false
  if (draft.value !== props.planText) emit('save', draft.value)
}
</script>

<template>
  <Card>
    <CardContent class="space-y-3 p-4">
      <div class="flex items-center justify-between">
        <h3 class="text-sm font-semibold">Plan</h3>
        <div class="flex items-center gap-2">
          <Badge :variant="planState === 'approved' ? 'success' : 'secondary'">
            {{ planState }}
          </Badge>
          <Button v-if="planText" variant="ghost" size="sm" title="Expand plan" @click="expanded = true">
            <Maximize2 />
          </Button>
          <Button v-if="editable && !editing && planText" variant="ghost" size="sm" @click="editing = true">
            <Pencil />
          </Button>
          <Button
            v-if="editable && planState === 'drafted' && !editing"
            size="sm"
            @click="emit('approve')"
          >
            <Check /> Approve
          </Button>
        </div>
      </div>
      <template v-if="editing">
        <MarkdownView v-model="draft" :editing="true" min-height="280px" />
        <div class="flex gap-2">
          <Button size="sm" @click="save">Save</Button>
          <Button size="sm" variant="ghost" @click="editing = false">Cancel</Button>
        </div>
      </template>
      <button
        v-else-if="planText"
        type="button"
        class="block w-full cursor-zoom-in text-left"
        title="Click to read the full plan"
        @click="expanded = true"
      >
        <MarkdownView :model-value="planText" :preview-only="true" compact />
      </button>
      <p v-else class="text-sm text-muted-foreground">
        No plan yet — the agent drafts one in the conversation, or tell it to “just implement it”.
      </p>
    </CardContent>
  </Card>

  <!-- Full-plan reader: the sidebar preview is deliberately compact; this
       modal is where the plan becomes fully readable (and approvable). -->
  <Dialog v-model:open="expanded">
    <DialogScrollContent class="max-w-3xl">
      <DialogHeader>
        <DialogTitle class="flex items-center gap-2">
          Plan
          <Badge :variant="planState === 'approved' ? 'success' : 'secondary'">
            {{ planState }}
          </Badge>
          <Button
            v-if="editable && planState === 'drafted'"
            size="sm"
            class="ml-auto mr-6"
            @click="((expanded = false), emit('approve'))"
          >
            <Check /> Approve
          </Button>
        </DialogTitle>
      </DialogHeader>
      <MarkdownView :model-value="planText" :preview-only="true" min-height="0" />
    </DialogScrollContent>
  </Dialog>
</template>
