<script setup lang="ts">
// The agent's NATIVE todo lists (Claude Code TodoWrite / Codex update_plan /
// OpenCode todowrite), one section per thread phase. Past phases come from
// the durable per-turn mirror on the thread; the CURRENT phase prefers the
// live list derived from the streaming transcript (fresher mid-turn).
import { computed } from 'vue'
import { CheckCircle2, Circle, CircleDot } from 'lucide-vue-next'
import { Card, CardContent } from '@/components/ui/card'
import type { AgentTodo, ThreadPhase } from '@/types/api'

const props = defineProps<{
  /** Live list from the transcript (current phase, mid-turn fresh). */
  live: AgentTodo[]
  /** Durable per-phase mirror captured by the platform each turn. */
  stored: Record<string, AgentTodo[]>
  phase: ThreadPhase
}>()

const PHASE_ORDER: string[] = ['refining', 'implementing', 'in_review']
const PHASE_LABELS: Record<string, string> = {
  refining: 'Planning',
  implementing: 'Implementation',
  in_review: 'Review fixes',
}

const sections = computed(() => {
  const out: { key: string; label: string; todos: AgentTodo[] }[] = []
  for (const key of PHASE_ORDER) {
    const isCurrent = key === props.phase
    const todos = isCurrent && props.live.length ? props.live : (props.stored[key] ?? [])
    if (todos.length) out.push({ key, label: PHASE_LABELS[key] ?? key, todos })
  }
  return out
})
</script>

<template>
  <Card v-if="sections.length">
    <CardContent class="space-y-3 p-4">
      <h3 class="text-sm font-semibold">Agent’s task lists</h3>
      <div v-for="section in sections" :key="section.key" class="space-y-1.5">
        <h4 class="text-xs font-semibold uppercase text-muted-foreground">
          {{ section.label }}
          <span v-if="section.key === phase" class="ml-1 font-normal normal-case text-primary">· current</span>
        </h4>
        <ul class="space-y-1.5">
          <li v-for="(todo, i) in section.todos" :key="i" class="flex items-start gap-2 text-xs">
            <CheckCircle2 v-if="todo.status === 'completed'" class="mt-0.5 size-3.5 shrink-0 text-good" />
            <CircleDot v-else-if="todo.status === 'in_progress'" class="mt-0.5 size-3.5 shrink-0 animate-pulse text-primary" />
            <Circle v-else class="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            <span :class="todo.status === 'completed' ? 'text-muted-foreground line-through' : ''">
              {{ todo.status === 'in_progress' && todo.activeForm ? todo.activeForm : todo.content }}
            </span>
          </li>
        </ul>
      </div>
    </CardContent>
  </Card>
</template>
