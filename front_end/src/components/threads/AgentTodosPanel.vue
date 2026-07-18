<script setup lang="ts">
// The agent's own working plan, mirrored live from its TodoWrite calls in
// the transcript — always in sync because it is DERIVED from the
// conversation, not copied.
import { CheckCircle2, Circle, CircleDot } from 'lucide-vue-next'
import { Card, CardContent } from '@/components/ui/card'
import type { AgentTodo } from '@/types/api'

defineProps<{ todos: AgentTodo[] }>()
</script>

<template>
  <Card v-if="todos.length">
    <CardContent class="space-y-2 p-4">
      <h3 class="text-sm font-semibold">Agent’s task list</h3>
      <ul class="space-y-1.5">
        <li v-for="(todo, i) in todos" :key="i" class="flex items-start gap-2 text-xs">
          <CheckCircle2 v-if="todo.status === 'completed'" class="mt-0.5 size-3.5 shrink-0 text-good" />
          <CircleDot v-else-if="todo.status === 'in_progress'" class="mt-0.5 size-3.5 shrink-0 animate-pulse text-primary" />
          <Circle v-else class="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
          <span :class="todo.status === 'completed' ? 'text-muted-foreground line-through' : ''">
            {{ todo.status === 'in_progress' && todo.activeForm ? todo.activeForm : todo.content }}
          </span>
        </li>
      </ul>
    </CardContent>
  </Card>
</template>
