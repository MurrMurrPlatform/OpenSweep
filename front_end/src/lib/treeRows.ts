/**
 * Generic tree-row builder for hierarchical key/value lists.
 *
 * Splits each item's key by `/`, synthesises a group row for every
 * intermediate prefix that has descendants (sorted alphabetically, emitted
 * once), and emits a leaf row for each item.  Depth = number of `/`-separated
 * ancestors above the row.
 *
 * Parent-that-is-also-a-leaf: when an item's key IS a prefix of other keys,
 * it is emitted as a leaf row at its natural depth AND its children are still
 * nested under it.  No synthetic group row is emitted for that prefix because
 * the leaf row itself acts as the anchor — callers render it with an indent
 * and the same child rows follow immediately after.
 */

export type TreeRow<T> =
  | { type: 'group'; key: string; name: string; depth: number }
  | { type: 'leaf'; key: string; name: string; depth: number; item: T }

interface TreeNode<T> {
  item: T | null
  children: Map<string, TreeNode<T>>
}

export function buildTreeRows<T>(items: T[], keyOf: (t: T) => string): TreeRow<T>[] {
  // 1. Build an in-memory prefix tree.
  const root: TreeNode<T> = { item: null, children: new Map() }

  for (const item of items) {
    const segments = keyOf(item).split('/')
    let node = root
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]
      let child = node.children.get(seg)
      if (!child) {
        child = { item: null, children: new Map() }
        node.children.set(seg, child)
      }
      node = child
    }
    node.item = item
  }

  // 2. Walk depth-first (children sorted alphabetically), emitting rows.
  const rows: TreeRow<T>[] = []

  function walk(node: TreeNode<T>, path: string, depth: number) {
    for (const [seg, child] of [...node.children.entries()].sort(([a], [b]) =>
      a.localeCompare(b),
    )) {
      const key = path ? `${path}/${seg}` : seg

      if (child.item !== null) {
        // Emit a leaf (even if this node also has children — it acts as its
        // own group header; children nest under it at depth + 1).
        rows.push({ type: 'leaf', key, name: seg, depth, item: child.item })
      } else {
        // No item at this prefix → synthetic group row.
        rows.push({ type: 'group', key, name: seg, depth })
      }

      // Always recurse into children regardless of whether we emitted a
      // leaf or a group row.
      walk(child, key, depth + 1)
    }
  }

  walk(root, '', 0)
  return rows
}
