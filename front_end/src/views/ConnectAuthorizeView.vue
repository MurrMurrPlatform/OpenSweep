<script setup lang="ts">
// OAuth consent for `opensweep connect` (unified dev flow, cloud auth).
// The backend gateway (/oauth/authorize) validated the client and redirected
// here; this view runs as the LOGGED-IN user (the router's auth guard sends
// anonymous visitors through the Zitadel login first). Approving calls the
// authenticated approve endpoint, which re-validates everything server-side,
// mints the single-use code, and returns the client redirect to follow.
//
// The client name shown to the user is FETCHED from the backend on mount
// (never taken from the URL query), so a hand-crafted /connect/authorize
// URL cannot spoof a friendly name over the top of a different client_id.
// The redirect_uri is likewise verified server-side against the client's
// registered list before we render it as a trusted destination.
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Plug, ShieldCheck, X } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError, apiGet, apiPost } from '@/services/api'
import { useCurrentUserStore } from '@/stores/currentUserStore'

const route = useRoute()
const currentUser = useCurrentUserStore()

const clientId = computed(() => String(route.query.client_id ?? ''))
const redirectUri = computed(() => String(route.query.redirect_uri ?? ''))
const state = computed(() => String(route.query.state ?? ''))
const codeChallenge = computed(() => String(route.query.code_challenge ?? ''))
const scope = computed(() => String(route.query.scope ?? 'mcp:read'))

const scopes = computed(() => scope.value.split(' ').filter(Boolean))

const SCOPE_DESCRIPTIONS: Record<string, string> = {
  'mcp:read': 'Read tickets, threads, plans, pull requests, docs, memories, findings, agents, and other org data; post comments.',
  'mcp:write': 'Also file findings, update tickets, propose docs and area edits, create scheduled agents and run policies, write memories, submit plans, and every other non-read tool.',
}

// Server-fetched metadata: the source of truth for what the user sees.
type ClientMetadata = {
  client_id: string
  client_name: string
  redirect_uris: string[]
  redirect_uri_registered: boolean
}
const metadata = ref<ClientMetadata | null>(null)
const metadataError = ref<string | null>(null)
const clientName = computed(() => metadata.value?.client_name || 'An MCP client')
const redirectRegistered = computed(() => metadata.value?.redirect_uri_registered ?? false)

const valid = computed(() =>
  Boolean(
    clientId.value
      && redirectUri.value
      && codeChallenge.value
      && metadata.value
      && redirectRegistered.value,
  ),
)

async function loadMetadata() {
  if (!clientId.value) return
  try {
    metadata.value = await apiGet<ClientMetadata>(
      `/oauth-mcp/client_metadata?client_id=${encodeURIComponent(clientId.value)}`
        + `&redirect_uri=${encodeURIComponent(redirectUri.value)}`,
    )
  } catch (e) {
    metadataError.value =
      e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
  }
}

onMounted(loadMetadata)

const busy = ref(false)
const error = ref<string | null>(null)

async function approve() {
  if (busy.value) return
  busy.value = true
  error.value = null
  try {
    const result = await apiPost<{ redirect_to: string }>('/oauth-mcp/approve', {
      client_id: clientId.value,
      redirect_uri: redirectUri.value,
      state: state.value,
      code_challenge: codeChallenge.value,
      scope: scope.value,
    })
    window.location.href = result.redirect_to
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    busy.value = false
  }
}

async function deny() {
  if (busy.value) return
  busy.value = true
  try {
    // Server-validated: only registered redirect URIs are followed (a raw
    // client-side redirect here would be an open redirect from our origin).
    const result = await apiPost<{ redirect_to: string }>('/oauth-mcp/deny', {
      client_id: clientId.value,
      redirect_uri: redirectUri.value,
      state: state.value,
    })
    window.location.href = result.redirect_to
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    busy.value = false
  }
}
</script>

<template>
  <div class="grid min-h-[70vh] place-items-center p-6">
    <Card class="w-full max-w-md">
      <CardHeader>
        <CardTitle class="flex items-center gap-2 text-base">
          <Plug class="size-4 text-muted-foreground" /> Connect a local agent
        </CardTitle>
      </CardHeader>
      <CardContent class="space-y-4">
        <template v-if="metadataError">
          <p class="text-sm text-bad">
            Couldn't verify this connection request: {{ metadataError }}
          </p>
        </template>
        <template v-else-if="!clientId || !redirectUri || !codeChallenge">
          <p class="text-sm text-bad">
            This authorization link is incomplete — start the connection again from your
            MCP client.
          </p>
        </template>
        <template v-else-if="metadata && !redirectRegistered">
          <p class="text-sm text-bad">
            The redirect address in this link isn't registered for
            <span class="font-semibold">{{ clientName }}</span> — this is either a
            malformed link or someone attempting to redirect the connection code
            elsewhere. Start the connection again from your MCP client.
          </p>
        </template>
        <template v-else-if="!metadata">
          <p class="text-sm text-muted-foreground">Verifying connection request…</p>
        </template>
        <template v-else-if="valid">
          <p class="text-sm">
            <span class="font-semibold">{{ clientName }}</span> wants to access OpenSweep as
            <span class="font-semibold">{{ currentUser.displayName || 'you' }}</span
            >, inside your organization only.
          </p>
          <div class="rounded-md border bg-muted/40 p-2 text-xs">
            <div class="mb-0.5 text-muted-foreground">Code will be delivered to</div>
            <code class="break-all">{{ redirectUri }}</code>
          </div>
          <ul class="space-y-2">
            <li v-for="s in scopes" :key="s" class="flex items-start gap-2 text-sm">
              <ShieldCheck class="mt-0.5 size-4 shrink-0 text-good" />
              <span>
                <Badge variant="outline" class="mr-1 px-1.5 text-[10px]">{{ s }}</Badge>
                {{ SCOPE_DESCRIPTIONS[s] ?? s }}
              </span>
            </li>
          </ul>
          <p class="text-xs text-muted-foreground">
            The client receives short-lived tokens that it refreshes automatically. You can
            revoke access at any time by signing the agent out, and tokens expire on their
            own.
          </p>
          <p v-if="error" class="text-sm text-bad">{{ error }}</p>
          <div class="flex gap-2">
            <Button class="flex-1" :loading="busy" @click="approve">Approve</Button>
            <Button class="flex-1" variant="outline" :disabled="busy" @click="deny">
              <X /> Deny
            </Button>
          </div>
        </template>
      </CardContent>
    </Card>
  </div>
</template>
