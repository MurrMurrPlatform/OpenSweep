import { inject, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import type { ActiveRunDTO, RepositoryDTO } from '@/types/api'

/**
 * Shared workspace state provided by WorkspaceLayout to its tab views.
 *
 * The layout owns the single useActiveRuns() heartbeat (it polls per-instance,
 * so tabs must never instantiate their own) and the Start-a-sweep dialog; tabs
 * read repo identity and the run signature from here instead of re-resolving.
 */
export interface WorkspaceCtx {
  repo: Ref<RepositoryDTO | null>
  repoUid: ComputedRef<string | null>
  repoSlug: ComputedRef<string | null>
  activeRun: ComputedRef<ActiveRunDTO | null>
  activeRuns: Ref<ActiveRunDTO[]>
  /** Fingerprint of in-flight runs — watch it to refresh silently. */
  signature: ComputedRef<string>
  openSweep: () => void
}

export const WorkspaceCtxKey: InjectionKey<WorkspaceCtx> = Symbol('workspace-ctx')

export function useWorkspaceCtx(): WorkspaceCtx {
  const ctx = inject(WorkspaceCtxKey)
  if (!ctx) throw new Error('useWorkspaceCtx() requires a WorkspaceLayout ancestor')
  return ctx
}
