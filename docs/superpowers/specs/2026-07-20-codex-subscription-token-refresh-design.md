# Codex subscription token refresh — design

**Date:** 2026-07-20
**Status:** Approved design, pending spec review
**Area:** `back_end/domains/llm_providers`, `back_end/domains/runs`, `back_end/infrastructure`

## Problem

Users want to use their **Codex (ChatGPT) subscription** — much cheaper than the
OpenAI API — as an LLM provider in both self-hosted (local) and hosted (cloud)
OpenSweep. Today the subscription path is broken in any environment that relies
on a **stored** `auth.json` credential (mandatory for cloud):

1. The user pastes `~/.codex/auth.json` into the provider's Credential field.
   It is sealed and stored as an **immutable snapshot** in
   `LLMProvider.credential_secret`.
2. On every run, `runtime_env.build_runtime` writes that *same stored snapshot*
   to a worker-private `CODEX_HOME/.codex/auth.json`
   (`runtime_env.py:84-93`); `codex_turn_env` performs the write
   (`turn_cli.py:197-204`).
3. Codex's access token (a short-lived JWT) expires. Codex tries to refresh
   using the `refresh_token` in the snapshot.
4. OpenAI's OAuth uses **single-use rotating refresh tokens** — every successful
   refresh invalidates the previous refresh token. The snapshot's refresh token
   has already been consumed and rotated away (by the user's laptop codex, or by
   a prior OpenSweep run whose rotated token was never persisted). OpenAI rejects
   it → *"Your access token could not be refreshed. Please log out and sign in
   again."*

There is **no write-back path**: `provider_secret()` is read-only everywhere, so
a rotated token is never persisted back into `credential_secret`. The `last_refresh`
the user sees in their host `~/.codex/auth.json` is fresh because the *host* copy
keeps winning the rotation race; the stored OpenSweep copy is stale by
construction.

The **bind-mount** path (no stored secret — codex owns the host `~/.codex`) works
correctly and is out of scope for changes.

## Goals

- Codex subscription works reliably in **local and cloud**, including across
  ephemeral cloud containers (no persistent worker filesystem).
- Correct under **concurrency**: OpenSweep is a parallel-agent platform and many
  runs share **one** provider row, so concurrent codex processes against one
  subscription is the common case, not an edge case.
- Custody the OAuth secret safely (sealed at rest, never logged).

## Non-goals

- Changing the bind-mount path.
- Solving the case where the **same** ChatGPT login is used simultaneously by
  OpenSweep *and* an external codex (laptop). That is inherent to sharing one
  OAuth identity; mitigated by guidance (use a dedicated login for cloud), not code.

## Chosen approach — B: OpenSweep owns the refresh

Rejected alternative **A** (let codex refresh, hold a per-subscription lock for
the whole turn): correct but serializes every concurrent run on a subscription
for the full turn duration — defeats the parallel-agent model.

Under **B**, OpenSweep refreshes the token itself, proactively, and hands codex
an access token that is valid for longer than any single turn. Codex therefore
never refreshes mid-turn; the only refresh that happens is OpenSweep's, which is
serialized across replicas. Concurrent turns share one valid access token (bearer
tokens can be used concurrently), so steady-state parallelism is unbounded.

### Load-bearing invariant

> OpenSweep always hands codex an access token whose remaining lifetime is
> **≥ `TURN_TIMEOUT_SECONDS` + buffer**.

So codex never needs to refresh during a turn. This requires the access-token
lifetime to exceed the turn timeout. If a token's observed lifetime is *shorter*
than the turn timeout, we **keep proactively refreshing and log a loud warning**
(decision: accept the rare mid-turn-refresh race rather than add a whole-turn
lock fallback). A cheap defensive post-turn check logs if the on-disk
`last_refresh` ever changed, to detect a wrong margin in the wild.

## Design

### 1. New module — `domains/llm_providers/services/codex_auth.py`

Pure, isolated, unit-testable token logic:

- `parse_blob(secret: str) -> CodexTokens` — parse auth.json:
  `tokens.{id_token, access_token, refresh_token, account_id}` + `last_refresh`.
- `access_expiry(tokens) -> datetime` — decode the access-token JWT `exp` claim
  (base64 payload only; **no signature verification** — we only read expiry).
- `needs_refresh(tokens, now, margin) -> bool`.
- `refresh(tokens) -> CodexTokens` — `POST https://auth.openai.com/oauth/token`
  with `grant_type=refresh_token`, codex's `client_id`, and the refresh token.
  Module-level seam so tests monkeypatch without real HTTP. Preserves
  `account_id`; updates `id_token`/`access_token`/`refresh_token` + `last_refresh`.
  Raises a typed `CodexReauthRequired` on `400 invalid_grant`.
- `build_auth_json(tokens) -> str` — serialize in the exact shape codex expects:
  `{"OPENAI_API_KEY": null, "tokens": {...}, "last_refresh": "<iso8601>"}`.

**Concrete constants to verify during implementation** against the codex version
pinned in `back_end/Dockerfile.prod`:
- Token endpoint: `https://auth.openai.com/oauth/token`
- `client_id`: codex's public CLI client id
- Request body: `{client_id, grant_type: "refresh_token", refresh_token, scope}`

These are pinned values plus an explicit verification step — not placeholders.

### 2. Refresh orchestration — `ensure_fresh_blob(provider) -> str`

Mirrors the proven L1/L2/single-flight-lock structure in
`infrastructure/github_app.py`. Key difference: because the refresh token
**rotates**, the sealed `LLMProvider.credential_secret` in the DB — not Redis —
is the **durable source of truth**. Redis is a cache + the lock.

1. **Steady state (no lock):** read the current blob (L1 in-process cache → L2
   Redis cache → DB). If the access token is fresh (`access_expiry > now +
   margin`), return its auth.json. Concurrent turns all take this path — full
   parallelism, no lock.
2. **Refresh needed (locked):** acquire a per-provider Redis single-flight lock
   (`SET key token NX EX`, poll pattern from `github_app._get_token_via_redis`).
   Under the lock:
   - **Re-read the blob from the DB** (a peer replica may have just refreshed →
     avoids clobbering a newer token) and re-check freshness; return early if now
     fresh.
   - Call `refresh()`.
   - **Persist the rotated blob:** seal → write to
     `LLMProvider.credential_secret` (durable) → update Redis L2 + L1 caches.
   - Release the lock (compare-and-delete: only release a lock we still own).
3. **Redis unreachable:** degrade to a per-process `asyncio.Lock`
   (like `github_app._mint_under_local_lock`) — correct within a replica,
   best-effort across replicas. Redis errors never fail a run on their own.

`margin = TURN_TIMEOUT_SECONDS + buffer`.

### 3. Integration points (localized)

- **`runs/services/turn_service.py:_build_subprocess_turn`** (already async):
  for `executor == "codex"` with a stored subscription secret,
  `await ensure_fresh_blob(provider)` and pass the fresh blob into env building.
- **`llm_providers/services/runtime_env.build_runtime` / `runs/services/turn_cli.codex_turn_env`:**
  seed `CODEX_HOME/.codex/auth.json` from the **fresh blob** instead of the raw
  stored secret. `home_override` and existing cleanup semantics are unchanged.
- **No post-turn read-back** is required (the invariant guarantees codex did not
  refresh). A cheap defensive check logs if on-disk `last_refresh` changed.

### 4. Re-authentication UX (decision: flag + clear error)

When `refresh()` raises `CodexReauthRequired` (dead refresh token — revoked or
rotated away externally):
- Fail the run with an actionable message: *"Your Codex subscription needs
  re-authentication: run `codex login` on your machine and re-paste
  `~/.codex/auth.json` into the provider."*
- Set a lightweight `needs_reauth` flag on the `LLMProvider` node (cleared on the
  next successful credential save / refresh) so the UI can badge the provider.
  Requires: schema field on `LLMProvider`, surface in the provider DTO
  (`llm_provider_service`), and a small UI badge/prompt in the provider list.

### 5. Security / custody

- The blob stays sealed via `infrastructure.secretbox` at rest (same as today and
  as `github_app` cached tokens). Refresh happens over HTTPS. Tokens are never
  logged (log only expiry timestamps / refresh outcomes).
- Cloud custodies a full ChatGPT refresh token (higher blast radius than an API
  key). This is an accepted, documented tradeoff for the cost savings.
- Docs guidance: for cloud, use a **dedicated Codex login**, not one shared with a
  laptop running codex, to avoid the external rotation race.

## Testing

- **Unit (`codex_auth`):** JWT `exp` parsing (valid / malformed → treat as
  expired); `needs_refresh` boundaries; `refresh()` success rotates all three
  tokens + bumps `last_refresh`; `refresh()` on `invalid_grant` →
  `CodexReauthRequired`; `build_auth_json` shape matches codex's expectation.
- **Concurrency (`ensure_fresh_blob`):** two racing callers → exactly one
  `refresh()` call, both receive the new token, DB written once; peer-refresh
  re-read short-circuits (no clobber); Redis-down path uses the local lock.
- **Integration (turn):** stale stored blob → seed → mocked codex turn → assert
  the seeded auth.json carries a fresh access token and codex performs no refresh;
  `needs_reauth` set + run fails with the actionable message on a dead token.
- Follow existing patterns in `tests/test_codex_continuation.py` and
  `tests/test_oauth_mcp.py`.

## Rollout / repo notes

- Shared product code → lives in the **public `opensweep` repo** (this repo),
  merged into `opensweep-cloud` via the normal upstream merge. No `if cloud:`
  branches; the token manager is shared and cloud-agnostic.
- No data migration for existing stored blobs: on the first run after deploy,
  `ensure_fresh_blob` refreshes the stale token once (or surfaces `needs_reauth`
  if it is already dead) and writes back the rotated blob.
