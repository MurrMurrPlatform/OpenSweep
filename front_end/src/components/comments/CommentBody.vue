<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { MENTION_ROUTES, parseMentionSegments } from '@/lib/mentions'
import MarkdownView from '@/components/ui/markdown/MarkdownView.vue'

/** Comment body renderer: markdown with @opensweep highlights and data-item
 *  mention chips (linked to the item's detail view when one exists).
 *
 *  Mention tokens are rewritten to internal markdown links BEFORE rendering
 *  so they survive the markdown pass; clicks on internal links are routed
 *  through vue-router instead of a full page load. */
const props = defineProps<{ body: string }>()

const router = useRouter()

/** App path per mentionable type — mirrors MENTION_ROUTES route params. */
const MENTION_PATHS: Record<string, string> = {
  ticket: '/tickets',
  finding: '/findings',
  pull_request: '/pull-requests',
  run: '/runs',
  scheduled_agent: '/scheduled-agents',
  investigation: '/scheduled-agents',
}

const markdown = computed(() =>
  parseMentionSegments(props.body)
    .map((segment) => {
      if (segment.kind === 'opensweep') return `**${segment.text}**`
      if (segment.kind === 'item') {
        const path = segment.type ? MENTION_PATHS[segment.type] : undefined
        const label = `@${segment.text}`.replace(/([[\]])/g, '\\$1')
        return path && segment.uid && MENTION_ROUTES[segment.type!]
          ? `[${label}](${path}/${segment.uid})`
          : `**${label}**`
      }
      return segment.text
    })
    .join(''),
)

function onClick(event: MouseEvent) {
  const anchor = (event.target as HTMLElement).closest('a')
  if (!anchor) return
  const href = anchor.getAttribute('href') || ''
  if (href.startsWith('/')) {
    event.preventDefault()
    void router.push(href)
  }
}
</script>

<template>
  <div class="comment-markdown" @click="onClick">
    <MarkdownView :model-value="markdown" preview-only min-height="0" />
  </div>
</template>

<style>
.comment-markdown .md-editor-preview {
  font-size: 0.875rem;
  line-height: 1.6;
}

.comment-markdown .md-editor-preview p {
  margin-block: 0.25em;
}

/* Internal mention links read as chips, matching the old renderer. */
.comment-markdown .md-editor-preview a[href^='/'] {
  display: inline-block;
  border-radius: 4px;
  background: hsl(var(--muted));
  padding: 0 0.3em;
  font-weight: 500;
  color: hsl(var(--foreground));
  text-decoration: none;
}

.comment-markdown .md-editor-preview a[href^='/']:hover {
  text-decoration: underline;
}
</style>
