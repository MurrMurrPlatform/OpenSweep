<script setup lang="ts">
import { Badge } from '@/components/ui/badge'
import { producesBadgeVariant, producesLabel } from '@/lib/produces'

defineProps<{ produces: string }>()
</script>

<template>
  <Badge :variant="producesBadgeVariant(produces)">{{ producesLabel(produces) }}</Badge>
</template>
