<script setup lang="ts">
// A structured question the agent asked via `opensweep_platform_ask_user`,
// rendered as an answerable card: option chips or free text. Answering marks
// the question answered (metadata) AND delivers the answer into the
// conversation as a follow-up message.
import { ref } from 'vue'
import { CircleHelp, SendHorizontal } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import type { ThreadEventDTO } from '@/types/api'

const props = defineProps<{ question: ThreadEventDTO }>()
const emit = defineEmits<{ (e: 'answer', text: string): void }>()

const freeText = ref('')
const busy = ref(false)

const options = Array.isArray(props.question.options)
  ? (props.question.options as string[])
  : []

function answer(text: string) {
  if (busy.value || !text.trim()) return
  busy.value = true
  emit('answer', text.trim())
}
</script>

<template>
  <Card class="border-primary/30 bg-primary/5">
    <CardContent class="space-y-2.5 p-4">
      <div class="flex items-start gap-2">
        <CircleHelp class="mt-0.5 size-4 shrink-0 text-primary" />
        <div class="min-w-0 space-y-1">
          <p class="text-sm font-medium">{{ String(question.question ?? '') }}</p>
          <p v-if="question.context" class="text-xs text-muted-foreground">
            {{ String(question.context) }}
          </p>
        </div>
      </div>
      <div v-if="options.length" class="flex flex-wrap gap-1.5">
        <Button
          v-for="opt in options"
          :key="opt"
          size="sm"
          variant="outline"
          :disabled="busy"
          @click="answer(opt)"
        >
          {{ opt }}
        </Button>
      </div>
      <div class="flex items-end gap-2">
        <Textarea
          v-model="freeText"
          :rows="1"
          class="resize-none text-sm"
          placeholder="Answer in your own words…"
          @keydown.enter.exact.prevent="answer(freeText)"
        />
        <Button size="sm" class="shrink-0" :disabled="busy || !freeText.trim()" @click="answer(freeText)">
          <SendHorizontal />
        </Button>
      </div>
    </CardContent>
  </Card>
</template>
