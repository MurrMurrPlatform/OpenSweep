import type { AreaDTO, AreaKind } from '@/types/api'

const KIND_VARIANT: Record<AreaKind, 'secondary' | 'info' | 'outline'> = {
  subsystem: 'secondary',
  feature: 'info',
  ignore: 'outline',
}

/** Badge variant for an area kind (defensive for unknown strings). */
export function areaKindVariant(kind: string): 'secondary' | 'info' | 'outline' {
  return KIND_VARIANT[kind as AreaKind] ?? 'secondary'
}

/** Tooltip for the amber "stale" dot: what changed and when it was reviewed. */
export function areaStaleTitle(a: AreaDTO): string {
  const count = `${a.stale_paths.length} path${a.stale_paths.length === 1 ? '' : 's'} changed since last review`
  const reviewed = a.last_reviewed_at ? `\nlast reviewed ${a.last_reviewed_at.slice(0, 10)}` : ''
  return count + reviewed
}
