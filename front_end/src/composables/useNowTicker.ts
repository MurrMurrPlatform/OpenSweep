import { onBeforeUnmount, ref, type Ref } from 'vue'

/**
 * A `Date.now()` ref that ticks on an interval — shared by every elapsed-time
 * display on a page (run rows, chips) so they advance from one timer instead
 * of each row running its own.
 */
export function useNowTicker(intervalMs = 1000): Ref<number> {
  const now = ref(Date.now())
  const id = setInterval(() => { now.value = Date.now() }, intervalMs)
  onBeforeUnmount(() => clearInterval(id))
  return now
}
