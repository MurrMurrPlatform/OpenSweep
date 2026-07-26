# OpenSweep frontend UI conventions (shadcn-vue migration)

Aesthetic: modern, minimalist, sleek. Generous whitespace, quiet borders, small
type scale, `tracking-tight` headings, no gratuitous shadows or gradients.
Every screen must work from 375px phones to wide desktops.

## Design tokens — ALWAYS use shadcn semantic tokens

Use ONLY these Tailwind color utilities in app code:

- Surfaces: `bg-background`, `bg-card`, `bg-muted`, `bg-popover`, `bg-accent`
- Text: `text-foreground`, `text-muted-foreground`, `text-card-foreground`
- Accents: `bg-primary text-primary-foreground`, `text-primary`, `bg-secondary`
- Danger: `bg-destructive text-destructive-foreground`, `text-destructive`
- Borders: `border` / `border-border`, `ring-ring`, `bg-input` (form fields)
- Status (OpenSweep extension, keep): `text-good`, `text-warn`, `text-bad` with
  `bg-good/10` etc. for tinted chips.

FORBIDDEN legacy utilities — replace on sight:
`bg-bg`, `bg-bg-elevated`, `bg-surface`, `bg-surface-2`, `bg-surface-3`,
`bg-surface-muted`, `text-text-1`, `text-text-2`, `text-text-3`,
`text-text-inverse`, `border-border-soft`, `border-strong`, `divider`,
`bg-brand*`, `text-brand`, `brand-soft`, `shadow-panel`, `shadow-floating`,
`shadow-glass`, `duration-fast`, `duration-base`, `ease-opensweep`,
`border-[hsl(var(--border))]` (now just `border`), `bg-[hsl(var(--...))]`
(use the token utility instead).

Mapping guide: `bg-bg`→`bg-background` · `bg-surface`→`bg-card` ·
`bg-surface-2/3/muted`→`bg-muted` (or `bg-accent` for hover) ·
`text-text-1`→`text-foreground` · `text-text-2`/`text-text-3`→`text-muted-foreground` ·
`text-brand`/`bg-brand-soft`→`text-primary`/`bg-primary/10` ·
`shadow-panel|floating|glass`→`shadow-sm` (cards) or `shadow-lg` (popovers) ·
`duration-fast ease-opensweep`→ just `transition-colors` (default timing).

Radii: `rounded-sm|md|lg|xl` now follow shadcn (`--radius` = 0.625rem base).
`rounded-pill` still exists for pills; prefer `rounded-full` for new code.

## Component APIs (all under `@/components/ui/*`)

Installed shadcn-vue components: alert, alert-dialog, avatar, badge, breadcrumb,
button, card, collapsible, command, dialog, dropdown-menu, input, label, popover,
scroll-area, select, separator, sheet, sidebar, skeleton, sonner, switch, table,
tabs, textarea, tooltip.
Kept OpenSweep components (already restyled — do not restyle again): empty-state
(`EmptyState`), error-state (`ErrorState`), page-header (`PageHeader`), markdown
(`MarkdownView`).

### Button
- OLD `variant="primary"` → NEW omit (default). `variant="secondary|ghost|outline|destructive"` unchanged. New `link` variant available.
- OLD `size="md"` → omit. `sm`, `lg`, `icon`, `icon-sm` exist. OLD `size="xs"` → `sm`.
- `loading` prop still works (OpenSweep extension: spinner + disabled).
- `as="a"` / `as-child` supported via reka Primitive. `@click` works via fallthrough.
- Icon sizing inside buttons is automatic (`[&_svg]:size-4`) — drop explicit `h-4 w-4` on icons inside buttons.

### Badge
- Variants: `default` (filled primary), `secondary` (gray chip), `outline`,
  `destructive`, plus OpenSweep tones `success`, `warn`, `info`.
- OLD→NEW: `default`→`secondary` · `danger`→`destructive` · `brand`→`default` ·
  `info|success|warn` unchanged. NO `size` prop anymore — drop `size="xs|sm"`;
  for tighter chips add `class="px-1.5 text-[10px]"`.
- Dynamic maps (`:variant="statusVariant(x)"`) MUST be re-mapped and typed as
  `BadgeVariants['variant']` (import from `@/components/ui/badge`).

### Card (compound now)
```vue
<Card>
  <CardHeader>
    <CardTitle>Title</CardTitle>
    <CardDescription>Optional sub</CardDescription>
  </CardHeader>
  <CardContent>…</CardContent>
  <CardFooter>…</CardFooter>
</Card>
```
- OLD `<CardBody>` is DELETED → use `<CardContent>`.
- OLD custom CardHeader (slot-based with title prop?) → use CardHeader+CardTitle.
- Default CardHeader/Content padding is p-6; for dense lists use `class="p-4"`.
  CardTitle renders `text-2xl` by default — usually override to
  `class="text-base"` (section cards) or `text-lg` (feature cards).

### Dialog (compound now — was prop-driven)
OLD: `<Dialog :open="x" title="T" description="D" @update:open>` + `#footer` slot.
NEW:
```vue
<Dialog v-model:open="x">
  <DialogContent class="sm:max-w-lg">
    <DialogHeader>
      <DialogTitle>T</DialogTitle>
      <DialogDescription>D</DialogDescription>
    </DialogHeader>
    <DialogBody>…body…</DialogBody>
    <DialogFooter>…buttons…</DialogFooter>
  </DialogContent>
</Dialog>
```
- `v-model:open` replaces `:open` + `@update:open` + `@close` (call the old
  close handler inside `@update:open` when it did more than set the flag).
- **Scrolling is handled by the primitives — do NOT add your own `max-h-[60vh]`
  or `overflow-y-auto` wrapper.** `DialogContent` caps itself at
  `100dvh - 2rem` and scrolls as a whole; wrap the body in `<DialogBody>` to
  get the better behaviour (header + footer stay pinned, only the body
  scrolls). `DialogBody` already bleeds the `-mx-6 px-6` padding, so pass it
  only layout classes (`class="space-y-3"`, `class="flex flex-col gap-3"`).
- `DialogContent` is a **flex column**, is `w-[calc(100%-2rem)]` (so phones get
  a side gutter), and sets `min-w-0` on its direct children so long unbreakable
  strings wrap instead of pushing the dialog off-screen. Genuinely wide content
  (code, diffs, tables) still needs its own `overflow-x-auto`.
- Only pass width classes to `DialogContent` (`sm:max-w-2xl`). Padding lives on
  the inner scroll wrapper, so `class="p-0"` on `DialogContent` does nothing.
- A custom scrolling region inside a dialog needs `min-h-0 flex-1` on itself
  *and* on every flex ancestor up to `DialogContent` — without `min-h-0` a flex
  child refuses to shrink and the content overflows instead of scrolling.
- For confirmation dialogs prefer `AlertDialog` (+`AlertDialogAction/Cancel`) —
  it gets the same viewport cap and gutter.

### Select (compound now — was native select with :options)
OLD: `<Select v-model="v" :options="opts" placeholder="…" />`
NEW:
```vue
<Select v-model="v">
  <SelectTrigger class="w-full sm:w-56">
    <SelectValue placeholder="…" />
  </SelectTrigger>
  <SelectContent>
    <SelectItem v-for="o in opts" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
  </SelectContent>
</Select>
```
- reka Select values are `AcceptableValue` — when v-model'ing into a typed ref
  use `@update:model-value="v = $event as string"` or widen the handler; empty
  string values are NOT allowed for SelectItem — use a sentinel like `'all'`
  and translate, or make the model nullable.

### Tabs (compound now — was `:tabs` array)
OLD: `<Tabs v-model="tab" :tabs="[{label,value,count}]" />`
NEW:
```vue
<Tabs v-model="tab">
  <TabsList class="max-w-full overflow-x-auto">
    <TabsTrigger value="a">Label <Badge variant="secondary" class="ml-1.5">3</Badge></TabsTrigger>
  </TabsList>
  <TabsContent value="a">…</TabsContent>
</Tabs>
```
- Keeping content outside TabsContent (old pattern: v-if on tab value below the
  bar) is acceptable — TabsList alone works fine; don't force-restructure.

### Switch
- reka-based now: `v-model` → `v-model:model-value` NO — use
  `:model-value="x" @update:model-value="x = $event"` or plain `v-model="x"`
  (reka Switch supports `v-model`). `:checked` prop is gone → `:model-value`.

### Input / Textarea
- Same `v-model` usage; `class` merges. `<Label for>` from `@/components/ui/label`
  replaces raw `<label class="…">`.

### Toast
- `useToast()` API unchanged (`toast.success(title, msg?, action?)`) — it now
  renders through vue-sonner. Never import `@/components/ui/toast` (deleted).

### Avatar
- Compound: `<Avatar><AvatarImage :src/><AvatarFallback>AB</AvatarFallback></Avatar>`.
  OLD `:name` prop is gone — compute initials for the fallback.

### Tables
- Data tables: use `Table, TableHeader, TableRow, TableHead, TableBody, TableCell`
  and wrap in `<div class="overflow-x-auto">` (or rely on Table's built-in wrapper)
  so they scroll horizontally on phones instead of breaking layout.

### Tooltips
- `<Tooltip><TooltipTrigger as-child>…</TooltipTrigger><TooltipContent>…` —
  a global TooltipProvider exists via SidebarProvider in the shell. Plain
  `title=` attributes are fine for low-value hints; don't convert them all.

## Responsiveness rules

- The shell already provides page padding (`p-4 sm:p-6 lg:p-8`) and max-w-7xl.
  Views must NOT add their own outer page padding.
- Filter/action toolbars: `flex flex-wrap items-center gap-2` (never fixed-width
  rows that overflow); prefer `w-full sm:w-auto` on inputs.
- Grids: `grid gap-4 sm:grid-cols-2 lg:grid-cols-3` style progressive columns;
  KPI rows `grid-cols-2 lg:grid-cols-4`.
- Two-column detail layouts: `grid gap-6 lg:grid-cols-[1fr_320px]` — sidebar
  stacks below on mobile.
- Long identifiers (SHAs, paths, URLs): `truncate` + `min-w-0` on flex children,
  or `break-all` where wrapping is better.
- Code/diff/log blocks: `overflow-x-auto` + `text-xs sm:text-sm`.
- Sticky action bars on detail pages are allowed: `sticky top-0 z-10` (the topbar
  is h-14 and sticky inside the scroll container; use `top-0` within the view's
  scroll context).
- Touch targets ≥ 36px on mobile controls.

## Process rules for migration agents

- Rewrite templates + minimal script changes only; do NOT change business logic,
  store calls, props/emits contracts between feature components, or route names.
- Keep code comments that explain domain behavior; drop comments that describe
  old styling.
- Import from `@/components/ui/<kebab>` index files, named imports.
- After editing your files, run
  `npx vue-tsc --noEmit 2>&1 | grep -E "(<your file names>)"` from `front_end/`
  and fix every error in YOUR files before finishing.
- Do not touch files outside your assigned batch.
