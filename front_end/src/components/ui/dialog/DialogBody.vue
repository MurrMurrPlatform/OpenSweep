<script setup lang="ts">
import type { HTMLAttributes } from "vue"
import { cn } from "@/lib/utils"

const props = defineProps<{ class?: HTMLAttributes["class"] }>()
</script>

<template>
  <!--
    Scrollable body for dialogs with long content. Wrapping the body in this
    makes DialogHeader/DialogFooter stay pinned while only the body scrolls.
    `min-h-0` is what actually lets a flex child shrink below its content size —
    without it `flex-1` never yields and the content overflows instead.
    The negative margin bleeds the scrollbar out to the dialog edge so the
    scroll track doesn't sit inside the padding.
  -->
  <div
    :class="
      cn(
        '-mx-6 min-h-0 flex-1 overflow-y-auto overscroll-contain px-6',
        props.class,
      )
    "
  >
    <slot />
  </div>
</template>
