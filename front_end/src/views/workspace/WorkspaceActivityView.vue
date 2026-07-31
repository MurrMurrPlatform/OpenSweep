<script setup lang="ts">
/**
 * Workspace Activity tab — this repository's slice of the audit stream
 * (every state change is an :Event node), newest first. Same table as the
 * org-wide AuditLogView, pre-scoped by repository_uid.
 */
import { ref, watch } from 'vue'
import { History } from 'lucide-vue-next'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import AuditFilters from '@/components/audit/AuditFilters.vue'
import AuditEventRow from '@/components/audit/AuditEventRow.vue'
import { useAuditStore } from '@/stores/auditStore'
import { useWorkspaceCtx } from './workspaceContext'

const { repoUid } = useWorkspaceCtx()
const store = useAuditStore()
const subjectFilter = ref('')
const kindFilter = ref('')
const loading = ref(true)

async function refresh() {
  const uid = repoUid.value
  if (!uid) return
  loading.value = true
  try {
    await store.fetchAll({
      subject_type: subjectFilter.value || undefined,
      kind: kindFilter.value || undefined,
      repository_uid: uid,
      limit: 200,
    })
  } finally {
    if (repoUid.value === uid) loading.value = false
  }
}

// repoUid resolves async on cold deep-links — immediate + watch covers both.
watch([repoUid, subjectFilter, kindFilter], refresh, { immediate: true })
</script>

<template>
  <div class="flex flex-col gap-4">
    <Card>
      <CardHeader class="p-4">
        <CardTitle class="text-base">Filters</CardTitle>
      </CardHeader>
      <CardContent class="p-4 pt-0">
        <AuditFilters
          v-model:subjectFilter="subjectFilter"
          v-model:kindFilter="kindFilter"
        />
      </CardContent>
    </Card>

    <Card>
      <CardContent class="p-0">
        <div v-if="loading" class="flex flex-col gap-2 p-4">
          <Skeleton v-for="i in 8" :key="i" class="h-10" />
        </div>
        <div v-else-if="store.list.length" class="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead class="w-[120px]">Time</TableHead>
                <TableHead class="w-[140px]">Subject</TableHead>
                <TableHead>Event</TableHead>
                <TableHead class="text-right">When</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <AuditEventRow v-for="e in store.list" :key="e.uid" :event="e" />
            </TableBody>
          </Table>
        </div>
        <div v-else class="p-4">
          <EmptyState
            :icon="History"
            title="No activity yet"
            description="Every state change in this workspace — runs, findings, docs, deliveries — is recorded here as it happens."
            class="border-0"
          />
        </div>
      </CardContent>
    </Card>
  </div>
</template>
