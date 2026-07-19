# Run Depth + Run Policy Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenSweep hunts/deep-scans match interactive Claude Code in depth and duration (find every evidenced bug, dead-code, improvement), and replace punitive post-hoc run limits with cooperative budgets: four seeded policies (short / normal / deep / unlimited), unlimited as the default, graceful wind-down instead of hard kills, and CLI-native caps — while keeping every run continuable from the UI and keeping all improvements harness-agnostic (claude code, codex, opencode).

**Architecture:** Three layers. (1) *Prompt layer*: append (never replace) Claude Code's system prompt; recall-first seeded prompts; a harness-agnostic budget briefing rendered into every instruction. (2) *Loop layer*: the claude adapter gains a continuation loop (resume the CLI session while the agent hasn't called `complete_run` and budget remains) plus a reserved wind-down pass; codex gets a one-shot transcript-tail continuation. (3) *Policy layer*: `InvestigationEffort` becomes short/normal/deep/unlimited mapping to four seeded RunPolicies; `max_wall_seconds=0` is an explicit "no wall guard" sentinel; post-hoc `LIMIT_EXCEEDED` conversion is deleted (warnings only) — `LIMIT_EXCEEDED` is reserved for runs actually *stopped* by a limit.

**Tech Stack:** Python 3.14 / FastAPI / neomodel (Neo4j) backend, Vue 3 + TS frontend, `claude` / `codex` / `opencode` CLIs as executors, pytest via `uv`.

## Global Constraints

- **Two-repo rule (CLAUDE.md):** ALL changes land in the public repo `/Users/jeroenbrouns/Desktop/opensweep`. Never edit shared code in `opensweep-cloud`; cloud syncs via `git fetch upstream && git merge upstream/main`.
- **Multi-harness:** nothing may assume claude-only. Prompt/budget text is plain text (works everywhere); loop features are gated per adapter capability (claude: `--resume`; codex: transcript-tail re-prompt; opencode: none yet — must keep working unchanged).
- **UI continuity:** every run must stay continuable from the UI. `LIMIT_EXCEEDED` is already in `FOLLOW_UP_STATUSES` (`domains/investigations/schemas.py:72-78`); the executor continuation loop MUST persist the last CLI session id to `Run.cli_session_id` so UI follow-up turns (`turn_service.py`) resume the same conversation.
- **Seeding discipline:** platform prompt bodies roll forward only when the row is unedited (checksum mechanism in `agent_prompts/services/platform_prompts.py`). Policy/template roll-forwards only fire when the stored value exactly matches a known legacy seeded value — human-tuned values are always preserved.
- **Legacy API values:** effort value `"quick"` must remain accepted (existing Investigation rows + old clients) and normalize to `"short"`.
- **Subscription metering:** subscription CLIs can't meter dollars/tokens; `--max-budget-usd` is NOT passed for `claude_subscription`. Dollar/token ceilings only ever produce warnings there.
- Tests run with: `cd /Users/jeroenbrouns/Desktop/opensweep/back_end && uv run pytest tests/<file> -v`
- **Already in working tree (uncommitted):** `domains/llm_providers/schemas.py` has the `--append-system-prompt` default + `_LEGACY_CLI_TEMPLATES` + `effective_cli_template()`. Task 1 verifies/commits it.

---

### Task 1: Append-system-prompt default + legacy template roll-forward

Claude Code's `--system-prompt` flag REPLACES the CLI's entire built-in system prompt (stripping its agentic persistence scaffolding — the main cause of shallow platform runs). `--append-system-prompt` layers our contract on top instead. Provider rows are stamped with the default template at creation (`llm_provider_service.py:122`), so a dispatch-time roll-forward is needed for existing installs.

**Files:**
- Modify: `back_end/domains/llm_providers/schemas.py` (already edited — verify content below)
- Modify: `back_end/domains/executors/claude_code.py:121-129`
- Test: `back_end/tests/test_cli_template_rollforward.py` (new)

**Interfaces:**
- Produces: `effective_cli_template(kind: str | LLMProviderKind, stored: str | None) -> str` in `domains.llm_providers.schemas` — used by Task 6's adapter code and any future adapter.

- [ ] **Step 1: Verify the schemas.py working-tree edits are present**

`schemas.py` must contain (a) the catalog default using `--append-system-prompt`:

```python
"default_cli": (
    'claude -p {{instruction_q}} --append-system-prompt {{system_prompt_q}} '
    '--mcp-config {{mcp_config_path_q}} '
    '--permission-mode bypassPermissions --output-format stream-json --verbose'
),
```

and (b) after `default_cli_template()`:

```python
_LEGACY_CLI_TEMPLATES: dict[str, tuple[str, ...]] = {
    LLMProviderKind.CLAUDE_SUBSCRIPTION.value: (
        'claude -p {{instruction_q}} --system-prompt {{system_prompt_q}} '
        '--mcp-config {{mcp_config_path_q}} '
        '--permission-mode bypassPermissions --output-format stream-json --verbose',
    ),
}


def effective_cli_template(kind: str | LLMProviderKind, stored: str | None) -> str:
    """The template a dispatch should actually use: the stored one, unless it
    is empty or a known legacy seeded default — both resolve to the current
    catalog default."""
    kind_value = kind.value if isinstance(kind, LLMProviderKind) else str(kind or "")
    template = (stored or "").strip()
    if not template or template in _LEGACY_CLI_TEMPLATES.get(kind_value, ()):
        return default_cli_template(kind)
    return template
```

- [ ] **Step 2: Write the failing test**

`back_end/tests/test_cli_template_rollforward.py`:

```python
"""effective_cli_template — legacy seeded templates roll forward at dispatch,
human-tuned templates are preserved, empty resolves to the catalog default."""

from domains.llm_providers.schemas import (
    default_cli_template,
    effective_cli_template,
)

_LEGACY = (
    'claude -p {{instruction_q}} --system-prompt {{system_prompt_q}} '
    '--mcp-config {{mcp_config_path_q}} '
    '--permission-mode bypassPermissions --output-format stream-json --verbose'
)


def test_legacy_claude_template_rolls_forward():
    out = effective_cli_template("claude_subscription", _LEGACY)
    assert out == default_cli_template("claude_subscription")
    assert "--append-system-prompt" in out
    assert "--system-prompt " not in out


def test_custom_template_is_preserved():
    custom = "claude -p {{instruction_q}} --system-prompt {{system_prompt_q}} --my-flag"
    assert effective_cli_template("claude_subscription", custom) == custom


def test_empty_template_resolves_to_default():
    assert effective_cli_template("claude_subscription", "  ") == default_cli_template(
        "claude_subscription"
    )


def test_current_default_uses_append_system_prompt():
    assert "--append-system-prompt" in default_cli_template("claude_subscription")
```

- [ ] **Step 3: Run test** — `uv run pytest tests/test_cli_template_rollforward.py -v` — all 4 must PASS (implementation exists). If any fail, fix `schemas.py` to match Step 1.

- [ ] **Step 4: Wire into the claude adapter**

In `claude_code.py`, change the import and template resolution:

```python
from domains.llm_providers.schemas import default_cli_template
```
becomes
```python
from domains.llm_providers.schemas import default_cli_template, effective_cli_template
```
(keep `default_cli_template` if still referenced; drop if not) and lines 119-123:

```python
        # The template is platform-owned — rows created before the service
        # defaulted it (or cleared by hand) fall back to the catalog default;
        # rows still holding a known legacy seeded default roll forward.
        template = effective_cli_template(provider.kind, provider.cli_command_template)
```

- [ ] **Step 5: Run the adjacent suites** — `uv run pytest tests/test_model_flag_injection.py tests/test_cli_template_rollforward.py -v` — all PASS.

- [ ] **Step 6: Commit**

```bash
git add back_end/domains/llm_providers/schemas.py back_end/domains/executors/claude_code.py back_end/tests/test_cli_template_rollforward.py
git commit -m "fix(executors): append to Claude Code's system prompt instead of replacing it"
```

---

### Task 2: Effort model — short / normal / deep / unlimited + seeded policies

Replace the quick/normal/deep effort trio with **short / normal / deep / unlimited**, each backed by a seeded RunPolicy; make **unlimited the default** (system default policy + deep-scan fallback); introduce the `max_wall_seconds = 0` "explicitly no wall guard" sentinel.

**Files:**
- Modify: `back_end/domains/investigations/schemas.py:87-90`
- Modify: `back_end/domains/run_policies/services/effort.py` (rewrite)
- Modify: `back_end/domains/run_policies/services/system_default.py`
- Modify: `back_end/domains/executors/_shared.py:80-94` (`resolve_wall_ceiling`)
- Modify: `back_end/api/v1/investigations.py:44` (normalize legacy value)
- Modify: `back_end/domains/investigations/services/sweep.py:385` (deep-scan default policy → unlimited)
- Test: `back_end/tests/test_effort_policies.py` (new)

**Interfaces:**
- Produces: `InvestigationEffort.SHORT/NORMAL/DEEP/UNLIMITED` (StrEnum values `"short"|"normal"|"deep"|"unlimited"`); `normalize_effort(value: str) -> InvestigationEffort` in `domains.investigations.schemas`; `ensure_policy_for_effort(effort) -> RunPolicy` unchanged signature; `resolve_wall_ceiling` returns `None` for a policy with `max_wall_seconds == 0`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

`back_end/tests/test_effort_policies.py`:

```python
"""Effort → policy mapping: 4 tiers, legacy 'quick' normalizes to short,
wall sentinel 0 = explicitly unlimited."""

from domains.executors.base import DispatchRequest
from domains.executors._shared import resolve_wall_ceiling
from domains.investigations.schemas import InvestigationEffort, normalize_effort
from domains.run_policies.services.effort import _EFFORT_POLICIES
from domains.run_policies.services.system_default import _DEFAULTS


def test_normalize_effort_accepts_legacy_quick():
    assert normalize_effort("quick") is InvestigationEffort.SHORT
    assert normalize_effort("short") is InvestigationEffort.SHORT
    assert normalize_effort("deep") is InvestigationEffort.DEEP
    assert normalize_effort("unlimited") is InvestigationEffort.UNLIMITED
    assert normalize_effort("") is InvestigationEffort.NORMAL
    assert normalize_effort("garbage") is InvestigationEffort.NORMAL


def test_four_effort_tiers_have_policies():
    assert set(_EFFORT_POLICIES) == {
        InvestigationEffort.SHORT,
        InvestigationEffort.NORMAL,
        InvestigationEffort.DEEP,
        InvestigationEffort.UNLIMITED,
    }
    assert _EFFORT_POLICIES[InvestigationEffort.UNLIMITED]["max_wall_seconds"] == 0
    assert _EFFORT_POLICIES[InvestigationEffort.UNLIMITED]["max_tool_turns"] is None


def test_system_default_is_unlimited():
    assert _DEFAULTS["max_wall_seconds"] == 0
    assert _DEFAULTS["max_tool_turns"] is None
    assert _DEFAULTS["max_dollars"] is None


class _P:
    def __init__(self, wall):
        self.max_wall_seconds = wall


def _req(policy):
    return DispatchRequest(
        run_uid="r", investigation_uid="i", repository_uid="repo",
        repository_local_path=None, intent="x", policy=policy,
    )


def test_wall_sentinel_zero_disables_guard():
    assert resolve_wall_ceiling(_req(_P(0)), "claude_subscription") is None


def test_wall_positive_is_used():
    assert resolve_wall_ceiling(_req(_P(7200)), "claude_subscription") == 7200


def test_wall_unset_falls_back_to_system_default():
    from domains.run_policies.services.system_default import DEFAULT_MAX_WALL_SECONDS
    assert resolve_wall_ceiling(_req(_P(None)), "claude_subscription") == DEFAULT_MAX_WALL_SECONDS
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_effort_policies.py -v` — FAILS (`normalize_effort` undefined, etc.).

- [ ] **Step 3: Implement — `investigations/schemas.py`**

Replace lines 87-90:

```python
class InvestigationEffort(StrEnum):
    SHORT = "short"
    NORMAL = "normal"
    DEEP = "deep"
    UNLIMITED = "unlimited"


# Legacy stored/typed values → current tiers ("quick" predates the rename).
_EFFORT_ALIASES = {"quick": InvestigationEffort.SHORT}


def normalize_effort(value: str | None) -> InvestigationEffort:
    """Tolerant parse for effort values from old rows, old clients, or seeds."""
    raw = (value or "").strip().lower()
    if raw in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[raw]
    try:
        return InvestigationEffort(raw)
    except ValueError:
        return InvestigationEffort.NORMAL
```

Then fix every remaining `InvestigationEffort.QUICK` reference: `grep -rn "InvestigationEffort.QUICK\|InvestigationEffort(\"quick\")\|'quick'" back_end --include="*.py"` and replace with `InvestigationEffort.SHORT` / `normalize_effort(...)`. In `api/v1/investigations.py:44`, replace `effort=InvestigationEffort(i.effort or "normal")` with `effort=normalize_effort(i.effort)`. Pydantic request models using `InvestigationEffort` as a field type: add a `field_validator(mode="before")` that calls `normalize_effort` where the field exists (`schemas.py:125,143,159`):

```python
from pydantic import field_validator

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort(cls, v):
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))
```

- [ ] **Step 4: Implement — `run_policies/services/effort.py`** (full replacement)

```python
"""Effort selector → seeded RunPolicy mapping (short/normal/deep/unlimited)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from domains.investigations.schemas import InvestigationEffort
from domains.run_policies.models import RunPolicy
from domains.run_policies.services.system_default import ensure_system_default

# max_wall_seconds: 0 = explicitly no wall guard (see resolve_wall_ceiling);
# None on any other ceiling = no ceiling.
_EFFORT_POLICIES: dict[InvestigationEffort, dict] = {
    InvestigationEffort.SHORT: {
        "name": "opensweep-short",
        "description": "Short run: quick, bounded checks.",
        "max_dollars": 2.0,
        "max_wall_seconds": 900,
        "max_tool_turns": 50,
        "max_files_touched": 25,
    },
    InvestigationEffort.NORMAL: {
        "name": "opensweep-normal",
        "description": "Normal run: standard investigation ceilings.",
        "max_dollars": 20.0,
        "max_wall_seconds": 3600,
        "max_tool_turns": 200,
        "max_files_touched": 100,
    },
    InvestigationEffort.DEEP: {
        "name": "opensweep-deep",
        "description": "Deep run: whole-repo audits, generous ceilings.",
        "max_dollars": 50.0,
        "max_wall_seconds": 14400,
        "max_tool_turns": 3000,
        "max_files_touched": 10000,
    },
    InvestigationEffort.UNLIMITED: {
        "name": "opensweep-unlimited",
        "description": "Unlimited run: no ceilings — stop it from the UI.",
        "max_dollars": None,
        "max_wall_seconds": 0,
        "max_tool_turns": None,
        "max_files_touched": None,
    },
}

# Old seeded rows (by name) whose values exactly match the legacy seed were
# never human-tuned: rename + roll them forward to the current tier config.
_LEGACY_POLICIES: dict[str, tuple[dict, InvestigationEffort]] = {
    "opensweep-effort-quick": (
        {"max_dollars": 0.25, "max_wall_seconds": 120, "max_tool_turns": 20, "max_files_touched": 25},
        InvestigationEffort.SHORT,
    ),
    "opensweep-effort-deep": (
        {"max_dollars": 25.0, "max_wall_seconds": 7200, "max_tool_turns": 1500, "max_files_touched": 10000},
        InvestigationEffort.DEEP,
    ),
}


async def ensure_policy_for_effort(effort: InvestigationEffort) -> RunPolicy:
    config = _EFFORT_POLICIES[effort]
    migrated = await _migrate_legacy_policy(effort)
    if migrated is not None:
        return migrated
    existing = await _find_by_name(config["name"])
    if existing is not None:
        return existing

    base = await ensure_system_default()
    policy = RunPolicy(
        uid=uuid4().hex,
        name=config["name"],
        description=config["description"],
        max_tokens=None,
        max_dollars=config["max_dollars"],
        max_wall_seconds=config["max_wall_seconds"],
        max_tool_turns=config["max_tool_turns"],
        max_files_touched=config["max_files_touched"],
        max_test_seconds=None,
        cloud_allowed=base.cloud_allowed,
        local_only=base.local_only,
        allowed_executors=list(base.allowed_executors or []),
        dry_run=base.dry_run,
        warn_at_pct=base.warn_at_pct,
        on_exceed=base.on_exceed,
        daily_repo_run_count=base.daily_repo_run_count,
        daily_repo_wall_seconds=base.daily_repo_wall_seconds,
        daily_repo_dollars=base.daily_repo_dollars,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await policy.save()
    return policy


async def _migrate_legacy_policy(effort: InvestigationEffort) -> RunPolicy | None:
    """Rename an untouched legacy effort policy in place (uid — and therefore
    every Investigation.run_policy_uid reference — is preserved)."""
    for legacy_name, (legacy_values, target) in _LEGACY_POLICIES.items():
        if target != effort:
            continue
        row = await _find_by_name(legacy_name)
        if row is None:
            continue
        untouched = all(
            getattr(row, field_name, None) == value
            for field_name, value in legacy_values.items()
        )
        if not untouched:
            return None  # human-tuned: leave it; caller seeds the new policy
        config = _EFFORT_POLICIES[effort]
        row.name = config["name"]
        row.description = config["description"]
        for field_name in ("max_dollars", "max_wall_seconds", "max_tool_turns", "max_files_touched"):
            setattr(row, field_name, config[field_name])
        row.updated_at = datetime.now(timezone.utc)
        await row.save()
        return row
    return None


async def _find_by_name(name: str) -> RunPolicy | None:
    for policy in await RunPolicy.nodes.all():
        if (policy.name or "") == name:
            return policy
    return None
```

- [ ] **Step 5: Implement — `system_default.py` goes unlimited**

Replace `_DEFAULTS` values and extend the legacy migration lists:

```python
DEFAULT_MAX_WALL_SECONDS = 3600  # fallback for policies with an UNSET wall only
LEGACY_DEFAULT_MAX_WALL_SECONDS = (300, 600, 3600)
LEGACY_DEFAULT_CEILINGS = {
    "max_dollars": (1.0, 3.0, 20.0),
    "max_tool_turns": (40, 200),
    "max_files_touched": (50, 100),
}

_DEFAULTS = {
    "description": "OpenSweep default -- unlimited; stop runs from the UI, or pick a bounded policy.",
    "max_tokens": None,
    "max_dollars": None,
    # 0 = explicitly no wall guard (None would fall back to DEFAULT_MAX_WALL_SECONDS).
    "max_wall_seconds": 0,
    "max_tool_turns": None,
    "max_files_touched": None,
    "max_test_seconds": None,
    "cloud_allowed": True,
    "local_only": False,
    "allowed_executors": [],
    "dry_run": False,
    "warn_at_pct": 80,
    "on_exceed": "abort",
    "daily_repo_run_count": None,
    "daily_repo_wall_seconds": None,
    "daily_repo_dollars": None,
}
```

In `ensure_system_default`, the wall migration line must handle the 0 sentinel (0 is falsy — `int(existing.max_wall_seconds or 0)` maps None→0; keep correctness by checking `is not None`):

```python
        if (
            existing.max_wall_seconds is not None
            and int(existing.max_wall_seconds) in LEGACY_DEFAULT_MAX_WALL_SECONDS
        ):
            existing.max_wall_seconds = _DEFAULTS["max_wall_seconds"]
            dirty = True
        for field, legacy_values in LEGACY_DEFAULT_CEILINGS.items():
            if getattr(existing, field, None) in legacy_values:
                setattr(existing, field, _DEFAULTS[field])
                dirty = True
```

Note the backfill loop (`if getattr(existing, k, None) is None and v is not None`) stays as-is: with unlimited `_DEFAULTS` mostly `None`, it correctly stops backfilling ceilings.

- [ ] **Step 6: Implement — wall sentinel in `_shared.resolve_wall_ceiling`**

```python
def resolve_wall_ceiling(req: DispatchRequest, provider_kind: str) -> int | None:
    """Effective wall ceiling for the run; None disables the guard.

    Ladder: explicit per-stage override > local-provider skip > policy value
    (0 = explicitly unlimited, positive = ceiling, None/unset = fall through)
    > system default.
    """
    if req.max_wall_seconds_override:
        return int(req.max_wall_seconds_override)
    if is_local_provider_kind(provider_kind):
        return None
    if req.policy is not None and req.policy.max_wall_seconds is not None:
        value = int(req.policy.max_wall_seconds)
        return value if value > 0 else None
    return DEFAULT_MAX_WALL_SECONDS
```

- [ ] **Step 7: Deep-scan default → unlimited** — in `sweep.py:377-386` replace `ensure_policy_for_effort(InvestigationEffort.DEEP)` with `ensure_policy_for_effort(InvestigationEffort.UNLIMITED)` and update the comment ("Deep scans default to the `unlimited` policy — everything defaults to unlimited for now; the analysis-stage pin or an explicit caller policy still overrides."). Also update `_create_deep_scan_investigation`'s `effort="deep"` → keep `"deep"` (it describes intent depth, not the policy).

- [ ] **Step 8: Run tests** — `uv run pytest tests/test_effort_policies.py tests/test_system_default_policy.py tests/test_executor_shared.py -v`. Fix `test_system_default_policy.py` expectations to the new `_DEFAULTS` (it currently asserts 3600/200 etc. — update asserts to `0`/`None`).

- [ ] **Step 9: Commit**

```bash
git add back_end/domains/investigations/schemas.py back_end/domains/run_policies/services/effort.py back_end/domains/run_policies/services/system_default.py back_end/domains/executors/_shared.py back_end/api/v1/investigations.py back_end/domains/investigations/services/sweep.py back_end/tests/test_effort_policies.py back_end/tests/test_system_default_policy.py
git commit -m "feat(policies): short/normal/deep/unlimited effort tiers, unlimited default, wall 0-sentinel"
```

---

### Task 3: Budget briefing in every instruction + de-narrow the user template

Tell the agent its actual budget (harness-agnostic — plain text works for claude, codex, opencode) and delete the depth-killing "keep the investigation narrow" line.

**Files:**
- Modify: `back_end/domains/executors/_shared.py` (new function)
- Modify: `back_end/domains/executors/claude_code.py` (`_build_instruction`, `_USER_TEMPLATE`, `_USER_TEMPLATE_WRITE`)
- Modify: `back_end/domains/executors/cli_tracking.py` (`_instruction`)
- Test: `back_end/tests/test_budget_briefing.py` (new)

**Interfaces:**
- Produces: `budget_briefing(policy: RunPolicy | None, wall_ceiling: int | None) -> str` in `domains.executors._shared` — pure, returns `""` when nothing is bounded. Consumed by both adapters here and by Task 6.

- [ ] **Step 1: Failing tests** — `back_end/tests/test_budget_briefing.py`:

```python
from domains.executors._shared import budget_briefing


class _P:
    max_tool_turns = 200
    max_dollars = 20.0
    warn_at_pct = 80


class _Unlimited:
    max_tool_turns = None
    max_dollars = None
    warn_at_pct = 80


def test_bounded_policy_renders_numbers_and_winddown_rule():
    text = budget_briefing(_P(), 3600)
    assert "60 minutes" in text
    assert "200 tool turns" in text
    assert "80%" in text
    assert "complete_run" in text


def test_unlimited_policy_renders_unbounded_briefing():
    text = budget_briefing(_Unlimited(), None)
    assert "no fixed budget" in text
    assert "complete_run" in text


def test_no_policy_no_wall_is_still_nonempty():
    assert "complete_run" in budget_briefing(None, None)
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_budget_briefing.py -v` — FAIL (function missing).

- [ ] **Step 3: Implement in `_shared.py`** (after `resolve_wall_ceiling`):

```python
def budget_briefing(policy, wall_ceiling: int | None) -> str:
    """Plain-text budget contract rendered into every instruction, for every
    harness. Best practice is a budget the agent can SEE and pace against
    (graceful wind-down) rather than a silent post-hoc verdict."""
    limits: list[str] = []
    if wall_ceiling:
        limits.append(f"~{max(1, int(wall_ceiling // 60))} minutes of wall clock")
    turns = getattr(policy, "max_tool_turns", None) if policy is not None else None
    if turns:
        limits.append(f"~{int(turns)} tool turns")
    dollars = getattr(policy, "max_dollars", None) if policy is not None else None
    if dollars:
        limits.append(f"a ${float(dollars):.2f} spend ceiling (where metered)")
    warn_pct = int(getattr(policy, "warn_at_pct", 80) or 80) if policy is not None else 80
    if limits:
        return (
            "# Run budget\n\n"
            f"This run has {', '.join(limits)}. Pace yourself against it:\n"
            f"- Keep a running coverage checklist of areas done / remaining.\n"
            f"- At roughly {warn_pct}% of budget, STOP opening new areas: file what\n"
            "  you have, record what you skipped, and finish with `complete_run`.\n"
            "- Never end the run without `complete_run` — an unfinished run gets\n"
            "  resumed and told to continue."
        )
    return (
        "# Run budget\n\n"
        "This run has no fixed budget — work to full completion of the intent,\n"
        "however long that takes. Keep a running coverage checklist, file results\n"
        "as you go, and finish with `complete_run` only when the whole scope is\n"
        "genuinely covered. Never end the run without `complete_run`."
    )
```

- [ ] **Step 4: Render it into both adapters**

`claude_code.py` — `_build_instruction` gains the briefing. Change signature/usage (dispatch already has `wall_ceiling` AFTER `resolve_wall_ceiling`; move `instruction = self._build_instruction(req)` after `wall_ceiling = resolve_wall_ceiling(...)` at line 166 and pass it):

```python
    def _build_instruction(self, req: DispatchRequest, wall_ceiling: int | None = None) -> str:
        target_blob = json.dumps(req.target or {}, indent=2)
        ctx_blob = req.context or "(no additional context provided)"
        template = (
            _USER_TEMPLATE_WRITE if req.mode == ExecutionMode.IMPLEMENT else _USER_TEMPLATE
        )
        return template.format(
            intent=req.intent,
            mode=req.mode.value,
            target=target_blob,
            context=ctx_blob,
            run_uid=req.run_uid,
            repository_uid=req.repository_uid,
            budget=budget_briefing(req.policy, wall_ceiling),
        )
```

Add `{budget}` to both templates and REPLACE the narrowing line. `_USER_TEMPLATE` instructions section becomes:

```python
_USER_TEMPLATE = """# Run

repository_uid: {repository_uid}
run_uid:        {run_uid}
mode:           {mode}

# Intent

{intent}

# Target

```json
{target}
```

# Context

{context}

{budget}

# Instructions

Use your native tools (Read/Glob/Grep/Bash; avoid Edit/Write) to investigate.
Work the intent to completion — do not stop early because the task is large.
Whenever you find something worth recording, call a `opensweep-platform` tool to
push it back into OpenSweep immediately. Use `create_finding` for
bugs/gaps/improvements, `propose_*` tools for structural/doc proposals,
and `attach_artifact` for logs, traces, or notes.

Before finishing, verify that the top-level agent has called at least one
OpenSweep write tool. If there are no actionable findings, create a low-severity
observation finding that explains what was checked. Finish with
`complete_run`, reporting what you did, skipped, what succeeded, what
failed, and next steps.
"""
```

Apply the same `{budget}` slot to `_USER_TEMPLATE_WRITE` (between `{context}` and `# Instructions`). In `cli_tracking.py`, find `_instruction(req)` and append the briefing the same way (it builds a string from req; add `budget_briefing(req.policy, timeout)` — note `timeout` is that adapter's wall variable; reorder so `_instruction` is called after `timeout = resolve_wall_ceiling(...)`, passing it in).

- [ ] **Step 5: Run** — `uv run pytest tests/test_budget_briefing.py -v` PASS, plus `uv run pytest tests/ -k "template or instruction" -v` for regressions (fix any test asserting the removed "narrow" sentence).

- [ ] **Step 6: Commit**

```bash
git add back_end/domains/executors/_shared.py back_end/domains/executors/claude_code.py back_end/domains/executors/cli_tracking.py back_end/tests/test_budget_briefing.py
git commit -m "feat(executors): visible run-budget briefing in every instruction; drop 'keep it narrow'"
```

---

### Task 4: Native `--max-turns` on the claude argv

Delegate the turn cap to the CLI so it stops cleanly between turns instead of being judged post-hoc.

**Files:**
- Modify: `back_end/domains/executors/claude_code.py`
- Test: `back_end/tests/test_claude_turn_cap.py` (new)

**Interfaces:**
- Produces: `with_turn_cap(argv: list[str], max_turns: int | None) -> list[str]` (module-level, `claude_code.py`). Task 6 uses it per continuation pass with the *remaining* turn budget.

- [ ] **Step 1: Failing tests** — `back_end/tests/test_claude_turn_cap.py`:

```python
from domains.executors.claude_code import with_turn_cap


def test_cap_appended():
    assert with_turn_cap(["claude", "-p", "x"], 200) == ["claude", "-p", "x", "--max-turns", "200"]


def test_none_and_zero_add_nothing():
    assert with_turn_cap(["claude", "-p", "x"], None) == ["claude", "-p", "x"]
    assert with_turn_cap(["claude", "-p", "x"], 0) == ["claude", "-p", "x"]


def test_operator_set_flag_respected():
    argv = ["claude", "-p", "x", "--max-turns", "50"]
    assert with_turn_cap(argv, 200) == argv
```

- [ ] **Step 2: Run** — FAIL (undefined).

- [ ] **Step 3: Implement** in `claude_code.py` (next to `ensure_stream_json_flags`):

```python
def with_turn_cap(argv: list[str], max_turns: int | None) -> list[str]:
    """Delegate the policy's turn ceiling to the CLI (`--max-turns` stops the
    loop cleanly between turns). An operator-set flag in the template wins."""
    if not max_turns or max_turns <= 0 or "--max-turns" in argv:
        return list(argv)
    return [*argv, "--max-turns", str(int(max_turns))]
```

Wire into `dispatch` right after the `with_model_flag(...)` call:

```python
        argv = with_turn_cap(
            argv,
            int(req.policy.max_tool_turns) if (req.policy and req.policy.max_tool_turns) else None,
        )
```

(Do NOT add `--max-budget-usd`: the adapter is subscription-only and unmetered — see Global Constraints.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_claude_turn_cap.py -v` PASS.
- [ ] **Step 5: Commit** — `git add back_end/domains/executors/claude_code.py back_end/tests/test_claude_turn_cap.py && git commit -m "feat(executors): delegate turn ceiling to claude --max-turns"`

---

### Task 5: Limits become checkpoints — delete post-hoc LIMIT_EXCEEDED

`enforce_ceilings` currently relabels *finished* runs as `LIMIT_EXCEEDED` after the fact (the "runs run out of limit often" complaint). Change all three adapters: post-run ceilings only produce **warnings** in usage; `LIMIT_EXCEEDED` is set only when a limit actually *stopped* the run (wall kill, or the CLI's own `--max-turns` stop once continuation budget is exhausted — Task 6).

**Files:**
- Modify: `back_end/domains/executors/_shared.py` (`enforce_ceilings` → warnings-only variant)
- Modify: `back_end/domains/executors/claude_code.py:300-337`
- Modify: `back_end/domains/executors/cli_tracking.py` (same pattern)
- Modify: `back_end/domains/executors/internal_llm.py` (same pattern)
- Test: modify `back_end/tests/test_executor_shared.py`

**Interfaces:**
- Produces: `ceiling_warnings(policy, usage, wall_ceiling) -> list[str]` in `_shared` (replaces `enforce_ceilings`; the old name is deleted — grep confirms only the three adapters import it).

- [ ] **Step 1: Write/adjust tests** in `tests/test_executor_shared.py`: add

```python
from domains.executors._shared import ceiling_warnings
from domains.run_policies.services.ceilings import UsageSnapshot


class _Policy:
    max_wall_seconds = 100
    max_tool_turns = 10
    max_files_touched = None
    max_test_seconds = None
    max_tokens = None
    max_dollars = None
    warn_at_pct = 80


def test_exceeding_a_ceiling_yields_warning_not_exception():
    warnings = ceiling_warnings(
        policy=_Policy(), usage=UsageSnapshot(wall_seconds=500, tool_turns=50), wall_ceiling=100
    )
    assert any("max_wall_seconds" in w for w in warnings)
    assert any("max_tool_turns" in w for w in warnings)
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** — in `_shared.py` replace `enforce_ceilings` with:

```python
def ceiling_warnings(*, policy, usage, wall_ceiling: int | None) -> list[str]:
    """Post-run ceiling accounting — WARNINGS ONLY. A finished run is never
    retroactively failed for running hot; LIMIT_EXCEEDED is reserved for runs
    a limit actually stopped (wall kill / CLI --max-turns stop)."""
    if policy is None:
        return []
    return check_ceilings(
        policy=_EffectivePolicy(policy, wall_ceiling),
        usage=usage,
        raise_on_exceed=False,
    )
```

In `ceilings.check`, exceedances with `raise_on_exceed=False` already fall through to the warn branch (`value >= ceiling * warn_pct`), so no change needed there. Delete `exceeded_usage` and its imports.

- [ ] **Step 4: Update the three adapters.** In `claude_code.py`, delete the `warnings, exceeded = enforce_ceilings(...)` / `if exceeded is not None: return DispatchResult(status=RunStatus.LIMIT_EXCEEDED, ...)` block (lines ~309-325) and replace with:

```python
        warnings = ceiling_warnings(
            policy=req.policy, usage=usage_snapshot, wall_ceiling=wall_ceiling
        )
```

and change the final status decision so a wall kill is a limit stop, not a generic failure:

```python
        if timed_out:
            status = RunStatus.LIMIT_EXCEEDED
            err = f"wall ceiling reached after {wall_ceiling}s"
        elif exit_code not in (0, None):
            status = RunStatus.FAILED
            err = f"claude CLI exited {exit_code}"
        else:
            status = RunStatus.AWAITING_INPUT
            err = ""
```

Apply the same two changes in `cli_tracking.py` and `internal_llm.py` (grep for `enforce_ceilings` / `exceeded_usage` — same shape in each).

- [ ] **Step 5: Run** — `uv run pytest tests/test_executor_shared.py -v` PASS; `uv run pytest tests/ -v -x -q` for the whole suite (fix imports).
- [ ] **Step 6: Commit** — `git add -A back_end && git commit -m "feat(executors): ceilings warn instead of retro-failing finished runs; wall kill = limit_exceeded"`

---

### Task 6: Continuation + wind-down loop (claude adapter)

The biggest depth lever: a headless `-p` run ends whenever the model emits a final message. Loop instead: while the agent has NOT called `complete_run` (detectable — it stamps `Run.completed_at`), wall/turn budget remains, no quota hit, and we have a session id → `--resume` with a "continue" nudge. Reserve the last ~10% of the wall for a wind-down pass. Persist the session id so the **UI can continue the same conversation afterward**.

**Files:**
- Modify: `back_end/domains/executors/claude_code.py` (dispatch loop + helpers)
- Test: `back_end/tests/test_claude_continuation.py` (new)

**Interfaces:**
- Consumes: `with_turn_cap` (Task 4), `budget_briefing` already rendered (Task 3), `extract_claude_meta(line) -> StreamMeta` from `domains.investigations.services.turn_cli` (exists).
- Produces: `build_continuation_argv(argv: list[str], *, instruction: str, nudge: str, session_id: str) -> list[str] | None`; `soft_wall(wall_ceiling: int | None) -> int | None`; constants `_CONTINUATION_NUDGE`, `_WINDDOWN_NUDGE`. Module setting: `OPENSWEEP_CONTINUATION_PASSES` (default 3) read via `getattr(settings, ...)`.

- [ ] **Step 1: Failing tests** — `back_end/tests/test_claude_continuation.py`:

```python
from domains.executors.claude_code import (
    _CONTINUATION_NUDGE,
    build_continuation_argv,
    soft_wall,
)

ARGV = ["claude", "-p", "the instruction", "--append-system-prompt", "sys", "--output-format", "stream-json"]


def test_continuation_swaps_instruction_and_appends_resume():
    out = build_continuation_argv(
        ARGV, instruction="the instruction", nudge=_CONTINUATION_NUDGE, session_id="sid-1"
    )
    assert out is not None
    assert out[out.index("-p") + 1] == _CONTINUATION_NUDGE
    assert out[-2:] == ["--resume", "sid-1"]
    assert "the instruction" not in out


def test_no_session_id_means_no_continuation():
    assert build_continuation_argv(ARGV, instruction="the instruction", nudge="n", session_id="") is None


def test_unfindable_instruction_means_no_continuation():
    assert build_continuation_argv(ARGV, instruction="not present", nudge="n", session_id="sid") is None


def test_soft_wall_reserves_winddown_share():
    assert soft_wall(3600) == 3240  # 90%
    assert soft_wall(None) is None
    assert soft_wall(60) == 54
```

- [ ] **Step 2: Run** — FAIL (helpers missing).

- [ ] **Step 3: Implement the pure helpers** in `claude_code.py`:

```python
# Continuation loop (see docs/superpowers/plans/2026-07-19-run-depth-and-policy-overhaul.md):
# a headless `-p` turn ends whenever the model emits a final message, which on
# huge open-ended tasks is reliably too early. While the agent has not called
# complete_run (it stamps Run.completed_at via MCP) and budget remains, resume
# the CLI session and tell it to continue. The last _WINDDOWN_SHARE of the wall
# is reserved for one wrap-up pass so runs end with a report, not a kill.
_WINDDOWN_SHARE = 0.10
_MIN_CONTINUATION_SECONDS = 120

_CONTINUATION_NUDGE = """Continue this run — it is not finished. You stopped without calling the
`complete_run` platform tool, so OpenSweep resumed your session. Pick up
exactly where you left off: work through every remaining area of your plan,
file each new issue with `create_finding` as you find it, and only when the
whole scope is genuinely covered finish with `complete_run`. Do not repeat
work you already recorded and do not re-file findings you already filed."""

_WINDDOWN_NUDGE = """Your run budget is exhausted — do NOT investigate anything new. Wrap up now:
file any findings you have evidence for but have not yet filed, record the
areas you did not reach as skipped, and call `complete_run` with your
end-of-run report. This is your final pass."""


def soft_wall(wall_ceiling: int | None) -> int | None:
    """The investigation portion of the wall: kill at this point, then spend
    the reserved remainder on one wind-down pass."""
    if wall_ceiling is None:
        return None
    return max(1, int(wall_ceiling * (1 - _WINDDOWN_SHARE)))


def build_continuation_argv(
    argv: list[str], *, instruction: str, nudge: str, session_id: str
) -> list[str] | None:
    """Continuation-pass argv: same invocation with the -p payload swapped for
    the nudge, resuming the recorded session. None (no continuation possible)
    when there is no session id or a custom template inlined the instruction
    in a way we cannot find."""
    if not session_id:
        return None
    out: list[str] = []
    replaced = False
    for token in argv:
        if not replaced and token == instruction:
            out.append(nudge)
            replaced = True
        else:
            out.append(token)
    if not replaced:
        return None
    return [*out, "--resume", session_id]
```

- [ ] **Step 4: Run helper tests** — `uv run pytest tests/test_claude_continuation.py -v` PASS.

- [ ] **Step 5: Restructure `dispatch`'s subprocess section into the loop.** Replace the block from `stdout_parts: list[str] = []` through the `finally: await recorder.close()` with:

```python
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        timed_out = False
        translator = ClaudeStreamTranslator()
        recorder = StreamRecorder(
            run_uid=req.run_uid,
            repository_uid=req.repository_uid,
            label="live claude_code transcript",
        )
        cli_usage: dict[str, Any] = {}
        turns_used = 0
        session_id = ""
        quota_hit = False
        max_extra_passes = int(getattr(settings, "OPENSWEEP_CONTINUATION_PASSES", 3))
        turn_cap = (
            int(req.policy.max_tool_turns)
            if (req.policy and req.policy.max_tool_turns)
            else None
        )
        investigate_wall = soft_wall(wall_ceiling)

        async def _run_pass(pass_argv: list[str], timeout: float | None) -> tuple[int | None, bool, str]:
            """One CLI invocation. Returns (exit_code, timed_out, pass_stdout)."""
            nonlocal cli_usage
            pass_offset = len(stdout_parts)
            pass_timed_out = False
            proc = await asyncio.create_subprocess_exec(
                *pass_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd or None,
                limit=16 * 1024 * 1024,
                **process_group_kwargs(),
            )

            async def _pump(stream, parts):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    if parts is stdout_parts:
                        delta = stream_event_delta(text)
                        if delta is not None:
                            if delta:
                                publish_delta(req.run_uid, delta)
                            continue
                        parts.append(text)
                        for event in translator.translate(text):
                            if event.get("type") == "turn_end" and isinstance(event.get("usage"), dict):
                                cli_usage.update(event["usage"])
                            append_event(req.run_uid, event.pop("type"), **event)
                    else:
                        parts.append(text)
                    await recorder.record_delta(
                        "stdout" if parts is stdout_parts else "stderr", text
                    )

            try:
                pumps = asyncio.gather(
                    _pump(proc.stdout, stdout_parts),
                    _pump(proc.stderr, stderr_parts),
                    proc.wait(),
                )
                if timeout is None:
                    await pumps
                else:
                    await asyncio.wait_for(pumps, timeout=timeout)
            except TimeoutError:
                pass_timed_out = True
                kill_tree(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
            return proc.returncode, pass_timed_out, "".join(stdout_parts[pass_offset:])

        def _remaining(ceiling: int | None) -> float | None:
            if ceiling is None:
                return None
            return ceiling - (time.monotonic() - started)

        try:
            pass_no = 0
            while True:
                remaining = _remaining(investigate_wall)
                if pass_no == 0:
                    pass_argv = with_turn_cap(argv, turn_cap)
                else:
                    if remaining is not None and remaining < _MIN_CONTINUATION_SECONDS:
                        break
                    remaining_turns = (
                        max(1, turn_cap - turns_used) if turn_cap else None
                    )
                    cont = build_continuation_argv(
                        argv,
                        instruction=instruction,
                        nudge=_CONTINUATION_NUDGE,
                        session_id=session_id,
                    )
                    if cont is None:
                        break
                    pass_argv = with_turn_cap(cont, remaining_turns)
                    append_event(req.run_uid, "user_message", text=_CONTINUATION_NUDGE)

                exit_code, timed_out, pass_stdout = await _run_pass(pass_argv, remaining)
                turns_used += int(cli_usage.get("num_turns") or 0)
                for line in pass_stdout.splitlines():
                    meta = extract_claude_meta(line)
                    if meta.session_id:
                        session_id = meta.session_id
                if timed_out:
                    break
                if detect_quota_exhaustion(exit_code, pass_stdout, "".join(stderr_parts)):
                    quota_hit = True
                    break
                if await _completed_via_mcp(req.run_uid):
                    break
                if turn_cap and turns_used >= turn_cap:
                    break
                if exit_code not in (0, None) and "--max-turns" not in " ".join(pass_argv):
                    break  # real CLI failure; --max-turns stops exit nonzero and MAY continue
                pass_no += 1
                if pass_no > max_extra_passes:
                    break

            # Wind-down: budget ran out (wall soft-kill, turn cap, or pass cap)
            # before complete_run — spend the reserved wall share on a wrap-up
            # pass so the run ends with a report instead of a cliff.
            if (
                session_id
                and not quota_hit
                and not await _completed_via_mcp(req.run_uid)
            ):
                winddown_budget = _remaining(wall_ceiling)
                if winddown_budget is None or winddown_budget > 30:
                    wind_argv = build_continuation_argv(
                        argv,
                        instruction=instruction,
                        nudge=_WINDDOWN_NUDGE,
                        session_id=session_id,
                    )
                    if wind_argv is not None:
                        append_event(req.run_uid, "user_message", text=_WINDDOWN_NUDGE)
                        exit_code, timed_out, _ = await _run_pass(
                            with_turn_cap(wind_argv, 30), winddown_budget
                        )
        except FileNotFoundError as exc:
            return DispatchResult(
                status=RunStatus.FAILED,
                error=f"claude CLI not found: {exc}",
                summary="claude_code adapter requires the `claude` CLI on PATH",
            )
        finally:
            await recorder.close()
            if session_id:
                await _persist_session_id(req.run_uid, session_id)
```

Add the two small DB helpers at module level:

```python
async def _completed_via_mcp(run_uid: str) -> bool:
    """True when the agent already finished deliberately — complete_run stamps
    completed_at on the Run through the MCP bridge."""
    from domains.investigations.models import Run

    run = await Run.nodes.get_or_none(uid=run_uid)
    return bool(run is not None and run.completed_at)


async def _persist_session_id(run_uid: str, session_id: str) -> None:
    """The UI's follow-up turns (turn_service) resume Run.cli_session_id —
    recording it here keeps executor runs continuable from the UI."""
    from domains.investigations.models import Run

    run = await Run.nodes.get_or_none(uid=run_uid)
    if run is not None and session_id:
        run.cli_session_id = session_id
        await run.save()
```

Add the import: `from domains.investigations.services.turn_cli import extract_claude_meta`. Note `turns_used` accumulation: `cli_usage["num_turns"]` is per-pass from the CLI result event, and `cli_usage.update()` overwrites — accumulate into `turns_used` per pass as shown, and use `turns_used` (not `cli_usage["num_turns"]`) in the `UsageSnapshot` below (`tool_turns=turns_used`).

- [ ] **Step 6: Post-loop status decision.** After the loop, if the run was stopped by budget (`timed_out` on the soft wall, or `turn_cap and turns_used >= turn_cap`) AND `_completed_via_mcp` is still false after wind-down → `RunStatus.LIMIT_EXCEEDED` with error `"run budget exhausted (wall/turns) — resumable from the UI"`. Otherwise the Task 5 status logic applies unchanged. Because `LIMIT_EXCEEDED` is in `FOLLOW_UP_STATUSES`, the UI composer stays enabled and — with `cli_session_id` persisted — a user message continues the same session.

- [ ] **Step 7: Manual smoke** — `docker compose up -d`, trigger a deep scan from the UI on a small repo, verify in the run detail view: (a) continuation nudges appear as user messages, (b) run ends with a `complete_run` report, (c) typing a follow-up message in the UI gets an in-context answer (session resumed).

- [ ] **Step 8: Commit** — `git add back_end/domains/executors/claude_code.py back_end/tests/test_claude_continuation.py && git commit -m "feat(executors): continuation + wind-down loop for claude runs; persist session id for UI continue"`

---

### Task 7: Codex continuation fallback (transcript tail, one pass)

Codex `exec` has no `--resume`; the platform already solves this for threads with a capped transcript-tail re-prompt (`turn_cli.build_codex_prompt`). Give the codex tracking adapter ONE continuation pass using the same technique, gated on the same `_completed_via_mcp` signal. OpenCode: no change (works as before; document why in a comment).

**Files:**
- Modify: `back_end/domains/executors/cli_tracking.py`
- Test: `back_end/tests/test_codex_continuation.py` (new)

**Interfaces:**
- Consumes: `build_codex_prompt(text, entries, cap, system_prompt)` from `domains.investigations.services.turn_cli`; `_completed_via_mcp` — move it from `claude_code.py` to `_shared.py` in this task (update the Task 6 import) so both adapters share it.
- Produces: `codex_continuation_prompt(nudge: str, transcript_tail: str) -> str` in `cli_tracking.py`.

- [ ] **Step 1: Failing test** — `back_end/tests/test_codex_continuation.py`:

```python
from domains.executors.cli_tracking import codex_continuation_prompt


def test_continuation_prompt_embeds_tail_and_nudge():
    out = codex_continuation_prompt("CONTINUE NOW", "assistant: found 3 issues so far")
    assert "CONTINUE NOW" in out
    assert "found 3 issues so far" in out
    assert "no session resume" in out
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement:**

```python
CODEX_CONTINUATION_TAIL_CAP = 8_000


def codex_continuation_prompt(nudge: str, transcript_tail: str) -> str:
    """codex exec has no --resume: re-prompt with a capped tail of the prior
    transcript as context (same technique as turn_cli.build_codex_prompt)."""
    tail = transcript_tail[-CODEX_CONTINUATION_TAIL_CAP:]
    return (
        "Your previous attempt at this task stopped early (context below — "
        "this CLI has no session resume):\n"
        f"{tail}\n\n{nudge}"
    )
```

In `_CLITrackingAdapter.dispatch`, after the first `invoke_provider(...)` completes and the envelope has been parsed: if the adapter is codex (`self.provider_kind == "codex_subscription"`), `not await _completed_via_mcp(req.run_uid)`, no quota hit, and wall budget remains > 120s → invoke once more with `codex_continuation_prompt(_CONTINUATION_NUDGE_TRACKING, raw_stdout)` where

```python
_CONTINUATION_NUDGE_TRACKING = (
    "Continue the run — it is not finished. Work through the remaining scope, "
    "then emit the final JSON envelope of platform tool calls INCLUDING a "
    "complete_run entry with your end-of-run report."
)
```

and merge both passes' envelopes (run `extract_envelope` on the second stdout too; concatenate `tool_calls` lists before `execute_envelope_tool_calls`).

- [ ] **Step 4: Run** — `uv run pytest tests/test_codex_continuation.py -v` PASS; full `uv run pytest tests/ -q`.
- [ ] **Step 5: Commit** — `git add back_end/domains/executors/cli_tracking.py back_end/domains/executors/_shared.py back_end/domains/executors/claude_code.py back_end/tests/test_codex_continuation.py && git commit -m "feat(executors): codex transcript-tail continuation pass"`

---

### Task 8: Recall-first prompt seeds (deep-scan base + deep-issue-hunt variant)

The seeded prompts currently lean precision ("prefer fewer findings", "an area with nothing wrong is a valid result" as the closing note). Rewrite for max recall with evidence discipline intact. Seeding rolls these forward automatically on boot for unedited rows (checksum mechanism).

**Files:**
- Modify: `back_end/domains/agent_prompts/services/seed_agent_bases.py` ("deep-scan" body)
- Modify: `back_end/domains/agent_prompts/services/seed_variants.py` ("deep-issue-hunt" body)
- Test: existing `back_end/tests/test_agent_base_seeds.py`, `back_end/tests/test_prompt_seeds.py` (update asserts if they pin body text)

- [ ] **Step 1: Replace the `"deep-scan"` body** in `seed_agent_bases.py` with:

```python
        "body": (
            "Deep-scan this repository end to end and author a full Analysis report.\n"
            "\n"
            "Act as a principal engineer, security reviewer, performance engineer, and\n"
            "maintainability specialist. This is the widest, most thorough pass you do.\n"
            "MAXIMIZE RECALL: the goal is to surface EVERY issue you can support with\n"
            "concrete evidence — bugs, vulnerabilities, data-integrity risks, performance\n"
            "problems, missing tests, dead code, duplicate logic, unused dependencies,\n"
            "over-complex or leaky abstractions, docs gaps, CI/build/config problems, and\n"
            "developer-experience friction. There is NO finding cap; low-severity real\n"
            "issues are worth filing. A deep scan that ends after a handful of findings\n"
            "in a few minutes has failed its coverage contract.\n"
            "\n"
            "# Phase 1 — Survey & plan (map first)\n"
            "\n"
            "Walk the repo with your file tools: languages/frameworks, entry points, major\n"
            "subsystems, data + persistence layers, auth/tenancy, external integrations,\n"
            "build/CI/deploy, migrations, tests. Record the architecture as the\n"
            "`repository_map` section and write your area checklist as `coverage` notes\n"
            "(status=partial) up front. Audit business-critical, high-risk surfaces first\n"
            "(auth, tenancy, money, untrusted input, migrations) — then EVERYTHING else.\n"
            "\n"
            "# Phase 2 — Baseline\n"
            "\n"
            "Inspect (and run where practical and non-destructive) the available checks —\n"
            "build, type-check, lint, tests, migrations — and record each as a `validation`\n"
            "note (command + result). A passing suite is NOT proof of correctness; note\n"
            "disabled checks, broad excludes, and skipped tests.\n"
            "\n"
            "# Phase 3 — Sweep area by area\n"
            "\n"
            "Go through your checklist one area at a time, tracing real execution paths\n"
            "(never assume behavior from names). Use subagents to fan out breadth work in\n"
            "parallel where your harness supports them — but verify their reports against\n"
            "the code yourself before filing; the top-level agent files the findings.\n"
            "For each area file Findings on:\n"
            "\n"
            "* Correctness — logic bugs, bad error handling, races, edge cases, broken\n"
            "  invariants, silent failure paths (kind=defect).\n"
            "* Security — authz/tenancy bypass, injection, SSRF, unsafe deserialization,\n"
            "  secret handling, missing validation (kind=defect; severity to match a\n"
            "  credible attack path — state the path).\n"
            "* Data integrity — transaction boundaries, lost updates, migration safety,\n"
            "  N+1s, missing indexes (kind=defect|improvement).\n"
            "* Performance — hot-path waste, unbounded growth, leaks (kind=improvement,\n"
            "  or defect if it bites in prod; identify the bottleneck, don't guess).\n"
            "* Missing tests for load-bearing/risky logic (kind=gap).\n"
            "* Dead code, unused exports/dependencies/config, duplicate logic, one-line\n"
            "  wrappers, over-abstraction, commented-out code, stale TODOs\n"
            "  (kind=improvement).\n"
            "* Docs gaps and stale/misleading comments (kind=gap, tags=[\"docs\"]).\n"
            "* Build/CI/deploy/config issues and DX friction (kind=improvement).\n"
            "\n"
            "Use the static-analysis candidates in your context as leads, but CONFIRM each\n"
            "against the real code before filing. Mark each area's `coverage` note\n"
            "examined as you finish it. Persist durable, non-obvious facts with\n"
            "`write_memory`.\n"
            "\n"
            "# Phase 4 — Re-sweep until dry\n"
            "\n"
            "When the checklist is done you are NOT done. Every problematic pattern you\n"
            "found late: grep the WHOLE repo for other instances (bugs cluster — a pattern\n"
            "found in one module usually recurs). Revisit the areas you finished first\n"
            "with everything you learned since. Only stop when a full extra pass turns up\n"
            "nothing new.\n"
            "\n"
            "# Core rules\n"
            "\n"
            "* Support every finding with evidence: file path, symbol, line anchors\n"
            "  (path:line in `affected_paths`), and the trigger conditions.\n"
            "* File uncertain-but-serious issues too: mark them unconfirmed, state what\n"
            "  evidence is missing, set severity for the impact if true. Do NOT file\n"
            "  pure speculation you cannot anchor to code, and do not file style nits.\n"
            "* Prefer root-cause fixes over local patches; don't recommend a rewrite\n"
            "  unless incremental improvement is demonstrably impractical.\n"
            "* Before finalizing a finding, try to disprove it; downgrade confidence when\n"
            "  evidence is incomplete. Never label a vulnerability without a credible\n"
            "  attack path.\n"
            "\n"
            "# Confidence & severity\n"
            "\n"
            "Confidence per finding: confirmed (demonstrated) / high / medium / low. Set\n"
            "`confidence` on create_finding accordingly. Severity: critical (severe\n"
            "security/data-loss/outage) / high (major incorrect behavior or exposure) /\n"
            "medium (meaningful defect or degradation) / low (localized). Rank by\n"
            "severity × confidence.\n"
            "\n"
            "# Coverage discipline\n"
            "\n"
            "Do not stop while unexamined areas remain — an early final message is a\n"
            "failed run and will be resumed. If you truly run out of budget, follow your\n"
            "run-budget briefing: reprioritise toward the highest-risk remaining areas\n"
            "and record what you did NOT reach as skipped `coverage` notes and in\n"
            "`limitations`."
        ),
```

- [ ] **Step 2: Update the `"deep-issue-hunt"` variant** in `seed_variants.py` — in Pass 3's text, after the "Do not file style nits at all." sentence, append to the body (same string, extending Pass 3 and the closing line):

```python
            "\n"
            "Also file: dead code, unused exports/dependencies, duplicate logic, and\n"
            "simplification opportunities you can evidence (kind=improvement) — recall\n"
            "includes cleanup, not just defects.\n"
            "\n"
            "No finding cap — exhaust the passes rather than stopping at a round number.\n"
            "Do not end the run while unexamined areas remain: an early stop gets resumed\n"
            "and told to continue. When a pattern proves buggy in one place, grep the\n"
            "whole repo for its siblings before moving on."
```

(replacing the existing final `"No finding cap — exhaust the passes rather than stopping at a round number."` line).

- [ ] **Step 3: Run seed tests** — `uv run pytest tests/test_agent_base_seeds.py tests/test_prompt_seeds.py -v`; update any assertion pinned to removed sentences (e.g. "Prefer fewer, higher-signal findings").
- [ ] **Step 4: Commit** — `git add back_end/domains/agent_prompts/services/seed_agent_bases.py back_end/domains/agent_prompts/services/seed_variants.py back_end/tests && git commit -m "feat(prompts): recall-first deep-scan and deep-issue-hunt seeds (sweep until dry, no cap)"`

---

### Task 9: Frontend — four-tier effort picker + policy surfaces

**Files:**
- Modify: `front_end/src/views/AskView.vue:49,85-97,301-310`
- Modify: `front_end/src/types/api.ts` (effort union type)
- Modify: `front_end/src/views/RunPoliciesView.vue` (render `0`/`null` ceilings as "unlimited")
- Possibly: `front_end/src/components/repositories/WorkflowCard.vue`, `front_end/src/views/InvestigationDetailView.vue` (effort labels) — discover in Step 1.

- [ ] **Step 1: Discover every effort literal** — `grep -rn "'small'\|'quick'\|'large'\|effort" front_end/src --include="*.vue" --include="*.ts" | grep -v node_modules`. Map the findings: the AskView currently uses `'small' | 'normal' | 'large'`; confirm what the backend endpoint it posts to expects (grep `default_effort\|effort` in `back_end/api/v1/agent_prompts.py` and `runs.py`) and align on the canonical four values `short | normal | deep | unlimited`.

- [ ] **Step 2: AskView** — change the ref and options:

```ts
const effort = ref<'short' | 'normal' | 'deep' | 'unlimited'>('normal')
```

```ts
const eff = (p.default_effort || 'normal').toString()
const legacy: Record<string, string> = { quick: 'short', small: 'short', large: 'deep' }
const mapped = legacy[eff] ?? eff
effort.value = (['short', 'normal', 'deep', 'unlimited'].includes(mapped) ? mapped : 'normal') as
  'short' | 'normal' | 'deep' | 'unlimited'
```

```html
<SelectContent>
  <SelectItem value="short">Short</SelectItem>
  <SelectItem value="normal">Normal</SelectItem>
  <SelectItem value="deep">Deep</SelectItem>
  <SelectItem value="unlimited">Unlimited</SelectItem>
</SelectContent>
```

- [ ] **Step 3: types/api.ts** — update the effort union to `'short' | 'normal' | 'deep' | 'unlimited'` (keep `'quick'` in the *response* type union if API DTOs may still emit it before normalization lands everywhere).

- [ ] **Step 4: RunPoliciesView** — wherever ceilings render, show `Unlimited` for `0`/`null` wall and `null` turns/dollars (e.g. `formatCeiling(v, unit)` helper: `v == null || v === 0 ? 'Unlimited' : ...` — for wall specifically `0` means unlimited; `null` renders as "default (60m)").

- [ ] **Step 5: Type-check + build** — `cd front_end && npm run build` (or the repo's `npm run type-check` if present) — must pass.

- [ ] **Step 6: Manual smoke in the UI** — `docker compose up -d`; in Ask view confirm 4 effort options; create an unlimited run; confirm Run Policies admin view shows the four seeded policies with "Unlimited" rendering.

- [ ] **Step 7: Commit** — `git add front_end && git commit -m "feat(ui): short/normal/deep/unlimited effort tiers; unlimited policy rendering"`

---

### Task 10: Full verification + cloud merge

- [ ] **Step 1: Full backend suite** — `cd back_end && uv run pytest tests/ -q` — all green.
- [ ] **Step 2: Stack smoke** — `docker compose up -d --build`; run one deep scan end-to-end on a small repo with the claude provider; verify: continuation nudges in transcript, `complete_run` report present, no `limit_exceeded` on the finished run, follow-up message from the UI works, codex provider still dispatches (envelope path unchanged).
- [ ] **Step 3: Sanity-check seeded state** — restart backend; confirm boot logs show policy migration (`opensweep-effort-deep` → `opensweep-deep`), the four policies exist, and `opensweep-default` now has wall `0` if it was on a legacy seeded value.
- [ ] **Step 4: Cloud merge** — in `/Users/jeroenbrouns/Desktop/opensweep-cloud`: `docker compose down` in public repo first (shared ports), then `git fetch upstream && git merge upstream/main`, re-run the suite + smoke there per CLAUDE.md.
- [ ] **Step 5: Commit any test-only fixups; done.**

---

## Self-Review Notes

- **Spec coverage:** append-system-prompt (T1), de-narrow + budget briefing (T3), continuation/wind-down + UI continuability via `cli_session_id` (T6), native caps (T4), post-hoc limit removal / LIMIT_EXCEEDED redefinition (T5+T6), recall prompts (T8), 4 seeded policies + unlimited default + legacy migration (T2), multi-harness (T3 text is harness-agnostic; T7 codex; opencode untouched-but-working), frontend (T9), two-repo flow (T10).
- **Known deferred items (explicitly out of scope, documented here so they aren't silently lost):** Anthropic *task budgets* for the `internal_llm` API executor (API-only beta; adopt later); `--max-budget-usd` for API-key claude runs (adapter is subscription-only today); auto-resume of `LIMIT_EXCEEDED` runs on a schedule (they are manually continuable from the UI).
- **Risk notes:** unlimited-by-default removes the runaway guard for scheduled/event-triggered runs — mitigations: quota detection still pauses on provider limits, runs are cancellable from the UI, and per-run policy pins still work. If this bites, seed `deep` as the scheduled-trigger default instead (one-line change in `event_triggers.py`).
