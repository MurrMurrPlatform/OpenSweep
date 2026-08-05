<script setup lang="ts">
// Mounted by main.ts INSTEAD of the app when VITE_ZITADEL_AUTHORITY /
// VITE_ZITADEL_CLIENT_ID are missing. Zitadel OIDC is the only supported
// auth — this screen mirrors the backend's fail-hard startup guard
// (infrastructure/production_guards.py) so a misconfigured deploy is loud
// on both surfaces instead of silently serving an unauthenticated app.
</script>

<template>
  <div class="flex min-h-full items-center justify-center bg-background px-5 text-foreground font-sans">
    <div class="w-full max-w-lg rounded-lg border border-border bg-card p-8 shadow-sm">
      <div class="flex items-center gap-3">
        <svg viewBox="0 0 32 32" class="h-8 w-8 text-primary" aria-hidden="true">
          <circle cx="16" cy="16" r="14" fill="currentColor" />
          <circle cx="11" cy="14" r="2.4" fill="white" />
          <circle cx="21" cy="14" r="2.4" fill="white" />
          <path d="M10 21 Q16 25 22 21" stroke="white" stroke-width="1.6" fill="none" stroke-linecap="round" />
        </svg>
        <h1 class="font-display text-xl font-bold">Authentication is not configured</h1>
      </div>

      <p class="mt-4 text-sm leading-relaxed text-muted-foreground">
        OpenSweep requires Zitadel OIDC login, but this frontend was started without
        <code class="font-mono text-xs">VITE_ZITADEL_AUTHORITY</code> and
        <code class="font-mono text-xs">VITE_ZITADEL_CLIENT_ID</code>.
      </p>

      <div class="mt-5 rounded-md bg-muted p-4 text-sm leading-relaxed text-muted-foreground">
        <p class="font-semibold text-foreground">Local development</p>
        <ol class="mt-2 list-decimal space-y-1 pl-5">
          <li><code class="font-mono text-xs">docker compose up -d</code> — Zitadel is part of the default stack</li>
          <li><code class="font-mono text-xs">scripts/zitadel-dev-setup.sh</code> — configures Zitadel and writes the env vars into <code class="font-mono text-xs">.env</code></li>
          <li>Restart the frontend so Vite picks up the new env</li>
        </ol>
        <p class="mt-3 font-semibold text-foreground">Production</p>
        <p class="mt-1">Set both vars at build time — see <code class="font-mono text-xs">deployment/ZITADEL.md</code>.</p>
      </div>
    </div>
  </div>
</template>
