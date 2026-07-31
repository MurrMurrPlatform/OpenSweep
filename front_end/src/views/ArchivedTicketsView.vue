<script setup lang="ts">
// Archived tickets — off the board, history kept. Unarchive returns a ticket
// to its lane with its status untouched.
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Archive, ArrowLeft, RefreshCw } from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { PageHeader } from '@/components/ui/page-header'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import TicketCard from '@/components/tickets/TicketCard.vue'
import type { TicketDTO } from '@/types/api'

const store = useTicketStore()
const route = useRoute()
const { uid: repoUid } = useCurrentRepo()

const tickets = ref<TicketDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const boardRoute = computed(() => ({
  name: 'tickets',
  params: { repoSlug: route.params.repoSlug },
}))

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
  try {
    tickets.value = await store.listTickets({ repository_uid: repoUid.value, archived: true })
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(repoUid, () => void reload(), { immediate: true })

function onTicketUpdated(updated: TicketDTO) {
  // Unarchived tickets go home to the board — drop them from this list.
  tickets.value = updated.archived
    ? tickets.value.map((t) => (t.uid === updated.uid ? updated : t))
    : tickets.value.filter((t) => t.uid !== updated.uid)
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Archived tickets"
      subtitle="Off the board, history kept — unarchive to put one back in its lane."
    >
      <Button variant="outline" size="sm" as-child>
        <RouterLink :to="boardRoute"><ArrowLeft /> Board</RouterLink>
      </Button>
      <Button variant="outline" size="sm" :disabled="loading" @click="reload()">
        <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
      </Button>
    </PageHeader>

    <div v-if="loading" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <Card v-for="i in 3" :key="i">
        <CardContent class="space-y-3 p-4">
          <Skeleton class="h-4 w-2/3" />
          <Skeleton class="h-16" />
        </CardContent>
      </Card>
    </div>

    <ErrorState v-else-if="error" title="Couldn't load archived tickets" :message="error">
      <Button variant="outline" size="sm" @click="reload()">Retry</Button>
    </ErrorState>

    <EmptyState
      v-else-if="tickets.length === 0"
      :icon="Archive"
      title="No archived tickets"
      description="Archive a ticket from its card menu on the board — it moves here, reversibly."
    >
      <Button variant="outline" size="sm" as-child>
        <RouterLink :to="boardRoute"><ArrowLeft /> Back to the board</RouterLink>
      </Button>
    </EmptyState>

    <div v-else class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <TicketCard
        v-for="ticket in tickets"
        :key="ticket.uid"
        :ticket="ticket"
        @updated="onTicketUpdated"
      />
    </div>
  </div>
</template>
