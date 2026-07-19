<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Activity, Bot, MessagesSquare, Plus, RefreshCw } from 'lucide-vue-next'
import { useRunStore } from '@/stores/runStore'
import { useCurrentUserStore } from '@/stores/currentUserStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PLAYBOOK_TO_PRODUCES, producesLabel } from '@/lib/produces'
import { runStatusLabel, runStatusVariant } from '@/lib/runStatus'
import { formatRelativeTime } from '@/lib/utils'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import { ErrorState } from '@/components/ui/error-state'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import type { ProducesKind, RunDTO } from '@/types/api'

/** runStatusVariant predates the shadcn Badge tones (danger/default gone). */
function statusBadgeVariant(run: RunDTO): BadgeVariants['variant'] {
  const v = runStatusVariant(run.status)
  if (v === 'danger') return 'destructive'
  if (v === 'default') return 'secondary'
  return v
}

// Produces chips filter client-side via the playbook→produces display map —
// playbook stays internal machinery.
const PRODUCES_CHIPS: ProducesKind[] = ['findings', 'answer', 'review-verdict', 'code-changes', 'verification', 'documentation']

const router = useRouter()
const runs = useRunStore()
const toast = useToast()
const currentUser = useCurrentUserStore()
const { uid: repoUid } = useCurrentRepo()
const items = ref<RunDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const producesFilter = ref<ProducesKind | ''>('')
/** Platform admins only: also list @opensweep comment replies + chat sessions. */
const showAgentActivity = ref(false)

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
  try {
    items.value = await runs.fetchAll({
      repository_uid: repoUid.value,
      surface: showAgentActivity.value && currentUser.isPlatformAdmin ? 'all' : undefined,
    })
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)
watch(repoUid, reload)
watch(showAgentActivity, reload)

function surfaceLabel(r: RunDTO): string {
  if (r.surface === 'comment') return 'comment reply'
  if (r.surface === 'chat') return 'chat bubble'
  return ''
}

const sorted = computed(() =>
  [...items.value]
    .filter(
      (r) => !producesFilter.value || PLAYBOOK_TO_PRODUCES[r.playbook] === producesFilter.value,
    )
    .sort((a, b) => {
      const ta = a.last_activity_at || a.updated_at || a.created_at || ''
      const tb = b.last_activity_at || b.updated_at || b.created_at || ''
      return tb.localeCompare(ta)
    }),
)

// ── New chat dialog ──────────────────────────────────────────────────────────

const chatOpen = ref(false)
const chatTitle = ref('')
const chatPrompt = ref('')
const creating = ref(false)

watch(chatOpen, (open) => {
  if (open) {
    chatTitle.value = ''
    chatPrompt.value = ''
  }
})

async function startChat() {
  if (!repoUid.value || creating.value) return
  creating.value = true
  try {
    const run = await runs.createRun({
      repository_uid: repoUid.value,
      playbook: 'chat',
      title: chatTitle.value.trim() || undefined,
      prompt: chatPrompt.value.trim() || undefined,
    })
    chatOpen.value = false
    void router.push({ name: 'run-detail', params: { uid: run.uid } })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t start chat', msg)
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Runs"
      subtitle="Every agent conversation — reviews, fixes, chats, one list."
    >
      <Button variant="outline" size="sm" :disabled="loading" @click="reload">
        <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
      </Button>
      <Button size="sm" @click="chatOpen = true">
        <Plus /> New chat
      </Button>
    </PageHeader>

    <!-- Playbook filter chips -->
    <div class="flex flex-wrap items-center gap-1.5">
      <Button
        :variant="producesFilter === '' ? 'default' : 'outline'"
        size="sm"
        class="rounded-full"
        @click="producesFilter = ''"
      >
        All
      </Button>
      <Button
        v-for="p in PRODUCES_CHIPS"
        :key="p"
        :variant="producesFilter === p ? 'default' : 'outline'"
        size="sm"
        class="rounded-full capitalize"
        @click="producesFilter = p"
      >
        {{ producesLabel(p) }}
      </Button>
      <Button
        v-if="currentUser.isPlatformAdmin"
        :variant="showAgentActivity ? 'default' : 'outline'"
        size="sm"
        class="ml-auto rounded-full"
        @click="showAgentActivity = !showAgentActivity"
      >
        <Bot /> Agent activity
      </Button>
    </div>

    <Card>
      <CardContent class="p-0">
        <!-- Loading -->
        <div v-if="loading" class="p-4 space-y-2">
          <Skeleton v-for="i in 6" :key="i" class="h-10" />
        </div>

        <!-- Error -->
        <div v-else-if="error" class="p-4">
          <ErrorState title="Couldn't load runs" :message="error" class="border-0">
            <Button variant="outline" size="sm" @click="reload">Retry</Button>
          </ErrorState>
        </div>

        <!-- Empty -->
        <div v-else-if="sorted.length === 0" class="p-4">
          <EmptyState
            :icon="Activity"
            title="No runs yet"
            description="Start a chat, ask a question, or dispatch a review to populate this list."
            class="border-0"
          >
            <Button size="sm" @click="chatOpen = true">
              <Plus /> New chat
            </Button>
          </EmptyState>
        </div>

        <!-- Table -->
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th class="px-4 py-2 font-medium">Run</th>
                <th class="px-4 py-2 font-medium">Playbook</th>
                <th class="px-4 py-2 font-medium">Status</th>
                <th class="px-4 py-2 font-medium">Turns</th>
                <th class="px-4 py-2 font-medium">Executor</th>
                <th class="px-4 py-2 font-medium">Last activity</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in sorted"
                :key="r.uid"
                class="cursor-pointer border-t border-border transition-colors hover:bg-accent"
                @click="$router.push({ name: 'run-detail', params: { uid: r.uid } })"
              >
                <td class="max-w-[280px] px-4 py-2">
                  <RouterLink
                    class="block truncate font-medium underline-offset-2 hover:underline"
                    :to="{ name: 'run-detail', params: { uid: r.uid } }"
                    @click.stop
                  >
                    {{ r.title || `Run ${r.uid.slice(0, 12)}` }}
                  </RouterLink>
                </td>
                <td class="px-4 py-2">
                  <ProducesBadge :produces="PLAYBOOK_TO_PRODUCES[r.playbook] ?? 'findings'" />
                  <Badge v-if="surfaceLabel(r)" variant="secondary" class="ml-1">
                    {{ surfaceLabel(r) }}
                  </Badge>
                </td>
                <td class="px-4 py-2">
                  <Badge :variant="statusBadgeVariant(r)">{{ runStatusLabel(r) }}</Badge>
                </td>
                <td class="px-4 py-2 tabular-nums">{{ r.turns }}</td>
                <td class="whitespace-nowrap px-4 py-2 font-mono">{{ r.executor }}</td>
                <td class="whitespace-nowrap px-4 py-2 text-muted-foreground">{{ formatRelativeTime(r.last_activity_at || r.updated_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>

    <!-- New chat dialog -->
    <Dialog v-model:open="chatOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New chat</DialogTitle>
          <DialogDescription>
            Start a conversation with an agent in a fresh workspace clone of this repository.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-3">
          <div class="space-y-1.5">
            <Label for="chat-title">Title (optional)</Label>
            <Input id="chat-title" v-model="chatTitle" placeholder="What is this conversation about?" />
          </div>
          <div class="space-y-1.5">
            <Label for="chat-prompt">First message (optional)</Label>
            <Textarea
              id="chat-prompt"
              v-model="chatPrompt"
              :rows="3"
              placeholder="Ask about the code, or tell the agent what to do…"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="chatOpen = false">Cancel</Button>
          <Button size="sm" :disabled="!repoUid || creating" :loading="creating" @click="startChat">
            <MessagesSquare /> Start chat
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
