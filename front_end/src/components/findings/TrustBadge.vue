<script setup lang="ts">
/**
 * The trust score as a tiered chip, with the evidence behind it in a tooltip.
 *
 * The number alone is not actionable — what makes a finding worth opening is
 * *why* the platform believes it (runs that agree, an analyzer that matched),
 * so the tooltip always enumerates the signals, positive and negative.
 */
import { computed } from 'vue'
import { ShieldCheck } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import TrustSignalList from './TrustSignalList.vue'
import { trustPercent, trustSignals, trustTier, type VerificationOutcome } from './findingMeta'
import type { FindingDTO } from '@/types/api'

const props = withDefaults(
  defineProps<{
    finding: FindingDTO
    /** Skeptic-pass outcome, when the caller has loaded verification runs. */
    verification?: VerificationOutcome
    /** Dense variant for list rows. */
    compact?: boolean
  }>(),
  { verification: 'none', compact: false },
)

const tier = computed(() => trustTier(props.finding.trust))
const percent = computed(() => trustPercent(props.finding.trust))
const signals = computed(() => trustSignals(props.finding, props.verification))
</script>

<template>
  <Tooltip>
    <TooltipTrigger as-child>
      <Badge :variant="tier.variant" :class="compact ? 'px-1.5 text-[10px]' : ''">
        <ShieldCheck :class="compact ? 'size-2.5' : 'size-3'" />
        {{ percent }}%
      </Badge>
    </TooltipTrigger>
    <TooltipContent class="max-w-72">
      <p class="text-xs font-semibold">{{ tier.label }} · {{ percent }}%</p>
      <TrustSignalList :signals="signals" class="mt-1.5" />
    </TooltipContent>
  </Tooltip>
</template>
