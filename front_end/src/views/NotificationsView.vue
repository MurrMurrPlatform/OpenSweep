<script setup lang="ts">
// /notifications — the full inbox: every audit-derived notification in the
// caller's org, filterable by category / repository / unread, with per-item
// mark-read / dismiss and mark-all-read. Read state persists server-side.
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { BellOff, Check, CheckCheck, X } from 'lucide-vue-next'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { PageHeader } from '@/components/ui/page-header'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { formatRelativeTime } from '@/lib/utils'
import { notificationLink } from '@/lib/notificationLinks'
import { useNotificationStore } from '@/stores/notificationStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import type { NotificationDTO } from '@/types/api'

const router = useRouter()
const store = useNotificationStore()
const repos = useRepositoryStore()

const categoryFilter = ref('')
const repoFilter = ref('')
const unreadOnly = ref(false)

const CATEGORY_LABELS: Record<string, string> = {
  attention: 'Needs attention',
  activity: 'Activity',
  mentions: 'Mentions',
}

const CATEGORY_TONE: Record<string, BadgeVariants['variant']> = {
  attention: 'warn',
  activity: 'secondary',
  mentions: 'info',
}

async function refresh() {
  await store.fetchList({
    category: categoryFilter.value || undefined,
    repository_uid: repoFilter.value || undefined,
    unread: unreadOnly.value,
    limit: 200,
  })
}

onMounted(() => {
  void refresh()
  void store.fetchCounts()
  if (!repos.loaded) void repos.fetchAll()
})
watch([categoryFilter, repoFilter, unreadOnly], refresh)

const subtitle = computed(() =>
  store.counts.total > 0
    ? `${store.counts.total} unread — ${store.counts.attention} need${store.counts.attention === 1 ? 's' : ''} your attention.`
    : 'Everything that happened across your workspaces, and where your input is needed.',
)

function repoSlug(n: NotificationDTO): string {
  return repos.find(n.repository_uid)?.slug || ''
}

function itemTitle(n: NotificationDTO): string {
  return n.title || String(n.payload.mentioned_user_label || '') || n.subject_uid.slice(0, 8)
}

function openItem(n: NotificationDTO) {
  if (!n.read_at) void store.markRead(n.uid)
  const link = notificationLink(n, repoSlug(n))
  if (link) void router.push(link)
}

function onCategory(v: unknown) {
  categoryFilter.value = v === 'all' ? '' : String(v ?? '')
}
function onRepo(v: unknown) {
  repoFilter.value = v === 'all' ? '' : String(v ?? '')
}
function onRead(v: unknown) {
  unreadOnly.value = v === 'unread'
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageHeader title="Notifications" :subtitle="subtitle">
      <Button variant="outline" size="sm" class="gap-1.5" @click="store.markAllRead()">
        <CheckCheck class="size-4" /> Mark all read
      </Button>
    </PageHeader>

    <div class="flex flex-wrap items-center gap-2">
      <Select :model-value="categoryFilter || 'all'" @update:model-value="onCategory">
        <SelectTrigger class="w-full sm:w-48">
          <SelectValue placeholder="All categories" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All categories</SelectItem>
          <SelectItem v-for="(label, key) in CATEGORY_LABELS" :key="key" :value="key">
            {{ label }}
          </SelectItem>
        </SelectContent>
      </Select>
      <Select :model-value="repoFilter || 'all'" @update:model-value="onRepo">
        <SelectTrigger class="w-full sm:w-56">
          <SelectValue placeholder="All repositories" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All repositories</SelectItem>
          <SelectItem v-for="r in repos.list" :key="r.uid" :value="r.uid">{{ r.slug }}</SelectItem>
        </SelectContent>
      </Select>
      <Select :model-value="unreadOnly ? 'unread' : 'everything'" @update:model-value="onRead">
        <SelectTrigger class="w-full sm:w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="everything">Read and unread</SelectItem>
          <SelectItem value="unread">Unread only</SelectItem>
        </SelectContent>
      </Select>
    </div>

    <Card>
      <CardContent class="p-0">
        <div v-if="!store.loaded" class="flex flex-col gap-2 p-4">
          <Skeleton v-for="i in 8" :key="i" class="h-12" />
        </div>
        <ul v-else-if="store.list.length" class="divide-y">
          <li
            v-for="n in store.list"
            :key="n.uid"
            class="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-accent/50"
          >
            <span
              class="size-1.5 shrink-0 rounded-full"
              :class="n.read_at ? 'bg-transparent' : 'bg-primary'"
            />
            <button type="button" class="min-w-0 flex-1 text-left" @click="openItem(n)">
              <div class="flex items-center gap-2">
                <span class="truncate text-sm" :class="n.read_at ? 'text-muted-foreground' : 'font-medium'">
                  {{ n.label }}
                </span>
                <Badge :variant="CATEGORY_TONE[n.category] || 'secondary'" class="shrink-0">
                  {{ CATEGORY_LABELS[n.category] || n.category }}
                </Badge>
              </div>
              <div class="truncate text-xs text-muted-foreground">
                {{ itemTitle(n) }}
                <template v-if="repoSlug(n)"> · {{ repoSlug(n) }}</template>
              </div>
            </button>
            <span class="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
              {{ n.occurred_at ? formatRelativeTime(n.occurred_at) : '' }}
            </span>
            <div class="flex shrink-0 items-center gap-1">
              <Button
                v-if="!n.read_at"
                variant="ghost"
                size="icon-sm"
                title="Mark read"
                @click="store.markRead(n.uid)"
              >
                <Check class="size-4" />
              </Button>
              <Button variant="ghost" size="icon-sm" title="Dismiss" @click="store.dismiss(n.uid)">
                <X class="size-4" />
              </Button>
            </div>
          </li>
        </ul>
        <div v-else class="p-4">
          <EmptyState
            :icon="BellOff"
            title="No notifications"
            description="Adjust the filters above, or wait for the next state change — runs, tickets, reviews and mentions all land here."
            class="border-0"
          />
        </div>
      </CardContent>
    </Card>
  </div>
</template>
