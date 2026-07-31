<script setup lang="ts">
// One kanban lane: sticky header, independently scrolling body, collapse-to-
// rail, per-lane card cap for high-volume lanes (Backlog/Done), and drop
// target behavior for cross-lane drags.
import { computed, ref, watch } from 'vue'
import { ChevronsLeftRight, ChevronsRightLeft } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import TicketCard from '@/components/tickets/TicketCard.vue'
import { statusVariant, STATUS_LABELS } from '@/components/tickets/ticketMeta'
import type { ThreadDTO, TicketDTO, TicketStatus } from '@/types/api'

const CARD_CAP = 12

interface Props {
  status: TicketStatus
  subtitle: string
  items: TicketDTO[]
  /** Unfiltered lane size — shown as "matches/total" while a board filter is active. */
  total?: number
  childCounts: Record<string, number>
  collapsed: boolean
  /** uid of the ticket currently being dragged anywhere on the board. */
  draggingUid: string | null
  /** whether that ticket may legally drop into this lane. */
  dropLegal: boolean
  /** ticket to flash briefly after a successful move (Atlassian pattern). */
  flashUid?: string | null
  /** Active (non-terminal) thread per ticket uid — thread badges on cards. */
  activeThreads?: Record<string, ThreadDTO>
}
const props = defineProps<Props>()

const emit = defineEmits<{
  'toggle-collapse': []
  'open-lane': []
  'drop-ticket': [toStatus: TicketStatus]
  'drag-start': [ticket: TicketDTO]
  'drag-end': []
  updated: [ticket: TicketDTO]
  deleted: [uid: string]
}>()

const title = computed(() => STATUS_LABELS[props.status])

const filtered = computed(() => props.total !== undefined && props.total !== props.items.length)
const countLabel = computed(() =>
  filtered.value ? `${props.items.length}/${props.total}` : `${props.items.length}`,
)

/* Cap long lanes; the user can open the tail explicitly. */
const showAllCards = ref(false)
watch(() => props.items.length, () => { if (props.items.length <= CARD_CAP) showAllCards.value = false })
const visibleItems = computed(() =>
  showAllCards.value ? props.items : props.items.slice(0, CARD_CAP),
)
const cappedCount = computed(() => props.items.length - visibleItems.value.length)

/* Drop-target feedback: highlight only while a legal drag hovers this lane. */
const dragOver = ref(false)

function onDragOver(event: DragEvent) {
  if (!props.dropLegal) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dragOver.value = true
}

function onDrop(event: DragEvent) {
  if (!props.dropLegal) return
  event.preventDefault()
  dragOver.value = false
  emit('drop-ticket', props.status)
}

function onCardDragStart(event: DragEvent, ticket: TicketDTO) {
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', ticket.uid)
  }
  emit('drag-start', ticket)
}
</script>

<template>
  <!-- Collapsed: a slim rail that re-expands on click. Still accepts drops
       (Jira/Linear behavior) so hiding Done doesn't break drag flows. -->
  <section
    v-if="collapsed"
    class="flex h-64 w-11 shrink-0 snap-start cursor-pointer flex-col items-center gap-2 rounded-xl border bg-muted/40 py-2 transition-[box-shadow,border-color,background-color] duration-150 hover:bg-accent"
    :class="dragOver && dropLegal ? 'border-primary/60 ring-2 ring-primary/30' : draggingUid && dropLegal ? 'border-primary/30' : ''"
    role="button"
    :aria-label="`Expand ${title} lane`"
    tabindex="0"
    @click="emit('toggle-collapse')"
    @keydown.enter="emit('toggle-collapse')"
    @dragover="onDragOver"
    @dragleave="dragOver = false"
    @drop="onDrop"
  >
    <ChevronsLeftRight class="size-3.5 text-muted-foreground" />
    <Badge :variant="statusVariant(status)" class="px-1.5 text-[10px]">{{ countLabel }}</Badge>
    <span class="text-xs font-semibold text-muted-foreground [writing-mode:vertical-rl]">{{ title }}</span>
  </section>

  <section
    v-else
    class="flex max-h-[calc(100svh-16rem)] w-[85vw] shrink-0 snap-start flex-col rounded-xl border bg-muted/40 transition-[box-shadow,border-color] duration-150 sm:w-72"
    :class="dragOver && dropLegal ? 'border-primary/60 ring-2 ring-primary/30' : draggingUid && dropLegal ? 'border-primary/30' : ''"
    @dragover="onDragOver"
    @dragleave="dragOver = false"
    @drop="onDrop"
  >
    <header class="flex items-start justify-between gap-1 p-3 pb-2">
      <div class="min-w-0">
        <h2 class="flex items-center gap-2 text-sm font-semibold">
          <button
            type="button"
            class="rounded-sm decoration-muted-foreground/50 underline-offset-4 transition-colors hover:underline"
            :title="`Focus the ${title} lane`"
            @click="emit('open-lane')"
          >
            {{ title }}
          </button>
          <Badge :variant="statusVariant(status)" class="px-1.5 text-[10px]">{{ countLabel }}</Badge>
        </h2>
        <p class="mt-0.5 text-xs text-muted-foreground">{{ subtitle }}</p>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        class="size-6 shrink-0 text-muted-foreground"
        :title="`Collapse ${title} lane`"
        @click="emit('toggle-collapse')"
      >
        <ChevronsRightLeft class="!size-3.5" />
      </Button>
    </header>

    <div class="relative min-h-[6rem] flex-1 space-y-2 overflow-y-auto px-2 pb-2">
      <p
        v-if="items.length === 0"
        class="rounded-lg border border-dashed px-2 py-6 text-center text-xs text-muted-foreground"
        :class="draggingUid && dropLegal ? 'border-primary/40 text-primary' : ''"
      >
        {{ draggingUid && dropLegal ? 'Drop here' : filtered ? 'No matching tickets.' : 'Nothing here.' }}
      </p>

      <TransitionGroup name="board-card">
        <div
          v-for="ticket in visibleItems"
          :key="ticket.uid"
          draggable="true"
          class="cursor-grab rounded-lg active:cursor-grabbing"
          :class="{ 'rotate-2 opacity-40': draggingUid === ticket.uid, 'board-card-flash': flashUid === ticket.uid }"
          @dragstart="onCardDragStart($event, ticket)"
          @dragend="emit('drag-end')"
        >
          <TicketCard
            :ticket="ticket"
            :subticket-count="childCounts[ticket.uid] ?? 0"
            :thread="activeThreads?.[ticket.uid] ?? null"
            @updated="emit('updated', $event)"
            @deleted="emit('deleted', $event)"
          />
        </div>
      </TransitionGroup>

      <Button
        v-if="cappedCount > 0"
        variant="ghost"
        size="sm"
        class="w-full text-xs text-muted-foreground"
        @click="showAllCards = true"
      >
        Show {{ cappedCount }} more…
      </Button>
    </div>
  </section>
</template>

<style scoped>
.board-card-enter-active,
.board-card-leave-active {
  transition: opacity 200ms cubic-bezier(.2, .7, .2, 1), transform 200ms cubic-bezier(.2, .7, .2, 1);
}
.board-card-enter-from {
  opacity: 0;
  transform: translateY(6px) scale(0.98);
}
.board-card-leave-to {
  opacity: 0;
  transform: scale(0.96);
}
.board-card-leave-active {
  position: absolute;
  width: calc(100% - 1rem); /* lane px-2 padding on both sides */
}
.board-card-move {
  transition: transform 250ms cubic-bezier(.2, .7, .2, 1);
}

/* Post-drop confirmation flash on the moved card (700ms, then gone). */
.board-card-flash {
  animation: board-card-flash 700ms cubic-bezier(0.25, 0.1, 0.25, 1) both;
}
@keyframes board-card-flash {
  0% { box-shadow: 0 0 0 2px hsl(var(--primary) / 0.55); }
  100% { box-shadow: 0 0 0 2px hsl(var(--primary) / 0); }
}
</style>
