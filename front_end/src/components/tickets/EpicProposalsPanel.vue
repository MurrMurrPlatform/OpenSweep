<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, HelpCircle, Layers } from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import EpicCard from '@/components/tickets/EpicCard.vue'
import type { TicketDTO, EpicProposalDTO } from '@/types/api'

interface Props {
  repositoryUid: string
  /** Board tickets — fallback title lookup for proposals without `member_titles`. */
  tickets: TicketDTO[]
}
const props = defineProps<Props>()
/** `applied` fires after an approval so the board can reload. */
const emit = defineEmits<{ applied: [] }>()

const store = useTicketStore()
const toast = useToast()

const proposals = ref<EpicProposalDTO[]>([])
/** uid of the epic being acted on, or `'plan:<uid>'` during an Approve all. */
const acting = ref<string | null>(null)

async function reload() {
  if (!props.repositoryUid) {
    proposals.value = []
    return
  }
  try {
    proposals.value = await store.listEpicProposals({
      repository_uid: props.repositoryUid,
      status: 'proposed',
    })
  } catch {
    // Non-blocking side panel — the board stays usable without it.
    proposals.value = []
  }
}

watch(() => props.repositoryUid, () => void reload(), { immediate: true })
defineExpose({ reload })

function memberTitle(uid: string): string {
  return props.tickets.find((t) => t.uid === uid)?.title || uid.slice(0, 8)
}

interface PlanGroup {
  /** Stable v-for key; the plan uid, or the epic uid for ungrouped epics. */
  key: string
  planUid: string | null
  epics: EpicProposalDTO[]
  /** Distinct member tickets across the plan — the "12 tickets" in the header. */
  ticketCount: number
}

/**
 * Sibling epics produced by one planning pass share a `plan_uid` and are
 * reviewed together (12 tickets → 4 epics). Epics without one predate the
 * field, or were proposed standalone; they render bare, with no plan header.
 */
const planGroups = computed<PlanGroup[]>(() => {
  const groups: PlanGroup[] = []
  const byPlan = new Map<string, PlanGroup>()
  for (const p of proposals.value) {
    if (!p.plan_uid) {
      groups.push({ key: p.uid, planUid: null, epics: [p], ticketCount: p.member_ticket_uids.length })
      continue
    }
    let group = byPlan.get(p.plan_uid)
    if (!group) {
      group = { key: p.plan_uid, planUid: p.plan_uid, epics: [], ticketCount: 0 }
      byPlan.set(p.plan_uid, group)
      groups.push(group)
    }
    group.epics.push(p)
  }
  for (const group of byPlan.values()) {
    group.ticketCount = new Set(group.epics.flatMap((b) => b.member_ticket_uids)).size
  }
  return groups
})

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

async function approve(p: EpicProposalDTO) {
  if (acting.value) return
  acting.value = p.uid
  try {
    const approved = await store.approveEpic(p.uid)
    proposals.value = proposals.value.filter((x) => x.uid !== p.uid)
    toast.success('Epic approved', `Parent ticket created for “${approved.title}”`)
    emit('applied')
  } catch (e) {
    toast.error('Approve failed', errorText(e))
  } finally {
    acting.value = null
  }
}

async function reject(p: EpicProposalDTO) {
  if (acting.value) return
  acting.value = p.uid
  try {
    await store.rejectEpic(p.uid)
    proposals.value = proposals.value.filter((x) => x.uid !== p.uid)
    toast.info('Epic rejected')
  } catch (e) {
    toast.error('Reject failed', errorText(e))
  } finally {
    acting.value = null
  }
}

/**
 * Approve every epic in a plan as one review action. The endpoint reports
 * partial success instead of failing the request, so a epic that went stale
 * since it was proposed does not stop its siblings from landing — which means
 * we must clear only the uids that actually succeeded and surface the rest.
 */
async function approvePlan(group: PlanGroup) {
  if (acting.value) return
  acting.value = `plan:${group.key}`
  const epics = [...group.epics]
  try {
    const result = await store.bulkApproveEpics(epics.map((p) => p.uid))
    const landed = new Set(result.approved.map((p) => p.uid))
    proposals.value = proposals.value.filter((x) => !landed.has(x.uid))
    if (result.errors.length) {
      toast.error(
        `Approved ${landed.size} of ${epics.length} epics`,
        result.errors.map((e) => e.detail).join('; '),
      )
    } else {
      toast.success(
        'Epics approved',
        `${landed.size} ${landed.size === 1 ? 'epic' : 'epics'} approved`,
      )
    }
    if (landed.size) emit('applied')
  } catch (e) {
    toast.error('Approve all failed', errorText(e))
  } finally {
    acting.value = null
  }
}
</script>

<template>
  <Card v-if="proposals.length">
    <CardContent class="space-y-2 p-6">
      <div class="flex items-center gap-2 text-sm font-semibold">
        <Layers class="size-4 text-muted-foreground" /> Proposed epics
        <span class="font-normal text-muted-foreground">· {{ proposals.length }}</span>
        <!-- The how-it-works blurb is one-time knowledge: on demand, not on every render. -->
        <Tooltip>
          <TooltipTrigger as-child>
            <button
              type="button"
              class="text-muted-foreground hover:text-foreground"
              aria-label="How ticket groups work"
            >
              <HelpCircle class="size-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent class="max-w-xs text-xs font-normal">
            Each of these says a set of work items should ship together as one epic — from a rule
            you ran, or from a grouping agent. The badge names the claim: an exact one
            (area, files, class) or a judged one (root cause, theme, co-change). Approving creates
            a parent ticket with the members as subtickets; nothing changes until you approve.
          </TooltipContent>
        </Tooltip>
      </div>

      <div class="divide-y">
        <div v-for="group in planGroups" :key="group.key" class="py-1">
          <div
            v-if="group.planUid"
            class="flex flex-wrap items-center justify-between gap-2 py-1 text-xs text-muted-foreground"
          >
            <span>
              {{ group.ticketCount }} {{ group.ticketCount === 1 ? 'ticket' : 'tickets' }} →
              {{ group.epics.length }} {{ group.epics.length === 1 ? 'epic' : 'epics' }}
            </span>
            <Button
              variant="ghost"
              size="sm"
              :disabled="acting !== null"
              :loading="acting === `plan:${group.key}`"
              @click="approvePlan(group)"
            >
              <Check /> Approve all
            </Button>
          </div>
          <EpicCard
            v-for="p in group.epics"
            :key="p.uid"
            :proposal="p"
            :acting="acting === p.uid"
            :disabled="acting !== null"
            :resolve-member-title="memberTitle"
            @approve="approve"
            @reject="reject"
          />
        </div>
      </div>
    </CardContent>
  </Card>
</template>
