<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Layers, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { useCampaignStore } from '@/stores/campaignStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import {
  CAMPAIGN_TEMPLATE_LABELS,
  campaignProgress,
  campaignStatusVariant,
} from '@/lib/campaignStatus'
import { formatRelativeTime } from '@/lib/utils'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Button } from '@/components/ui/button'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import NewCampaignDialog from '@/components/campaigns/NewCampaignDialog.vue'
import type { CampaignDTO } from '@/types/api'

const router = useRouter()
const campaigns = useCampaignStore()
const toast = useToast()
const { uid: repoUid } = useCurrentRepo()

const items = ref<CampaignDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const createOpen = ref(false)

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
  try {
    items.value = await campaigns.fetchForRepo(repoUid.value)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)
watch(repoUid, reload)

const sorted = computed(() =>
  [...items.value].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '')),
)

function progressLabel(c: CampaignDTO): string {
  const { finished, total } = campaignProgress(c)
  return `${finished}/${total} parts`
}

function onCreated(campaign: CampaignDTO) {
  void router.push({ name: 'campaign-detail', params: { uid: campaign.uid } })
}

// ── Delete (running/finalizing campaigns must be cancelled first) ────────────

// The confirm dialog is driven by a separate `open` boolean, not by the target
// ref being non-null: AlertDialogAction closes the dialog (update:open fires)
// before the forwarded @click runs, so deriving `open` from the target and
// nulling it on close would wipe the target before confirmDelete could read it.
const deleteTarget = ref<CampaignDTO | null>(null)
const deleteOpen = ref(false)
const deleting = ref(false)

function requestDelete(c: CampaignDTO) {
  deleteTarget.value = c
  deleteOpen.value = true
}

function isLive(c: CampaignDTO): boolean {
  return c.status === 'running' || c.status === 'finalizing'
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target || deleting.value) return
  deleting.value = true
  try {
    await campaigns.remove(target.uid)
    items.value = items.value.filter((c) => c.uid !== target.uid)
    toast.success('Campaign deleted', target.title || target.uid.slice(0, 12))
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t delete campaign', msg)
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Campaigns"
      subtitle="Bounded audit sweeps — partition the repo, run every part through your lenses, roll the results into one digest."
    >
      <Button variant="outline" size="sm" :disabled="loading" @click="reload">
        <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
      </Button>
      <Button size="sm" @click="createOpen = true">
        <Plus /> New campaign
      </Button>
    </PageHeader>

    <Card>
      <CardContent class="p-0">
        <!-- Loading -->
        <div v-if="loading" class="p-4 space-y-2">
          <Skeleton v-for="i in 6" :key="i" class="h-10" />
        </div>

        <!-- Error -->
        <div v-else-if="error" class="p-4">
          <ErrorState title="Couldn't load campaigns" :message="error" class="border-0">
            <Button variant="outline" size="sm" @click="reload">Retry</Button>
          </ErrorState>
        </div>

        <!-- Empty -->
        <div v-else-if="sorted.length === 0" class="p-4">
          <EmptyState
            :icon="Layers"
            title="No campaigns yet"
            description="Partition this repo into bounded audit runs — each part sweeps one area through your enabled lenses, and the results roll up into a single coverage digest."
            class="border-0"
          >
            <Button size="sm" @click="createOpen = true">
              <Plus /> New campaign
            </Button>
          </EmptyState>
        </div>

        <!-- Table -->
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th class="px-4 py-2 font-medium">Campaign</th>
                <th class="px-4 py-2 font-medium">Template</th>
                <th class="px-4 py-2 font-medium">Status</th>
                <th class="px-4 py-2 font-medium">Progress</th>
                <th class="px-4 py-2 font-medium">Created</th>
                <th class="px-4 py-2"><span class="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="c in sorted"
                :key="c.uid"
                class="cursor-pointer border-t border-border transition-colors hover:bg-accent"
                @click="$router.push({ name: 'campaign-detail', params: { uid: c.uid } })"
              >
                <td class="max-w-[280px] px-4 py-2">
                  <RouterLink
                    class="block truncate font-medium underline-offset-2 hover:underline"
                    :to="{ name: 'campaign-detail', params: { uid: c.uid } }"
                    @click.stop
                  >
                    {{ c.title || `Campaign ${c.uid.slice(0, 12)}` }}
                  </RouterLink>
                </td>
                <td class="px-4 py-2">
                  <Badge variant="outline">{{ CAMPAIGN_TEMPLATE_LABELS[c.template] ?? c.template }}</Badge>
                </td>
                <td class="px-4 py-2">
                  <Badge :variant="campaignStatusVariant(c.status)">{{ c.status }}</Badge>
                </td>
                <td class="whitespace-nowrap px-4 py-2 tabular-nums">{{ progressLabel(c) }}</td>
                <td class="whitespace-nowrap px-4 py-2 text-muted-foreground">
                  {{ formatRelativeTime(c.created_at) }}
                </td>
                <td class="px-2 py-2 text-right">
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    class="text-muted-foreground hover:text-destructive"
                    :disabled="isLive(c)"
                    :title="isLive(c) ? 'Cancel the campaign before deleting it' : 'Delete campaign'"
                    @click.stop="requestDelete(c)"
                  >
                    <Trash2 />
                  </Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>

    <NewCampaignDialog
      v-if="repoUid"
      v-model:open="createOpen"
      :repository-uid="repoUid"
      @created="onCreated"
    />

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this campaign?</AlertDialogTitle>
          <AlertDialogDescription>
            “{{ deleteTarget?.title || deleteTarget?.uid.slice(0, 12) }}” and its plan are removed
            permanently. Runs it already dispatched — and the findings they produced — are kept.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDelete"
          >
            Delete campaign
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
