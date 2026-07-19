<script setup lang="ts">
import { onMounted } from 'vue'
import { Boxes } from 'lucide-vue-next'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { PageHeader } from '@/components/ui/page-header'
import { EmptyState } from '@/components/ui/empty-state'
import SandboxRow from '@/components/execution/SandboxRow.vue'
import { useSandboxStore } from '@/stores/sandboxStore'
import { useToast } from '@/composables/useToast'

const store = useSandboxStore()
const toast = useToast()

onMounted(() => store.fetchAll())

async function destroy(uid: string) {
  try {
    await store.destroy(uid)
    toast.info('Sandbox destroyed', uid)
  } catch (e: any) {
    toast.error('Destroy failed', e.detail || e.message)
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Sandboxes"
      subtitle="Per-run workspaces. Auto-cleaned after the retention window; you can also destroy manually."
    />

    <Card>
      <CardHeader class="p-4">
        <CardTitle class="text-base">Active sandboxes</CardTitle>
        <span class="font-mono text-xs text-muted-foreground">~/.opensweep/sandboxes/&lt;uid&gt;/</span>
      </CardHeader>
      <CardContent class="p-0">
        <div v-if="!store.loaded" class="flex flex-col gap-2 p-4">
          <Skeleton v-for="i in 4" :key="i" class="h-12" />
        </div>
        <div v-else-if="store.list.length" class="stagger-children p-2">
          <SandboxRow v-for="s in store.list" :key="s.uid" :sandbox="s" @destroy="destroy" />
        </div>
        <div v-else class="p-4">
          <EmptyState
            :icon="Boxes"
            title="No active sandboxes"
            description="Sandboxes are created on demand when an agent run needs a repo clone."
            class="border-0"
          />
        </div>
      </CardContent>
    </Card>
  </div>
</template>
