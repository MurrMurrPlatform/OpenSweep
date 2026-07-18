import { createRouter, createWebHistory, type RouteRecordRaw, type RouteRecordRedirectOption } from 'vue-router'
import { useUiStore } from '@/stores/uiStore'
import { installGuards } from './guards'

const ShellLayout = () => import('@/layouts/ShellLayout.vue')

function scopedRedirect(target: string): RouteRecordRedirectOption {
  return (to) => {
    const ui = useUiStore()
    if (ui.currentRepoSlug) {
      return { name: target, params: { repoSlug: ui.currentRepoSlug }, query: to.query, hash: to.hash }
    }
    return { name: 'repositories' }
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: ShellLayout,
      children: [
        // ── Global ──────────────────────────────────────────────────────────
        // Root lands in the remembered workspace; falls back to the workspace list.
        { path: '', name: 'root', redirect: scopedRedirect('workspace-home') },
        { path: 'overview', name: 'overview', component: () => import('@/views/OverviewView.vue'),
          meta: { title: 'All workspaces', eyebrow: 'Overview', section: 'main' } },
        { path: 'repositories', name: 'repositories', component: () => import('@/views/RepositoryListView.vue'),
          meta: { title: 'Workspaces', eyebrow: 'Sources', section: 'main' } },

        // Audit is still reachable but no longer a sidebar entry; surfaces inside Run detail.
        { path: 'audit', name: 'audit', component: () => import('@/views/AuditLogView.vue'),
          meta: { title: 'Audit log', eyebrow: 'History', section: 'main' } },

        // ── Workspace-scoped (/r/:repoSlug/...) ─────────────────────────────
        {
          path: 'r/:repoSlug',
          meta: { repoScoped: true },
          children: [
            { path: '', name: 'workspace-home', component: () => import('@/views/RepositoryDetailView.vue'),
              meta: { title: 'Dashboard', eyebrow: 'Workspace', section: 'main', repoScoped: true } },
            { path: 'findings', name: 'findings', component: () => import('@/views/FindingsView.vue'),
              meta: { title: 'Findings', eyebrow: 'Inbox', section: 'main', repoScoped: true } },
            { path: 'ideas', name: 'ideas', component: () => import('@/views/FeatureIdeasView.vue'),
              meta: { title: 'Feature ideas', eyebrow: 'Inbox', section: 'main', repoScoped: true } },
            { path: 'news', name: 'news', component: () => import('@/views/NewsView.vue'),
              meta: { title: 'News', eyebrow: 'Inbox', section: 'main', repoScoped: true } },
            { path: 'tickets', name: 'tickets', component: () => import('@/views/TicketsView.vue'),
              meta: { title: 'Tickets', eyebrow: 'Deliver', section: 'main', repoScoped: true } },
            { path: 'queue', name: 'queue', component: () => import('@/views/QueueView.vue'),
              meta: { title: 'Queue', eyebrow: 'Deliver', section: 'main', repoScoped: true } },
            { path: 'docs', name: 'documentation', component: () => import('@/views/DocumentationView.vue'),
              meta: { title: 'Documentation', eyebrow: 'Knowledge', section: 'main', repoScoped: true } },
            { path: 'analyses', name: 'analyses', component: () => import('@/views/AnalysisListView.vue'),
              meta: { title: 'Analyses', eyebrow: 'Health', section: 'main' } },
            { path: 'health', name: 'health', component: () => import('@/views/HealthView.vue'),
              meta: { title: 'Health', eyebrow: 'Health', section: 'main', repoScoped: true } },
            { path: 'runs', name: 'runs', component: () => import('@/views/RunsView.vue'),
              meta: { title: 'Runs', eyebrow: 'Live', section: 'main', repoScoped: true } },
            { path: 'investigations', name: 'investigations', redirect: (to) => ({ name: 'runs', params: { repoSlug: to.params.repoSlug } }) },
            { path: 'ask', name: 'ask', component: () => import('@/views/AskView.vue'),
              meta: { title: 'Ask a question', eyebrow: 'Plan', section: 'main', repoScoped: true } },
          ],
        },

        // Detail pages stay flat — detail context is the item itself, not the workspace.
        // Ticket, thread and PR are the SAME piece of work: all three routes
        // render the unified WorkItemView (tabbed); the route picks the tab.
        { path: 'pull-requests/:uid', name: 'pull-request-detail', component: () => import('@/views/WorkItemView.vue'),
          meta: { title: 'Pull request', eyebrow: 'Deliver', section: 'main' } },
        { path: 'tickets/:uid', name: 'ticket-detail', component: () => import('@/views/WorkItemView.vue'),
          meta: { title: 'Ticket', eyebrow: 'Deliver', section: 'main' } },
        { path: 'threads/:uid', name: 'thread-detail', component: () => import('@/views/WorkItemView.vue'),
          meta: { title: 'Thread', eyebrow: 'Deliver', section: 'main' } },
        // OAuth consent for `opensweep connect` — the backend gateway
        // redirects MCP clients' browsers here (login enforced by the guard).
        { path: 'connect/authorize', name: 'connect-authorize', component: () => import('@/views/ConnectAuthorizeView.vue'),
          meta: { title: 'Connect agent', eyebrow: 'Connect', section: 'main' } },
        { path: 'findings/:uid', name: 'finding-detail', component: () => import('@/views/FindingDetailView.vue'),
          meta: { title: 'Finding', eyebrow: 'Inbox', section: 'main' } },
        { path: 'analyses/:uid', name: 'analysis-detail', component: () => import('@/views/AnalysisDetailView.vue'),
          meta: { title: 'Analysis', eyebrow: 'Health', section: 'main' } },
        { path: 'runs/:uid', name: 'run-detail', component: () => import('@/views/RunDetailView.vue'),
          meta: { title: 'Run', eyebrow: 'Live', section: 'main' } },
        { path: 'investigations/:uid', name: 'investigation-detail', component: () => import('@/views/InvestigationDetailView.vue'),
          meta: { title: 'Investigation', eyebrow: 'Plan', section: 'main' } },

        // ── Legacy redirects ────────────────────────────────────────────────
        // Old flat scoped paths → /r/:current/... if a workspace is remembered,
        // else /repositories.
        { path: 'findings', redirect: scopedRedirect('findings') },
        { path: 'tickets', redirect: scopedRedirect('tickets') },
        { path: 'queue', redirect: scopedRedirect('queue') },
        { path: 'knowledge', redirect: scopedRedirect('documentation') },
        { path: 'health', redirect: scopedRedirect('health') },
        { path: 'runs', redirect: scopedRedirect('runs') },
        { path: 'investigations', redirect: scopedRedirect('investigations') },
        { path: 'ask', redirect: scopedRedirect('ask') },
        { path: 'issues', redirect: scopedRedirect('findings') },
        { path: 'proposals', redirect: scopedRedirect('findings') },
        { path: 'improvements', redirect: scopedRedirect('findings') },
        { path: 'docs', redirect: scopedRedirect('documentation') },
        { path: 'conventions', redirect: scopedRedirect('documentation') },
        { path: 'memories', redirect: scopedRedirect('documentation') },
        // /repositories/:uid → /r/:slug/... resolved by guard via uid lookup.
        { path: 'repositories/:uid', meta: { legacyRepoUidRedirect: 'workspace-home' },
          component: () => import('@/views/RepositoryDetailView.vue') },

        // ── Settings (every org user) ───────────────────────────────────────
        { path: 'settings/account', name: 'account-settings', component: () => import('@/views/settings/AccountSettingsView.vue'),
          meta: { title: 'Account', eyebrow: 'Settings', section: 'settings' } },
        { path: 'settings/organization', name: 'organization-settings', component: () => import('@/views/settings/OrganizationSettingsView.vue'),
          meta: { title: 'Organization', eyebrow: 'Settings', section: 'settings' } },
// Path is /settings/slack because the Slack OAuth callback 302s to
        // exactly this URL with ?slack=connected|denied|error.
        { path: 'settings/slack', name: 'slack-settings', component: () => import('@/views/settings/SlackSettingsView.vue'),
          meta: { title: 'Slack', eyebrow: 'Settings', section: 'settings' } },
        // Org agent overlays — any org member tunes per-playbook agent guidance.
        { path: 'settings/agents', name: 'agent-overlays-settings', component: () => import('@/views/settings/AgentOverlaysView.vue'),
          meta: { title: 'Agents', eyebrow: 'Settings', section: 'settings' } },

        // ── Admin ───────────────────────────────────────────────────────────
        // Path is /settings/github (not /admin/…) because the GitHub App
        // callback 302s to exactly this URL after App creation.
        { path: 'settings/github', name: 'settings-github', component: () => import('@/views/admin/GitHubSettingsView.vue'),
          meta: { title: 'GitHub', eyebrow: 'Admin', section: 'admin' } },
        { path: 'admin/run-policies', name: 'admin-run-policies', component: () => import('@/views/RunPoliciesView.vue'),
          meta: { title: 'Run policies', eyebrow: 'Admin', section: 'admin' } },
        { path: 'admin/agent-prompts', name: 'admin-agent-prompts', component: () => import('@/views/admin/AgentPromptsView.vue'),
          meta: { title: 'Agent prompts', eyebrow: 'Admin', section: 'admin' } },
        { path: 'admin/platform-config', name: 'admin-platform-config', component: () => import('@/views/PlatformConfigView.vue'),
          meta: { title: 'Platform config', eyebrow: 'Admin', section: 'admin' } },
        { path: 'admin/llm-providers', name: 'admin-llm-providers', component: () => import('@/views/LLMProvidersView.vue'),
          meta: { title: 'LLM providers', eyebrow: 'Admin', section: 'admin' } },
        { path: 'admin/sandboxes', name: 'admin-sandboxes', component: () => import('@/views/SandboxesView.vue'),
          meta: { title: 'Sandboxes', eyebrow: 'Admin', section: 'admin' } },
      ],
    },
    // Onboarding — outside ShellLayout on purpose: fresh org owners see only
    // the setup wizard (no sidebar/topbar) until they finish or skip it.
    { path: '/welcome', name: 'welcome', component: () => import('@/views/WelcomeView.vue'),
      meta: { title: 'Welcome' } },
    // OIDC redirect target — outside ShellLayout, exempt from the auth guard.
    { path: '/auth/callback', name: 'auth-callback',
      component: () => import('@/views/AuthCallbackView.vue') },
    { path: '/:catchAll(.*)', redirect: '/' },
  ],
})

// Cloud overlay routes — the cloud repo adds src/cloud/routes.ts as a purely
// additive module (marketing/landing page, cloud-only surfaces). In this repo
// the glob matches nothing, so this is a no-op for self-hosters. Overlay
// routes marked meta.public are exempt from the auth guard (guards.ts); a
// route named 'landing' becomes the signed-out entry for '/'.
const cloudRouteModules = import.meta.glob<{ default: RouteRecordRaw[] }>('../cloud/routes.ts', { eager: true })
for (const mod of Object.values(cloudRouteModules)) {
  for (const route of mod.default) router.addRoute(route)
}

installGuards(router)

export default router
