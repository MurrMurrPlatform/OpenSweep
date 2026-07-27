import type { RunTranscriptEvent } from '@/types/api'

/** Friendly present-tense labels for the tools opensweep reaches for most.
 *  Anything unlisted falls back to a humanized tool name. */
const TOOL_LABELS: Record<string, string> = {
  opensweep_platform_add_comment: 'writing a reply…',
  opensweep_platform_update_ticket: 'updating the ticket…',
  opensweep_platform_update_finding: 'updating the finding…',
  opensweep_platform_create_finding: 'filing a finding…',
  opensweep_platform_propose_epic: 'proposing an epic…',
  opensweep_platform_write_memory: 'writing a memory…',
  opensweep_platform_search_memory: 'searching memories…',
  opensweep_platform_list_docs: 'browsing the docs…',
  opensweep_platform_read_doc: 'reading documentation…',
  opensweep_platform_list_comments: 'reading the thread…',
  opensweep_platform_complete_run: 'wrapping up…',
  opensweep_platform_web_search: 'searching the web…',
  opensweep_platform_fetch_url: 'fetching a page…',
  read_code: 'reading code…',
  trace: 'tracing code…',
}

/** What opensweep is doing right now, from a live transcript event.
 *  Returns null for events that shouldn't change the shown activity. */
export function activityLabel(event: RunTranscriptEvent): string | null {
  if (event.type === 'assistant_text') return 'thinking…'
  if (event.type !== 'tool_use') return null
  const name = (event.name || '').trim()
  if (!name) return 'working…'
  if (TOOL_LABELS[name]) return TOOL_LABELS[name]
  const bare = name.replace(/^opensweep_platform_/, '').replace(/^opensweep_/, '')
  return `${bare.replace(/_/g, ' ')}…`
}
