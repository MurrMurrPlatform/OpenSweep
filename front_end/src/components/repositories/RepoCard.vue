<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { formatRelativeTime } from '@/lib/utils'
import type { Repository } from '@/types/api'
import { GitBranch, Clock, Pencil, Trash2 } from 'lucide-vue-next'

interface Props { repo: Repository }
defineProps<Props>()
const emit = defineEmits<{
  edit: [repo: Repository]
  delete: [repo: Repository]
}>()

function onEdit(e: Event, repo: Repository) {
  e.preventDefault()
  e.stopPropagation()
  emit('edit', repo)
}
function onDelete(e: Event, repo: Repository) {
  e.preventDefault()
  e.stopPropagation()
  emit('delete', repo)
}
</script>

<template>
  <RouterLink :to="{ name: 'workspace-home', params: { repoSlug: repo.slug } }" class="block h-full">
    <Card class="hover:shadow-md transition-shadow p-5 flex flex-col gap-3 h-full">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="font-semibold text-foreground truncate">{{ repo.name }}</div>
          <div class="text-xs text-muted-foreground truncate">{{ repo.description || repo.slug }}</div>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span class="inline-flex items-center gap-1">
          <GitBranch class="h-3 w-3" />
          {{ repo.default_branch }}
        </span>
        <Badge v-if="repo.github_owner" variant="outline" class="px-1.5 text-[10px]">
          {{ repo.github_owner }}/{{ repo.github_repo }}
        </Badge>
        <Badge
          v-if="repo.github_connection_status === 'disconnected'"
          variant="destructive"
          class="px-1.5 text-[10px]"
          title="OpenSweep lost its GitHub credential for this repo — reconnect the App."
        >
          GitHub disconnected
        </Badge>
        <Badge
          v-else-if="repo.github_connection_status === 'suspended'"
          variant="destructive"
          class="px-1.5 text-[10px]"
          title="GitHub App installation is suspended for this repo."
        >
          GitHub suspended
        </Badge>
        <Badge
          v-else-if="repo.github_connection_status === 'error'"
          variant="destructive"
          class="px-1.5 text-[10px]"
          title="OpenSweep hit an error talking to GitHub for this repo."
        >
          GitHub error
        </Badge>
      </div>
      <div class="mt-auto flex items-center justify-between text-xs text-muted-foreground">
        <span class="inline-flex items-center gap-1">
          <Clock class="h-3 w-3" />
          Synced
          {{ formatRelativeTime(repo.last_synced_at) }}
        </span>
        <div class="flex items-center gap-1">
          <button
            type="button"
            class="text-muted-foreground hover:text-foreground p-1 rounded-sm"
            title="Edit"
            @click="onEdit($event, repo)"
          >
            <Pencil class="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            class="text-muted-foreground hover:text-destructive p-1 rounded-sm"
            title="Delete"
            @click="onDelete($event, repo)"
          >
            <Trash2 class="h-3.5 w-3.5" />
          </button>
          <span :class="repo.is_active ? 'text-good' : 'text-muted-foreground'" class="ml-1">●</span>
        </div>
      </div>
    </Card>
  </RouterLink>
</template>
