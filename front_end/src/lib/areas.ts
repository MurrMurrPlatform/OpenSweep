import type { AreaDTO, AreaKind } from '@/types/api'

const KIND_VARIANT: Record<AreaKind, 'secondary' | 'info' | 'outline'> = {
  subsystem: 'secondary',
  feature: 'info',
  ignore: 'outline',
  feature_ignore: 'outline',
}

/** Badge variant for an area kind (defensive for unknown strings). */
export function areaKindVariant(kind: string): 'secondary' | 'info' | 'outline' {
  return KIND_VARIANT[kind as AreaKind] ?? 'secondary'
}

/** What each area kind means — hover help for kind badges and section headers. */
export const AREA_KIND_HELP: Record<AreaKind, string> = {
  subsystem:
    'Subsystem areas: the exclusive partition — every auditable file belongs to exactly one leaf area. Parents (scopeless keys) are groupings, not audit targets.',
  feature:
    "Feature areas: end-to-end flows that overlay the partition (their files also belong to subsystem areas). Audited against their spec — 'does the implementation match this contract?'",
  ignore:
    'Explicitly non-auditable files (lockfiles, generated, vendored), each with its reason. Excluded from coverage debt.',
  feature_ignore:
    'The feature axis\u2019s ignore: paths that implement no product feature (lockfiles, generated code, static assets), each with its reason. They close the feature partition without claiming to be a feature.',
}

/** Kind explanation (defensive for unknown strings — '' hides the help). */
export function areaKindHelp(kind: string): string {
  return AREA_KIND_HELP[kind as AreaKind] ?? ''
}

/** Tooltip for the amber "stale" dot: what changed and when it was reviewed. */
export function areaStaleTitle(a: AreaDTO): string {
  const count = `${a.stale_paths.length} path${a.stale_paths.length === 1 ? '' : 's'} changed since last review`
  const reviewed = a.last_reviewed_at ? `\nlast reviewed ${a.last_reviewed_at.slice(0, 10)}` : ''
  return count + reviewed
}
