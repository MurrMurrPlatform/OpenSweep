import type { RouteLocationRaw, Router } from 'vue-router'
import { useUiStore } from '@/stores/uiStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useCurrentUserStore } from '@/stores/currentUserStore'
import { loadStoredUser, signIn } from '@/services/auth'

export function installGuards(router: Router) {
  // Auth first: every navigation needs a live Zitadel session (main.ts
  // refuses to mount without the OIDC config, so auth is always on here).
  // The callback route completes the redirect and is exempt.
  router.beforeEach(async (to) => {
    // Public marketing routes need no session — but signed-in visitors go
    // straight into the app instead of the pitch.
    if (to.meta.public) {
      if (await loadStoredUser()) return { name: 'root' }
      return true
    }
    if (to.name === 'auth-callback') return true
    const user = await loadStoredUser()
    if (!user) {
      // When a cloud overlay registered a marketing page (route 'landing'),
      // the bare root lands there. Without one — the public build — every
      // signed-out visit goes straight to Zitadel and returns via /auth/callback.
      if (
        (to.fullPath === '/' || to.redirectedFrom?.fullPath === '/') &&
        router.hasRoute('landing')
      ) {
        return { name: 'landing' }
      }
      await signIn(to.fullPath)
      return false // navigation stops — browser is redirecting to Zitadel
    }
    return true
  })

  router.beforeEach(async (to) => {
    if (to.meta.public) return true // marketing pages skip app bootstrapping

    const ui = useUiStore()
    const repos = useRepositoryStore()

    // Resolve the real current user once per session. Awaited so a fresh
    // org owner's FIRST landing already redirects to onboarding; load()
    // swallows failures (local-user defaults stay, retried next navigation).
    const currentUser = useCurrentUserStore()
    if (!currentUser.loaded) await currentUser.load()

    // Fresh org owners stay in the onboarding wizard until they finish (or
    // skip) it — every other destination bounces back to /welcome.
    if (
      currentUser.loaded && !currentUser.onboarded &&
      to.name !== 'welcome' && to.name !== 'auth-callback'
    ) {
      return { name: 'welcome' }
    }

    // Finished onboarding — /welcome no longer applies.
    if (currentUser.loaded && currentUser.onboarded && to.name === 'welcome') {
      return { name: 'root' }
    }

    // 1. /repositories/:uid[/...] → /r/:slug/[...]
    const legacyUidTarget = to.meta.legacyRepoUidRedirect as string | undefined
    if (legacyUidTarget && to.params.uid) {
      const uidParam = String(to.params.uid)
      try {
        const repo = await repos.get(uidParam)
        const params: Record<string, string> = { repoSlug: repo.slug }
        if (to.params.nodeId) params.nodeId = String(to.params.nodeId)
        const next: RouteLocationRaw = {
          name: legacyUidTarget,
          params,
          query: to.query,
          hash: to.hash,
        }
        return next
      } catch {
        return { name: 'repositories', replace: true }
      }
    }

    // 2. Sync :repoSlug param → ui.currentRepoSlug.
    const slugParam = to.params.repoSlug
    if (slugParam) {
      const slug = Array.isArray(slugParam) ? slugParam[0] : slugParam
      if (slug && slug !== ui.currentRepoSlug) ui.setCurrentRepoSlug(slug)
    }

    return true
  })
}
