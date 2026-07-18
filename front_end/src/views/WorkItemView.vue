<script setup lang="ts">
/**
 * Unified work-item view — ONE page with tabs for the ticket, its thread and
 * its pull request, which are inherently the same piece of work.
 *
 * All three legacy routes (/tickets/:uid, /threads/:uid, /pull-requests/:uid)
 * render this view; the route decides the focused tab and the uid kind, and
 * the sibling uids are resolved from whichever anchor we landed on. Deep
 * links keep working and tab switches rewrite the URL (router.replace) so
 * what you share matches what you see.
 *
 * A PR can exist WITHOUT a ticket (hand-made branch pushed to GitHub and
 * synced in) — then only the PR tab exists and the page says so.
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { GitPullRequest, MessagesSquare, SquareKanban } from 'lucide-vue-next'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import TicketDetailView from '@/views/TicketDetailView.vue'
import ThreadView from '@/views/ThreadView.vue'
import PullRequestDetailView from '@/views/PullRequestDetailView.vue'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useThreadStore } from '@/stores/threadStore'
import { useTicketStore } from '@/stores/ticketStore'
import type { ThreadDTO } from '@/types/api'

type Kind = 'ticket' | 'thread' | 'pr'

const route = useRoute()
const router = useRouter()
const tickets = useTicketStore()
const threads = useThreadStore()
const delivery = useDeliveryStore()

const KIND_BY_ROUTE: Record<string, Kind> = {
  'ticket-detail': 'ticket',
  'thread-detail': 'thread',
  'pull-request-detail': 'pr',
}

const kind = computed<Kind>(() => KIND_BY_ROUTE[String(route.name)] ?? 'ticket')
const uid = computed(() => String(route.params.uid))

const ticketUid = ref<string | null>(null)
const threadUid = ref<string | null>(null)
const prUid = ref<string | null>(null)
const resolving = ref(true)

function pickThread(list: ThreadDTO[]): ThreadDTO | null {
  const active = list.find((t) => t.phase !== 'done' && t.phase !== 'abandoned')
  if (active) return active
  // Terminal threads still hold the conversation history — show the latest.
  return [...list].sort((a, b) => ((a.created_at ?? '') < (b.created_at ?? '') ? 1 : -1))[0] ?? null
}

async function threadForTicket(tUid: string): Promise<string | null> {
  try {
    return pickThread(await threads.listThreads({ subject_ticket_uid: tUid }))?.uid ?? null
  } catch {
    return null
  }
}

/** Resolve the ticket/thread/PR cluster from the route's anchor uid. */
async function resolve() {
  resolving.value = true
  const anchor = uid.value
  let t: string | null = null
  let th: string | null = null
  let pr: string | null = null
  try {
    if (kind.value === 'ticket') {
      t = anchor
      th = await threadForTicket(anchor)
      try {
        const detail = await tickets.getTicket(anchor)
        pr = detail.linked_pr_uids?.[detail.linked_pr_uids.length - 1] ?? null
      } catch {
        pr = null
      }
      if (!pr && th) {
        try {
          pr = (await threads.getThread(th)).pr_uid || null
        } catch {
          pr = null
        }
      }
    } else if (kind.value === 'thread') {
      th = anchor
      try {
        const detail = await threads.getThread(anchor)
        t = detail.subject_ticket_uid || null
        pr = detail.pr_uid || null
      } catch {
        t = null
      }
    } else {
      pr = anchor
      try {
        const detail = await delivery.getPullRequest(anchor)
        t = detail.ticket_uid || null
        if (t) th = await threadForTicket(t)
      } catch {
        t = null
      }
    }
  } finally {
    // Route anchor may have changed mid-flight — only commit the latest.
    if (anchor === uid.value) {
      ticketUid.value = t
      threadUid.value = th
      prUid.value = pr
      resolving.value = false
    }
  }
}

watch([uid, kind], () => void resolve(), { immediate: true })

interface Tab {
  kind: Kind
  label: string
  icon: typeof SquareKanban
}

const tabs = computed<Tab[]>(() => {
  const out: Tab[] = []
  if (ticketUid.value) out.push({ kind: 'ticket', label: 'Ticket', icon: SquareKanban })
  if (threadUid.value) out.push({ kind: 'thread', label: 'Thread', icon: MessagesSquare })
  if (prUid.value) out.push({ kind: 'pr', label: 'Pull request', icon: GitPullRequest })
  return out
})

const ROUTE_BY_KIND: Record<Kind, string> = {
  ticket: 'ticket-detail',
  thread: 'thread-detail',
  pr: 'pull-request-detail',
}

const activeTab = computed({
  get: () => kind.value,
  set: (next: Kind) => {
    const target =
      next === 'ticket' ? ticketUid.value : next === 'thread' ? threadUid.value : prUid.value
    if (!target || next === kind.value) return
    void router.replace({ name: ROUTE_BY_KIND[next], params: { uid: target } })
  },
})

/** The prop each embedded view receives — its OWN uid, never the anchor's. */
const focusedUid = computed(() =>
  kind.value === 'ticket' ? ticketUid.value : kind.value === 'thread' ? threadUid.value : prUid.value,
)
</script>

<template>
  <div class="space-y-3">
    <Tabs v-if="tabs.length > 1" v-model="activeTab">
      <TabsList>
        <TabsTrigger v-for="tab in tabs" :key="tab.kind" :value="tab.kind">
          <component :is="tab.icon" class="mr-1 size-3.5" /> {{ tab.label }}
        </TabsTrigger>
      </TabsList>
    </Tabs>
    <p
      v-else-if="!resolving && kind === 'pr' && !ticketUid"
      class="text-xs text-muted-foreground"
    >
      This pull request has no linked ticket — it was likely opened outside OpenSweep. You can
      still review, discuss and converge it here.
    </p>

    <TicketDetailView v-if="kind === 'ticket' && focusedUid" :uid="focusedUid" />
    <ThreadView v-else-if="kind === 'thread' && focusedUid" :uid="focusedUid" />
    <PullRequestDetailView v-else-if="kind === 'pr' && focusedUid" :uid="focusedUid" />
  </div>
</template>
