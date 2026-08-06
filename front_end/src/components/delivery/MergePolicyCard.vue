<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Shield } from 'lucide-vue-next'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import type { MergePolicyDTO } from '@/types/api'

interface Props {
  repositoryUid: string
}
const props = defineProps<Props>()

const delivery = useDeliveryStore()
const toast = useToast()

const policy = ref<MergePolicyDTO | null>(null)
const loading = ref(true)
const saving = ref(false)
const loadError = ref<string | null>(null)

// Editable copies — the form owns these, `policy` mirrors the server.
const denylistText = ref('')
const requireCleanRound = ref(true)
const maxFixRounds = ref('2')
const enforceConvergedStatus = ref(false)
// What GitHub actually did on the last flip — only set by a save response,
// never persisted, so a reload clears it.
const protectionOutcome = ref<string | null>(null)

const protectionWarning = computed(() => {
  if (!enforceConvergedStatus.value) return null
  if (protectionOutcome.value === 'not-required')
    return 'Saved, but GitHub is not enforcing this: the default branch already has a protection rule, and OpenSweep never rewrites one. Add the opensweep/converged context to that rule in GitHub’s branch settings.'
  if (protectionOutcome.value === 'failed')
    return 'Saved, but GitHub is not enforcing this: OpenSweep could not write branch protection (the connected credential needs repo admin rights).'
  return null
})

function hydrate(p: MergePolicyDTO) {
  policy.value = p
  denylistText.value = (p.path_denylist ?? []).join('\n')
  requireCleanRound.value = p.require_clean_round
  maxFixRounds.value = String(p.max_fix_rounds)
  enforceConvergedStatus.value = p.enforce_converged_status ?? false
  // Outcome is per-save, never persisted — `save()` re-sets it right after.
  protectionOutcome.value = null
}

async function load() {
  loading.value = true
  loadError.value = null
  try {
    hydrate(await delivery.getMergePolicy(props.repositoryUid))
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.repositoryUid, () => void load())

const parsedDenylist = computed(() =>
  denylistText.value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean),
)

const maxRoundsValid = computed(() => {
  const n = Number(maxFixRounds.value)
  return Number.isInteger(n) && n >= 0 && n <= 10
})

const dirty = computed(() => {
  const p = policy.value
  if (!p) return false
  return (
    parsedDenylist.value.join('\n') !== (p.path_denylist ?? []).join('\n') ||
    requireCleanRound.value !== p.require_clean_round ||
    Number(maxFixRounds.value) !== p.max_fix_rounds ||
    enforceConvergedStatus.value !== (p.enforce_converged_status ?? false)
  )
})

async function save() {
  if (saving.value || !maxRoundsValid.value) return
  saving.value = true
  try {
    const updated = await delivery.updateMergePolicy(props.repositoryUid, {
      path_denylist: parsedDenylist.value,
      require_clean_round: requireCleanRound.value,
      max_fix_rounds: Number(maxFixRounds.value),
      enforce_converged_status: enforceConvergedStatus.value,
    })
    hydrate(updated)
    // Saving the toggle ON is not the same as GitHub enforcing it: the branch
    // may already have a protection rule we refuse to rewrite, or the
    // credential may lack repo admin. Say so instead of letting the switch
    // imply a gate that isn't there.
    const outcome = updated.branch_protection_outcome ?? null
    if (outcome) protectionOutcome.value = outcome
    if (outcome === 'not-required') {
      toast.error(
        'Saved, but GitHub is not enforcing it',
        'The default branch already has a branch protection rule. OpenSweep never rewrites an existing rule — add the opensweep/converged context to it in GitHub’s branch settings.',
      )
    } else if (outcome === 'failed') {
      toast.error(
        'Saved, but GitHub is not enforcing it',
        'OpenSweep could not write branch protection — the connected credential needs repo admin rights. Add the opensweep/converged required check by hand, or reconnect with an admin credential.',
      )
    } else {
      toast.success('Merge policy saved')
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save merge policy', msg)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Card>
    <CardHeader class="flex-row items-center justify-between gap-3 space-y-0">
      <CardTitle class="flex items-center gap-2 text-base">
        <Shield class="h-4 w-4 text-muted-foreground" /> Merge policy
      </CardTitle>
      <Button
        size="sm"
        :disabled="loading || !policy || !dirty || !maxRoundsValid"
        :loading="saving"
        @click="save"
      >
        Save
      </Button>
    </CardHeader>
    <CardContent>
      <div v-if="loading" class="text-sm text-muted-foreground">Loading policy…</div>
      <div v-else-if="loadError" class="text-sm text-muted-foreground">
        Couldn’t load the merge policy: {{ loadError }}
        <Button variant="outline" size="sm" class="ml-2" @click="load">Retry</Button>
      </div>
      <div v-else-if="policy" class="space-y-4 text-sm">
        <div class="space-y-1">
          <Label>Path denylist (regex, one per line)</Label>
          <Textarea
            v-model="denylistText"
            :rows="4"
            class="font-mono text-xs"
            placeholder="^\.github/workflows/&#10;^deploy/"
          />
          <p class="text-xs text-muted-foreground">
            Write-path runs may never touch matching paths — the gate rejects the whole commit.
          </p>
        </div>

        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-xs font-medium text-foreground">Require clean round</div>
            <p class="text-xs text-muted-foreground">Last review at head must introduce zero new blocking findings.</p>
          </div>
          <Switch v-model="requireCleanRound" />
        </div>

        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-xs font-medium text-foreground">Max fix rounds</div>
            <p class="text-xs text-muted-foreground">Automated fix runs per PR before a human is required (0–10).</p>
          </div>
          <Input v-model="maxFixRounds" type="number" min="0" max="10" class="w-20 text-center" />
        </div>
        <p v-if="!maxRoundsValid" class="text-xs text-destructive">
          Max fix rounds must be a whole number between 0 and 10.
        </p>

        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-xs font-medium text-foreground">Enforce the converged check on GitHub</div>
            <p class="text-xs text-muted-foreground">
              Off by default. Turning this on writes a branch protection rule to this
              repository&rsquo;s GitHub settings, making
              <span class="font-mono">opensweep/converged</span> a required check on the default
              branch — for <em>every</em> pull request, not just OpenSweep&rsquo;s. Existing
              protection rules are never modified.
            </p>
          </div>
          <Switch v-model="enforceConvergedStatus" />
        </div>
        <p v-if="protectionWarning" class="text-xs text-warn">
          {{ protectionWarning }}
        </p>
      </div>
    </CardContent>
  </Card>
</template>
