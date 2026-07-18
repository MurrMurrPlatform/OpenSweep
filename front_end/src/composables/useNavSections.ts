import type { Component, ComputedRef } from 'vue'
import { computed } from 'vue'
import {
  LayoutGrid, GitPullRequest, ClipboardList, BookOpen,
  Activity, Search, MessageCircleQuestion, ShieldAlert, FolderArchive, Settings2, Sparkles,
  FileText, SquareKanban, Github, Building2, User, Radar, Lightbulb, Newspaper, Slack, Bot,
} from 'lucide-vue-next'
import { useUiStore } from '@/stores/uiStore'
import { useCurrentUserStore } from '@/stores/currentUserStore'

export interface NavItem { to: string; label: string; icon: Component; scoped?: boolean; exact?: boolean }
export interface NavSection { label: string | null; items: NavItem[] }

/**
 * Single source of truth for app navigation — consumed by both the sidebar
 * and the ⌘K command palette. Workspace items follow the workflow:
 * see state → read context → triage → plan → deliver → observe.
 */
export function useNavSections(): { sections: ComputedRef<NavSection[]> } {
  const ui = useUiStore()
  const currentUser = useCurrentUserStore()

  const sections = computed<NavSection[]>(() => {
    const slug = ui.currentRepoSlug
    const r = slug ? `/r/${slug}` : ''
    return [
      // Grouped by what the user is doing: the core working set, the
      // operate/observe surfaces, then the incoming-signal inbox.
      {
        label: 'Workspace',
        items: [
          { to: r || '/', label: 'Dashboard', icon: LayoutGrid, scoped: true, exact: true },
          { to: `${r}/docs`, label: 'Documentation', icon: BookOpen, scoped: true },
          { to: `${r}/findings`, label: 'Findings', icon: ClipboardList, scoped: true },
          { to: `${r}/workitems`, label: 'Work items', icon: SquareKanban, scoped: true },
        ],
      },
      {
        label: 'Operate',
        items: [
          { to: `${r}/ask`, label: 'Ask', icon: MessageCircleQuestion, scoped: true },
          { to: `${r}/analyses`, label: 'Analyses', icon: Radar, scoped: true },
          { to: `${r}/health`, label: 'Health', icon: Search, scoped: true },
          { to: `${r}/runs`, label: 'Runs', icon: Activity, scoped: true },
          { to: `${r}/queue`, label: 'Queue', icon: GitPullRequest, scoped: true },
        ],
      },
      {
        label: 'Inbox',
        items: [
          { to: `${r}/news`, label: 'News', icon: Newspaper, scoped: true },
          { to: `${r}/ideas`, label: 'Ideas', icon: Lightbulb, scoped: true },
        ],
      },
      {
        label: 'Settings',
        items: [
          { to: '/settings/organization', label: 'Organization', icon: Building2 },
          { to: '/settings/agents', label: 'Agents', icon: Bot },
          { to: '/settings/slack', label: 'Slack', icon: Slack },
          { to: '/admin/llm-providers', label: 'LLM providers', icon: Sparkles },
          { to: '/settings/account', label: 'Account', icon: User },
        ],
      },
      // Platform-operator surface — org users get 403 on writes there.
      ...(currentUser.isPlatformAdmin ? [{
        label: 'Admin',
        items: [
          { to: '/settings/github', label: 'GitHub', icon: Github },
          { to: '/admin/platform-config', label: 'Platform config', icon: ShieldAlert },
          { to: '/admin/run-policies', label: 'Run policies', icon: Settings2 },
          { to: '/admin/agent-prompts', label: 'Agent prompts', icon: FileText },
          { to: '/admin/sandboxes', label: 'Sandboxes', icon: FolderArchive },
        ],
      }] : []),
    ]
  })

  return { sections }
}
