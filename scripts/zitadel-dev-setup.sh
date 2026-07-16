#!/usr/bin/env bash
# One-command local Zitadel bootstrap (deployment/ZITADEL.md, dev flavor).
#
# Drives the Zitadel management API with the first-instance admin PAT
# (written to the bootstrap volume by docker-compose.yml) to create what the
# console walkthrough does by hand:
#   - project "opensweep" with role assertion on
#   - roles viewer / maintainer / admin
#   - SPA app "opensweep-spa" (PKCE, JWT access tokens, refresh tokens, dev mode,
#     localhost + 127.0.0.1 :5173/:5174 redirect URIs)
#   - grants the zitadel-admin human the `admin` role
# then writes the ZITADEL_*/VITE_ZITADEL_* vars into .env, stamps existing
# repositories into the admin's org (multi-tenancy: they'd be invisible
# otherwise), and restarts backend + frontend.
#
# Idempotent — safe to re-run. Prereq: docker compose up -d (or just ./start.sh)

set -euo pipefail
cd "$(dirname "$0")/.."

BASE="http://localhost:8300"

PAT_FILE=$(mktemp)
trap 'rm -f "$PAT_FILE"' EXIT
docker cp opensweep_zitadel:/zitadel/bootstrap/admin.pat "$PAT_FILE" >/dev/null
PAT=$(tr -d '\r\n' < "$PAT_FILE")

api() { # api METHOD PATH [JSON]
  curl -sf -X "$1" "$BASE$2" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    ${3:+-d "$3"}
}

jsonget() { python3 -c "import sys,json,functools; d=json.load(sys.stdin); print(functools.reduce(lambda a,k: a[int(k)] if isinstance(a,list) else a.get(k,{}), '$1'.split('.'), d) or '')"; }

# ── project ──────────────────────────────────────────────────────────────────
PROJECT_ID=$(api POST /management/v1/projects/_search \
  '{"queries":[{"nameQuery":{"name":"opensweep"}}]}' | jsonget "result.0.id")
if [ -z "$PROJECT_ID" ]; then
  PROJECT_ID=$(api POST /management/v1/projects \
    '{"name":"opensweep","projectRoleAssertion":true}' | jsonget "id")
  echo "created project opensweep ($PROJECT_ID)"
else
  echo "project opensweep exists ($PROJECT_ID)"
fi

# ── roles (409s on re-run are fine) ─────────────────────────────────────────
for role in viewer maintainer admin; do
  api POST "/management/v1/projects/$PROJECT_ID/roles" \
    "{\"roleKey\":\"$role\",\"displayName\":\"$role\"}" >/dev/null 2>&1 \
    && echo "created role $role" || echo "role $role exists"
done

# ── SPA app ──────────────────────────────────────────────────────────────────
# Every origin the SPA is served on needs its callback here — the SPA sends
# redirect_uri=<origin>/auth/callback and Zitadel validates it against this
# list even in dev mode. start.sh advertises 127.0.0.1; localhost also works.
OIDC_FIELDS='
  "redirectUris": [
    "http://localhost:5173/auth/callback","http://localhost:5174/auth/callback",
    "http://127.0.0.1:5173/auth/callback","http://127.0.0.1:5174/auth/callback"
  ],
  "postLogoutRedirectUris": [
    "http://localhost:5173","http://localhost:5174",
    "http://127.0.0.1:5173","http://127.0.0.1:5174"
  ],
  "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
  "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE","OIDC_GRANT_TYPE_REFRESH_TOKEN"],
  "appType": "OIDC_APP_TYPE_USER_AGENT",
  "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
  "accessTokenType": "OIDC_TOKEN_TYPE_JWT",
  "accessTokenRoleAssertion": true,
  "idTokenRoleAssertion": true,
  "idTokenUserinfoAssertion": true,
  "devMode": true
'
APP_JSON=$(api POST "/management/v1/projects/$PROJECT_ID/apps/_search" \
  '{"queries":[{"nameQuery":{"name":"opensweep-spa"}}]}')
CLIENT_ID=$(echo "$APP_JSON" | jsonget "result.0.oidcConfig.clientId")
if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID=$(api POST "/management/v1/projects/$PROJECT_ID/apps/oidc" \
    "{\"name\":\"opensweep-spa\",$OIDC_FIELDS}" | jsonget "clientId")
  echo "created app opensweep-spa (client_id=$CLIENT_ID)"
else
  # Re-assert the config so existing installs pick up list changes (e.g. the
  # 127.0.0.1 redirect URIs). Zitadel rejects a no-change update — that's fine.
  APP_ID=$(echo "$APP_JSON" | jsonget "result.0.id")
  api PUT "/management/v1/projects/$PROJECT_ID/apps/$APP_ID/oidc_config" \
    "{$OIDC_FIELDS}" >/dev/null 2>&1 \
    && echo "app opensweep-spa exists (client_id=$CLIENT_ID) — OIDC config updated" \
    || echo "app opensweep-spa exists (client_id=$CLIENT_ID) — OIDC config unchanged"
fi

# ── self-registration (multi-tenancy) ───────────────────────────────────────
# Anyone may sign up; the OpenSweep backend gives each new user their own OpenSweep
# organization on first login (or attaches them to a pending invitation).
ALLOW_REGISTER=$(api GET /admin/v1/policies/login 2>/dev/null | jsonget "policy.allowRegister" || true)
if [ "$ALLOW_REGISTER" = "True" ] || [ "$ALLOW_REGISTER" = "true" ]; then
  echo "self-registration already enabled"
else
  BODY=$(api GET /admin/v1/policies/login 2>/dev/null | python3 -c '
import sys, json
p = json.load(sys.stdin).get("policy", {})
drop = {"details", "isDefault", "allowRegister", "secondFactors", "multiFactors", "idps"}
body = {k: v for k, v in p.items() if k not in drop and v not in (None, "")}
body["allowRegister"] = True
print(json.dumps(body))' || echo "")
  if [ -n "$BODY" ] && api PUT /admin/v1/policies/login "$BODY" >/dev/null 2>&1; then
    echo "enabled self-registration (default login policy)"
  else
    echo "WARNING: could not enable self-registration — Zitadel console:"
    echo "         Default settings → Login Behavior and Security → Register allowed"
  fi
fi

# ── grant the admin human the `admin` role ──────────────────────────────────
USER_ID=$(api POST /management/v1/users/_search \
  '{"queries":[{"userNameQuery":{"userName":"zitadel-admin","method":"TEXT_QUERY_METHOD_CONTAINS"}}]}' \
  | jsonget "result.0.id")
if [ -n "$USER_ID" ]; then
  api POST "/management/v1/users/$USER_ID/grants" \
    "{\"projectId\":\"$PROJECT_ID\",\"roleKeys\":[\"admin\"]}" >/dev/null 2>&1 \
    && echo "granted admin role to zitadel-admin" || echo "zitadel-admin grant exists"
fi

# ── seed the QA user (browser tools / headless QA) ──────────────────────────
# A plain human user with a fixed password so MCP/browser tooling can drive
# the real Zitadel login form in dev (no CAPTCHA/MFA locally). It's a regular
# user: on first OpenSweep login it gets its own org, so QA runs never mask
# permission bugs by acting as the platform admin.
QA_USER_ID=$(api POST /management/v1/users/_search \
  '{"queries":[{"userNameQuery":{"userName":"opensweep-qa","method":"TEXT_QUERY_METHOD_EQUALS"}}]}' \
  | jsonget "result.0.id")
if [ -z "$QA_USER_ID" ]; then
  api POST /management/v1/users/human/_import '{
    "userName": "opensweep-qa",
    "profile": {"firstName": "OpenSweep", "lastName": "QA"},
    "email": {"email": "qa@opensweep.localhost", "isEmailVerified": true},
    "password": "OpenSweepQA-Password1!",
    "passwordChangeRequired": false
  }' >/dev/null && echo "seeded QA user qa@opensweep.localhost" \
    || echo "WARNING: could not seed QA user"
else
  echo "QA user exists"
fi

# ── lock the console down to the operator (super admin) ─────────────────────
# 1. Disallow anonymous org self-registration (login v1 endpoint stays served
#    in v4 and is open by default). User self-registration is unaffected.
api PUT /admin/v1/restrictions '{"disallowPublicOrgRegistration": true}' >/dev/null 2>&1 \
  && echo "disallowed public org registration" || echo "org registration already restricted"

# 2. Require a project grant to log into the Zitadel CONSOLE. Self-registered
#    OpenSweep users land in the default org (which owns the ZITADEL project), so
#    the documented hasProjectCheck would let them in — projectRoleCheck is the
#    real gate: it needs a user grant on the ZITADEL project regardless of org.
#    Grant the operator FIRST (empty roleKeys is valid), then enable the check —
#    the admin PAT keeps working either way, so this can't lock you out.
ZPROJECT_ID=$(api POST /management/v1/projects/_search \
  '{"queries":[{"nameQuery":{"name":"ZITADEL","method":"TEXT_QUERY_METHOD_EQUALS"}}]}' \
  | jsonget "result.0.id")
if [ -n "$ZPROJECT_ID" ] && [ -n "$USER_ID" ]; then
  api POST "/management/v1/users/$USER_ID/grants" \
    "{\"projectId\":\"$ZPROJECT_ID\",\"roleKeys\":[]}" >/dev/null 2>&1 \
    && echo "granted operator console access on the ZITADEL project" \
    || echo "operator console grant exists"
  api PUT "/management/v1/projects/$ZPROJECT_ID" \
    "{\"name\":\"ZITADEL\",\"projectRoleCheck\":true}" >/dev/null 2>&1 \
    && echo "console now requires a project grant (operator-only)" \
    || echo "WARNING: could not enable projectRoleCheck on the ZITADEL project"
else
  echo "WARNING: ZITADEL project or operator user not found — console not locked down"
fi

# ── login UI branding (label policy — rendered by the v2 login container) ───
# OpenSweep logo/icon per theme, app brand colors, no loginname suffix, no
# Zitadel watermark. Changes stage on the preview policy; _activate publishes.
api PUT /admin/v1/policies/label '{
  "primaryColor": "#6366f1",
  "backgroundColor": "#f6f5f3",
  "warnColor": "#b32424",
  "fontColor": "#0f0f10",
  "primaryColorDark": "#6366f1",
  "backgroundColorDark": "#0a0a0b",
  "warnColorDark": "#ef7a7a",
  "fontColorDark": "#f2f2f3",
  "hideLoginNameSuffix": true,
  "disableWatermark": true,
  "themeMode": "THEME_MODE_AUTO"
}' >/dev/null 2>&1 && echo "set login brand colors" || echo "login brand colors unchanged"
for upload in logo:opensweep-logo-light.svg logo/dark:opensweep-logo-dark.svg \
              icon:opensweep-icon-light.svg icon/dark:opensweep-icon-dark.svg; do
  asset_path="${upload%%:*}"; asset_file="scripts/assets/${upload##*:}"
  curl -sf -X POST "$BASE/assets/v1/instance/policy/label/$asset_path" \
    -H "Authorization: Bearer $PAT" -F "file=@$asset_file;type=image/svg+xml" >/dev/null \
    && echo "uploaded label asset $asset_path" \
    || echo "WARNING: failed to upload label asset $asset_path"
done
api POST /admin/v1/policies/label/_activate '{}' >/dev/null 2>&1 \
  && echo "activated login branding" || echo "login branding already active"
api PUT /v2/settings/hosted_login_translation '{
  "instance": true,
  "locale": "en",
  "translations": {"register": {"description": "Create your OpenSweep account."}}
}' >/dev/null 2>&1 && echo "set login text overrides" \
  || echo "WARNING: could not set login text overrides"

ORG_ID=$(api GET /management/v1/orgs/me | jsonget "org.id")

# ── write .env ───────────────────────────────────────────────────────────────
sedi() { if [ "$(uname)" = "Darwin" ]; then sed -i '' "$@"; else sed -i "$@"; fi; }

set_env() { # set_env KEY VALUE — replace or append
  if grep -q "^$1=" .env; then
    sedi "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
set_env ZITADEL_ISSUER "http://localhost:8300"
set_env ZITADEL_INTERNAL_URL "http://opensweep_zitadel:8080"
set_env ZITADEL_CLIENT_ID "$CLIENT_ID"
set_env ZITADEL_PROJECT_ID "$PROJECT_ID"
set_env VITE_ZITADEL_AUTHORITY "http://localhost:8300"
set_env VITE_ZITADEL_CLIENT_ID "$CLIENT_ID"
echo "wrote ZITADEL_*/VITE_ZITADEL_* to .env"

# ── stamp PRE-TENANCY repos into the admin's org (only if any exist) ─────────
# Repos created before Zitadel setup carry no org (or the local-org default)
# and would be invisible under multi-tenancy. Stamp them into the admin's
# Zitadel org so they stay reachable. IMPORTANT: only create the Organization
# node when there is actually something to stamp — otherwise every
# self-registered user (who all share this default Zitadel org) would be
# funneled into it by the legacy-IdP-org join in provisioning, defeating the
# org-per-user model. With no legacy repos, new users each get a personal org.
NEO4J_PASSWORD=$(grep '^NEO4J_PASSWORD=' .env | cut -d= -f2)
NEED=$(docker exec opensweep_neo4j cypher-shell --format plain -u neo4j -p "${NEO4J_PASSWORD:-opensweeppassword}" \
  "MATCH (r:Repository) WHERE r.org_uid IS NULL OR r.org_uid = '' OR r.org_uid = 'local-org' RETURN count(r);" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
if [ "${NEED:-0}" -gt 0 ]; then
  docker exec opensweep_neo4j cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-opensweeppassword}" \
    "MERGE (o:Organization {uid: '$ORG_ID'}) ON CREATE SET o.name = 'ZITADEL', o.created_at = datetime();
     MATCH (r:Repository) WHERE r.org_uid IS NULL OR r.org_uid = '' OR r.org_uid = 'local-org' SET r.org_uid = '$ORG_ID';" >/dev/null 2>&1 || true
  echo "stamped $NEED pre-tenancy repositories into org $ORG_ID"
else
  echo "no pre-tenancy repositories to stamp — new users each get their own org"
fi

# ── restart app containers with the new env ─────────────────────────────────
# (the login container caches branding/translations — restart applies them)
docker compose up -d opensweep_backend opensweep_frontend >/dev/null 2>&1
docker compose restart opensweep_zitadel_login >/dev/null 2>&1
echo
echo "Done. Open http://localhost:5174 — you'll be redirected to Zitadel."
echo "Operator login: zitadel-admin@zitadel.localhost / Password1!"
echo "QA login (browser tools): qa@opensweep.localhost / OpenSweepQA-Password1!"
