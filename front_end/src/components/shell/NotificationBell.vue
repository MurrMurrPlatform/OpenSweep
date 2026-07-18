<script setup lang="ts">
// Topbar bell — unread badge polled every ~5s off the audit-derived
// notification feed, and a popover grouping the latest items into
// Needs attention / Activity / Mentions (empty sections collapse).
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Bell, Check } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { formatRelativeTime } from '@/lib/utils'
import { notificationLink } from '@/lib/notificationLinks'
import { useNotificationStore } from '@/stores/notificationStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import type { NotificationDTO } from '@/types/api'

const POLL_MS = 5000
const POPOVER_LIMIT = 30

const router = useRouter()
const store = useNotificationStore()
const repos = useRepositoryStore()

const open = ref(false)
let timer: number | undefined

async function poll() {
  try {
    await store.fetchCounts()
  } catch {
    /* transient poll error — the next tick retries */
  }
}

function onVisibilityChange() {
  if (!document.hidden) void poll()
}

onMounted(() => {
  void poll()
  timer = window.setInterval(() => {
    if (!document.hidden) void poll()
  }, POLL_MS)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

watch(open, (isOpen) => {
  if (!isOpen) return
  void store.fetchList({ limit: POPOVER_LIMIT })
  if (!repos.loaded) void repos.fetchAll()
})

const badge = computed(() => (store.counts.total > 99 ? '99+' : String(store.counts.total)))

const SECTIONS = [
  { key: 'attention', label: 'Needs attention' },
  { key: 'activity', label: 'Activity' },
  { key: 'mentions', label: 'Mentions' },
] as const

const sections = computed(() =>
  SECTIONS.map((s) => ({
    ...s,
    items: store.list.filter((n) => n.category === s.key),
  })).filter((s) => s.items.length > 0),
)

function itemTitle(n: NotificationDTO): string {
  return n.title || String(n.payload.mentioned_user_label || '') || n.subject_uid.slice(0, 8)
}

function openItem(n: NotificationDTO) {
  open.value = false
  if (!n.read_at) void store.markRead(n.uid)
  const link = notificationLink(n, repos.find(n.repository_uid)?.slug)
  if (link) void router.push(link)
}

function viewAll() {
  open.value = false
  void router.push({ name: 'notifications' })
}
</script>

<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <Button variant="ghost" size="icon-sm" class="relative" title="Notifications">
        <Bell class="size-4" />
        <span
          v-if="store.counts.total > 0"
          class="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[10px] font-semibold leading-none text-primary-foreground"
        >
          {{ badge }}
        </span>
      </Button>
    </PopoverTrigger>
    <PopoverContent align="end" class="w-96 p-0">
      <div class="flex items-center justify-between px-3 py-2">
        <span class="text-sm font-semibold">Notifications</span>
        <Button
          v-if="store.counts.total > 0"
          variant="ghost"
          size="sm"
          class="h-7 gap-1 text-xs text-muted-foreground"
          @click="store.markAllRead()"
        >
          <Check class="size-3.5" /> Mark all read
        </Button>
      </div>
      <Separator />
      <ScrollArea class="max-h-96">
        <div v-if="!sections.length" class="px-3 py-8 text-center text-sm text-muted-foreground">
          Nothing yet — activity across your workspaces lands here.
        </div>
        <div v-for="section in sections" :key="section.key" class="py-1">
          <div class="px-3 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            {{ section.label }}
          </div>
          <button
            v-for="n in section.items"
            :key="n.uid"
            type="button"
            class="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-accent"
            @click="openItem(n)"
          >
            <span
              class="mt-1.5 size-1.5 shrink-0 rounded-full"
              :class="n.read_at ? 'bg-transparent' : 'bg-primary'"
            />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm" :class="n.read_at ? 'text-muted-foreground' : 'font-medium'">
                {{ n.label }}
              </span>
              <span class="block truncate text-xs text-muted-foreground">{{ itemTitle(n) }}</span>
            </span>
            <span class="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
              {{ n.occurred_at ? formatRelativeTime(n.occurred_at) : '' }}
            </span>
          </button>
        </div>
      </ScrollArea>
      <Separator />
      <div class="p-1">
        <Button variant="ghost" size="sm" class="w-full justify-center text-xs" @click="viewAll()">
          View all notifications
        </Button>
      </div>
    </PopoverContent>
  </Popover>
</template>
