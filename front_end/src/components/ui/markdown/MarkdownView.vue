<script setup lang="ts">
import { computed } from 'vue'
import { MdEditor, MdPreview } from 'md-editor-v3'
import 'md-editor-v3/lib/style.css'
import DOMPurify from 'dompurify'
import { useTheme } from '@/composables/useTheme'

// md-editor-v3 initializes markdown-it with html:true, so raw HTML in
// agent- and repo-derived markdown passes through untouched unless we
// sanitize it ourselves here (stored-XSS: a malicious README/finding body
// rendered through this component).
const sanitize = (html: string) => DOMPurify.sanitize(html)

const props = withDefaults(
  defineProps<{
    modelValue: string
    editing?: boolean
    placeholder?: string
    minHeight?: string
    previewOnly?: boolean
    compact?: boolean
  }>(),
  {
    editing: false,
    placeholder: 'Write markdown…',
    minHeight: '320px',
    previewOnly: false,
    compact: false,
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

const { theme } = useTheme()

const value = computed({
  get: () => props.modelValue,
  set: (v: string) => emit('update:modelValue', v),
})

const containerStyle = computed(() => ({ '--md-min-height': props.minHeight }))
</script>

<template>
  <div
    class="markdown-view"
    :class="{
      'markdown-view--compact': compact,
      'markdown-view--editing': editing && !previewOnly,
    }"
    :style="containerStyle"
  >
    <MdPreview
      v-if="previewOnly || !editing"
      :model-value="modelValue"
      :theme="theme"
      preview-theme="github"
      code-theme="atom"
      :sanitize="sanitize"
    />
    <MdEditor
      v-else
      v-model="value"
      :theme="theme"
      preview-theme="github"
      code-theme="atom"
      :placeholder="placeholder"
      :toolbars-exclude="['github', 'save', 'fullscreen', 'pageFullscreen']"
      :show-code-row-number="false"
      :sanitize="sanitize"
    />
  </div>
</template>

<style>
.markdown-view .md-editor,
.markdown-view .md-editor-preview-wrapper {
  background: transparent;
  border: 0;
  box-shadow: none;
}

.markdown-view--editing .md-editor {
  min-height: var(--md-min-height, 320px);
  border: 1px solid hsl(var(--border));
  border-radius: 4px;
}

.markdown-view .md-editor-preview {
  padding: 0;
  background: transparent;
  font-size: 0.95rem;
  line-height: 1.65;
}

.markdown-view .md-editor-preview h1,
.markdown-view .md-editor-preview h2,
.markdown-view .md-editor-preview h3,
.markdown-view .md-editor-preview h4 {
  color: hsl(var(--foreground));
  border-bottom-color: hsl(var(--border));
}

.markdown-view .md-editor-preview p,
.markdown-view .md-editor-preview li {
  color: hsl(var(--foreground));
}

.markdown-view .md-editor-preview a {
  color: hsl(var(--primary));
}

/* Tailwind preflight strips list markers/indent; md-editor's themes assume
   browser defaults, so restore them for rendered markdown. */
.markdown-view .md-editor-preview ul,
.markdown-view .md-editor-preview ol {
  padding-left: 1.6em;
  margin-block: 0.5em;
}

.markdown-view .md-editor-preview ul {
  list-style: disc outside;
}

.markdown-view .md-editor-preview ol {
  list-style: decimal outside;
}

.markdown-view .md-editor-preview ul ul {
  list-style-type: circle;
}

.markdown-view .md-editor-preview li > ul,
.markdown-view .md-editor-preview li > ol {
  margin-block: 0.25em;
}

.markdown-view .md-editor-preview li.task-list-item {
  list-style: none;
}

.markdown-view .md-editor-preview code {
  background: hsl(var(--muted));
  color: hsl(var(--foreground));
  border-radius: 3px;
  padding: 0.1em 0.35em;
}

.markdown-view .md-editor-preview pre {
  background: hsl(var(--muted));
  border: 1px solid hsl(var(--border));
  border-radius: 4px;
}

.markdown-view .md-editor-preview blockquote {
  border-left-color: hsl(var(--border));
  color: hsl(var(--muted-foreground));
  background: hsl(var(--muted));
}

.markdown-view .md-editor-preview table th,
.markdown-view .md-editor-preview table td {
  border-color: hsl(var(--border));
}

/* Compact mode — for list cards and previews. */
.markdown-view--compact {
  position: relative;
  max-height: 4.5em;
  overflow: hidden;
  pointer-events: none;
}

.markdown-view--compact::after {
  content: '';
  position: absolute;
  inset: auto 0 0 0;
  height: 1.5em;
  background: linear-gradient(to bottom, transparent, hsl(var(--card)));
  pointer-events: none;
}

.markdown-view--compact .md-editor-preview {
  font-size: 0.8rem;
  line-height: 1.5;
  color: hsl(var(--muted-foreground));
}

.markdown-view--compact .md-editor-preview * {
  margin: 0 !important;
  padding: 0 !important;
  font-size: inherit !important;
  font-weight: 400 !important;
  line-height: inherit !important;
  background: transparent !important;
  border: 0 !important;
  color: inherit !important;
}

.markdown-view--compact .md-editor-preview strong,
.markdown-view--compact .md-editor-preview h1,
.markdown-view--compact .md-editor-preview h2,
.markdown-view--compact .md-editor-preview h3,
.markdown-view--compact .md-editor-preview h4 {
  font-weight: 600 !important;
}

.markdown-view--compact .md-editor-preview code {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}

.markdown-view--compact .md-editor-preview ul,
.markdown-view--compact .md-editor-preview ol {
  padding-left: 1.1em !important;
  list-style-position: outside;
}
</style>
