<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Check, ChevronDown, ChevronRight, X } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { MarkdownView } from '@/components/ui/markdown'
import { priorityVariant } from '@/components/tickets/ticketMeta'
import type { EpicProposalDTO } from '@/types/api'

const props = defineProps<{
  proposal: EpicProposalDTO
  /** Another approve/reject is in flight anywhere in the panel — lock the buttons. */
  disabled?: boolean
  /** THIS epic's action is in flight — spinner on Approve. */
  acting?: boolean
  /**
   * Fallback uid → title resolver, used only for proposals that predate
   * `member_titles`. Without it those members render as short uids.
   */
  resolveMemberTitle?: (uid: string) => string
}>()

const emit = defineEmits<{
  approve: [EpicProposalDTO]
  reject: [EpicProposalDTO]
}>()

const open = ref(false)

const memberCount = computed(() => props.proposal.member_ticket_uids.length)

/** Server-resolved titles win; the prop lookup is the pre-rollout fallback. */
const members = computed(() =>
  props.proposal.member_ticket_uids.map((uid, i) => ({
    uid,
    title:
      props.proposal.member_titles?.[i] ||
      props.resolveMemberTitle?.(uid) ||
      uid.slice(0, 8),
  })),
)

const labels = computed(() => props.proposal.suggested_labels || [])

// ── Evidence → one line ─────────────────────────────────────────────────────
// The backend sends a small per-axis object. Rather than switch on every axis
// (the vocabulary grows server-side), we pick the most identifying key present
// in priority order and render it; unknown shapes fall through to whatever
// scalar-ish key the payload does carry, so a new axis still says something.

/** Keys that only restate what the card header already shows. */
const EVIDENCE_NOISE = new Set([
  'axis',
  'plan_uid',
  'member_count',
  'member_uids',
  // Which members the agent added to a computed cluster. Real information, but
  // raw uids read as noise on a one-line card; the member list below has them.
  'added_uids',
])

/**
 * Most identifying first — the winner leads the evidence line.
 *
 * The judged claims (`root_cause`, `theme`) come first because they ARE the
 * epic: a root-cause proposal also carries `shared_paths`, and leading with
 * the path renders it identically to a computed `files` grouping, hiding the
 * one thing a human is being asked to check. `extends` trails everything —
 * "and it grew this computed cluster" is context for the claim, not the claim.
 */
const EVIDENCE_PRIORITY = [
  'root_cause',
  'theme',
  'shared_paths',
  'paths',
  'files',
  'area_key',
  'area',
  'feature_key',
  'feature',
  'class',
  'lens_key',
  'lens',
  'link_kind',
  'linked_uids',
  'extends',
]

/** `["a.py","b.py","c.py"]` → `a.py +2`; scalars stringify; anything else is skipped. */
function formatEvidenceValue(value: unknown): string {
  if (Array.isArray(value)) {
    const items = value.filter((v) => typeof v === 'string' || typeof v === 'number')
    if (!items.length) return ''
    return items.length > 1 ? `${items[0]} +${items.length - 1}` : String(items[0])
  }
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

const evidenceLine = computed(() => {
  const evidence = props.proposal.evidence
  if (!evidence || typeof evidence !== 'object') return ''
  const keys = Object.keys(evidence).filter((k) => !EVIDENCE_NOISE.has(k))
  keys.sort((a, b) => {
    const ai = EVIDENCE_PRIORITY.indexOf(a)
    const bi = EVIDENCE_PRIORITY.indexOf(b)
    return (ai === -1 ? EVIDENCE_PRIORITY.length : ai) - (bi === -1 ? EVIDENCE_PRIORITY.length : bi)
  })
  const parts: string[] = []
  for (const key of keys) {
    const text = formatEvidenceValue(evidence[key])
    // Only the leading fact is self-explanatory; later ones need their key.
    if (text) parts.push(parts.length ? `${key.replace(/_/g, ' ')}: ${text}` : text)
    if (parts.length === 2) break
  }
  return parts.join(' · ')
})

/**
 * Rationale is model markdown. Collapsed we want one flat line, so strip the
 * markup that would otherwise render as literal `**`/`#` noise inside a clamp.
 */
const rationaleOneLine = computed(() =>
  (props.proposal.rationale || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[*_`#>]/g, '')
    .replace(/\s+/g, ' ')
    .trim(),
)

</script>

<template>
  <div class="py-2">
    <!-- Collapsed: two lines — identity row, then the evidence that justifies it -->
    <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
      <div class="flex min-w-0 flex-1 items-center gap-1.5">
        <button
          type="button"
          class="flex min-w-0 flex-1 items-center gap-1.5 rounded-sm text-left text-sm hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :aria-expanded="open"
          @click="open = !open"
        >
          <component :is="open ? ChevronDown : ChevronRight" class="size-3.5 shrink-0 text-muted-foreground" />
          <Badge v-if="proposal.axis" variant="outline" class="shrink-0 px-1.5 text-[10px]">{{ proposal.axis }}</Badge>
          <span class="truncate font-medium">{{ proposal.title }}</span>
        </button>
        <span class="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
          {{ memberCount }} {{ memberCount === 1 ? 'ticket' : 'tickets' }}
        </span>
        <Badge :variant="priorityVariant(proposal.suggested_priority)" class="shrink-0 px-1.5 text-[10px]">
          {{ proposal.suggested_priority }}
        </Badge>
        <!-- Capped at 3 — a heavily-labelled epic would otherwise push its own
             title off the row. The rest are in the +N tooltip. -->
        <Badge
          v-for="label in labels.slice(0, 3)"
          :key="label"
          variant="secondary"
          class="shrink-0 px-1.5 text-[10px]"
        >
          {{ label }}
        </Badge>
        <span
          v-if="labels.length > 3"
          class="shrink-0 text-[10px] text-muted-foreground"
          :title="labels.join(', ')"
        >
          +{{ labels.length - 3 }}
        </span>
      </div>
      <div class="flex shrink-0 items-center gap-1.5">
        <Button variant="outline" size="sm" :disabled="disabled" :loading="acting" @click="emit('approve', proposal)">
          <Check /> Approve
        </Button>
        <Button variant="ghost" size="sm" :disabled="disabled" @click="emit('reject', proposal)">
          <X /> Reject
        </Button>
      </div>
    </div>

    <!-- Second line: evidence leads (it identifies the epic), rationale trails.
         Both share one clamped line so a collapsed card stays ~2 lines tall. -->
    <p
      v-if="!open && (evidenceLine || rationaleOneLine)"
      class="ml-5 line-clamp-1 text-xs text-muted-foreground"
    >
      <span v-if="evidenceLine" class="font-mono text-foreground/70">{{ evidenceLine }}</span>
      <span v-if="evidenceLine && rationaleOneLine"> · </span>
      <span v-if="rationaleOneLine">{{ rationaleOneLine }}</span>
    </p>

    <div v-if="open" class="ml-5 mt-1.5 space-y-2">
      <p v-if="evidenceLine" class="break-all font-mono text-xs text-muted-foreground">{{ evidenceLine }}</p>
      <ul class="space-y-0.5 text-xs">
        <li v-for="m in members" :key="m.uid">
          <RouterLink
            :to="{ name: 'ticket-detail', params: { uid: m.uid } }"
            class="text-muted-foreground hover:text-primary hover:underline"
          >
            {{ m.title }}
          </RouterLink>
        </li>
      </ul>
      <MarkdownView
        v-if="proposal.rationale"
        :model-value="proposal.rationale"
        preview-only
        class="text-xs text-muted-foreground"
      />
    </div>
  </div>
</template>
